import re
from collections import defaultdict

from config import DEFAULT_TARGET_FOOD_COST_PERCENT
from helpers.formatting import (
    collect_ingredient_usage_from_components,
    flatten_components_for_pdf,
    normalize_match_key,
    split_instruction_steps,
)
from helpers.recipes import (
    build_component_tree,
    collect_batch_recipe_usage_from_components,
    collect_direct_ingredients_for_prep,
    collect_direct_subrecipes_for_prep,
    collect_ingredients_from_components,
    collect_subrecipes_from_components,
    get_recipe_by_id,
    get_recipe_total_cost,
)
from helpers.shared import to_float
from helpers.units import (
    convert_cost_per_unit,
    convert_quantity_between_units,
    format_number,
    smart_quantity,
    summarize_yield_pricing,
)

def parse_menu_items(
    cur,
    unit_system,
    recipe_ids,
    batch_values,
    menu_price_values=None,
    target_percent_values=None,
    popularity_values=None,
    default_target_percent=None,
    section_values=None,
    descriptor_values=None
):
    menu_items = []
    total_cost = 0
    errors = []

    for idx, recipe_id in enumerate(recipe_ids):
        recipe_id = (recipe_id or '').strip()
        if not recipe_id:
            continue
        batches = to_float(batch_values[idx]) if idx < len(batch_values) else 0
        if batches <= 0:
            errors.append('Batch counts must be greater than 0.')
            continue

        recipe = get_recipe_by_id(cur, recipe_id)
        if not recipe:
            errors.append('One or more recipes could not be found.')
            continue

        menu_price = None
        if menu_price_values and idx < len(menu_price_values):
            menu_price = to_float(menu_price_values[idx])
            if menu_price <= 0:
                menu_price = None

        target_percent = None
        if target_percent_values and idx < len(target_percent_values):
            target_percent = to_float(target_percent_values[idx])
            if target_percent <= 0:
                target_percent = None
        if target_percent is None and default_target_percent is not None:
            target_percent = to_float(default_target_percent)
            if target_percent <= 0:
                target_percent = None

        base_cost = get_recipe_total_cost(cur, recipe_id, unit_system)
        item_total = base_cost * batches
        total_cost += item_total

        section = None
        if section_values and idx < len(section_values):
            section = (section_values[idx] or '').strip() or None

        descriptor = None
        if descriptor_values and idx < len(descriptor_values):
            descriptor = (descriptor_values[idx] or '').strip() or None
        if not descriptor:
            descriptor = (recipe.get('menu_descriptor') or '').strip() or None

        menu_items.append({
            'recipe': recipe,
            'recipe_id': recipe_id,
            'batches': batches,
            'base_cost': base_cost,
            'item_total': item_total,
            'menu_price': menu_price,
            'target_food_cost_percent': target_percent,
            'popularity_score': None,
            'menu_section': section,
            'menu_descriptor': descriptor
        })

    if not menu_items:
        errors.append('Add at least one recipe to calculate a cost.')

    return menu_items, total_cost, errors

def apply_menu_pricing(menu_items, default_target_percent=None):
    if default_target_percent is None:
        default_target_percent = DEFAULT_TARGET_FOOD_COST_PERCENT
    priced_line_count = 0
    total_food_cost_percent = 0
    total_menu_price = 0

    for item in menu_items:
        target = to_float(item.get('target_food_cost_percent'))
        if target <= 0:
            target = to_float(default_target_percent)
        if target <= 0:
            target = 20
        item['target_food_cost_percent'] = target

        suggested_price = None
        if target > 0:
            suggested_price = item['base_cost'] / (target / 100)
        item['suggested_price'] = suggested_price

        menu_price = to_float(item.get('menu_price'))
        if menu_price <= 0:
            menu_price = suggested_price
        item['effective_menu_price'] = menu_price

        if menu_price and menu_price > 0:
            item['food_cost_percent'] = (item['base_cost'] / menu_price) * 100
            total_food_cost_percent += item['food_cost_percent']
            total_menu_price += menu_price
            priced_line_count += 1
        else:
            item['food_cost_percent'] = None

    avg_food_cost_percent = None
    avg_menu_price = None
    if priced_line_count > 0:
        avg_food_cost_percent = total_food_cost_percent / priced_line_count
        avg_menu_price = total_menu_price / priced_line_count

    return {
        'avg_food_cost_percent': avg_food_cost_percent,
        'avg_menu_price': avg_menu_price,
        'priced_line_count': priced_line_count
    }

