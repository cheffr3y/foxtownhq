from collections import defaultdict

from config import RECIPE_Q_FACTOR_PERCENT
from helpers.db_helpers import db_table_exists
from helpers.shared import generate_id, parse_float_field, to_float
from helpers.units import (
    convert_cost_per_unit,
    convert_count_units,
    convert_quantity_between_units,
    format_number,
    normalize_count_unit,
    normalize_unit,
    smart_quantity,
)

def normalize_recipe_type(value):
    if not value:
        return None
    key = value.strip().lower()
    if key in ('menu', 'plated', 'rm', 'plated recipe'):
        return 'menu'
    if key in ('batch', 'rb', 'sub', 'subrecipe', 'sub-recipe', 'prep'):
        return 'batch'
    return None

def infer_recipe_type(name, selected_type):
    normalized = normalize_recipe_type(selected_type)
    if normalized:
        return normalized
    if name:
        trimmed = name.strip().upper()
        if trimmed.startswith('RB '):
            return 'batch'
        if trimmed.startswith('RM '):
            return 'menu'
    return None

RECIPE_TYPE_CHOICES = [
    ('menu', 'Plated (RM)'),
    ('batch', 'Batch (RB)')
]

RECIPE_CATEGORIES = [
    'Appetizers',
    'Salads',
    'Soups',
    'Sauces',
    'Dressings',
    'Marinades',
    'Sides',
    'Proteins',
    'Sandwiches',
    'Pasta',
    'Pizza',
    'Breads',
    'Desserts',
    'Breakfast/Brunch',
    'Snacks',
    'Beverages',
    'Stocks & Bases',
    'Batch/Prep',
    'Other'
]

def get_recipe_by_id(cur, recipe_id):
    cur.execute(
        "SELECT id, name, category, yield_qty, yield_unit, instructions, source_venue, equipment, recipe_type, menu_descriptor FROM recipes WHERE id = %s",
        (recipe_id,)
    )
    return cur.fetchone()

def make_unique_recipe_name(cur, base_name):
    candidate = (base_name or '').strip() or 'Recipe Copy'
    suffix = 2
    while True:
        cur.execute("""
            SELECT 1
            FROM recipes
            WHERE LOWER(name) = LOWER(%s)
            LIMIT 1
        """, (candidate,))
        if not cur.fetchone():
            return candidate
        candidate = f"{base_name} {suffix}"
        suffix += 1

