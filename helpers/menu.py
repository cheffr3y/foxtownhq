import re

from config import DEFAULT_TARGET_FOOD_COST_PERCENT
from helpers.formatting import normalize_match_key
from helpers.recipes import (
    build_component_tree,
    collect_ingredients_from_components,
    collect_subrecipes_from_components,
    get_recipe_by_id,
    get_recipe_total_cost,
)
from helpers.shared import to_float
from helpers.units import convert_cost_per_unit, smart_quantity, summarize_yield_pricing

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
    'clean_menu_text',
    'auto_match_menu_recipe_id',
    'MENU_SECTION_OPTIONS',
]