def group_menu_items(menu_items):
    grouped = {}
    for item in menu_items:
        section = (item.get('menu_section') or '').strip() or 'Uncategorized'
        grouped.setdefault(section, []).append(item)

    ordered_sections = []
    for section in MENU_SECTION_OPTIONS:
        if section in grouped:
            ordered_sections.append(section)
    for section in sorted(grouped.keys(), key=lambda value: value.lower()):
        if section not in ordered_sections:
            ordered_sections.append(section)

    grouped_list = []
    for section in ordered_sections:
        items = grouped.get(section, [])
        items_sorted = sorted(
            items,
            key=lambda item: (
                (item.get('menu_descriptor') or '').lower(),
                (item.get('recipe', {}).get('name') or '').lower()
            )
        )
        grouped_list.append({'section': section, 'items': items_sorted})

    return grouped_list

def build_rollout_breakdown(cur, unit_system, menu_items):
    ingredient_totals = {}
    subrecipe_ids = set()

    for item in menu_items:
        recipe_id = item.get('recipe_id')
        batches = to_float(item.get('batches'))
        if not recipe_id or batches <= 0:
            continue
        components, _, _ = build_component_tree(cur, recipe_id, batches, 0, set(), unit_system)
        collect_ingredients_from_components(components, ingredient_totals)
        collect_subrecipes_from_components(components, subrecipe_ids)

    ingredient_master = []
    ingredient_total_cost = 0
    if ingredient_totals:
        ingredient_ids = list({ing_id for ing_id, _ in ingredient_totals.keys()})
        cur.execute("""
            SELECT id, name, unit, category, cost_per_unit, vendor, vendor_code, g_code
            FROM ingredients
            WHERE id = ANY(%s)
        """, (ingredient_ids,))
        ingredient_map = {row['id']: row for row in cur.fetchall()}

        for (ing_id, unit), qty in ingredient_totals.items():
            ingredient = ingredient_map.get(ing_id, {})
            cost_per_unit_raw = ingredient.get('cost_per_unit')
            cost_per_unit = convert_cost_per_unit(
                cost_per_unit_raw,
                ingredient.get('unit'),
                unit
            )
            ext_cost = qty * cost_per_unit if cost_per_unit else 0
            ingredient_total_cost += ext_cost
            display = smart_quantity(qty, unit, unit_system)
            ingredient_master.append({
                'id': ing_id,
                'name': ingredient.get('name') or 'Unknown',
                'category': ingredient.get('category'),
                'unit': unit or ingredient.get('unit'),
                'quantity': qty,
                'display_quantity': display['quantity'],
                'display_unit': display['unit'],
                'cost_per_unit': cost_per_unit,
                'ext_cost': ext_cost,
                'vendor': ingredient.get('vendor'),
                'vendor_code': ingredient.get('vendor_code'),
                'g_code': ingredient.get('g_code')
            })

        ingredient_master.sort(key=lambda item: (item['name'] or '').lower())

    batch_recipes = []
    if subrecipe_ids:
        cur.execute("""
            SELECT id, name, category, yield_qty, yield_unit
            FROM recipes
            WHERE id = ANY(%s)
            ORDER BY name
        """, (list(subrecipe_ids),))
        for recipe in cur.fetchall():
            total_cost = get_recipe_total_cost(cur, recipe['id'], unit_system)
            yield_qty = to_float(recipe.get('yield_qty'))
            yield_pricing = summarize_yield_pricing(
                total_cost,
                recipe.get('yield_qty'),
                recipe.get('yield_unit'),
                unit_system
            )
            batch_recipes.append({
                'id': recipe['id'],
                'name': recipe['name'],
                'category': recipe.get('category'),
                'yield_qty': yield_qty,
                'yield_unit': recipe.get('yield_unit'),
                'display_yield_qty': yield_pricing['display_yield_qty'],
                'display_yield_qty_value': yield_pricing['display_yield_qty_value'],
                'display_yield_unit': yield_pricing['display_yield_unit'],
                'total_cost': total_cost,
                'cost_per_yield': yield_pricing['cost_per_yield'],
                'cost_per_yield_unit': yield_pricing['cost_per_yield_unit']
            })

    return ingredient_master, ingredient_total_cost, batch_recipes