def clone_recipe(cur, source_recipe_id):
    source_recipe = get_recipe_by_id(cur, source_recipe_id)
    if not source_recipe:
        return None

    base_name = f"Copy of {source_recipe.get('name') or 'Recipe'}"
    clone_name = make_unique_recipe_name(cur, base_name)
    clone_id = generate_id('rec_')

    cur.execute("""
        INSERT INTO recipes (id, name, category, yield_qty, yield_unit, instructions, recipe_type, menu_descriptor)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        clone_id,
        clone_name,
        source_recipe.get('category'),
        source_recipe.get('yield_qty'),
        source_recipe.get('yield_unit'),
        source_recipe.get('instructions'),
        source_recipe.get('recipe_type'),
        source_recipe.get('menu_descriptor')
    ))

    cur.execute("""
        SELECT type, item_id, quantity, unit
        FROM recipe_ingredients
        WHERE recipe_id = %s
        ORDER BY id
    """, (source_recipe_id,))
    for row in cur.fetchall():
        item_id = row.get('item_id')
        if row.get('type') == 'recipe' and item_id == source_recipe_id:
            item_id = clone_id
        cur.execute("""
            INSERT INTO recipe_ingredients (id, recipe_id, type, item_id, quantity, unit)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            generate_id('ri_'),
            clone_id,
            row.get('type'),
            item_id,
            row.get('quantity'),
            row.get('unit')
        ))

    cur.execute("""
        SELECT group_name, item_type, item_id, quantity, unit, weight_percent
        FROM recipe_weighted_options
        WHERE recipe_id = %s
        ORDER BY group_name, id
    """, (source_recipe_id,))
    for row in cur.fetchall():
        item_id = row.get('item_id')
        if row.get('item_type') == 'recipe' and item_id == source_recipe_id:
            item_id = clone_id
        cur.execute("""
            INSERT INTO recipe_weighted_options (id, recipe_id, group_name, item_type, item_id, quantity, unit, weight_percent)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            generate_id('rwo_'),
            clone_id,
            row.get('group_name'),
            row.get('item_type'),
            item_id,
            row.get('quantity'),
            row.get('unit'),
            row.get('weight_percent')
        ))

    if db_table_exists(cur, 'public.recipe_venues'):
        cur.execute("""
            SELECT venue_id
            FROM recipe_venues
            WHERE recipe_id = %s
        """, (source_recipe_id,))
        for row in cur.fetchall():
            cur.execute("""
                INSERT INTO recipe_venues (recipe_id, venue_id)
                VALUES (%s, %s)
                ON CONFLICT (recipe_id, venue_id) DO NOTHING
            """, (clone_id, row.get('venue_id')))

    return {
        'id': clone_id,
        'name': clone_name
    }

def get_recipe_weighted_options(cur, recipe_id):
    cur.execute("""
        SELECT rwo.*,
               CASE
                   WHEN rwo.item_type = 'ingredient' THEN i.name
                   WHEN rwo.item_type = 'recipe' THEN r.name
               END AS item_name,
               CASE
                   WHEN rwo.item_type = 'ingredient' THEN i.unit
                   WHEN rwo.item_type = 'recipe' THEN r.yield_unit
               END AS item_default_unit,
               CASE
                   WHEN rwo.item_type = 'ingredient' THEN i.cost_per_unit
                   ELSE NULL
               END AS ingredient_cost_per_unit
        FROM recipe_weighted_options rwo
        LEFT JOIN ingredients i ON rwo.item_type = 'ingredient' AND rwo.item_id = i.id
        LEFT JOIN recipes r ON rwo.item_type = 'recipe' AND rwo.item_id = r.id
        WHERE rwo.recipe_id = %s
        ORDER BY rwo.group_name, item_name
    """, (recipe_id,))
    return cur.fetchall()

def distribute_group_weights(option_rows, errors):
    groups = {}
    for row in option_rows:
        groups.setdefault(row['group_name'], []).append(row)

    for group_name, rows in groups.items():
        explicit_total = 0.0
        has_missing = False
        for row in rows:
            if row['weight_percent'] is None:
                has_missing = True
            else:
                explicit_total += row['weight_percent']

        if has_missing:
            errors.append(f'Weighted options in "{group_name}" must include a weight percent for every row.')
            continue

        if explicit_total > 100.0001:
            errors.append(f'Weighted options in "{group_name}" exceed 100%.')
            continue

        if abs(explicit_total - 100.0) > 0.0001:
            errors.append(f'Weighted options in "{group_name}" must total 100%.')

def parse_weighted_options_from_form(request, recipe_type, cur, errors):
    if recipe_type != 'menu':
        return []

    group_names = request.form.getlist('option_group_name[]')
    item_types = request.form.getlist('option_item_type[]')
    item_ids = request.form.getlist('option_item_id[]')
    item_names = request.form.getlist('option_item_name[]')
    quantities = request.form.getlist('option_qty[]')
    units = request.form.getlist('option_unit[]')
    weights = request.form.getlist('option_weight[]')

    max_len = max(
        len(group_names),
        len(item_types),
        len(item_ids),
        len(item_names),
        len(quantities),
        len(units),
        len(weights),
        0
    )

    option_rows = []
    for idx in range(max_len):
        group_name = (group_names[idx] if idx < len(group_names) else '').strip()
        item_type = (item_types[idx] if idx < len(item_types) else '').strip().lower()
        item_id = (item_ids[idx] if idx < len(item_ids) else '').strip()
        item_name = (item_names[idx] if idx < len(item_names) else '').strip()
        qty_raw = (quantities[idx] if idx < len(quantities) else '').strip()
        unit_raw = (units[idx] if idx < len(units) else '').strip()
        weight_raw = (weights[idx] if idx < len(weights) else '').strip()

        if not any([group_name, item_id, item_name, qty_raw, unit_raw, weight_raw]):
            continue

        if not group_name:
            errors.append('Each weighted option row needs a group name.')
        if not item_type:
            item_type = 'recipe'
        if item_type != 'recipe':
            errors.append('Weighted option type must be sub-recipe.')
        if not item_id and item_name:
            errors.append(f'Weighted option "{item_name}" was not found. Select it from the list.')
        if not item_id and not item_name:
            errors.append('Each weighted option row needs an item.')

        qty_value = parse_float_field(qty_raw, 'Weighted option quantity', errors, required=True, min_value=0.0001)
        unit_value = normalize_unit(unit_raw) or unit_raw
        if not unit_value:
            errors.append('Each weighted option row needs a unit.')

        weight_value = parse_float_field(weight_raw, 'Weighted option percent', errors, required=True, min_value=0.0)
        if weight_value is not None and weight_value > 100:
            errors.append('Weighted option percent cannot exceed 100.')

        option_rows.append({
            'group_name': group_name,
            'item_type': item_type,
            'item_id': item_id,
            'quantity': qty_value,
            'unit': unit_value,
            'weight_percent': weight_value
        })

    distribute_group_weights(option_rows, errors)
    return option_rows

def get_recipe_components(cur, recipe_id):
    cur.execute("""
        SELECT ri.*, ri.type, ri.quantity, ri.unit,
               CASE 
                   WHEN ri.type = 'ingredient' THEN i.name
                   WHEN ri.type = 'recipe' THEN r.name
               END as item_name,
               CASE
                   WHEN ri.type = 'ingredient' THEN i.category
                   WHEN ri.type = 'recipe' THEN r.category
               END as category,
               CASE
                   WHEN ri.type = 'ingredient' THEN i.cost_per_unit
                   ELSE NULL
               END as cost_per_unit,
               CASE
                   WHEN ri.type = 'ingredient' THEN i.unit
                   ELSE NULL
               END as ingredient_unit
        FROM recipe_ingredients ri
        LEFT JOIN ingredients i ON ri.type = 'ingredient' AND ri.item_id = i.id
        LEFT JOIN recipes r ON ri.type = 'recipe' AND ri.item_id = r.id
        WHERE ri.recipe_id = %s
        ORDER BY ri.type, item_name
    """, (recipe_id,))
    return cur.fetchall()

def compute_scale_ratio(component, sub_recipe):
    if not sub_recipe:
        return None
    qty = to_float(component.get('quantity'))
    yield_qty = to_float(sub_recipe.get('yield_qty'))
    if qty <= 0 or yield_qty <= 0:
        return None
    unit = (component.get('unit') or '').strip()
    yield_unit = (sub_recipe.get('yield_unit') or '').strip()
    if unit and yield_unit and unit != yield_unit:
        return None
    return qty / yield_qty

def apply_recipe_q_factor(total_cost):
    if total_cost is None:
        return total_cost
    q_percent = RECIPE_Q_FACTOR_PERCENT
    if q_percent <= 0:
        return total_cost
    return total_cost * (1 + (q_percent / 100))

def build_component_tree(cur, recipe_id, scale_ratio, depth, path, unit_system, apply_q_factor=True):
    if recipe_id in path:
        return [], 0, True
    path = path | {recipe_id}
    current_recipe = get_recipe_by_id(cur, recipe_id)

    components = []
    total_cost = 0
    has_cycle = False

    for component in get_recipe_components(cur, recipe_id):
        qty = to_float(component.get('quantity'))
        scaled_qty = qty * scale_ratio
        item = dict(component)
        item['scaled_quantity'] = scaled_qty
        item['scaled_quantity_display'] = format_number(scaled_qty)
        item['children'] = []
        item['sub_total_cost'] = None
        item['sub_cost_per_unit'] = None
        item['sub_cost_unit'] = None
        item['scale_note'] = None
        item['scale_ratio'] = None
        item['cycle'] = False

        display = smart_quantity(scaled_qty, component.get('unit'), unit_system)
        item['display_quantity'] = display['quantity']
        item['display_unit'] = display['unit']
        item['display_converted'] = display['converted']
        item['display_factor'] = display['factor']
        item['display_type'] = display['type']

        if component.get('type') == 'ingredient':
            ingredient_unit = component.get('ingredient_unit')
            cost_per_unit = convert_cost_per_unit(
                component.get('cost_per_unit'),
                ingredient_unit,
                component.get('unit')
            )
            item['cost_total'] = scaled_qty * cost_per_unit
            if item['display_factor']:
                item['display_cost_per_unit'] = cost_per_unit / item['display_factor']
            else:
                item['display_cost_per_unit'] = cost_per_unit
            total_cost += item['cost_total']
        else:
            sub_recipe = get_recipe_by_id(cur, component.get('item_id'))
            item['sub_recipe'] = sub_recipe
            child_ratio = compute_scale_ratio(component, sub_recipe)
            applied_ratio = child_ratio if child_ratio is not None else 1
            if child_ratio is None:
                if sub_recipe and sub_recipe.get('yield_qty') and sub_recipe.get('yield_unit') and component.get('unit'):
                    item['scale_note'] = 'Unit mismatch; showing full batch'
                elif sub_recipe and sub_recipe.get('yield_qty'):
                    item['scale_note'] = 'No unit match; showing full batch'
                else:
                    item['scale_note'] = 'No yield info; showing full batch'
            else:
                item['scale_note'] = 'Scaled to match requested quantity'

            item['scale_ratio'] = applied_ratio
            item['display_ratio'] = applied_ratio
            item['display_ratio_pct'] = round(applied_ratio * 100, 1)

            if depth < 3 and sub_recipe:
                child_items, child_cost, child_cycle = build_component_tree(
                    cur,
                    sub_recipe['id'],
                    scale_ratio * applied_ratio,
                    depth + 1,
                    path,
                    unit_system,
                    apply_q_factor=apply_q_factor
                )
                item['children'] = child_items
                item['sub_total_cost'] = child_cost
                item['cycle'] = child_cycle
                has_cycle = has_cycle or child_cycle
                total_cost += child_cost
            else:
                item['children'] = []
                item['sub_total_cost'] = 0

            if sub_recipe:
                sub_yield_qty = to_float(sub_recipe.get('yield_qty'))
                if sub_yield_qty > 0:
                    full_cost = get_recipe_total_cost(cur, sub_recipe['id'], unit_system)
                    cost_per_yield = full_cost / sub_yield_qty
                    display_yield = smart_quantity(sub_yield_qty, sub_recipe.get('yield_unit'), unit_system)
                    display_unit = display_yield.get('unit') or sub_recipe.get('yield_unit')
                    item['sub_cost_per_unit'] = convert_cost_per_unit(
                        cost_per_yield,
                        sub_recipe.get('yield_unit'),
                        display_unit
                    )
                    item['sub_cost_unit'] = display_unit

        components.append(item)

    # Weighted option groups are only applied to plated/menu recipes.
    current_recipe_type = normalize_recipe_type(current_recipe.get('recipe_type')) if current_recipe else None
    if current_recipe_type == 'menu':
        weighted_rows = get_recipe_weighted_options(cur, recipe_id)
        grouped_options = {}
        for row in weighted_rows:
            group_name = (row.get('group_name') or '').strip() or 'Options'
            grouped_options.setdefault(group_name, []).append(row)

        for group_name, options in grouped_options.items():
            group_item = {
                'id': f"optgrp_{recipe_id}_{group_name}",
                'type': 'option_group',
                'item_name': group_name,
                'category': 'Weighted options',
                'scaled_quantity': '',
                'scaled_quantity_display': '',
                'display_quantity': '',
                'display_unit': '',
                'display_converted': False,
                'display_factor': 1,
                'display_type': None,
                'children': [],
                'sub_total_cost': None,
                'sub_cost_per_unit': None,
                'sub_cost_unit': None,
                'scale_note': None,
                'scale_ratio': None,
                'cycle': False,
                'group_weight_total': 0,
                'raw_cost_total': 0,
                'cost_total': 0,
                'display_cost_per_unit': None
            }

            for option in options:
                opt_weight_pct = to_float(option.get('weight_percent'))
                opt_weight_ratio = opt_weight_pct / 100.0
                qty = to_float(option.get('quantity'))
                scaled_qty = qty * scale_ratio

                opt_item = {
                    'id': option.get('id'),
                    'type': option.get('item_type'),
                    'item_id': option.get('item_id'),
                    'item_name': option.get('item_name') or 'Unknown option',
                    'category': 'Weighted option',
                    'unit': option.get('unit'),
                    'scaled_quantity': scaled_qty,
                    'scaled_quantity_display': format_number(scaled_qty),
                    'children': [],
                    'sub_total_cost': None,
                    'sub_cost_per_unit': None,
                    'sub_cost_unit': None,
                    'scale_note': None,
                    'scale_ratio': None,
                    'cycle': False,
                    'weight_percent': opt_weight_pct,
                    'weighted_cost': 0,
                    'raw_cost_total': 0
                }

                display = smart_quantity(scaled_qty, option.get('unit'), unit_system)
                opt_item['display_quantity'] = display['quantity']
                opt_item['display_unit'] = display['unit']
                opt_item['display_converted'] = display['converted']
                opt_item['display_factor'] = display['factor']
                opt_item['display_type'] = display['type']

                raw_option_cost = 0
                if option.get('item_type') == 'ingredient':
                    source_unit = option.get('item_default_unit')
                    cost_per_unit = convert_cost_per_unit(
                        option.get('ingredient_cost_per_unit'),
                        source_unit,
                        option.get('unit')
                    )
                    raw_option_cost = scaled_qty * cost_per_unit
                    if opt_item['display_factor']:
                        opt_item['display_cost_per_unit'] = cost_per_unit / opt_item['display_factor']
                    else:
                        opt_item['display_cost_per_unit'] = cost_per_unit
                else:
                    sub_recipe = get_recipe_by_id(cur, option.get('item_id'))
                    opt_item['sub_recipe'] = sub_recipe
                    pseudo_component = {
                        'quantity': option.get('quantity'),
                        'unit': option.get('unit')
                    }
                    child_ratio = compute_scale_ratio(pseudo_component, sub_recipe)
                    applied_ratio = child_ratio if child_ratio is not None else 1
                    opt_item['scale_ratio'] = applied_ratio

                    if depth < 3 and sub_recipe:
                        child_items, child_cost, child_cycle = build_component_tree(
                            cur,
                            sub_recipe['id'],
                            scale_ratio * applied_ratio,
                            depth + 1,
                            path,
                            unit_system,
                            apply_q_factor=apply_q_factor
                        )
                        opt_item['children'] = child_items
                        opt_item['sub_total_cost'] = child_cost
                        opt_item['cycle'] = child_cycle
                        has_cycle = has_cycle or child_cycle
                        raw_option_cost = child_cost
                    elif sub_recipe:
                        raw_option_cost = get_recipe_total_cost(cur, sub_recipe['id'], unit_system) * (scale_ratio * applied_ratio)

                    if sub_recipe:
                        sub_yield_qty = to_float(sub_recipe.get('yield_qty'))
                        if sub_yield_qty > 0:
                            full_cost = get_recipe_total_cost(cur, sub_recipe['id'], unit_system)
                            cost_per_yield = full_cost / sub_yield_qty
                            display_yield = smart_quantity(sub_yield_qty, sub_recipe.get('yield_unit'), unit_system)
                            display_unit = display_yield.get('unit') or sub_recipe.get('yield_unit')
                            opt_item['sub_cost_per_unit'] = convert_cost_per_unit(
                                cost_per_yield,
                                sub_recipe.get('yield_unit'),
                                display_unit
                            )
                            opt_item['sub_cost_unit'] = display_unit

                weighted_cost = raw_option_cost * opt_weight_ratio
                opt_item['raw_cost_total'] = raw_option_cost
                opt_item['cost_total'] = weighted_cost
                opt_item['weighted_cost'] = weighted_cost

                group_item['children'].append(opt_item)
                group_item['group_weight_total'] += opt_weight_pct
                group_item['raw_cost_total'] += raw_option_cost
                group_item['cost_total'] += weighted_cost

            total_cost += group_item['cost_total']
            components.append(group_item)

    if apply_q_factor:
        total_cost = apply_recipe_q_factor(total_cost)

    return components, total_cost, has_cycle

def get_recipe_total_cost(cur, recipe_id, unit_system, apply_q_factor=True):
    _, total_cost, _ = build_component_tree(cur, recipe_id, 1, 0, set(), unit_system, apply_q_factor=apply_q_factor)
    return total_cost

def compute_q_factor(total_cost, q_factor_percent):
    q_percent = to_float(q_factor_percent)
    if q_percent < 0:
        q_percent = 0
    q_amount = total_cost * (q_percent / 100)
    grand_total = total_cost + q_amount
    return q_percent, q_amount, grand_total

def ratio_from_line_quantity(line, recipe):
    qty = to_float(line.get('quantity'))
    recipe_yield = to_float(recipe.get('yield_qty'))
    if qty <= 0:
        return 0
    if recipe_yield <= 0:
        return qty

    line_unit = (line.get('quantity_unit') or '').strip()
    yield_unit = (recipe.get('yield_unit') or '').strip()
    qty_in_yield = qty
    if line_unit and yield_unit and line_unit != yield_unit:
        converted = convert_quantity_between_units(qty, line_unit, yield_unit)
        if converted is not None:
            qty_in_yield = converted
        else:
            line_count_unit = normalize_count_unit(line_unit)
            yield_count_unit = normalize_count_unit(yield_unit)
            if line_count_unit and not yield_count_unit:
                # If line quantity is a count unit (each/dozen) but the recipe yield
                # is in prep units (oz/fl oz/etc), treat the line qty as direct batch pulls.
                return qty
    return qty_in_yield / recipe_yield

def menu_line_base_multiplier(line_quantity, line_unit, base_yield_qty, base_yield_unit):
    qty = to_float(line_quantity)
    if qty <= 0:
        return 0
    base_qty = to_float(base_yield_qty)
    if base_qty <= 0:
        base_qty = 1

    converted = convert_count_units(qty, line_unit, base_yield_unit)
    base_requested = converted if converted is not None else qty
    return base_requested / base_qty

def collect_ingredients_from_components(components, totals, multiplier=1.0):
    for item in components:
        current_multiplier = multiplier
        if item.get('weight_percent') is not None:
            current_multiplier = current_multiplier * (to_float(item.get('weight_percent')) / 100.0)
        if item.get('type') == 'ingredient':
            ing_id = item.get('item_id')
            unit = (item.get('unit') or '').strip()
            qty = to_float(item.get('scaled_quantity')) * current_multiplier
            if ing_id:
                key = (ing_id, unit)
                totals[key] = totals.get(key, 0) + qty
        if item.get('children'):
            collect_ingredients_from_components(item['children'], totals, current_multiplier)

def collect_direct_ingredients_for_prep(components, totals, multiplier=1.0):
    """Collect only direct raw ingredients for a recipe prep card (exclude nested sub-recipe leaves)."""
    for item in components:
        current_multiplier = multiplier
        if item.get('weight_percent') is not None:
            current_multiplier = current_multiplier * (to_float(item.get('weight_percent')) / 100.0)

        item_type = item.get('type')
        if item_type == 'ingredient':
            ing_id = item.get('item_id')
            unit = (item.get('unit') or '').strip()
            qty = to_float(item.get('scaled_quantity')) * current_multiplier
            if ing_id:
                key = (ing_id, unit)
                totals[key] = totals.get(key, 0) + qty
            continue

        # Weighted option groups may include direct ingredient options; recurse into those.
        if item_type == 'option_group' and item.get('children'):
            collect_direct_ingredients_for_prep(item['children'], totals, current_multiplier)

def collect_direct_subrecipes_for_prep(components, totals, multiplier=1.0):
    """Collect only direct sub-recipe pulls for a recipe prep card (exclude deeper nesting)."""
    for item in components:
        current_multiplier = multiplier
        if item.get('weight_percent') is not None:
            current_multiplier = current_multiplier * (to_float(item.get('weight_percent')) / 100.0)

        item_type = item.get('type')
        if item_type == 'recipe' and item.get('sub_recipe'):
            sub_recipe = item.get('sub_recipe') or {}
            key = (sub_recipe.get('id'), item.get('unit') or sub_recipe.get('yield_unit') or '')
            totals[key] = totals.get(key, 0) + (to_float(item.get('scaled_quantity')) * current_multiplier)
            continue

        # Weighted option groups may include recipe options; recurse into those.
        if item_type == 'option_group' and item.get('children'):
            collect_direct_subrecipes_for_prep(item['children'], totals, current_multiplier)

def collect_batch_recipe_usage_from_components(
    components,
    usage_map,
    event_usage_map=None,
    menu_item_usage_map=None,
    event_id=None,
    menu_item_name=None,
    multiplier=1.0
):
    for item in components:
        current_multiplier = multiplier
        if item.get('weight_percent') is not None:
            current_multiplier = current_multiplier * (to_float(item.get('weight_percent')) / 100.0)

        if item.get('type') == 'recipe' and item.get('sub_recipe'):
            sub_recipe = item.get('sub_recipe') or {}
            key = (sub_recipe.get('id'), item.get('unit') or sub_recipe.get('yield_unit') or '')
            usage_map[key] = usage_map.get(key, 0) + (to_float(item.get('scaled_quantity')) * current_multiplier)
            if event_usage_map is not None and event_id:
                event_usage_map[sub_recipe.get('id')].add(event_id)
            if menu_item_usage_map is not None and menu_item_name:
                menu_item_usage_map[sub_recipe.get('id')].add(menu_item_name)

        if item.get('children'):
            collect_batch_recipe_usage_from_components(
                item['children'],
                usage_map,
                event_usage_map=event_usage_map,
                menu_item_usage_map=menu_item_usage_map,
                event_id=event_id,
                menu_item_name=menu_item_name,
                multiplier=current_multiplier
            )

def collect_subrecipes_from_components(components, subrecipes):
    for item in components:
        if item.get('type') == 'recipe' and item.get('item_id'):
            subrecipes.add(item['item_id'])
        if item.get('children'):
            collect_subrecipes_from_components(item['children'], subrecipes)

__all__ = [
    'normalize_recipe_type',
    'infer_recipe_type',
    'RECIPE_TYPE_CHOICES',
    'RECIPE_CATEGORIES',
    'get_recipe_by_id',
    'make_unique_recipe_name',
    'clone_recipe',
    'get_recipe_weighted_options',
    'distribute_group_weights',
    'parse_weighted_options_from_form',
    'get_recipe_components',
    'compute_scale_ratio',
    'apply_recipe_q_factor',
    'build_component_tree',
    'get_recipe_total_cost',
    'compute_q_factor',
    'ratio_from_line_quantity',
    'menu_line_base_multiplier',
    'collect_ingredients_from_components',
    'collect_direct_ingredients_for_prep',
    'collect_direct_subrecipes_for_prep',
    'collect_batch_recipe_usage_from_components',
    'collect_subrecipes_from_components',
]