def build_rollout_ops_dataset(cur, unit_system, menu_items):
    station_specs = {}
    batch_usage_totals = {}
    batch_menu_usage = defaultdict(set)
    ingredient_menu_usage = {}
    ingredient_station_usage = {}
    all_menu_cards = []

    for item in menu_items:
        recipe = item.get('recipe') or {}
        recipe_id = item.get('recipe_id')
        scale_ratio = to_float(item.get('batches')) or 1
        if not recipe_id or scale_ratio <= 0:
            continue

        station_name = (recipe.get('station') or '').strip() or 'General'
        station_spec = station_specs.setdefault(
            station_name,
            {
                'station': station_name,
                'menu_cards': [],
                'subrecipes': {},
                'ingredients': {},
                'subrecipe_sources': {},
                'ingredient_sources': {},
                'sections': set(),
                'equipment': set(),
            },
        )

        components, _, _ = build_component_tree(
            cur,
            recipe_id,
            scale_ratio,
            0,
            set(),
            unit_system,
            apply_q_factor=False,
        )

        menu_name = recipe.get('name') or 'Menu Item'
        direct_subrecipe_totals = {}
        direct_ingredient_totals = {}
        nested_ingredient_usage = {}
        collect_direct_subrecipes_for_prep(components, direct_subrecipe_totals)
        collect_direct_ingredients_for_prep(components, direct_ingredient_totals)
        collect_batch_recipe_usage_from_components(
            components,
            batch_usage_totals,
            menu_item_usage_map=batch_menu_usage,
            menu_item_name=menu_name,
        )
        collect_ingredient_usage_from_components(components, menu_name, nested_ingredient_usage)

        for ingredient_id in nested_ingredient_usage:
            ingredient_menu_usage.setdefault(ingredient_id, set()).add(menu_name)
            ingredient_station_usage.setdefault(ingredient_id, set()).add(station_name)

        for key, qty in direct_subrecipe_totals.items():
            station_spec['subrecipes'][key] = station_spec['subrecipes'].get(key, 0) + qty
            recipe_key = key[0]
            if recipe_key:
                station_spec['subrecipe_sources'].setdefault(recipe_key, set()).add(menu_name)

        for key, qty in direct_ingredient_totals.items():
            station_spec['ingredients'][key] = station_spec['ingredients'].get(key, 0) + qty
            ingredient_key = key[0]
            if ingredient_key:
                station_spec['ingredient_sources'].setdefault(ingredient_key, set()).add(menu_name)

        station_spec['sections'].add((item.get('menu_section') or '').strip() or 'Uncategorized')
        if recipe.get('equipment'):
            station_spec['equipment'].add(recipe.get('equipment'))

        direct_subrecipe_rows = []
        for (subrecipe_id, required_unit), required_qty in sorted(
            direct_subrecipe_totals.items(),
            key=lambda row: (row[0][0] or '', row[0][1] or ''),
        ):
            display_required = smart_quantity(required_qty, required_unit, unit_system)
            direct_subrecipe_rows.append(
                {
                    'recipe_id': subrecipe_id,
                    'required_qty': required_qty,
                    'required_unit': required_unit,
                    'display_required_qty': display_required.get('quantity'),
                    'display_required_unit': display_required.get('unit'),
                }
            )

        direct_ingredient_rows = []
        for (ingredient_id, unit), total_qty in sorted(
            direct_ingredient_totals.items(),
            key=lambda row: (row[0][0] or '', row[0][1] or ''),
        ):
            display_total = smart_quantity(total_qty, unit, unit_system)
            direct_ingredient_rows.append(
                {
                    'ingredient_id': ingredient_id,
                    'total_qty': total_qty,
                    'unit': unit,
                    'display_total_qty': display_total.get('quantity'),
                    'display_total_unit': display_total.get('unit'),
                }
            )

        card = {
            'recipe_id': recipe_id,
            'menu_name': menu_name,
            'menu_section': item.get('menu_section') or 'Uncategorized',
            'menu_descriptor': item.get('menu_descriptor') or '',
            'station': station_name,
            'equipment': recipe.get('equipment') or '',
            'yield_qty': recipe.get('yield_qty'),
            'yield_unit': recipe.get('yield_unit') or '',
            'price': item.get('menu_price'),
            'food_cost_percent': item.get('food_cost_percent'),
            'components': components,
            'flat_components': flatten_components_for_pdf(components),
            'instruction_steps': split_instruction_steps(recipe.get('instructions')),
            'critical_steps': split_instruction_steps(recipe.get('critical_steps')),
            'storage_lines': split_instruction_steps(recipe.get('storage_instructions')),
            'prep_time_minutes': recipe.get('prep_time_minutes'),
            'shelf_life_days': recipe.get('shelf_life_days'),
            'direct_subrecipes': direct_subrecipe_rows,
            'direct_ingredients': direct_ingredient_rows,
        }
        station_spec['menu_cards'].append(card)
        all_menu_cards.append(card)

    batch_recipe_ids = sorted({recipe_id for recipe_id, _unit in batch_usage_totals.keys() if recipe_id})
    batch_recipe_map = {}
    if batch_recipe_ids:
        cur.execute(
            """
            SELECT
                id,
                name,
                category,
                yield_qty,
                yield_unit,
                instructions,
                equipment,
                station,
                critical_steps,
                storage_instructions,
                shelf_life_days,
                prep_time_minutes,
                recipe_type
            FROM recipes
            WHERE id = ANY(%s)
            """,
            (batch_recipe_ids,),
        )
        batch_recipe_map = {row['id']: row for row in cur.fetchall()}

    ingredient_master, _, _ = build_rollout_breakdown(cur, unit_system, menu_items)
    ingredient_map = {row['id']: row for row in ingredient_master}

    for card in all_menu_cards:
        for row in card['direct_subrecipes']:
            recipe_meta = batch_recipe_map.get(row.get('recipe_id')) or {}
            row['recipe_name'] = recipe_meta.get('name') or 'Sub-recipe'
            row['yield_qty'] = recipe_meta.get('yield_qty')
            row['yield_unit'] = recipe_meta.get('yield_unit') or row.get('required_unit') or ''
            yield_qty = to_float(recipe_meta.get('yield_qty'))
            required_qty = to_float(row.get('required_qty'))
            required_unit = (row.get('required_unit') or '').strip()
            yield_unit = (row.get('yield_unit') or '').strip()
            qty_in_yield_unit = required_qty
            if required_unit and yield_unit and required_unit != yield_unit:
                converted = convert_quantity_between_units(required_qty, required_unit, yield_unit)
                if converted is not None:
                    qty_in_yield_unit = converted
            row['required_batches'] = (qty_in_yield_unit / yield_qty) if yield_qty > 0 else None
            row['display_required_batches'] = format_number(row['required_batches']) if row['required_batches'] is not None else ''

        for row in card['direct_ingredients']:
            ingredient_meta = ingredient_map.get(row.get('ingredient_id')) or {}
            row['ingredient_name'] = ingredient_meta.get('name') or 'Ingredient'
            row['category'] = ingredient_meta.get('category') or ''
            row['vendor'] = ingredient_meta.get('vendor') or ''
            row['vendor_code'] = ingredient_meta.get('vendor_code') or ''
            row['g_code'] = ingredient_meta.get('g_code') or ''

    batch_cards = []
    for (recipe_id, required_unit), required_qty in sorted(
        batch_usage_totals.items(),
        key=lambda row: (batch_recipe_map.get(row[0][0], {}).get('name') or '').lower(),
    ):
        recipe = batch_recipe_map.get(recipe_id) or {}
        yield_qty = to_float(recipe.get('yield_qty'))
        yield_unit = (recipe.get('yield_unit') or required_unit or '').strip()
        qty_in_yield_unit = required_qty
        if required_unit and yield_unit and required_unit != yield_unit:
            converted = convert_quantity_between_units(required_qty, required_unit, yield_unit)
            if converted is not None:
                qty_in_yield_unit = converted
        required_batches = (qty_in_yield_unit / yield_qty) if yield_qty > 0 else None
        display_required = smart_quantity(qty_in_yield_unit, yield_unit or required_unit, unit_system)
        scale_ratio = required_batches if required_batches and required_batches > 0 else 1
        components, _, _ = build_component_tree(
            cur,
            recipe_id,
            scale_ratio,
            0,
            set(),
            unit_system,
            apply_q_factor=False,
        )
        batch_cards.append(
            {
                'recipe_id': recipe_id,
                'recipe_name': recipe.get('name') or 'Sub-recipe',
                'category': recipe.get('category') or '',
                'station': recipe.get('station') or 'General',
                'equipment': recipe.get('equipment') or '',
                'yield_qty': recipe.get('yield_qty'),
                'yield_unit': recipe.get('yield_unit') or required_unit or '',
                'required_qty': qty_in_yield_unit,
                'required_unit': yield_unit or required_unit,
                'display_required_qty': display_required.get('quantity'),
                'display_required_unit': display_required.get('unit'),
                'required_batches': required_batches,
                'display_required_batches': format_number(required_batches) if required_batches is not None else '',
                'source_menu_items': sorted(batch_menu_usage.get(recipe_id, set())),
                'components': components,
                'flat_components': flatten_components_for_pdf(components),
                'instruction_steps': split_instruction_steps(recipe.get('instructions')),
                'critical_steps': split_instruction_steps(recipe.get('critical_steps')),
                'storage_lines': split_instruction_steps(recipe.get('storage_instructions')),
                'prep_time_minutes': recipe.get('prep_time_minutes'),
                'shelf_life_days': recipe.get('shelf_life_days'),
            }
        )

    station_groups = []
    for station_name in sorted(station_specs, key=lambda value: value.lower()):
        station_spec = station_specs[station_name]
        menu_cards = sorted(
            station_spec['menu_cards'],
            key=lambda row: ((row.get('menu_section') or '').lower(), (row.get('menu_name') or '').lower()),
        )

        subrecipe_rows = []
        for (recipe_id, required_unit), required_qty in sorted(
            station_spec['subrecipes'].items(),
            key=lambda row: (batch_recipe_map.get(row[0][0], {}).get('name') or '').lower(),
        ):
            recipe = batch_recipe_map.get(recipe_id) or {}
            yield_qty = to_float(recipe.get('yield_qty'))
            yield_unit = (recipe.get('yield_unit') or required_unit or '').strip()
            qty_in_yield_unit = required_qty
            if required_unit and yield_unit and required_unit != yield_unit:
                converted = convert_quantity_between_units(required_qty, required_unit, yield_unit)
                if converted is not None:
                    qty_in_yield_unit = converted
            required_batches = (qty_in_yield_unit / yield_qty) if yield_qty > 0 else None
            display_required = smart_quantity(qty_in_yield_unit, yield_unit or required_unit, unit_system)
            subrecipe_rows.append(
                {
                    'recipe_id': recipe_id,
                    'recipe_name': recipe.get('name') or 'Sub-recipe',
                    'required_qty': qty_in_yield_unit,
                    'required_unit': yield_unit or required_unit,
                    'display_required_qty': display_required.get('quantity'),
                    'display_required_unit': display_required.get('unit'),
                    'required_batches': required_batches,
                    'display_required_batches': format_number(required_batches) if required_batches is not None else '',
                    'source_menu_items': sorted(station_spec['subrecipe_sources'].get(recipe_id, set())),
                    'station': recipe.get('station') or station_name,
                }
            )

        ingredient_rows = []
        for (ingredient_id, unit), total_qty in sorted(
            station_spec['ingredients'].items(),
            key=lambda row: (ingredient_map.get(row[0][0], {}).get('name') or '').lower(),
        ):
            ingredient = ingredient_map.get(ingredient_id) or {}
            display_total = smart_quantity(total_qty, unit, unit_system)
            ingredient_rows.append(
                {
                    'ingredient_id': ingredient_id,
                    'ingredient_name': ingredient.get('name') or 'Ingredient',
                    'category': ingredient.get('category') or '',
                    'vendor': ingredient.get('vendor') or '',
                    'vendor_code': ingredient.get('vendor_code') or '',
                    'g_code': ingredient.get('g_code') or '',
                    'total_qty': total_qty,
                    'unit': unit,
                    'display_total_qty': display_total.get('quantity'),
                    'display_total_unit': display_total.get('unit'),
                    'source_menu_items': sorted(station_spec['ingredient_sources'].get(ingredient_id, set())),
                }
            )

        station_groups.append(
            {
                'station': station_name,
                'menu_cards': menu_cards,
                'menu_count': len(menu_cards),
                'batch_count': len(subrecipe_rows),
                'ingredient_count': len(ingredient_rows),
                'sections': sorted(station_spec['sections']),
                'equipment_list': sorted(station_spec['equipment']),
                'subrecipe_rows': subrecipe_rows,
                'ingredient_rows': ingredient_rows,
            }
        )

    for ingredient in ingredient_master:
        ingredient['used_in_menu_items'] = sorted(ingredient_menu_usage.get(ingredient['id'], set()))
        ingredient['used_in_stations'] = sorted(ingredient_station_usage.get(ingredient['id'], set()))

    quantity_basis_label = 'Saved rollout quantities'
    if all(abs(to_float(item.get('batches')) - 1) < 1e-9 for item in menu_items):
        quantity_basis_label = 'Sold-as recipe quantities'

    return {
        'station_groups': station_groups,
        'menu_cards': sorted(
            all_menu_cards,
            key=lambda row: ((row.get('station') or '').lower(), (row.get('menu_name') or '').lower()),
        ),
        'batch_cards': batch_cards,
        'ingredient_master': ingredient_master,
        'quantity_basis_label': quantity_basis_label,
    }

def clean_menu_text(value):
    text = re.sub(r'\s+', ' ', (value or '')).strip()
    text = text.replace('  ', ' ')
    return text

def auto_match_menu_recipe_id(menu_name, recipes):
    needle = normalize_match_key(menu_name)
    if not needle:
        return None
    exact = [row['id'] for row in recipes if normalize_match_key(row.get('name')) == needle]
    if exact:
        return exact[0]
    contains = [row['id'] for row in recipes if needle in normalize_match_key(row.get('name')) or normalize_match_key(row.get('name')) in needle]
    if len(contains) == 1:
        return contains[0]
    return None

MENU_SECTION_OPTIONS = [
    'Appetizers',
    'Salads',
    'Wraps',
    'BBQ Dinner',
    'Handhelds',
    'Fish Fry Friday',
    'Entrees',
    'Sides',
    'Modifier',
    'Desserts',
    'Other'
]

__all__ = [
    'parse_menu_items',
    'apply_menu_pricing',
    'group_menu_items',
    'build_rollout_breakdown',
    'build_rollout_ops_dataset',
    'clean_menu_text',
    'auto_match_menu_recipe_id',
    'MENU_SECTION_OPTIONS',
]
