import json
import os
import re
import shutil
import uuid
from collections import defaultdict
from io import BytesIO
from datetime import date, datetime, timedelta

from flask import current_app, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.exceptions import HTTPException
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor
from werkzeug.security import check_password_hash
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from db import get_db
from pypdf import PdfReader
from config import (
    DEFAULT_Q_FACTOR_PERCENT,
    DEFAULT_TARGET_FOOD_COST_PERCENT,
    PRICE_REFRESH_DAYS,
    RECIPE_Q_FACTOR_PERCENT,
)

load_dotenv()

# Admin auth (single user for MVP)
def get_admin_config():
    username = os.getenv('ADMIN_USERNAME')
    password_hash = os.getenv('ADMIN_PASSWORD_HASH')
    if not username or not password_hash:
        return None
    return {
        'username': username,
        'password_hash': password_hash
    }

# Unit conversion helpers
UNIT_DEFS = {
    # Weight (base: g)
    'g': {'type': 'weight', 'to_base': 1.0, 'display': 'g', 'system': 'metric'},
    'kg': {'type': 'weight', 'to_base': 1000.0, 'display': 'kg', 'system': 'metric'},
    'oz': {'type': 'weight', 'to_base': 28.349523125, 'display': 'oz', 'system': 'imperial'},
    'lb': {'type': 'weight', 'to_base': 453.59237, 'display': 'lb', 'system': 'imperial'},
    # Volume (base: ml)
    'ml': {'type': 'volume', 'to_base': 1.0, 'display': 'ml', 'system': 'metric'},
    'l': {'type': 'volume', 'to_base': 1000.0, 'display': 'L', 'system': 'metric'},
    'tsp': {'type': 'volume', 'to_base': 4.92892159375, 'display': 'tsp', 'system': 'imperial'},
    'tbsp': {'type': 'volume', 'to_base': 14.78676478125, 'display': 'tbsp', 'system': 'imperial'},
    'fl oz': {'type': 'volume', 'to_base': 29.5735295625, 'display': 'fl oz', 'system': 'imperial'},
    'cup': {'type': 'volume', 'to_base': 236.5882365, 'display': 'cup', 'system': 'imperial'},
    'pt': {'type': 'volume', 'to_base': 473.176473, 'display': 'pt', 'system': 'imperial'},
    'qt': {'type': 'volume', 'to_base': 946.352946, 'display': 'qt', 'system': 'imperial'},
    'gal': {'type': 'volume', 'to_base': 3785.411784, 'display': 'gal', 'system': 'imperial'},
}

UNIT_ALIASES = {
    # Weight
    'g': 'g', 'gram': 'g', 'grams': 'g',
    'kg': 'kg', 'kilogram': 'kg', 'kilograms': 'kg',
    'oz': 'oz', 'ounce': 'oz', 'ounces': 'oz',
    'lb': 'lb', 'lbs': 'lb', 'pound': 'lb', 'pounds': 'lb',
    # Volume
    'ml': 'ml', 'milliliter': 'ml', 'milliliters': 'ml', 'millilitre': 'ml', 'millilitres': 'ml',
    'l': 'l', 'liter': 'l', 'liters': 'l', 'litre': 'l', 'litres': 'l',
    'tsp': 'tsp', 'teaspoon': 'tsp', 'teaspoons': 'tsp',
    'tbsp': 'tbsp', 'tablespoon': 'tbsp', 'tablespoons': 'tbsp',
    'floz': 'fl oz', 'fluidounce': 'fl oz', 'fluidounces': 'fl oz',
    'cup': 'cup', 'cups': 'cup',
    'pt': 'pt', 'pint': 'pt', 'pints': 'pt',
    'qt': 'qt', 'quart': 'qt', 'quarts': 'qt',
    'gal': 'gal', 'gallon': 'gal', 'gallons': 'gal',
}

SYSTEM_UNITS = {
    'metric': {
        'weight': ['kg', 'g'],
        'volume': ['l', 'ml'],
    },
    'imperial': {
        'weight': ['lb', 'oz'],
        'volume': ['gal', 'qt', 'pt', 'cup', 'fl oz'],
    },
    # Kitchen hybrid: metric weights, imperial volume.
    'hybrid': {
        'weight': ['kg', 'g'],
        'volume': ['gal', 'qt', 'pt', 'cup', 'fl oz'],
    }
}

def normalize_unit(unit):
    if not unit:
        return None
    key = ''.join(ch for ch in unit.lower() if ch.isalnum())
    return UNIT_ALIASES.get(key)

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

def to_float(value):
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0

def parse_float_field(value, label, errors, required=False, min_value=None):
    text = (value or '').strip()
    if not text:
        if required:
            errors.append(f"{label} is required.")
        return None
    try:
        number = float(text)
    except ValueError:
        errors.append(f"{label} must be a number.")
        return None
    if min_value is not None and number < min_value:
        errors.append(f"{label} must be at least {min_value}.")
        return None
    return number

def generate_id(prefix):
    return f"{prefix}{uuid.uuid4().hex[:12]}"

RECIPE_TYPE_CHOICES = [
    ('menu', 'Plated (RM)'),
    ('batch', 'Batch (RB)')
]

INGREDIENT_CATEGORIES = [
    'Produce',
    'Herbs',
    'Dairy',
    'Meat',
    'Poultry',
    'Seafood',
    'Dry Goods',
    'Spices',
    'Baking',
    'Oils & Vinegars',
    'Condiments & Sauces',
    'Bread',
    'Frozen',
    'Beverages',
    'Packaging',
    'Disposables',
    'Cleaning',
    'Other'
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

def get_unit_system():
    raw_system = (request.args.get('units') or '').strip().lower()
    aliases = {
        'kitchen': 'hybrid',
        'metric_weights': 'hybrid',
        'metric-weights': 'hybrid',
    }
    system = aliases.get(raw_system, raw_system)
    if system in ('auto', 'metric', 'imperial', 'hybrid'):
        session['unit_system'] = system
    selected = session.get('unit_system', 'auto')
    if selected not in ('auto', 'metric', 'imperial', 'hybrid'):
        return 'auto'
    return selected

def format_number(value, decimals=2):
    if value is None:
        return ''
    rounded = round(value, decimals)
    if abs(rounded - round(rounded)) < (10 ** -decimals):
        return str(int(round(rounded)))
    return f"{rounded:.{decimals}f}".rstrip('0').rstrip('.')

def convert_cost_per_unit(cost_per_unit, from_unit, to_unit):
    cost = to_float(cost_per_unit)
    from_canonical = normalize_unit(from_unit)
    to_canonical = normalize_unit(to_unit)
    if not from_canonical or not to_canonical:
        return cost
    if from_canonical == to_canonical:
        return cost
    from_def = UNIT_DEFS.get(from_canonical)
    to_def = UNIT_DEFS.get(to_canonical)
    if not from_def or not to_def:
        return cost
    if from_def['type'] != to_def['type']:
        return cost
    factor = to_def['to_base'] / from_def['to_base']
    return cost * factor

def convert_quantity(quantity, unit, system='auto'):
    quantity = to_float(quantity)
    canonical = normalize_unit(unit)
    if quantity is None or canonical not in UNIT_DEFS:
        return {
            'quantity': quantity,
            'unit': unit,
            'factor': 1.0,
            'converted': False,
            'type': None
        }

    unit_def = UNIT_DEFS[canonical]
    unit_type = unit_def['type']
    base_qty = quantity * unit_def['to_base']

    if system == 'auto':
        system = unit_def['system']

    candidates = SYSTEM_UNITS.get(system, {}).get(unit_type, [])
    if not candidates:
        return {
            'quantity': quantity,
            'unit': unit_def['display'],
            'factor': 1.0,
            'converted': False,
            'type': unit_type
        }

    chosen = candidates[-1]
    for candidate in candidates:
        candidate_qty = base_qty / UNIT_DEFS[candidate]['to_base']
        if candidate_qty >= 1:
            chosen = candidate
            break

    display_qty = base_qty / UNIT_DEFS[chosen]['to_base']
    factor = unit_def['to_base'] / UNIT_DEFS[chosen]['to_base']

    return {
        'quantity': display_qty,
        'unit': UNIT_DEFS[chosen]['display'],
        'factor': factor,
        'converted': chosen != canonical,
        'type': unit_type
    }

def smart_quantity(quantity, unit, system=None):
    unit_system = system or get_unit_system()
    result = convert_quantity(quantity, unit, unit_system)
    return {
        'quantity': format_number(result['quantity']),
        'unit': result['unit'] or unit or '',
        'converted': result['converted'],
        'factor': result['factor'],
        'type': result['type']
    }

def inject_helpers():
    return {
        'smart_quantity': smart_quantity,
        'unit_system': get_unit_system(),
        'recipe_type_choices': RECIPE_TYPE_CHOICES,
        'ingredient_categories': INGREDIENT_CATEGORIES,
        'recipe_categories': RECIPE_CATEGORIES,
        'venue_defaults': VENUE_DEFAULTS
    }

def handle_route_error(error, context='route'):
    if isinstance(error, HTTPException):
        raise error
    current_app.logger.exception("Route error in %s", context)
    if request.path.startswith('/api/'):
        return jsonify({'ok': False, 'error': 'Unexpected server error'}), 500
    flash('Something went wrong. Please try again.', 'error')
    return redirect(request.referrer or url_for('dashboard'))

# Recipe helpers
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

def format_display_value(value, precision=2):
    if value is None:
        return ''
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.{precision}f}".rstrip('0').rstrip('.')

def flatten_components_for_pdf(items, level=0):
    rows = []
    for item in items:
        name = item.get('item_name') or ''
        prefix = '  ' * level
        label = ''
        if item.get('type') == 'recipe':
            label = 'Sub-recipe'
        elif item.get('category'):
            label = item.get('category')

        quantity = item.get('display_quantity')
        if quantity is None:
            quantity = item.get('scaled_quantity_display') or item.get('scaled_quantity') or ''
        qty_text = format_display_value(quantity, precision=2)

        unit = item.get('display_unit') or item.get('unit') or ''
        rows.append({
            'name': f"{prefix}{name}",
            'qty': qty_text,
            'unit': unit,
            'notes': label
        })
        if item.get('children'):
            rows.extend(flatten_components_for_pdf(item.get('children'), level + 1))
    return rows

def flatten_components_for_packet(items, ingredient_map, level=0, explode_subrecipes=True, preserve_base_units=False):
    rows = []
    for item in items:
        item_type = item.get('type') or 'component'
        name = item.get('item_name') or 'Unknown'
        if preserve_base_units:
            quantity = item.get('scaled_quantity_display') or item.get('scaled_quantity') or ''
            unit = item.get('unit') or item.get('display_unit') or ''
        else:
            quantity = item.get('display_quantity')
            if quantity in (None, ''):
                quantity = item.get('scaled_quantity_display') or item.get('scaled_quantity') or ''
            unit = item.get('display_unit') or item.get('unit') or ''
        ext_cost = to_float(item.get('cost_total'))

        g_code = ''
        vendor = ''
        vendor_code = ''
        category = item.get('category') or ''
        if item_type == 'ingredient':
            ing = ingredient_map.get(item.get('item_id')) or {}
            g_code = ing.get('g_code') or ''
            vendor = ing.get('vendor') or ''
            vendor_code = ing.get('vendor_code') or ''
            category = ing.get('category') or category

        rows.append({
            'type': item_type,
            'name': f"{'  ' * level}{name}",
            'quantity': quantity,
            'unit': unit,
            'ext_cost': ext_cost,
            'g_code': g_code,
            'vendor': vendor,
            'vendor_code': vendor_code,
            'category': category,
            'notes': item.get('scale_note') or ''
        })
        has_children = bool(item.get('children'))
        if has_children and not explode_subrecipes and item_type == 'recipe':
            has_children = False
        if has_children:
            rows.extend(
                flatten_components_for_packet(
                    item['children'],
                    ingredient_map,
                    level + 1,
                    explode_subrecipes=explode_subrecipes,
                    preserve_base_units=preserve_base_units
                )
            )
    return rows

def collect_ingredient_usage_from_components(components, menu_label, usage_map):
    for item in components:
        if item.get('type') == 'ingredient' and item.get('item_id'):
            key = item.get('item_id')
            usage_map.setdefault(key, set()).add(menu_label)
        if item.get('children'):
            collect_ingredient_usage_from_components(item['children'], menu_label, usage_map)

def compute_q_factor(total_cost, q_factor_percent):
    q_percent = to_float(q_factor_percent)
    if q_percent < 0:
        q_percent = 0
    q_amount = total_cost * (q_percent / 100)
    grand_total = total_cost + q_amount
    return q_percent, q_amount, grand_total

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
            cost_per_yield = total_cost / yield_qty if yield_qty > 0 else None
            display_yield = smart_quantity(yield_qty, recipe.get('yield_unit'), unit_system) if yield_qty > 0 else None
            batch_recipes.append({
                'id': recipe['id'],
                'name': recipe['name'],
                'category': recipe.get('category'),
                'yield_qty': yield_qty,
                'yield_unit': recipe.get('yield_unit'),
                'display_yield_qty': display_yield['quantity'] if display_yield else None,
                'display_yield_unit': display_yield['unit'] if display_yield else None,
                'total_cost': total_cost,
                'cost_per_yield': cost_per_yield
            })

    return ingredient_master, ingredient_total_cost, batch_recipes

def sanitize_sheet_title(name, existing):
    title = (name or 'Vendor').strip()
    for ch in ['[', ']', ':', '*', '?', '/', '\\']:
        title = title.replace(ch, ' ')
    title = ' '.join(title.split())
    if not title:
        title = 'Vendor'
    title = title[:31]
    base = title
    counter = 1
    while title in existing:
        suffix = f" {counter}"
        max_len = 31 - len(suffix)
        title = f"{base[:max_len]}{suffix}"
        counter += 1
    existing.add(title)
    return title

def make_safe_filename(text):
    cleaned = ''.join(ch for ch in (text or '') if ch.isalnum() or ch in (' ', '-', '_')).strip()
    if not cleaned:
        return 'order_guide'
    return cleaned.replace(' ', '_').lower()

def convert_quantity_between_units(quantity, from_unit, to_unit):
    amount = to_float(quantity)
    from_canonical = normalize_unit(from_unit)
    to_canonical = normalize_unit(to_unit)
    if not from_canonical or not to_canonical:
        return amount if (from_unit or '').strip().lower() == (to_unit or '').strip().lower() else None
    if from_canonical == to_canonical:
        return amount
    from_def = UNIT_DEFS.get(from_canonical)
    to_def = UNIT_DEFS.get(to_canonical)
    if not from_def or not to_def or from_def['type'] != to_def['type']:
        return None
    base_value = amount * from_def['to_base']
    return base_value / to_def['to_base']

def get_banquet_date_window(start_raw, end_raw):
    today = date.today()
    default_start = today
    default_end = default_start + timedelta(days=9)

    start_date = default_start
    end_date = default_end
    try:
        if (start_raw or '').strip():
            start_date = datetime.strptime(start_raw.strip(), '%Y-%m-%d').date()
    except ValueError:
        start_date = default_start
    try:
        if (end_raw or '').strip():
            end_date = datetime.strptime(end_raw.strip(), '%Y-%m-%d').date()
    except ValueError:
        end_date = default_end

    if start_date > end_date:
        start_date, end_date = end_date, start_date
    return start_date, end_date

def clean_menu_text(value):
    text = re.sub(r'\s+', ' ', (value or '')).strip()
    text = text.replace('  ', ' ')
    return text

PREP_FAMILY_ADJECTIVE_STOPWORDS = {
    'rb', 'rm', 'traditional', 'prepared', 'custom', 'house', 'signature',
    'classic', 'style', 'the', 'a', 'an'
}
PREP_FAMILY_FORM_STOPWORDS = {
    'sauce', 'dressing', 'vinaigrette', 'glaze', 'stock', 'broth',
    'base', 'mix', 'seasoning'
}

def derive_prep_family_label(recipe_name):
    raw = clean_menu_text(recipe_name)
    if not raw:
        return 'Other Prep'

    text = raw.lower()
    text = re.sub(r'^(rb|rm)\s+', '', text)
    text = re.sub(r'\([^)]*\)', ' ', text)
    text = text.replace('&', ' and ')
    text = re.sub(r'[^a-z0-9 ]+', ' ', text)
    tokens = [token for token in text.split() if token]
    if not tokens:
        return raw

    base_tokens = [token for token in tokens if token not in PREP_FAMILY_ADJECTIVE_STOPWORDS and not token.isdigit()]
    core_tokens = [token for token in base_tokens if token not in PREP_FAMILY_FORM_STOPWORDS]

    if len(core_tokens) >= 2:
        chosen = core_tokens
    elif len(base_tokens) >= 2:
        chosen = base_tokens
    elif core_tokens:
        chosen = core_tokens
    elif base_tokens:
        chosen = base_tokens
    else:
        chosen = tokens

    label = ' '.join(chosen[:4]).strip()
    return label.title() if label else raw

def split_instruction_steps(instructions):
    raw = (instructions or '').strip()
    if not raw:
        return []
    normalized = raw.replace('\r\n', '\n').replace('\r', '\n')
    numbered_chunks = re.split(r'(?:^|\s+)(?=\d+\.\s+)', normalized.strip())
    parts = []
    if len(numbered_chunks) > 1:
        for chunk in numbered_chunks:
            text = re.sub(r'^\d+\.\s*', '', chunk.strip())
            if text:
                parts.append(text)
        if parts:
            return parts
    for line in normalized.split('\n'):
        text = re.sub(r'^\d+\.\s*', '', line.strip())
        if text:
            parts.append(text)
    if parts:
        return parts
    return [raw]

def normalize_match_key(value):
    text = clean_menu_text(value).lower()
    text = re.sub(r'^(rm|rb)\s+', '', text)
    text = re.sub(r'[^a-z0-9 ]+', ' ', text)
    return ' '.join(text.split())

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

def get_plated_recipes_for_venue(cur, venue_id='', include_unassigned=True):
    has_recipe_venues = db_table_exists(cur, 'public.recipe_venues') and db_table_exists(cur, 'public.venues')
    if has_recipe_venues:
        if include_unassigned:
            cur.execute("""
                SELECT r.id,
                       r.name,
                       r.recipe_type,
                       r.category,
                       r.yield_qty,
                       r.yield_unit,
                       r.menu_descriptor,
                       COALESCE(string_agg(DISTINCT v.name, ', ' ORDER BY v.name), '') AS venue_names
                FROM recipes r
                LEFT JOIN recipe_venues rv ON rv.recipe_id = r.id
                LEFT JOIN venues v ON v.id = rv.venue_id AND v.active = TRUE
                WHERE r.recipe_type = 'menu'
                  AND (
                        %s = '' OR
                        EXISTS (
                            SELECT 1
                            FROM recipe_venues rvf
                            WHERE rvf.recipe_id = r.id AND rvf.venue_id = %s
                        ) OR
                        NOT EXISTS (
                            SELECT 1
                            FROM recipe_venues rvn
                            WHERE rvn.recipe_id = r.id
                        )
                  )
                GROUP BY r.id
                ORDER BY r.name
            """, (venue_id, venue_id))
        else:
            cur.execute("""
                SELECT r.id,
                       r.name,
                       r.recipe_type,
                       r.category,
                       r.yield_qty,
                       r.yield_unit,
                       r.menu_descriptor,
                       COALESCE(string_agg(DISTINCT v.name, ', ' ORDER BY v.name), '') AS venue_names
                FROM recipes r
                LEFT JOIN recipe_venues rv ON rv.recipe_id = r.id
                LEFT JOIN venues v ON v.id = rv.venue_id AND v.active = TRUE
                WHERE r.recipe_type = 'menu'
                  AND (
                        %s = '' OR
                        EXISTS (
                            SELECT 1
                            FROM recipe_venues rvf
                            WHERE rvf.recipe_id = r.id AND rvf.venue_id = %s
                        )
                  )
                GROUP BY r.id
                ORDER BY r.name
            """, (venue_id, venue_id))
    else:
        cur.execute("""
            SELECT r.id,
                   r.name,
                   r.recipe_type,
                   r.category,
                   r.yield_qty,
                   r.yield_unit,
                   r.menu_descriptor,
                   '' AS venue_names
            FROM recipes r
            WHERE r.recipe_type = 'menu'
            ORDER BY r.name
        """)
    return cur.fetchall()

def get_component_recipes_for_venue(cur, venue_id=''):
    has_recipe_venues = db_table_exists(cur, 'public.recipe_venues') and db_table_exists(cur, 'public.venues')
    if has_recipe_venues:
        cur.execute("""
            SELECT r.id,
                   r.name,
                   r.recipe_type,
                   r.category,
                   r.yield_qty,
                   r.yield_unit,
                   r.menu_descriptor,
                   COALESCE(string_agg(DISTINCT v.name, ', ' ORDER BY v.name), '') AS venue_names
            FROM recipes r
            LEFT JOIN recipe_venues rv ON rv.recipe_id = r.id
            LEFT JOIN venues v ON v.id = rv.venue_id AND v.active = TRUE
            WHERE (
                %s = '' OR
                EXISTS (
                    SELECT 1
                    FROM recipe_venues rvf
                    WHERE rvf.recipe_id = r.id AND rvf.venue_id = %s
                ) OR
                NOT EXISTS (
                    SELECT 1
                    FROM recipe_venues rvn
                    WHERE rvn.recipe_id = r.id
                )
            )
            GROUP BY r.id
            ORDER BY CASE WHEN r.recipe_type = 'menu' THEN 0 WHEN r.recipe_type = 'batch' THEN 1 ELSE 2 END, r.name
        """, (venue_id, venue_id))
    else:
        cur.execute("""
            SELECT r.id,
                   r.name,
                   r.recipe_type,
                   r.category,
                   r.yield_qty,
                   r.yield_unit,
                   r.menu_descriptor,
                   '' AS venue_names
            FROM recipes r
            ORDER BY CASE WHEN r.recipe_type = 'menu' THEN 0 WHEN r.recipe_type = 'batch' THEN 1 ELSE 2 END, r.name
        """)
    options = [dict(row) for row in cur.fetchall()]
    for option in options:
        option['option_source'] = 'recipe'
        option['menu_item_name'] = None
        option['menu_item_base_yield_unit'] = None
        option['source_menu_item_id'] = None
        option['menu_item_base_yield_qty'] = None
        option['default_component_unit'] = (option.get('yield_unit') or 'each')

    has_banquet_menu_tables = db_table_exists(cur, 'public.banquet_menu_items') and db_table_exists(cur, 'public.banquet_menu_item_recipes')
    if has_banquet_menu_tables:
        cur.execute("""
            SELECT r.id,
                   r.name,
                   r.recipe_type,
                   r.category,
                   r.yield_qty,
                   r.yield_unit,
                   r.menu_descriptor,
                   '' AS venue_names,
                   mi.id AS source_menu_item_id,
                   mi.name AS menu_item_name,
                   mi.base_yield_qty AS menu_item_base_yield_qty,
                   mi.base_yield_unit AS menu_item_base_yield_unit
            FROM banquet_menu_items mi
            JOIN LATERAL (
                SELECT recipe_id
                FROM banquet_menu_item_recipes
                WHERE menu_item_id = mi.id
                ORDER BY id
                LIMIT 1
            ) pr ON TRUE
            JOIN recipes r ON r.id = pr.recipe_id
            WHERE (%s = '' OR mi.venue_id = %s OR mi.venue_id IS NULL)
            ORDER BY mi.name, r.name
        """, (venue_id, venue_id))
        for row in cur.fetchall():
            option = dict(row)
            option['option_source'] = 'menu_item'
            option['default_component_unit'] = normalize_count_unit(option.get('menu_item_base_yield_unit')) or 'each'
            options.append(option)

    return options

BANQUET_ACTIVE_STATUSES = ('planning', 'confirmed')
BANQUET_VENUE_ID = 'ven_banquets'
BANQUET_BEO_MAX_BYTES = 25 * 1024 * 1024
BANQUET_BEO_UPLOAD_ROOT = os.path.join(os.path.dirname(__file__), 'uploads', 'banquet_beo')
BANQUET_STATUS_CHOICES = [
    ('planning', 'Planning'),
    ('confirmed', 'Confirmed'),
    ('completed', 'Completed'),
    ('cancelled', 'Cancelled')
]
COMMISSARY_ACTIVE_STATUSES = ('pending', 'confirmed', 'in_production')
COMMISSARY_STATUS_CHOICES = [
    ('pending', 'Pending'),
    ('confirmed', 'Confirmed'),
    ('in_production', 'In Production'),
    ('completed', 'Completed'),
    ('cancelled', 'Cancelled')
]
DEFAULT_COMMISSARY_OUTLET = 'Foxtown Brewing'

def resolve_banquet_venue(venues):
    for venue in venues:
        if venue.get('id') == BANQUET_VENUE_ID:
            return venue
    if venues:
        return venues[0]
    return {'id': '', 'name': 'Banquets'}

def banquet_tables_ready(cur):
    required = (
        'public.banquet_menu_items',
        'public.banquet_menu_item_recipes',
        'public.banquet_menu_item_ingredients',
        'public.banquet_events',
        'public.banquet_event_menu_items'
    )
    return all(db_table_exists(cur, table_name) for table_name in required)

def commissary_tables_ready(cur):
    required = (
        'public.outlet_orders',
        'public.outlet_order_items'
    )
    return all(db_table_exists(cur, table_name) for table_name in required)

def ensure_commissary_tables(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS outlet_orders (
            id TEXT PRIMARY KEY,
            outlet TEXT NOT NULL,
            needed_date DATE NOT NULL,
            status TEXT DEFAULT 'pending',
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS outlet_order_items (
            id BIGSERIAL PRIMARY KEY,
            order_id TEXT NOT NULL REFERENCES outlet_orders(id) ON DELETE CASCADE,
            recipe_id TEXT REFERENCES recipes(id) ON DELETE SET NULL,
            item_name TEXT,
            quantity NUMERIC NOT NULL DEFAULT 1,
            quantity_unit TEXT DEFAULT 'each',
            notes TEXT,
            sort_order INTEGER DEFAULT 0
        )
    """)
    cur.execute("ALTER TABLE outlet_orders ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'pending'")
    cur.execute("ALTER TABLE outlet_orders ADD COLUMN IF NOT EXISTS notes TEXT")
    cur.execute("ALTER TABLE outlet_orders ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    cur.execute("ALTER TABLE outlet_orders ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")

    cur.execute("ALTER TABLE outlet_order_items ADD COLUMN IF NOT EXISTS item_name TEXT")
    cur.execute("ALTER TABLE outlet_order_items ADD COLUMN IF NOT EXISTS quantity_unit TEXT DEFAULT 'each'")
    cur.execute("ALTER TABLE outlet_order_items ADD COLUMN IF NOT EXISTS notes TEXT")
    cur.execute("ALTER TABLE outlet_order_items ADD COLUMN IF NOT EXISTS sort_order INTEGER DEFAULT 0")
    cur.execute("ALTER TABLE outlet_order_items ALTER COLUMN quantity SET DEFAULT 1")
    cur.execute("""
        UPDATE outlet_order_items
        SET quantity_unit = 'each'
        WHERE quantity_unit IS NULL OR TRIM(quantity_unit) = ''
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_outlet_orders_needed_date ON outlet_orders (needed_date)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_outlet_orders_status ON outlet_orders (status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_outlet_order_items_order_id ON outlet_order_items (order_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_outlet_order_items_recipe_id ON outlet_order_items (recipe_id)")

def get_commissary_outlet_options(cur):
    options = {DEFAULT_COMMISSARY_OUTLET}
    for venue in get_active_venues(cur):
        venue_name = clean_menu_text(venue.get('name'))
        if venue_name:
            options.add(venue_name)
    if db_table_exists(cur, 'public.outlet_orders'):
        cur.execute("""
            SELECT DISTINCT outlet
            FROM outlet_orders
            WHERE outlet IS NOT NULL AND TRIM(outlet) <> ''
            ORDER BY outlet
        """)
        for row in cur.fetchall():
            outlet_name = clean_menu_text(row.get('outlet'))
            if outlet_name:
                options.add(outlet_name)
    return sorted(options, key=lambda value: value.lower())

def banquet_shopping_checks_ready(cur):
    return db_table_exists(cur, 'public.banquet_shopping_checks')

def auto_complete_past_banquet_events(cur, venue_id=''):
    if not banquet_tables_ready(cur):
        return 0

    params = [date.today(), list(BANQUET_ACTIVE_STATUSES)]
    venue_filter_sql = ''
    if venue_id:
        venue_filter_sql = ' AND venue_id = %s'
        params.append(venue_id)

    cur.execute(f"""
        UPDATE banquet_events
        SET status = 'completed',
            updated_at = CURRENT_TIMESTAMP
        WHERE event_date < %s
          AND status = ANY(%s)
          {venue_filter_sql}
    """, params)
    updated_count = cur.rowcount or 0
    if updated_count > 0:
        cur.connection.commit()
    return updated_count

def banquet_event_beo_files_ready(cur):
    return db_table_exists(cur, 'public.banquet_event_beo_files')

def ensure_banquet_event_beo_files_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS banquet_event_beo_files (
            id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL REFERENCES banquet_events(id) ON DELETE CASCADE,
            original_filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL,
            mime_type TEXT,
            file_size_bytes BIGINT,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_banquet_event_beo_files_event
        ON banquet_event_beo_files (event_id, uploaded_at DESC)
    """)

def safe_pdf_filename(filename):
    base = os.path.basename((filename or '').strip())
    cleaned = re.sub(r'[^A-Za-z0-9._-]+', '_', base).strip('._')
    if not cleaned:
        cleaned = 'beo.pdf'
    if not cleaned.lower().endswith('.pdf'):
        cleaned = f"{cleaned}.pdf"
    return cleaned

def event_beo_file_path(event_id, stored_filename):
    return os.path.join(BANQUET_BEO_UPLOAD_ROOT, event_id, stored_filename)

def list_banquet_event_beo_files(cur, event_id):
    if not event_id:
        return []
    ensure_banquet_event_beo_files_table(cur)
    cur.execute("""
        SELECT id,
               event_id,
               original_filename,
               stored_filename,
               mime_type,
               file_size_bytes,
               uploaded_at
        FROM banquet_event_beo_files
        WHERE event_id = %s
        ORDER BY uploaded_at DESC, id DESC
    """, (event_id,))
    return cur.fetchall()

def list_banquet_event_beo_files_by_events(cur, event_ids):
    event_ids = [event_id for event_id in (event_ids or []) if event_id]
    if not event_ids:
        return {}
    ensure_banquet_event_beo_files_table(cur)
    cur.execute("""
        SELECT id,
               event_id,
               original_filename,
               stored_filename,
               mime_type,
               file_size_bytes,
               uploaded_at
        FROM banquet_event_beo_files
        WHERE event_id = ANY(%s)
        ORDER BY event_id, uploaded_at DESC, id DESC
    """, (event_ids,))
    grouped = defaultdict(list)
    for row in cur.fetchall():
        grouped[row.get('event_id')].append(row)
    return grouped

def get_banquet_event_beo_file(cur, event_id, file_id):
    ensure_banquet_event_beo_files_table(cur)
    cur.execute("""
        SELECT id,
               event_id,
               original_filename,
               stored_filename,
               mime_type,
               file_size_bytes,
               uploaded_at
        FROM banquet_event_beo_files
        WHERE event_id = %s
          AND id = %s
        LIMIT 1
    """, (event_id, file_id))
    return cur.fetchone()

def store_banquet_event_beo_file(cur, event_id, upload):
    if upload is None or not upload.filename:
        return None, 'Choose a PDF file to upload.'

    original_filename = safe_pdf_filename(upload.filename)
    if not original_filename.lower().endswith('.pdf'):
        return None, 'Only PDF files are allowed.'

    try:
        upload.stream.seek(0, os.SEEK_END)
        file_size = upload.stream.tell()
        upload.stream.seek(0)
    except Exception:
        file_size = 0

    if file_size <= 0:
        return None, 'Uploaded file is empty.'
    if file_size > BANQUET_BEO_MAX_BYTES:
        max_mb = int(BANQUET_BEO_MAX_BYTES / (1024 * 1024))
        return None, f'BEO PDF is too large. Max size is {max_mb} MB.'

    header = upload.stream.read(5)
    upload.stream.seek(0)
    if header != b'%PDF-':
        return None, 'File does not look like a valid PDF.'

    file_id = generate_id('beo_')
    stored_filename = f"{file_id}.pdf"
    event_folder = os.path.join(BANQUET_BEO_UPLOAD_ROOT, event_id)
    os.makedirs(event_folder, exist_ok=True)
    file_path = os.path.join(event_folder, stored_filename)
    upload.save(file_path)

    ensure_banquet_event_beo_files_table(cur)
    cur.execute("""
        INSERT INTO banquet_event_beo_files (
            id, event_id, original_filename, stored_filename, mime_type, file_size_bytes
        ) VALUES (%s, %s, %s, %s, %s, %s)
    """, (
        file_id,
        event_id,
        original_filename,
        stored_filename,
        'application/pdf',
        file_size
    ))

    return {
        'id': file_id,
        'event_id': event_id,
        'original_filename': original_filename,
        'stored_filename': stored_filename,
        'file_size_bytes': file_size
    }, None

def ensure_banquet_shopping_checks_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS banquet_shopping_checks (
            id BIGSERIAL PRIMARY KEY,
            venue_id TEXT NOT NULL,
            start_date DATE NOT NULL,
            end_date DATE NOT NULL,
            item_key TEXT NOT NULL,
            ingredient_id TEXT,
            unit TEXT,
            vendor TEXT,
            checked BOOLEAN NOT NULL DEFAULT FALSE,
            note TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_banquet_shopping_checks_unique
        ON banquet_shopping_checks (venue_id, start_date, end_date, item_key)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_banquet_shopping_checks_scope
        ON banquet_shopping_checks (venue_id, start_date, end_date)
    """)

def load_banquet_shopping_checklist(cur, venue_id, start_date, end_date):
    ensure_banquet_shopping_checks_table(cur)
    cur.execute("""
        SELECT item_key, checked, note
        FROM banquet_shopping_checks
        WHERE venue_id = %s
          AND start_date = %s
          AND end_date = %s
    """, (venue_id, start_date, end_date))
    return {
        row['item_key']: {
            'checked': bool(row.get('checked')),
            'note': row.get('note') or ''
        }
        for row in cur.fetchall()
    }

def list_banquet_menu_items(cur, venue_id=''):
    if not banquet_tables_ready(cur):
        return []
    has_base_yield = db_column_exists(cur, 'public.banquet_menu_items', 'base_yield_qty') and db_column_exists(cur, 'public.banquet_menu_items', 'base_yield_unit')
    base_yield_qty_sql = "mi.base_yield_qty" if has_base_yield else "1::numeric"
    base_yield_unit_sql = "mi.base_yield_unit" if has_base_yield else "'each'"
    if venue_id:
        cur.execute("""
            SELECT mi.id,
                   mi.name,
                   mi.venue_id,
                   COALESCE(v.name, '') AS venue_name,
                   mi.menu_section,
                   mi.menu_descriptor,
                   mi.notes,
                   {base_yield_qty_sql} AS base_yield_qty,
                   {base_yield_unit_sql} AS base_yield_unit,
                   pr.recipe_id AS linked_recipe_id,
                   r.name AS recipe_name,
                   COALESCE(rc.recipe_count, 0) AS recipe_count,
                   COALESCE(extra.extra_count, 0) AS extra_count
            FROM banquet_menu_items mi
            LEFT JOIN venues v ON v.id = mi.venue_id
            LEFT JOIN LATERAL (
                SELECT recipe_id
                FROM banquet_menu_item_recipes
                WHERE menu_item_id = mi.id
                ORDER BY id
                LIMIT 1
            ) pr ON TRUE
            LEFT JOIN recipes r ON r.id = pr.recipe_id
            LEFT JOIN (
                SELECT menu_item_id, COUNT(*) AS recipe_count
                FROM banquet_menu_item_recipes
                GROUP BY menu_item_id
            ) rc ON rc.menu_item_id = mi.id
            LEFT JOIN (
                SELECT menu_item_id, COUNT(*) AS extra_count
                FROM banquet_menu_item_ingredients
                GROUP BY menu_item_id
            ) extra ON extra.menu_item_id = mi.id
            WHERE mi.venue_id = %s OR mi.venue_id IS NULL
            ORDER BY COALESCE(mi.menu_section, 'zzzz'), mi.name
        """.format(base_yield_qty_sql=base_yield_qty_sql, base_yield_unit_sql=base_yield_unit_sql), (venue_id,))
    else:
        cur.execute("""
            SELECT mi.id,
                   mi.name,
                   mi.venue_id,
                   COALESCE(v.name, '') AS venue_name,
                   mi.menu_section,
                   mi.menu_descriptor,
                   mi.notes,
                   {base_yield_qty_sql} AS base_yield_qty,
                   {base_yield_unit_sql} AS base_yield_unit,
                   pr.recipe_id AS linked_recipe_id,
                   r.name AS recipe_name,
                   COALESCE(rc.recipe_count, 0) AS recipe_count,
                   COALESCE(extra.extra_count, 0) AS extra_count
            FROM banquet_menu_items mi
            LEFT JOIN venues v ON v.id = mi.venue_id
            LEFT JOIN LATERAL (
                SELECT recipe_id
                FROM banquet_menu_item_recipes
                WHERE menu_item_id = mi.id
                ORDER BY id
                LIMIT 1
            ) pr ON TRUE
            LEFT JOIN recipes r ON r.id = pr.recipe_id
            LEFT JOIN (
                SELECT menu_item_id, COUNT(*) AS recipe_count
                FROM banquet_menu_item_recipes
                GROUP BY menu_item_id
            ) rc ON rc.menu_item_id = mi.id
            LEFT JOIN (
                SELECT menu_item_id, COUNT(*) AS extra_count
                FROM banquet_menu_item_ingredients
                GROUP BY menu_item_id
            ) extra ON extra.menu_item_id = mi.id
            ORDER BY COALESCE(mi.menu_section, 'zzzz'), mi.name
        """.format(base_yield_qty_sql=base_yield_qty_sql, base_yield_unit_sql=base_yield_unit_sql))
    return cur.fetchall()

def get_banquet_menu_item(cur, menu_item_id):
    if not banquet_tables_ready(cur):
        return None
    has_base_yield = db_column_exists(cur, 'public.banquet_menu_items', 'base_yield_qty') and db_column_exists(cur, 'public.banquet_menu_items', 'base_yield_unit')
    base_yield_qty_sql = "mi.base_yield_qty" if has_base_yield else "1::numeric"
    base_yield_unit_sql = "mi.base_yield_unit" if has_base_yield else "'each'"
    cur.execute("""
        SELECT mi.id,
               mi.name,
               mi.venue_id,
               mi.menu_section,
               mi.menu_descriptor,
               mi.notes,
               {base_yield_qty_sql} AS base_yield_qty,
               {base_yield_unit_sql} AS base_yield_unit,
               pr.recipe_id AS linked_recipe_id,
               pr.quantity AS linked_recipe_qty,
               pr.unit AS linked_recipe_unit
        FROM banquet_menu_items mi
        LEFT JOIN LATERAL (
            SELECT recipe_id, quantity, unit
            FROM banquet_menu_item_recipes
            WHERE menu_item_id = mi.id
            ORDER BY id
            LIMIT 1
        ) pr ON TRUE
        WHERE mi.id = %s
        LIMIT 1
    """.format(base_yield_qty_sql=base_yield_qty_sql, base_yield_unit_sql=base_yield_unit_sql), (menu_item_id,))
    return cur.fetchone()

def get_banquet_menu_item_recipe_components(cur, menu_item_id):
    if not banquet_tables_ready(cur):
        return []
    has_choice_columns = db_column_exists(cur, 'public.banquet_menu_item_recipes', 'choice_group') and db_column_exists(cur, 'public.banquet_menu_item_recipes', 'choice_weight_percent')
    choice_group_sql = "mr.choice_group AS choice_group" if has_choice_columns else "NULL::text AS choice_group"
    choice_weight_sql = "mr.choice_weight_percent AS choice_weight_percent" if has_choice_columns else "NULL::numeric AS choice_weight_percent"
    cur.execute(f"""
        SELECT mr.id,
               mr.menu_item_id,
               mr.recipe_id,
               mr.quantity,
               mr.unit,
               {choice_group_sql},
               {choice_weight_sql},
               r.name AS recipe_name,
               r.yield_qty,
               r.yield_unit,
               r.recipe_type
        FROM banquet_menu_item_recipes mr
        JOIN recipes r ON r.id = mr.recipe_id
        WHERE mr.menu_item_id = %s
        ORDER BY mr.id
    """, (menu_item_id,))
    return cur.fetchall()

def collect_banquet_recipe_components(cur, menu_item_ids):
    if not menu_item_ids:
        return {}
    has_choice_columns = db_column_exists(cur, 'public.banquet_menu_item_recipes', 'choice_group') and db_column_exists(cur, 'public.banquet_menu_item_recipes', 'choice_weight_percent')
    choice_group_sql = "mr.choice_group AS choice_group" if has_choice_columns else "NULL::text AS choice_group"
    choice_weight_sql = "mr.choice_weight_percent AS choice_weight_percent" if has_choice_columns else "NULL::numeric AS choice_weight_percent"
    cur.execute(f"""
        SELECT mr.menu_item_id,
               mr.recipe_id,
               mr.quantity,
               mr.unit,
               {choice_group_sql},
               {choice_weight_sql},
               r.name AS recipe_name,
               r.yield_qty,
               r.yield_unit,
               r.recipe_type
        FROM banquet_menu_item_recipes mr
        JOIN recipes r ON r.id = mr.recipe_id
        WHERE mr.menu_item_id = ANY(%s)
        ORDER BY mr.menu_item_id, mr.id
    """, (menu_item_ids,))
    grouped = defaultdict(list)
    for row in cur.fetchall():
        grouped[row['menu_item_id']].append(row)
    return grouped

def get_banquet_menu_item_component_summaries(cur, menu_item_ids):
    if not menu_item_ids:
        return {}
    summaries = {menu_item_id: {'recipes': [], 'ingredients': []} for menu_item_id in menu_item_ids}

    has_choice_columns = db_column_exists(cur, 'public.banquet_menu_item_recipes', 'choice_group') and db_column_exists(cur, 'public.banquet_menu_item_recipes', 'choice_weight_percent')
    choice_group_sql = "mr.choice_group AS choice_group" if has_choice_columns else "NULL::text AS choice_group"
    choice_weight_sql = "mr.choice_weight_percent AS choice_weight_percent" if has_choice_columns else "NULL::numeric AS choice_weight_percent"
    cur.execute(f"""
        SELECT mr.menu_item_id,
               mr.recipe_id,
               mr.quantity,
               mr.unit,
               {choice_group_sql},
               {choice_weight_sql},
               r.name AS recipe_name
        FROM banquet_menu_item_recipes mr
        JOIN recipes r ON r.id = mr.recipe_id
        WHERE mr.menu_item_id = ANY(%s)
        ORDER BY mr.menu_item_id, mr.id
    """, (menu_item_ids,))
    for row in cur.fetchall():
        summaries.setdefault(row['menu_item_id'], {'recipes': [], 'ingredients': []})
        summaries[row['menu_item_id']]['recipes'].append({
            'id': row.get('recipe_id'),
            'name': row.get('recipe_name'),
            'quantity': to_float(row.get('quantity')),
            'unit': row.get('unit') or '',
            'choice_group': row.get('choice_group'),
            'choice_weight_percent': to_float(row.get('choice_weight_percent'))
        })

    cur.execute("""
        SELECT ai.menu_item_id,
               ai.ingredient_id,
               ai.quantity,
               ai.unit,
               i.name AS ingredient_name
        FROM banquet_menu_item_ingredients ai
        JOIN ingredients i ON i.id = ai.ingredient_id
        WHERE ai.menu_item_id = ANY(%s)
        ORDER BY ai.menu_item_id, ai.id
    """, (menu_item_ids,))
    for row in cur.fetchall():
        summaries.setdefault(row['menu_item_id'], {'recipes': [], 'ingredients': []})
        summaries[row['menu_item_id']]['ingredients'].append({
            'id': row.get('ingredient_id'),
            'name': row.get('ingredient_name'),
            'quantity': to_float(row.get('quantity')),
            'unit': row.get('unit') or ''
        })

    return summaries

def get_banquet_menu_item_ingredients(cur, menu_item_id):
    if not banquet_tables_ready(cur):
        return []
    cur.execute("""
        SELECT ai.id,
               ai.menu_item_id,
               ai.ingredient_id,
               ai.quantity,
               ai.unit,
               i.name AS ingredient_name
        FROM banquet_menu_item_ingredients ai
        JOIN ingredients i ON i.id = ai.ingredient_id
        WHERE ai.menu_item_id = %s
        ORDER BY i.name
    """, (menu_item_id,))
    return cur.fetchall()

def get_banquet_event(cur, event_id):
    if not banquet_tables_ready(cur):
        return None
    cur.execute("""
        SELECT e.id,
               e.name,
               e.event_date,
               e.guest_count AS guests,
               e.guest_count,
               e.venue_id,
               e.building,
               e.room,
               e.service_timing,
               e.dietary_notes,
               e.notes,
               e.status,
               COALESCE(v.name, '') AS venue_name
        FROM banquet_events e
        LEFT JOIN venues v ON v.id = e.venue_id
        WHERE e.id = %s
    """, (event_id,))
    return cur.fetchone()

def get_banquet_event_lines(cur, event_id):
    if not banquet_tables_ready(cur):
        return []
    has_choice_selections_column = db_column_exists(cur, 'public.banquet_event_menu_items', 'choice_selections')
    choice_selections_sql = "emi.choice_selections AS line_choice_selections" if has_choice_selections_column else "NULL::text AS line_choice_selections"
    cur.execute(f"""
        SELECT emi.id AS line_id,
               emi.event_id,
               emi.menu_item_id,
               COALESCE(NULLIF(TRIM(emi.menu_item_name), ''), mi.name) AS menu_item_name,
               COALESCE(emi.menu_descriptor, mi.menu_descriptor) AS menu_descriptor,
               COALESCE(emi.menu_section, mi.menu_section) AS menu_section,
               COALESCE(emi.recipe_id, primary_recipe.recipe_id) AS recipe_id,
               emi.quantity,
               emi.quantity_unit,
               emi.notes AS line_notes,
               {choice_selections_sql},
               r.name AS recipe_name,
               r.yield_qty,
               r.yield_unit,
               r.menu_descriptor AS recipe_descriptor
        FROM banquet_event_menu_items emi
        JOIN banquet_menu_items mi ON mi.id = emi.menu_item_id
        LEFT JOIN LATERAL (
            SELECT recipe_id
            FROM banquet_menu_item_recipes
            WHERE menu_item_id = mi.id
            ORDER BY id
            LIMIT 1
        ) primary_recipe ON TRUE
        LEFT JOIN recipes r ON r.id = COALESCE(emi.recipe_id, primary_recipe.recipe_id)
        WHERE emi.event_id = %s
        ORDER BY COALESCE(emi.sort_order, 0), emi.id
    """, (event_id,))
    return cur.fetchall()

def resolve_or_create_banquet_menu_item(cur, line, venue_id=''):
    name = clean_menu_text(line.get('menu_item_name'))
    if not name:
        return None

    cur.execute("""
        SELECT id, menu_section, menu_descriptor
        FROM banquet_menu_items
        WHERE LOWER(name) = LOWER(%s)
          AND (venue_id = %s OR venue_id IS NULL)
        ORDER BY CASE WHEN venue_id = %s THEN 0 ELSE 1 END, created_at
        LIMIT 1
    """, (name, venue_id or None, venue_id or None))
    existing = cur.fetchone()
    if existing:
        updates = []
        params = []
        if line.get('menu_section') and not existing.get('menu_section'):
            updates.append('menu_section = %s')
            params.append(line.get('menu_section'))
        if line.get('menu_descriptor') and not existing.get('menu_descriptor'):
            updates.append('menu_descriptor = %s')
            params.append(line.get('menu_descriptor'))
        if updates:
            updates.append('updated_at = CURRENT_TIMESTAMP')
            params.append(existing['id'])
            cur.execute(f"UPDATE banquet_menu_items SET {', '.join(updates)} WHERE id = %s", params)
        return existing['id']

    menu_item_id = generate_id('bmi_')
    cur.execute("""
        INSERT INTO banquet_menu_items (id, name, venue_id, menu_section, menu_descriptor, notes)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (
        menu_item_id,
        name,
        venue_id or None,
        line.get('menu_section') or None,
        line.get('menu_descriptor') or None,
        None
    ))
    return menu_item_id

def upsert_menu_item_recipe_link(cur, menu_item_id, recipe_id):
    if not menu_item_id or not recipe_id:
        return
    cur.execute("""
        SELECT id
        FROM banquet_menu_item_recipes
        WHERE menu_item_id = %s
          AND recipe_id = %s
        LIMIT 1
    """, (menu_item_id, recipe_id))
    if cur.fetchone():
        return
    cur.execute("""
        INSERT INTO banquet_menu_item_recipes (menu_item_id, recipe_id, quantity, unit)
        VALUES (%s, %s, 1, 'serving')
    """, (menu_item_id, recipe_id))

def normalize_return_path(value):
    path = (value or '').strip()
    if not path:
        return None
    if not path.startswith('/') or path.startswith('//'):
        return None
    return path

def make_unique_banquet_menu_item_name(cur, base_name, venue_id):
    candidate = clean_menu_text(base_name) or 'Custom Menu Item'
    suffix = 2
    while True:
        cur.execute("""
            SELECT 1
            FROM banquet_menu_items
            WHERE LOWER(name) = LOWER(%s)
              AND (venue_id = %s OR venue_id IS NULL)
            LIMIT 1
        """, (candidate, venue_id or None))
        if not cur.fetchone():
            return candidate
        candidate = f"{base_name} {suffix}"
        suffix += 1

def clone_banquet_menu_item(cur, source_menu_item_id, venue_id, clone_label='Custom'):
    source_item = get_banquet_menu_item(cur, source_menu_item_id)
    if not source_item:
        return None

    base_name = f"{source_item.get('name') or 'Menu Item'} ({clone_label})"
    clone_name = make_unique_banquet_menu_item_name(cur, base_name, venue_id)
    clone_id = generate_id('bmi_')

    has_base_yield = db_column_exists(cur, 'public.banquet_menu_items', 'base_yield_qty') and db_column_exists(cur, 'public.banquet_menu_items', 'base_yield_unit')
    if has_base_yield:
        cur.execute("""
            INSERT INTO banquet_menu_items (
                id, name, venue_id, menu_section, menu_descriptor, base_yield_qty, base_yield_unit, notes
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            clone_id,
            clone_name,
            venue_id or source_item.get('venue_id'),
            source_item.get('menu_section'),
            source_item.get('menu_descriptor'),
            source_item.get('base_yield_qty') or 1,
            source_item.get('base_yield_unit') or 'each',
            source_item.get('notes')
        ))
    else:
        cur.execute("""
            INSERT INTO banquet_menu_items (
                id, name, venue_id, menu_section, menu_descriptor, notes
            ) VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            clone_id,
            clone_name,
            venue_id or source_item.get('venue_id'),
            source_item.get('menu_section'),
            source_item.get('menu_descriptor'),
            source_item.get('notes')
        ))

    has_choice_columns = db_column_exists(cur, 'public.banquet_menu_item_recipes', 'choice_group') and db_column_exists(cur, 'public.banquet_menu_item_recipes', 'choice_weight_percent')
    for row in get_banquet_menu_item_recipe_components(cur, source_menu_item_id):
        if has_choice_columns:
            cur.execute("""
                INSERT INTO banquet_menu_item_recipes (menu_item_id, recipe_id, quantity, unit, choice_group, choice_weight_percent)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (
                clone_id,
                row.get('recipe_id'),
                row.get('quantity') or 1,
                row.get('unit') or 'each',
                row.get('choice_group'),
                row.get('choice_weight_percent')
            ))
        else:
            cur.execute("""
                INSERT INTO banquet_menu_item_recipes (menu_item_id, recipe_id, quantity, unit)
                VALUES (%s, %s, %s, %s)
            """, (
                clone_id,
                row.get('recipe_id'),
                row.get('quantity') or 1,
                row.get('unit') or 'each'
            ))

    for row in get_banquet_menu_item_ingredients(cur, source_menu_item_id):
        cur.execute("""
            INSERT INTO banquet_menu_item_ingredients (menu_item_id, ingredient_id, quantity, unit)
            VALUES (%s, %s, %s, %s)
        """, (
            clone_id,
            row.get('ingredient_id'),
            row.get('quantity') or 0,
            row.get('unit')
        ))

    return {
        'id': clone_id,
        'name': clone_name,
        'menu_section': source_item.get('menu_section'),
        'menu_descriptor': source_item.get('menu_descriptor')
    }

def get_commissary_order(cur, order_id):
    if not commissary_tables_ready(cur):
        return None
    cur.execute("""
        SELECT id,
               outlet,
               needed_date,
               status,
               notes,
               created_at,
               updated_at
        FROM outlet_orders
        WHERE id = %s
        LIMIT 1
    """, (order_id,))
    return cur.fetchone()

def get_commissary_order_lines(cur, order_id):
    if not commissary_tables_ready(cur):
        return []
    cur.execute("""
        SELECT oi.id AS line_id,
               oi.order_id,
               oi.recipe_id,
               oi.item_name,
               oi.quantity,
               oi.quantity_unit,
               oi.notes AS line_notes,
               oi.sort_order,
               r.name AS recipe_name,
               r.yield_qty,
               r.yield_unit,
               r.recipe_type
        FROM outlet_order_items oi
        LEFT JOIN recipes r ON r.id = oi.recipe_id
        WHERE oi.order_id = %s
        ORDER BY COALESCE(oi.sort_order, 0), oi.id
    """, (order_id,))
    return cur.fetchall()

def parse_commissary_order_lines(request, valid_recipe_ids):
    line_ids = request.form.getlist('line_id[]')
    recipe_ids = request.form.getlist('line_recipe_id[]')
    item_names = request.form.getlist('line_item_name[]')
    quantities = request.form.getlist('line_qty[]')
    quantity_units = request.form.getlist('line_unit[]')
    notes = request.form.getlist('line_notes[]')

    max_len = max(
        len(line_ids),
        len(recipe_ids),
        len(item_names),
        len(quantities),
        len(quantity_units),
        len(notes),
        0
    )
    rows = []
    errors = []
    for idx in range(max_len):
        line_id = (line_ids[idx] if idx < len(line_ids) else '').strip()
        recipe_id = (recipe_ids[idx] if idx < len(recipe_ids) else '').strip()
        item_name = clean_menu_text(item_names[idx] if idx < len(item_names) else '')
        qty_raw = (quantities[idx] if idx < len(quantities) else '').strip()
        qty_unit_raw = clean_menu_text(quantity_units[idx] if idx < len(quantity_units) else '')
        note = clean_menu_text(notes[idx] if idx < len(notes) else '')

        if not any([recipe_id, item_name, qty_raw, qty_unit_raw, note]):
            continue
        if recipe_id and recipe_id not in valid_recipe_ids:
            errors.append('One or more commissary line items reference an invalid recipe.')
            continue
        if not recipe_id and not item_name:
            errors.append('Each commissary line needs a recipe or item name.')
        qty = parse_float_field(qty_raw, 'Order quantity', errors, required=True, min_value=0.0001)
        if qty is None:
            continue
        normalized_unit = normalize_unit(qty_unit_raw) or normalize_count_unit(qty_unit_raw) or qty_unit_raw or None
        rows.append({
            'line_id': line_id or None,
            'recipe_id': recipe_id or None,
            'item_name': item_name or None,
            'quantity': qty,
            'quantity_unit': normalized_unit,
            'notes': note or None
        })
    return rows, errors

def normalize_event_line_choice_selections(value):
    text = (value or '').strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return None

    if isinstance(payload, dict):
        raw_items = payload.get('items')
    elif isinstance(payload, list):
        raw_items = payload
    else:
        raw_items = []

    items = []
    for raw in raw_items or []:
        if not isinstance(raw, dict):
            continue
        recipe_id = clean_menu_text(raw.get('recipe_id'))
        if not recipe_id:
            continue
        count = to_float(raw.get('count'))
        if count <= 0:
            continue
        items.append({
            'recipe_id': recipe_id,
            'choice_group': clean_menu_text(raw.get('choice_group')) or None,
            'recipe_name': clean_menu_text(raw.get('recipe_name')) or None,
            'count': round(count, 6)
        })

    if not items:
        return None

    return json.dumps({'items': items}, separators=(',', ':'))

def parse_event_line_choice_selections(value):
    normalized = normalize_event_line_choice_selections(value)
    if not normalized:
        return []
    try:
        payload = json.loads(normalized)
    except (TypeError, ValueError):
        return []
    return payload.get('items') or []

def parse_event_recipe_lines(request):
    line_ids = request.form.getlist('line_id[]')
    menu_item_ids = request.form.getlist('line_menu_item_id[]')
    recipe_ids = request.form.getlist('line_recipe_id[]')
    names = request.form.getlist('line_menu_item_name[]')
    descriptors = request.form.getlist('line_menu_descriptor[]')
    sections = request.form.getlist('line_menu_section[]')
    quantities = request.form.getlist('line_qty[]')
    quantity_units = request.form.getlist('line_unit[]')
    notes = request.form.getlist('line_notes[]')
    choice_selections = request.form.getlist('line_choice_selections[]')

    max_len = max(
        len(line_ids), len(menu_item_ids), len(recipe_ids), len(names), len(descriptors), len(sections),
        len(quantities), len(quantity_units), len(notes), len(choice_selections), 0
    )
    rows = []
    errors = []
    for idx in range(max_len):
        line_id = (line_ids[idx] if idx < len(line_ids) else '').strip()
        menu_item_id = (menu_item_ids[idx] if idx < len(menu_item_ids) else '').strip()
        recipe_id = (recipe_ids[idx] if idx < len(recipe_ids) else '').strip()
        menu_name = clean_menu_text(names[idx] if idx < len(names) else '')
        descriptor = clean_menu_text(descriptors[idx] if idx < len(descriptors) else '')
        section = clean_menu_text(sections[idx] if idx < len(sections) else '')
        qty_raw = (quantities[idx] if idx < len(quantities) else '').strip()
        qty_unit = clean_menu_text(quantity_units[idx] if idx < len(quantity_units) else '')
        note = clean_menu_text(notes[idx] if idx < len(notes) else '')
        choice_selection_text = normalize_event_line_choice_selections(choice_selections[idx] if idx < len(choice_selections) else '')

        if not any([menu_item_id, recipe_id, menu_name, descriptor, section, qty_raw, qty_unit, note, choice_selection_text]):
            continue
        qty = parse_float_field(qty_raw, 'Menu quantity', errors, required=True, min_value=0.0001)
        if not menu_name and not menu_item_id:
            errors.append('Each banquet line needs a menu item name.')
        rows.append({
            'line_id': line_id or None,
            'menu_item_id': menu_item_id or None,
            'recipe_id': recipe_id or None,
            'menu_item_name': menu_name,
            'menu_descriptor': descriptor or None,
            'menu_section': section or None,
            'quantity': qty,
            'quantity_unit': normalize_unit(qty_unit) or qty_unit or None,
            'notes': note or None,
            'choice_selections': choice_selection_text
        })
    return rows, errors

def parse_menu_item_ingredient_lines(request, valid_ingredient_ids):
    ingredient_ids = request.form.getlist('ingredient_id[]')
    quantities = request.form.getlist('ingredient_qty[]')
    units = request.form.getlist('ingredient_unit[]')

    max_len = max(len(ingredient_ids), len(quantities), len(units), 0)
    rows = []
    errors = []
    for idx in range(max_len):
        ingredient_id = (ingredient_ids[idx] if idx < len(ingredient_ids) else '').strip()
        quantity_raw = (quantities[idx] if idx < len(quantities) else '').strip()
        unit = clean_menu_text(units[idx] if idx < len(units) else '')
        if not any([ingredient_id, quantity_raw, unit]):
            continue
        if ingredient_id not in valid_ingredient_ids:
            errors.append('Additional ingredient row has an invalid ingredient.')
            continue
        quantity = parse_float_field(
            quantity_raw,
            'Additional ingredient quantity',
            errors,
            required=True,
            min_value=0.0001
        )
        if quantity is None:
            continue
        rows.append({
            'ingredient_id': ingredient_id,
            'quantity': quantity,
            'unit': normalize_unit(unit) or unit
        })
    return rows, errors

def parse_menu_item_recipe_lines(request, valid_recipe_ids, valid_source_menu_item_ids=None):
    recipe_ids = request.form.getlist('component_recipe_id[]')
    source_menu_item_ids = request.form.getlist('component_source_menu_item_id[]')
    quantities = request.form.getlist('component_qty[]')
    units = request.form.getlist('component_unit[]')
    choice_groups = request.form.getlist('component_choice_group[]')
    choice_weights = request.form.getlist('component_choice_weight[]')

    max_len = max(len(recipe_ids), len(source_menu_item_ids), len(quantities), len(units), len(choice_groups), len(choice_weights), 0)
    rows = []
    errors = []
    valid_source_ids = set(valid_source_menu_item_ids or set())
    for idx in range(max_len):
        recipe_id = (recipe_ids[idx] if idx < len(recipe_ids) else '').strip()
        source_menu_item_id = (source_menu_item_ids[idx] if idx < len(source_menu_item_ids) else '').strip()
        quantity_raw = (quantities[idx] if idx < len(quantities) else '').strip()
        unit = clean_menu_text(units[idx] if idx < len(units) else '')
        choice_group = clean_menu_text(choice_groups[idx] if idx < len(choice_groups) else '')
        choice_weight_raw = (choice_weights[idx] if idx < len(choice_weights) else '').strip()

        if not any([recipe_id, source_menu_item_id, quantity_raw, unit, choice_group, choice_weight_raw]):
            continue
        if recipe_id not in valid_recipe_ids:
            errors.append('Recipe component row has an invalid recipe.')
            continue
        if source_menu_item_id and source_menu_item_id not in valid_source_ids:
            errors.append('Recipe component row has an invalid menu-item source.')
            continue
        quantity = parse_float_field(
            quantity_raw,
            'Recipe component quantity',
            errors,
            required=True,
            min_value=0.0001
        )
        if quantity is None:
            continue

        choice_weight = None
        if choice_weight_raw:
            choice_weight = parse_float_field(
                choice_weight_raw,
                'Choice weight percent',
                errors,
                required=False,
                min_value=0
            )
            if choice_weight is not None and choice_weight > 100:
                errors.append('Choice weight percent cannot exceed 100.')

        rows.append({
            'recipe_id': recipe_id,
            'source_menu_item_id': source_menu_item_id or None,
            'quantity': quantity,
            'unit': normalize_unit(unit) or unit,
            'choice_group': choice_group or None,
            'choice_weight_percent': choice_weight if choice_group else None
        })

    grouped_choice_rows = defaultdict(list)
    for row in rows:
        group = (row.get('choice_group') or '').strip()
        if group:
            grouped_choice_rows[group].append(row)

    for group_name, group_rows in grouped_choice_rows.items():
        if not group_rows:
            continue
        explicit = [row for row in group_rows if row.get('choice_weight_percent') is not None]
        if not explicit:
            equal = 100.0 / len(group_rows)
            for row in group_rows:
                row['choice_weight_percent'] = equal
            continue

        explicit_total = sum(to_float(row.get('choice_weight_percent')) for row in explicit)
        if explicit_total > 100.0001:
            errors.append(f'Choice group "{group_name}" exceeds 100%.')
            continue

        missing = [row for row in group_rows if row.get('choice_weight_percent') is None]
        if missing:
            remaining = max(0.0, 100.0 - explicit_total)
            each = remaining / len(missing)
            for row in missing:
                row['choice_weight_percent'] = each
        elif 0 < explicit_total < 100:
            scale = 100.0 / explicit_total
            for row in explicit:
                row['choice_weight_percent'] = to_float(row.get('choice_weight_percent')) * scale
    return rows, errors

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

def normalize_count_unit(unit):
    token = clean_menu_text(unit).lower().replace('.', '')
    if not token:
        return ''
    aliases = {
        'ea': 'each',
        'each': 'each',
        'serving': 'each',
        'servings': 'each',
        'plate': 'each',
        'plates': 'each',
        'person': 'each',
        'people': 'each',
        'guest': 'each',
        'guests': 'each',
        'dozen': 'dozen',
        'doz': 'dozen'
    }
    if token in aliases:
        return aliases[token]
    canonical = normalize_unit(token)
    if canonical in ('each', 'dozen'):
        return canonical
    return ''

def convert_count_units(quantity, from_unit, to_unit):
    qty = to_float(quantity)
    from_norm = normalize_count_unit(from_unit)
    to_norm = normalize_count_unit(to_unit)
    if qty <= 0:
        return 0
    if not from_norm or not to_norm:
        return None
    if from_norm == to_norm:
        return qty
    if from_norm == 'dozen' and to_norm == 'each':
        return qty * 12
    if from_norm == 'each' and to_norm == 'dozen':
        return qty / 12
    return None

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

def collect_banquet_additional_ingredients(cur, menu_item_ids):
    if not menu_item_ids:
        return {}
    cur.execute("""
        SELECT ai.menu_item_id,
               ai.ingredient_id,
               ai.quantity,
               ai.unit,
               i.name AS ingredient_name,
               i.unit AS ingredient_unit,
               i.cost_per_unit,
               i.category,
               i.vendor,
               i.vendor_code,
               i.g_code
        FROM banquet_menu_item_ingredients ai
        JOIN ingredients i ON i.id = ai.ingredient_id
        WHERE ai.menu_item_id = ANY(%s)
    """, (menu_item_ids,))
    grouped = defaultdict(list)
    for row in cur.fetchall():
        grouped[row['menu_item_id']].append(row)
    return grouped

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

def fetch_banquet_event_lines(cur, start_date, end_date, venue_id=''):
    if not banquet_tables_ready(cur):
        return []
    has_base_yield = db_column_exists(cur, 'public.banquet_menu_items', 'base_yield_qty') and db_column_exists(cur, 'public.banquet_menu_items', 'base_yield_unit')
    has_choice_selections_column = db_column_exists(cur, 'public.banquet_event_menu_items', 'choice_selections')
    base_yield_qty_sql = "mi.base_yield_qty" if has_base_yield else "1::numeric"
    base_yield_unit_sql = "mi.base_yield_unit" if has_base_yield else "'each'"
    choice_selections_sql = "emi.choice_selections AS line_choice_selections" if has_choice_selections_column else "NULL::text AS line_choice_selections"
    params = [start_date, end_date, list(BANQUET_ACTIVE_STATUSES)]
    venue_filter_sql = ""
    if venue_id:
        venue_filter_sql = " AND e.venue_id = %s"
        params.append(venue_id)

    cur.execute(f"""
        SELECT e.id AS event_id,
               e.name AS event_name,
               e.event_date,
               e.guest_count AS guests,
               e.venue_id,
               e.status,
               COALESCE(v.name, e.building, 'Banquet') AS venue_name,
               e.building,
               e.room,
               e.service_timing,
               e.dietary_notes,
               e.notes AS event_notes,
               emi.id AS line_id,
               emi.menu_item_id,
               COALESCE(NULLIF(TRIM(emi.menu_item_name), ''), mi.name) AS menu_item_name,
               COALESCE(emi.menu_descriptor, mi.menu_descriptor) AS menu_descriptor,
               COALESCE(emi.menu_section, mi.menu_section) AS menu_section,
               {base_yield_qty_sql} AS base_yield_qty,
               {base_yield_unit_sql} AS base_yield_unit,
               COALESCE(emi.recipe_id, primary_recipe.recipe_id) AS recipe_id,
               emi.quantity,
               emi.quantity_unit,
               emi.notes AS line_notes,
               {choice_selections_sql},
               r.name AS recipe_name,
               r.category AS recipe_category,
               r.yield_qty,
               r.yield_unit,
               r.menu_descriptor AS recipe_descriptor
        FROM banquet_events e
        LEFT JOIN venues v ON v.id = e.venue_id
        LEFT JOIN banquet_event_menu_items emi ON emi.event_id = e.id
        LEFT JOIN banquet_menu_items mi ON mi.id = emi.menu_item_id
        LEFT JOIN LATERAL (
            SELECT recipe_id
            FROM banquet_menu_item_recipes
            WHERE menu_item_id = mi.id
            ORDER BY id
            LIMIT 1
        ) primary_recipe ON TRUE
        LEFT JOIN recipes r ON r.id = COALESCE(emi.recipe_id, primary_recipe.recipe_id)
        WHERE e.event_date BETWEEN %s AND %s
          AND e.status = ANY(%s)
          {venue_filter_sql}
        ORDER BY e.event_date, e.name, COALESCE(emi.sort_order, 0), emi.id
    """, params)
    return cur.fetchall()

def build_banquet_datasets(cur, start_date, end_date, venue_id='', unit_system='imperial'):
    rows = fetch_banquet_event_lines(cur, start_date, end_date, venue_id)
    events_map = {}
    ingredient_totals = {}
    ingredient_usage = defaultdict(set)
    batch_usage_qty = {}
    batch_usage_events = defaultdict(set)
    batch_usage_menu_items = defaultdict(set)
    event_daily_ingredients = defaultdict(lambda: defaultdict(float))
    menu_item_direct_pull_totals = {}

    # Keep prep/shopping usable for older event lines that lost menu_item_id
    # by resolving from menu-item name within the same venue.
    unresolved_name_keys = defaultdict(list)
    for row in rows:
        effective_menu_item_id = row.get('menu_item_id')
        row['_effective_menu_item_id'] = effective_menu_item_id
        if effective_menu_item_id:
            continue
        menu_name_key = clean_menu_text(row.get('menu_item_name') or '').lower()
        if not menu_name_key:
            continue
        unresolved_name_keys[(row.get('venue_id') or '', menu_name_key)].append(row)

    if unresolved_name_keys:
        name_keys = sorted({name_key for _, name_key in unresolved_name_keys.keys()})
        cur.execute("""
            SELECT id, venue_id, LOWER(TRIM(name)) AS menu_name_key
            FROM banquet_menu_items
            WHERE LOWER(TRIM(name)) = ANY(%s)
        """, (name_keys,))
        candidates_by_name = defaultdict(list)
        for candidate in cur.fetchall():
            key = candidate.get('menu_name_key') or ''
            candidates_by_name[key].append(candidate)

        for (target_venue_id, menu_name_key), target_rows in unresolved_name_keys.items():
            candidates = candidates_by_name.get(menu_name_key, [])
            resolved = None
            if target_venue_id:
                resolved = next((item for item in candidates if item.get('venue_id') == target_venue_id), None)
            if not resolved:
                resolved = next((item for item in candidates if not item.get('venue_id')), None)
            if not resolved and candidates:
                resolved = candidates[0]
            if resolved:
                resolved_id = resolved.get('id')
                for row in target_rows:
                    row['_effective_menu_item_id'] = resolved_id

    menu_item_ids = list({row.get('_effective_menu_item_id') for row in rows if row.get('_effective_menu_item_id')})
    additional_ingredients = collect_banquet_additional_ingredients(cur, menu_item_ids)
    menu_item_recipe_components = collect_banquet_recipe_components(cur, menu_item_ids)

    for row in rows:
        event_id = row['event_id']
        if event_id not in events_map:
            events_map[event_id] = {
                'id': event_id,
                'name': row.get('event_name'),
                'event_date': row.get('event_date'),
                'guests': row.get('guests'),
                'venue_name': row.get('venue_name'),
                'building': row.get('building'),
                'room': row.get('room'),
                'service_timing': row.get('service_timing'),
                'dietary_notes': row.get('dietary_notes'),
                'notes': row.get('event_notes'),
                'status': row.get('status') or 'planning',
                'lines': [],
                'line_count': 0
            }

        if not row.get('line_id'):
            continue

        recipe = None
        if row.get('recipe_id'):
            recipe = {
                'id': row.get('recipe_id'),
                'name': row.get('recipe_name') or row.get('menu_item_name'),
                'yield_qty': row.get('yield_qty'),
                'yield_unit': row.get('yield_unit')
            }

        effective_menu_item_id = row.get('_effective_menu_item_id') or row.get('menu_item_id')

        line_recipes = []
        for component in menu_item_recipe_components.get(effective_menu_item_id, []):
            line_recipes.append({
                'id': component.get('recipe_id'),
                'name': component.get('recipe_name'),
                'yield_qty': component.get('yield_qty'),
                'yield_unit': component.get('yield_unit'),
                'quantity': to_float(component.get('quantity')),
                'unit': component.get('unit') or component.get('yield_unit'),
                'recipe_type': component.get('recipe_type'),
                'choice_group': component.get('choice_group'),
                'choice_weight_percent': to_float(component.get('choice_weight_percent'))
            })
        if not line_recipes and recipe:
            line_recipes = [dict(recipe)]

        line_choice_selections = parse_event_line_choice_selections(row.get('line_choice_selections'))
        line_choice_count_by_group_recipe = defaultdict(float)
        line_choice_count_by_recipe = defaultdict(float)
        for choice in line_choice_selections:
            recipe_id = clean_menu_text(choice.get('recipe_id'))
            if not recipe_id:
                continue
            count = max(0.0, to_float(choice.get('count')))
            if count <= 0:
                continue
            group_name = clean_menu_text(choice.get('choice_group') or '')
            line_choice_count_by_recipe[recipe_id] += count
            if group_name:
                line_choice_count_by_group_recipe[(group_name, recipe_id)] += count

        choice_group_rows = defaultdict(list)
        for line_recipe in line_recipes:
            group_name = clean_menu_text(line_recipe.get('choice_group') or '')
            if group_name:
                choice_group_rows[group_name].append(line_recipe)

        choice_recipe_occurrences = defaultdict(int)
        for group_rows in choice_group_rows.values():
            for item in group_rows:
                recipe_id = item.get('id')
                if recipe_id:
                    choice_recipe_occurrences[recipe_id] += 1

        choice_multipliers = {}
        for group_name, group_rows in choice_group_rows.items():
            selected_total = 0.0
            has_selected_counts = False
            for item in group_rows:
                recipe_id = item.get('id')
                explicit_count = line_choice_count_by_group_recipe.get((group_name, recipe_id))
                if explicit_count <= 0 and choice_recipe_occurrences.get(recipe_id, 0) == 1:
                    explicit_count = line_choice_count_by_recipe.get(recipe_id)
                if explicit_count > 0:
                    selected_total += explicit_count
                    has_selected_counts = True

            if has_selected_counts and selected_total > 0:
                for item in group_rows:
                    recipe_id = item.get('id')
                    selected_count = line_choice_count_by_group_recipe.get((group_name, recipe_id))
                    if selected_count <= 0 and choice_recipe_occurrences.get(recipe_id, 0) == 1:
                        selected_count = line_choice_count_by_recipe.get(recipe_id)
                    choice_multipliers[id(item)] = max(0.0, selected_count) / selected_total
                continue

            total_weight = sum(max(0.0, to_float(item.get('choice_weight_percent'))) for item in group_rows)
            if total_weight <= 0:
                equal_share = 1.0 / len(group_rows) if group_rows else 0.0
                for item in group_rows:
                    choice_multipliers[id(item)] = equal_share
            else:
                for item in group_rows:
                    choice_multipliers[id(item)] = max(0.0, to_float(item.get('choice_weight_percent'))) / total_weight

        line = {
            'id': row.get('line_id'),
            'menu_item_id': effective_menu_item_id,
            'menu_item_name': row.get('menu_item_name') or row.get('recipe_name') or 'Menu Item',
            'menu_descriptor': row.get('menu_descriptor') or row.get('recipe_descriptor'),
            'menu_section': row.get('menu_section') or 'Uncategorized',
            'quantity': to_float(row.get('quantity')),
            'quantity_unit': row.get('quantity_unit') or row.get('yield_unit') or 'each',
            'base_yield_qty': to_float(row.get('base_yield_qty')) or 1,
            'base_yield_unit': row.get('base_yield_unit') or 'each',
            'notes': row.get('line_notes'),
            'choice_selections': line_choice_selections,
            'recipe': line_recipes[0] if line_recipes else recipe,
            'recipes': line_recipes
        }
        line['ratio'] = None
        line['estimated_cost_total'] = None
        line['estimated_cost_per_unit'] = None
        line['components'] = []
        line['additional_ingredients'] = []
        line['additional_ingredient_cost'] = 0

        line_cost_total = 0
        line_multiplier = menu_line_base_multiplier(
            line.get('quantity'),
            line.get('quantity_unit'),
            line.get('base_yield_qty'),
            line.get('base_yield_unit')
        )
        line['ratio'] = line_multiplier

        for line_recipe in line_recipes:
            ratio_per_menu_unit = ratio_from_line_quantity(
                {
                    'quantity': line_recipe.get('quantity', line.get('base_yield_qty')),
                    'quantity_unit': line_recipe.get('unit', line_recipe.get('yield_unit'))
                },
                line_recipe
            )
            group_multiplier = choice_multipliers.get(id(line_recipe), 1.0)
            total_ratio = ratio_per_menu_unit * line_multiplier * group_multiplier
            line_cost_total += get_recipe_total_cost(cur, line_recipe['id'], unit_system, apply_q_factor=True) * total_ratio

            recipe_yield_qty = to_float(line_recipe.get('yield_qty'))
            required_output_qty = (recipe_yield_qty * total_ratio) if recipe_yield_qty > 0 else total_ratio
            required_output_unit = line_recipe.get('yield_unit') or line_recipe.get('unit') or ''
            root_key = (line_recipe.get('id'), required_output_unit)
            batch_usage_qty[root_key] = batch_usage_qty.get(root_key, 0) + required_output_qty
            if event_id:
                batch_usage_events[line_recipe.get('id')].add(event_id)
            if line.get('menu_item_name'):
                batch_usage_menu_items[line_recipe.get('id')].add(line['menu_item_name'])

            components, _, _ = build_component_tree(cur, line_recipe['id'], total_ratio, 0, set(), unit_system, apply_q_factor=False)
            if components:
                line['components'].extend(components)
            collect_ingredients_from_components(components, ingredient_totals)
            collect_ingredients_from_components(components, event_daily_ingredients[event_id])
            collect_batch_recipe_usage_from_components(
                components,
                batch_usage_qty,
                event_usage_map=batch_usage_events,
                menu_item_usage_map=batch_usage_menu_items,
                event_id=event_id,
                menu_item_name=line['menu_item_name']
            )

            line_label = line['menu_item_name']
            collect_ingredient_usage_from_components(components, line_label, ingredient_usage)

        for extra in additional_ingredients.get(effective_menu_item_id, []):
            ingredient_id = extra.get('ingredient_id')
            if not ingredient_id:
                continue
            per_unit_qty = to_float(extra.get('quantity'))
            extra_qty = per_unit_qty * line_multiplier
            unit = (extra.get('unit') or extra.get('ingredient_unit') or '').strip()
            key = (ingredient_id, unit)
            ingredient_totals[key] = ingredient_totals.get(key, 0) + extra_qty
            event_daily_ingredients[event_id][key] += extra_qty
            ingredient_usage[ingredient_id].add(line.get('menu_item_name') or 'Menu Item')
            converted_cost = convert_cost_per_unit(
                extra.get('cost_per_unit'),
                extra.get('ingredient_unit'),
                unit
            )
            ext_cost = extra_qty * converted_cost if converted_cost else 0
            line_cost_total += ext_cost
            line['additional_ingredient_cost'] += ext_cost
            line['additional_ingredients'].append({
                'ingredient_id': ingredient_id,
                'name': extra.get('ingredient_name'),
                'quantity': extra_qty,
                'unit': unit,
                'ext_cost': ext_cost
            })
            pull_group_key = (
                effective_menu_item_id or line.get('menu_item_name') or 'menu_item',
                line.get('menu_item_name') or 'Menu Item'
            )
            if pull_group_key not in menu_item_direct_pull_totals:
                menu_item_direct_pull_totals[pull_group_key] = {
                    'menu_item_name': line.get('menu_item_name') or 'Menu Item',
                    'ingredient_totals': {},
                    'event_ids': set()
                }
            pull_group = menu_item_direct_pull_totals[pull_group_key]
            pull_group['event_ids'].add(event_id)
            ingredient_key = (ingredient_id, unit, extra.get('ingredient_name') or 'Unknown')
            ingredient_row = pull_group['ingredient_totals'].get(ingredient_key)
            if not ingredient_row:
                ingredient_row = {
                    'ingredient_id': ingredient_id,
                    'name': extra.get('ingredient_name') or 'Unknown',
                    'quantity': 0,
                    'unit': unit
                }
                pull_group['ingredient_totals'][ingredient_key] = ingredient_row
            ingredient_row['quantity'] = to_float(ingredient_row.get('quantity')) + extra_qty

        line['estimated_cost_total'] = line_cost_total
        line['estimated_cost_per_unit'] = line_cost_total / line['quantity'] if line['quantity'] > 0 else None

        events_map[event_id]['lines'].append(line)
        events_map[event_id]['line_count'] += 1

    events = sorted(events_map.values(), key=lambda item: ((item.get('event_date') or date.today()), (item.get('name') or '').lower()))

    ingredient_master = []
    ingredient_total_cost = 0
    if ingredient_totals:
        ingredient_ids = list({ing_id for ing_id, _ in ingredient_totals.keys() if ing_id})
        ingredient_map = {}
        if ingredient_ids:
            cur.execute("""
                SELECT id, name, unit, category, cost_per_unit, vendor, vendor_code, g_code
                FROM ingredients
                WHERE id = ANY(%s)
            """, (ingredient_ids,))
            ingredient_map = {row['id']: row for row in cur.fetchall()}

        for (ing_id, unit), qty in ingredient_totals.items():
            ingredient = ingredient_map.get(ing_id, {})
            cost_per_unit = convert_cost_per_unit(
                ingredient.get('cost_per_unit'),
                ingredient.get('unit'),
                unit
            )
            ext_cost = qty * cost_per_unit if cost_per_unit else 0
            ingredient_total_cost += ext_cost
            display = smart_quantity(qty, unit, unit_system)
            ingredient_master.append({
                'id': ing_id,
                'name': ingredient.get('name') or 'Unknown',
                'category': ingredient.get('category') or 'Uncategorized',
                'vendor': ingredient.get('vendor'),
                'vendor_code': ingredient.get('vendor_code'),
                'g_code': ingredient.get('g_code'),
                'quantity': qty,
                'unit': unit,
                'display_quantity': display['quantity'],
                'display_unit': display['unit'],
                'cost_per_unit': cost_per_unit,
                'ext_cost': ext_cost,
                'used_in': sorted(ingredient_usage.get(ing_id, set()))
            })
    ingredient_master.sort(key=lambda item: ((item.get('vendor') or '').lower(), (item.get('category') or '').lower(), (item.get('name') or '').lower()))

    weekly_prep = []
    if batch_usage_qty:
        batch_ids = list({recipe_id for recipe_id, _ in batch_usage_qty.keys() if recipe_id})
        recipe_map = {}
        if batch_ids:
            cur.execute("""
                SELECT id, name, category, yield_qty, yield_unit, instructions, recipe_type
                FROM recipes
                WHERE id = ANY(%s)
            """, (batch_ids,))
            recipe_map = {row['id']: row for row in cur.fetchall()}

        for (recipe_id, qty_unit), required_qty in batch_usage_qty.items():
            recipe = recipe_map.get(recipe_id)
            if not recipe:
                continue
            yield_qty = to_float(recipe.get('yield_qty'))
            yield_unit = recipe.get('yield_unit')
            required_qty_in_yield = required_qty
            if qty_unit and yield_unit and qty_unit != yield_unit:
                converted = convert_quantity_between_units(required_qty, qty_unit, yield_unit)
                if converted is not None:
                    required_qty_in_yield = converted

            required_batches = (required_qty_in_yield / yield_qty) if yield_qty > 0 else required_qty_in_yield
            components, total_cost, _ = build_component_tree(cur, recipe_id, required_batches, 0, set(), unit_system, apply_q_factor=False)
            ingredient_totals_for_batch = {}
            collect_direct_ingredients_for_prep(components, ingredient_totals_for_batch)
            subrecipe_totals_for_batch = {}
            collect_direct_subrecipes_for_prep(components, subrecipe_totals_for_batch)
            ingredient_rows = []
            for (ing_id, unit), qty in ingredient_totals_for_batch.items():
                display = smart_quantity(qty, unit, unit_system)
                ingredient_rows.append({
                    'ingredient_id': ing_id,
                    'name': next((item['name'] for item in ingredient_master if item['id'] == ing_id), 'Unknown'),
                    'quantity': qty,
                    'unit': unit,
                    'display_quantity': display['quantity'],
                    'display_unit': display['unit']
                })
            ingredient_rows.sort(key=lambda item: (item.get('name') or '').lower())

            subrecipe_rows = []
            for (sub_recipe_id, sub_unit), sub_qty in subrecipe_totals_for_batch.items():
                sub_recipe = recipe_map.get(sub_recipe_id)
                if not sub_recipe:
                    sub_recipe = get_recipe_by_id(cur, sub_recipe_id)
                if not sub_recipe:
                    continue
                sub_yield_qty = to_float(sub_recipe.get('yield_qty'))
                sub_yield_unit = sub_recipe.get('yield_unit') or sub_unit
                sub_required_qty_in_yield = sub_qty
                if sub_unit and sub_yield_unit and sub_unit != sub_yield_unit:
                    converted = convert_quantity_between_units(sub_qty, sub_unit, sub_yield_unit)
                    if converted is not None:
                        sub_required_qty_in_yield = converted
                sub_required_batches = (sub_required_qty_in_yield / sub_yield_qty) if sub_yield_qty > 0 else sub_required_qty_in_yield
                display = smart_quantity(sub_qty, sub_unit, unit_system)
                subrecipe_rows.append({
                    'recipe_id': sub_recipe_id,
                    'recipe_name': sub_recipe.get('name') or 'Sub-recipe',
                    'required_qty': sub_qty,
                    'required_unit': sub_unit,
                    'display_required': display,
                    'required_batches': sub_required_batches,
                    'yield_qty': sub_yield_qty,
                    'yield_unit': sub_yield_unit
                })
            subrecipe_rows.sort(key=lambda item: (item.get('recipe_name') or '').lower())

            weekly_prep.append({
                'recipe_id': recipe_id,
                'recipe_name': recipe.get('name'),
                'category': recipe.get('category'),
                'recipe_type': normalize_recipe_type(recipe.get('recipe_type')),
                'required_batches': required_batches,
                'required_qty': required_qty_in_yield,
                'required_unit': yield_unit or qty_unit,
                'display_required': smart_quantity(required_qty_in_yield, yield_unit or qty_unit, unit_system),
                'yield_qty': yield_qty,
                'yield_unit': yield_unit,
                'ingredient_rows': ingredient_rows,
                'subrecipe_rows': subrecipe_rows,
                'instructions': recipe.get('instructions'),
                'used_in_events': sorted(batch_usage_events.get(recipe_id, set())),
                'used_in_menu_items': sorted(batch_usage_menu_items.get(recipe_id, set())),
                'estimated_cost': total_cost
            })
    for prep in weekly_prep:
        prep_family = derive_prep_family_label(prep.get('recipe_name'))
        prep['prep_family'] = prep_family
        prep['prep_family_key'] = normalize_match_key(prep_family)
    weekly_prep.sort(key=lambda item: (item.get('prep_family_key') or '', (item.get('recipe_name') or '').lower()))

    weekly_menu_pulls = []
    for group in menu_item_direct_pull_totals.values():
        ingredient_rows = []
        for row in group['ingredient_totals'].values():
            display = smart_quantity(row.get('quantity'), row.get('unit'), unit_system)
            ingredient_rows.append({
                'ingredient_id': row.get('ingredient_id'),
                'name': row.get('name'),
                'quantity': row.get('quantity'),
                'unit': row.get('unit'),
                'display_quantity': display.get('quantity'),
                'display_unit': display.get('unit')
            })
        ingredient_rows.sort(key=lambda item: (item.get('name') or '').lower())
        weekly_menu_pulls.append({
            'menu_item_name': group.get('menu_item_name') or 'Menu Item',
            'ingredient_rows': ingredient_rows,
            'used_in_events': sorted(group.get('event_ids') or set())
        })
    weekly_menu_pulls.sort(key=lambda item: (item.get('menu_item_name') or '').lower())

    daily_groups = []
    by_day = defaultdict(list)
    for event in events:
        by_day[event.get('event_date')].append(event)
    for day in sorted(by_day.keys()):
        day_events = by_day[day]
        for event in day_events:
            ingredient_rows = []
            for (ing_id, unit), qty in event_daily_ingredients.get(event['id'], {}).items():
                display = smart_quantity(qty, unit, unit_system)
                ingredient_rows.append({
                    'ingredient_id': ing_id,
                    'name': next((item['name'] for item in ingredient_master if item['id'] == ing_id), 'Unknown'),
                    'quantity': qty,
                    'unit': unit,
                    'display_quantity': display['quantity'],
                    'display_unit': display['unit']
                })
            ingredient_rows.sort(key=lambda item: (item.get('name') or '').lower())
            event['daily_ingredients'] = ingredient_rows
        daily_groups.append({'date': day, 'events': day_events})

    total_menu_lines = sum(event.get('line_count', 0) for event in events)
    total_estimated_cost = sum(sum(line.get('estimated_cost_total', 0) for line in event.get('lines', [])) for event in events)

    return {
        'events': events,
        'daily_groups': daily_groups,
        'shopping_ingredients': ingredient_master,
        'shopping_total_cost': ingredient_total_cost,
        'weekly_prep': weekly_prep,
        'weekly_menu_pulls': weekly_menu_pulls,
        'event_count': len(events),
        'menu_line_count': total_menu_lines,
        'total_estimated_cost': total_estimated_cost
    }

def fetch_commissary_order_rows(cur, start_date, end_date, outlet=''):
    if not commissary_tables_ready(cur):
        return []
    params = [start_date, end_date]
    outlet_filter_sql = ""
    if outlet:
        outlet_filter_sql = " AND o.outlet = %s"
        params.append(outlet)

    cur.execute(f"""
        SELECT o.id AS order_id,
               o.outlet,
               o.needed_date,
               o.status,
               o.notes AS order_notes,
               o.created_at,
               o.updated_at,
               oi.id AS line_id,
               oi.recipe_id,
               oi.item_name,
               oi.quantity,
               oi.quantity_unit,
               oi.notes AS line_notes,
               oi.sort_order,
               r.name AS recipe_name,
               r.category AS recipe_category,
               r.yield_qty,
               r.yield_unit,
               r.instructions,
               r.recipe_type
        FROM outlet_orders o
        LEFT JOIN outlet_order_items oi ON oi.order_id = o.id
        LEFT JOIN recipes r ON r.id = oi.recipe_id
        WHERE o.needed_date BETWEEN %s AND %s
          AND COALESCE(NULLIF(TRIM(o.status), ''), 'pending') <> 'cancelled'
          {outlet_filter_sql}
        ORDER BY o.needed_date, o.outlet, o.created_at, COALESCE(oi.sort_order, 0), oi.id
    """, params)
    return cur.fetchall()

def build_commissary_datasets(cur, start_date, end_date, outlet='', unit_system='imperial'):
    rows = fetch_commissary_order_rows(cur, start_date, end_date, outlet)
    orders_map = {}
    ingredient_totals = {}
    ingredient_usage = defaultdict(set)
    batch_usage_qty = {}
    batch_usage_orders = defaultdict(set)
    batch_usage_items = defaultdict(set)
    order_daily_ingredients = defaultdict(lambda: defaultdict(float))

    for row in rows:
        order_id = row.get('order_id')
        if not order_id:
            continue
        if order_id not in orders_map:
            orders_map[order_id] = {
                'id': order_id,
                'outlet': row.get('outlet') or DEFAULT_COMMISSARY_OUTLET,
                'needed_date': row.get('needed_date'),
                'status': (row.get('status') or 'pending'),
                'notes': row.get('order_notes'),
                'created_at': row.get('created_at'),
                'updated_at': row.get('updated_at'),
                'lines': [],
                'line_count': 0
            }

        if not row.get('line_id'):
            continue

        recipe_id = row.get('recipe_id')
        quantity = to_float(row.get('quantity'))
        quantity_unit = row.get('quantity_unit') or row.get('yield_unit') or 'each'
        item_name = clean_menu_text(row.get('item_name')) or row.get('recipe_name') or 'Item'

        line = {
            'id': row.get('line_id'),
            'recipe_id': recipe_id,
            'item_name': item_name,
            'recipe_name': row.get('recipe_name'),
            'recipe_type': normalize_recipe_type(row.get('recipe_type')),
            'quantity': quantity,
            'quantity_unit': quantity_unit,
            'notes': row.get('line_notes'),
            'estimated_cost_total': None,
            'estimated_cost_per_unit': None,
            'ratio': None,
            'components': []
        }

        if recipe_id:
            recipe = {
                'id': recipe_id,
                'name': row.get('recipe_name') or item_name,
                'yield_qty': row.get('yield_qty'),
                'yield_unit': row.get('yield_unit')
            }
            ratio = ratio_from_line_quantity(
                {
                    'quantity': quantity,
                    'quantity_unit': quantity_unit
                },
                recipe
            )
            line['ratio'] = ratio
            line_cost_total = get_recipe_total_cost(cur, recipe_id, unit_system, apply_q_factor=True) * ratio
            line['estimated_cost_total'] = line_cost_total
            line['estimated_cost_per_unit'] = line_cost_total / quantity if quantity > 0 else None

            recipe_yield_qty = to_float(recipe.get('yield_qty'))
            required_output_qty = (recipe_yield_qty * ratio) if recipe_yield_qty > 0 else ratio
            required_output_unit = recipe.get('yield_unit') or quantity_unit
            root_key = (recipe_id, required_output_unit)
            batch_usage_qty[root_key] = batch_usage_qty.get(root_key, 0) + required_output_qty
            batch_usage_orders[recipe_id].add(order_id)
            batch_usage_items[recipe_id].add(item_name)

            components, _, _ = build_component_tree(cur, recipe_id, ratio, 0, set(), unit_system, apply_q_factor=False)
            line['components'] = components
            collect_ingredients_from_components(components, ingredient_totals)
            collect_ingredients_from_components(components, order_daily_ingredients[order_id])
            collect_batch_recipe_usage_from_components(
                components,
                batch_usage_qty,
                event_usage_map=batch_usage_orders,
                menu_item_usage_map=batch_usage_items,
                event_id=order_id,
                menu_item_name=item_name
            )
            collect_ingredient_usage_from_components(components, item_name, ingredient_usage)

        orders_map[order_id]['lines'].append(line)
        orders_map[order_id]['line_count'] += 1

    orders = sorted(
        orders_map.values(),
        key=lambda item: (
            (item.get('needed_date') or date.today()),
            (item.get('outlet') or '').lower(),
            (item.get('id') or '')
        )
    )
    order_label_map = {
        order.get('id'): f"{order.get('outlet') or DEFAULT_COMMISSARY_OUTLET} ({order.get('needed_date')})"
        for order in orders
    }

    ingredient_master = []
    ingredient_total_cost = 0
    if ingredient_totals:
        ingredient_ids = list({ing_id for ing_id, _ in ingredient_totals.keys() if ing_id})
        ingredient_map = {}
        if ingredient_ids:
            cur.execute("""
                SELECT id, name, unit, category, cost_per_unit, vendor, vendor_code, g_code
                FROM ingredients
                WHERE id = ANY(%s)
            """, (ingredient_ids,))
            ingredient_map = {row['id']: row for row in cur.fetchall()}

        for (ing_id, unit), qty in ingredient_totals.items():
            ingredient = ingredient_map.get(ing_id, {})
            cost_per_unit = convert_cost_per_unit(
                ingredient.get('cost_per_unit'),
                ingredient.get('unit'),
                unit
            )
            ext_cost = qty * cost_per_unit if cost_per_unit else 0
            ingredient_total_cost += ext_cost
            display = smart_quantity(qty, unit, unit_system)
            ingredient_master.append({
                'id': ing_id,
                'name': ingredient.get('name') or 'Unknown',
                'category': ingredient.get('category') or 'Uncategorized',
                'vendor': ingredient.get('vendor'),
                'vendor_code': ingredient.get('vendor_code'),
                'g_code': ingredient.get('g_code'),
                'quantity': qty,
                'unit': unit,
                'display_quantity': display['quantity'],
                'display_unit': display['unit'],
                'cost_per_unit': cost_per_unit,
                'ext_cost': ext_cost,
                'used_in': sorted(ingredient_usage.get(ing_id, set()))
            })
    ingredient_master.sort(key=lambda item: ((item.get('vendor') or '').lower(), (item.get('category') or '').lower(), (item.get('name') or '').lower()))

    weekly_prep = []
    if batch_usage_qty:
        batch_ids = list({recipe_id for recipe_id, _ in batch_usage_qty.keys() if recipe_id})
        recipe_map = {}
        if batch_ids:
            cur.execute("""
                SELECT id, name, category, yield_qty, yield_unit, instructions, recipe_type
                FROM recipes
                WHERE id = ANY(%s)
            """, (batch_ids,))
            recipe_map = {row['id']: row for row in cur.fetchall()}

        for (recipe_id, qty_unit), required_qty in batch_usage_qty.items():
            recipe = recipe_map.get(recipe_id)
            if not recipe:
                continue
            yield_qty = to_float(recipe.get('yield_qty'))
            yield_unit = recipe.get('yield_unit')
            required_qty_in_yield = required_qty
            if qty_unit and yield_unit and qty_unit != yield_unit:
                converted = convert_quantity_between_units(required_qty, qty_unit, yield_unit)
                if converted is not None:
                    required_qty_in_yield = converted

            required_batches = (required_qty_in_yield / yield_qty) if yield_qty > 0 else required_qty_in_yield
            components, total_cost, _ = build_component_tree(cur, recipe_id, required_batches, 0, set(), unit_system, apply_q_factor=False)
            ingredient_totals_for_batch = {}
            collect_direct_ingredients_for_prep(components, ingredient_totals_for_batch)
            subrecipe_totals_for_batch = {}
            collect_direct_subrecipes_for_prep(components, subrecipe_totals_for_batch)
            ingredient_rows = []
            for (ing_id, unit), qty in ingredient_totals_for_batch.items():
                display = smart_quantity(qty, unit, unit_system)
                ingredient_rows.append({
                    'ingredient_id': ing_id,
                    'name': next((item['name'] for item in ingredient_master if item['id'] == ing_id), 'Unknown'),
                    'quantity': qty,
                    'unit': unit,
                    'display_quantity': display['quantity'],
                    'display_unit': display['unit']
                })
            ingredient_rows.sort(key=lambda item: (item.get('name') or '').lower())

            subrecipe_rows = []
            for (sub_recipe_id, sub_unit), sub_qty in subrecipe_totals_for_batch.items():
                sub_recipe = recipe_map.get(sub_recipe_id)
                if not sub_recipe:
                    sub_recipe = get_recipe_by_id(cur, sub_recipe_id)
                if not sub_recipe:
                    continue
                sub_yield_qty = to_float(sub_recipe.get('yield_qty'))
                sub_yield_unit = sub_recipe.get('yield_unit') or sub_unit
                sub_required_qty_in_yield = sub_qty
                if sub_unit and sub_yield_unit and sub_unit != sub_yield_unit:
                    converted = convert_quantity_between_units(sub_qty, sub_unit, sub_yield_unit)
                    if converted is not None:
                        sub_required_qty_in_yield = converted
                sub_required_batches = (sub_required_qty_in_yield / sub_yield_qty) if sub_yield_qty > 0 else sub_required_qty_in_yield
                display = smart_quantity(sub_qty, sub_unit, unit_system)
                subrecipe_rows.append({
                    'recipe_id': sub_recipe_id,
                    'recipe_name': sub_recipe.get('name') or 'Sub-recipe',
                    'required_qty': sub_qty,
                    'required_unit': sub_unit,
                    'display_required': display,
                    'required_batches': sub_required_batches,
                    'yield_qty': sub_yield_qty,
                    'yield_unit': sub_yield_unit
                })
            subrecipe_rows.sort(key=lambda item: (item.get('recipe_name') or '').lower())

            weekly_prep.append({
                'recipe_id': recipe_id,
                'recipe_name': recipe.get('name'),
                'category': recipe.get('category'),
                'recipe_type': normalize_recipe_type(recipe.get('recipe_type')),
                'required_batches': required_batches,
                'required_qty': required_qty_in_yield,
                'required_unit': yield_unit or qty_unit,
                'display_required': smart_quantity(required_qty_in_yield, yield_unit or qty_unit, unit_system),
                'yield_qty': yield_qty,
                'yield_unit': yield_unit,
                'ingredient_rows': ingredient_rows,
                'subrecipe_rows': subrecipe_rows,
                'instructions': recipe.get('instructions'),
                'used_in_orders': sorted(batch_usage_orders.get(recipe_id, set())),
                'used_in_order_labels': [order_label_map.get(order_id, order_id) for order_id in sorted(batch_usage_orders.get(recipe_id, set()))],
                'used_in_items': sorted(batch_usage_items.get(recipe_id, set())),
                'estimated_cost': total_cost
            })
    for prep in weekly_prep:
        prep_family = derive_prep_family_label(prep.get('recipe_name'))
        prep['prep_family'] = prep_family
        prep['prep_family_key'] = normalize_match_key(prep_family)
    weekly_prep.sort(key=lambda item: (item.get('prep_family_key') or '', (item.get('recipe_name') or '').lower()))

    daily_groups = []
    by_day = defaultdict(list)
    for order in orders:
        by_day[order.get('needed_date')].append(order)
    for day in sorted(by_day.keys()):
        day_orders = by_day[day]
        for order in day_orders:
            ingredient_rows = []
            for (ing_id, unit), qty in order_daily_ingredients.get(order['id'], {}).items():
                display = smart_quantity(qty, unit, unit_system)
                ingredient_rows.append({
                    'ingredient_id': ing_id,
                    'name': next((item['name'] for item in ingredient_master if item['id'] == ing_id), 'Unknown'),
                    'quantity': qty,
                    'unit': unit,
                    'display_quantity': display['quantity'],
                    'display_unit': display['unit']
                })
            ingredient_rows.sort(key=lambda item: (item.get('name') or '').lower())
            order['daily_ingredients'] = ingredient_rows
        daily_groups.append({'date': day, 'orders': day_orders})

    total_lines = sum(order.get('line_count', 0) for order in orders)
    total_estimated_cost = sum(
        sum(line.get('estimated_cost_total', 0) or 0 for line in order.get('lines', []))
        for order in orders
    )

    return {
        'orders': orders,
        'daily_groups': daily_groups,
        'shopping_ingredients': ingredient_master,
        'shopping_total_cost': ingredient_total_cost,
        'weekly_prep': weekly_prep,
        'order_count': len(orders),
        'line_count': total_lines,
        'total_estimated_cost': total_estimated_cost
    }

def build_commissary_prep_groups(cur, datasets, unit_system='imperial'):
    weekly_prep = datasets.get('weekly_prep') or []
    if not weekly_prep:
        return []

    prep_by_recipe = {}
    for prep in weekly_prep:
        recipe_id = prep.get('recipe_id')
        if recipe_id and recipe_id not in prep_by_recipe:
            prep_by_recipe[recipe_id] = prep

    root_recipe_sequence = []
    root_recipe_labels = defaultdict(list)
    for order in datasets.get('orders', []):
        for line in order.get('lines', []):
            recipe_id = line.get('recipe_id')
            if not recipe_id:
                continue
            if recipe_id not in root_recipe_sequence:
                root_recipe_sequence.append(recipe_id)
            line_label = clean_menu_text(line.get('item_name') or line.get('recipe_name') or '')
            if line_label and line_label not in root_recipe_labels[recipe_id]:
                root_recipe_labels[recipe_id].append(line_label)

    if not root_recipe_sequence:
        root_recipe_sequence = [prep.get('recipe_id') for prep in weekly_prep if prep.get('recipe_id')]
    stocked_root_recipe_ids = {recipe_id for recipe_id in root_recipe_sequence if recipe_id}
    original_root_order = {recipe_id: idx for idx, recipe_id in enumerate(root_recipe_sequence) if recipe_id}

    # Ensure stocked dependency cards appear before recipes that consume them.
    dependency_map = {recipe_id: set() for recipe_id in stocked_root_recipe_ids}
    for recipe_id in stocked_root_recipe_ids:
        prep_row = prep_by_recipe.get(recipe_id) or {}
        for sub_row in prep_row.get('subrecipe_rows', []) or []:
            child_recipe_id = sub_row.get('recipe_id')
            if child_recipe_id in stocked_root_recipe_ids and child_recipe_id != recipe_id:
                dependency_map[recipe_id].add(child_recipe_id)

    ordered_roots = []
    visited = set()
    visiting = set()

    def visit(recipe_id):
        if recipe_id in visited:
            return
        if recipe_id in visiting:
            # Cycle fallback: keep original order behavior without recursion blowup.
            return
        visiting.add(recipe_id)
        deps = sorted(
            dependency_map.get(recipe_id, set()),
            key=lambda dep_id: original_root_order.get(dep_id, 10**9)
        )
        for dep_id in deps:
            visit(dep_id)
        visiting.remove(recipe_id)
        visited.add(recipe_id)
        ordered_roots.append(recipe_id)

    for recipe_id in root_recipe_sequence:
        if recipe_id:
            visit(recipe_id)
    root_recipe_sequence = ordered_roots

    root_recipe_name_map = {}
    for recipe_id in stocked_root_recipe_ids:
        prep_row = prep_by_recipe.get(recipe_id) or {}
        root_recipe_name_map[recipe_id] = prep_row.get('recipe_name') or 'Main prep card'

    ingredient_name_map = {
        row.get('id'): row.get('name')
        for row in datasets.get('shopping_ingredients', [])
        if row.get('id')
    }
    recipe_cache = {}

    def get_recipe_cached(recipe_id):
        if not recipe_id:
            return None
        if recipe_id not in recipe_cache:
            recipe_cache[recipe_id] = get_recipe_by_id(cur, recipe_id)
        return recipe_cache.get(recipe_id)

    def build_sub_card(recipe_id, required_qty, required_unit, required_batches, ancestry, group_root_recipe_id):
        if not recipe_id:
            return None
        recipe = get_recipe_cached(recipe_id)
        if not recipe:
            return None

        batches = to_float(required_batches)
        if batches <= 0:
            yield_qty = to_float(recipe.get('yield_qty'))
            if yield_qty > 0 and to_float(required_qty) > 0:
                required_qty_in_yield = to_float(required_qty)
                yield_unit = recipe.get('yield_unit') or required_unit
                if required_unit and yield_unit and required_unit != yield_unit:
                    converted = convert_quantity_between_units(required_qty_in_yield, required_unit, yield_unit)
                    if converted is not None:
                        required_qty_in_yield = converted
                batches = required_qty_in_yield / yield_qty
            else:
                batches = to_float(required_qty)
        if batches <= 0:
            return None

        components, _, _ = build_component_tree(
            cur,
            recipe_id,
            batches,
            0,
            set(),
            unit_system,
            apply_q_factor=False
        )

        ingredient_totals = {}
        collect_direct_ingredients_for_prep(components, ingredient_totals)
        ingredient_rows = []
        for (ing_id, unit), qty in ingredient_totals.items():
            display = smart_quantity(qty, unit, unit_system)
            ingredient_rows.append({
                'ingredient_id': ing_id,
                'name': ingredient_name_map.get(ing_id, 'Unknown'),
                'quantity': qty,
                'unit': unit,
                'display_quantity': display.get('quantity'),
                'display_unit': display.get('unit')
            })
        ingredient_rows.sort(key=lambda item: (item.get('name') or '').lower())

        subrecipe_totals = {}
        collect_direct_subrecipes_for_prep(components, subrecipe_totals)
        subrecipe_rows = []
        for (child_recipe_id, child_unit), child_qty in subrecipe_totals.items():
            child_recipe = get_recipe_cached(child_recipe_id)
            if not child_recipe:
                continue
            child_yield_qty = to_float(child_recipe.get('yield_qty'))
            child_yield_unit = child_recipe.get('yield_unit') or child_unit
            child_required_qty_in_yield = child_qty
            if child_unit and child_yield_unit and child_unit != child_yield_unit:
                converted = convert_quantity_between_units(child_qty, child_unit, child_yield_unit)
                if converted is not None:
                    child_required_qty_in_yield = converted
            child_required_batches = (child_required_qty_in_yield / child_yield_qty) if child_yield_qty > 0 else child_required_qty_in_yield
            covered_by_stock_prep = child_recipe_id in stocked_root_recipe_ids and child_recipe_id != group_root_recipe_id
            subrecipe_rows.append({
                'recipe_id': child_recipe_id,
                'recipe_name': child_recipe.get('name') or 'Sub-recipe',
                'required_qty': child_qty,
                'required_unit': child_unit,
                'display_required': smart_quantity(child_qty, child_unit, unit_system),
                'required_batches': child_required_batches,
                'covered_by_stock_prep': covered_by_stock_prep,
                'stock_prep_recipe_name': root_recipe_name_map.get(child_recipe_id) if covered_by_stock_prep else None
            })
        subrecipe_rows.sort(key=lambda item: (item.get('recipe_name') or '').lower())

        child_cards = []
        if len(ancestry) < 3:
            for sub_row in subrecipe_rows:
                child_recipe_id = sub_row.get('recipe_id')
                if not child_recipe_id or child_recipe_id in ancestry:
                    continue
                if sub_row.get('covered_by_stock_prep'):
                    continue
                child_card = build_sub_card(
                    child_recipe_id,
                    sub_row.get('required_qty'),
                    sub_row.get('required_unit'),
                    sub_row.get('required_batches'),
                    ancestry | {child_recipe_id},
                    group_root_recipe_id
                )
                if child_card:
                    child_cards.append(child_card)

        display_required = smart_quantity(required_qty, required_unit, unit_system)
        if (display_required.get('quantity') in ('', '0')) and to_float(required_qty) <= 0:
            yield_qty = to_float(recipe.get('yield_qty'))
            display_required = smart_quantity(yield_qty * batches if yield_qty > 0 else batches, recipe.get('yield_unit') or required_unit, unit_system)

        return {
            'recipe_id': recipe_id,
            'recipe_name': recipe.get('name') or 'Sub-recipe',
            'display_required': display_required,
            'required_batches': batches,
            'ingredient_rows': ingredient_rows,
            'subrecipe_rows': subrecipe_rows,
            'instruction_steps': split_instruction_steps(recipe.get('instructions')),
            'child_cards': child_cards
        }

    groups = []
    for root_recipe_id in root_recipe_sequence:
        root_prep = prep_by_recipe.get(root_recipe_id)
        if not root_prep:
            continue

        main_labels = (
            root_recipe_labels.get(root_recipe_id)
            or root_prep.get('used_in_items')
            or [root_prep.get('recipe_name')]
        )
        main_labels = [label for label in main_labels if label]
        main_label = main_labels[0] if main_labels else (root_prep.get('recipe_name') or 'Prep Item')

        sub_cards = []
        root_subrecipe_rows = []
        for sub_row in root_prep.get('subrecipe_rows', []):
            sub_recipe_id = sub_row.get('recipe_id')
            required_batches = to_float(sub_row.get('required_batches'))
            if not sub_recipe_id or required_batches <= 0:
                continue

            covered_by_stock_prep = sub_recipe_id in stocked_root_recipe_ids and sub_recipe_id != root_recipe_id
            enriched_row = dict(sub_row)
            enriched_row['covered_by_stock_prep'] = covered_by_stock_prep
            enriched_row['stock_prep_recipe_name'] = root_recipe_name_map.get(sub_recipe_id) if covered_by_stock_prep else None
            root_subrecipe_rows.append(enriched_row)
            if covered_by_stock_prep:
                continue

            sub_card = build_sub_card(
                sub_recipe_id,
                sub_row.get('required_qty'),
                sub_row.get('required_unit'),
                required_batches,
                {root_recipe_id, sub_recipe_id},
                root_recipe_id
            )
            if not sub_card:
                continue
            if sub_row.get('display_required'):
                sub_card['display_required'] = sub_row.get('display_required')
            sub_cards.append(sub_card)
        root_prep['subrecipe_rows'] = root_subrecipe_rows

        groups.append({
            'main_label': main_label,
            'main_labels': main_labels,
            'root': root_prep,
            'sub_cards': sub_cards
        })

    return groups

def build_banquet_prep_groups(cur, datasets, unit_system='imperial'):
    weekly_prep = datasets.get('weekly_prep') or []
    if not weekly_prep:
        return []

    prep_by_recipe = {}
    for prep in weekly_prep:
        recipe_id = prep.get('recipe_id')
        if recipe_id and recipe_id not in prep_by_recipe:
            prep_by_recipe[recipe_id] = dict(prep)

    root_recipe_sequence = []
    root_recipe_labels = defaultdict(list)
    for event in datasets.get('events', []):
        for line in event.get('lines', []):
            for recipe in line.get('recipes', []) or []:
                recipe_id = recipe.get('id')
                if not recipe_id:
                    continue
                if recipe_id not in root_recipe_sequence:
                    root_recipe_sequence.append(recipe_id)
                line_label = clean_menu_text(line.get('menu_item_name') or recipe.get('name') or '')
                if line_label and line_label not in root_recipe_labels[recipe_id]:
                    root_recipe_labels[recipe_id].append(line_label)

    if not root_recipe_sequence:
        root_recipe_sequence = [prep.get('recipe_id') for prep in weekly_prep if prep.get('recipe_id')]

    stocked_root_recipe_ids = {recipe_id for recipe_id in root_recipe_sequence if recipe_id}
    root_recipe_name_map = {}
    for recipe_id in stocked_root_recipe_ids:
        prep_row = prep_by_recipe.get(recipe_id) or {}
        root_recipe_name_map[recipe_id] = prep_row.get('recipe_name') or 'Main prep card'

    ingredient_name_map = {
        row.get('id'): row.get('name')
        for row in datasets.get('shopping_ingredients', [])
        if row.get('id')
    }
    recipe_cache = {}

    def get_recipe_cached(recipe_id):
        if not recipe_id:
            return None
        if recipe_id not in recipe_cache:
            recipe_cache[recipe_id] = get_recipe_by_id(cur, recipe_id)
        return recipe_cache.get(recipe_id)

    def build_sub_card(recipe_id, required_qty, required_unit, required_batches, ancestry, group_root_recipe_id):
        if not recipe_id:
            return None
        recipe = get_recipe_cached(recipe_id)
        if not recipe:
            return None

        batches = to_float(required_batches)
        if batches <= 0:
            yield_qty = to_float(recipe.get('yield_qty'))
            if yield_qty > 0 and to_float(required_qty) > 0:
                required_qty_in_yield = to_float(required_qty)
                yield_unit = recipe.get('yield_unit') or required_unit
                if required_unit and yield_unit and required_unit != yield_unit:
                    converted = convert_quantity_between_units(required_qty_in_yield, required_unit, yield_unit)
                    if converted is not None:
                        required_qty_in_yield = converted
                batches = required_qty_in_yield / yield_qty
            else:
                batches = to_float(required_qty)
        if batches <= 0:
            return None

        components, _, _ = build_component_tree(
            cur,
            recipe_id,
            batches,
            0,
            set(),
            unit_system,
            apply_q_factor=False
        )

        ingredient_totals = {}
        collect_direct_ingredients_for_prep(components, ingredient_totals)
        ingredient_rows = []
        for (ing_id, unit), qty in ingredient_totals.items():
            display = smart_quantity(qty, unit, unit_system)
            ingredient_rows.append({
                'ingredient_id': ing_id,
                'name': ingredient_name_map.get(ing_id, 'Unknown'),
                'quantity': qty,
                'unit': unit,
                'display_quantity': display.get('quantity'),
                'display_unit': display.get('unit')
            })
        ingredient_rows.sort(key=lambda item: (item.get('name') or '').lower())

        subrecipe_totals = {}
        collect_direct_subrecipes_for_prep(components, subrecipe_totals)
        subrecipe_rows = []
        for (child_recipe_id, child_unit), child_qty in subrecipe_totals.items():
            child_recipe = get_recipe_cached(child_recipe_id)
            if not child_recipe:
                continue
            child_yield_qty = to_float(child_recipe.get('yield_qty'))
            child_yield_unit = child_recipe.get('yield_unit') or child_unit
            child_required_qty_in_yield = child_qty
            if child_unit and child_yield_unit and child_unit != child_yield_unit:
                converted = convert_quantity_between_units(child_qty, child_unit, child_yield_unit)
                if converted is not None:
                    child_required_qty_in_yield = converted
            child_required_batches = (child_required_qty_in_yield / child_yield_qty) if child_yield_qty > 0 else child_required_qty_in_yield
            covered_by_parent_recipe = child_recipe_id in printed_recipe_owner and printed_recipe_owner.get(child_recipe_id) != group_root_recipe_id
            subrecipe_rows.append({
                'recipe_id': child_recipe_id,
                'recipe_name': child_recipe.get('name') or 'Sub-recipe',
                'required_qty': child_qty,
                'required_unit': child_unit,
                'display_required': smart_quantity(child_qty, child_unit, unit_system),
                'required_batches': child_required_batches,
                'covered_by_parent_recipe': covered_by_parent_recipe,
                'covered_by_recipe_name': root_recipe_name_map.get(printed_recipe_owner.get(child_recipe_id)) if covered_by_parent_recipe else None
            })
        subrecipe_rows.sort(key=lambda item: (item.get('recipe_name') or '').lower())

        child_cards = []
        if len(ancestry) < 4:
            for sub_row in subrecipe_rows:
                child_recipe_id = sub_row.get('recipe_id')
                if not child_recipe_id or child_recipe_id in ancestry:
                    continue
                if sub_row.get('covered_by_parent_recipe'):
                    continue
                child_card = build_sub_card(
                    child_recipe_id,
                    sub_row.get('required_qty'),
                    sub_row.get('required_unit'),
                    sub_row.get('required_batches'),
                    ancestry | {child_recipe_id},
                    group_root_recipe_id
                )
                if child_card:
                    child_cards.append(child_card)

        display_required = smart_quantity(required_qty, required_unit, unit_system)
        if (display_required.get('quantity') in ('', '0')) and to_float(required_qty) <= 0:
            yield_qty = to_float(recipe.get('yield_qty'))
            display_required = smart_quantity(yield_qty * batches if yield_qty > 0 else batches, recipe.get('yield_unit') or required_unit, unit_system)

        return {
            'recipe_id': recipe_id,
            'recipe_name': recipe.get('name') or 'Sub-recipe',
            'display_required': display_required,
            'required_batches': batches,
            'ingredient_rows': ingredient_rows,
            'subrecipe_rows': subrecipe_rows,
            'instruction_steps': split_instruction_steps(recipe.get('instructions')),
            'child_cards': child_cards
        }

    def mark_tree_owner(card, owner_recipe_id):
        recipe_id = card.get('recipe_id')
        if recipe_id and recipe_id not in printed_recipe_owner:
            printed_recipe_owner[recipe_id] = owner_recipe_id
        for child in card.get('child_cards') or []:
            mark_tree_owner(child, owner_recipe_id)

    printed_recipe_owner = {}
    groups = []
    for root_recipe_id in root_recipe_sequence:
        root_prep = prep_by_recipe.get(root_recipe_id)
        if not root_prep:
            continue
        if root_recipe_id in printed_recipe_owner:
            continue

        main_labels = (
            root_recipe_labels.get(root_recipe_id)
            or root_prep.get('used_in_menu_items')
            or [root_prep.get('recipe_name')]
        )
        main_labels = [label for label in main_labels if label]
        main_label = main_labels[0] if main_labels else (root_prep.get('recipe_name') or 'Prep Item')

        root_prep['instruction_steps'] = split_instruction_steps(root_prep.get('instructions'))
        root_subrecipe_rows = []
        sub_cards = []

        for sub_row in root_prep.get('subrecipe_rows', []) or []:
            sub_recipe_id = sub_row.get('recipe_id')
            required_batches = to_float(sub_row.get('required_batches'))
            if not sub_recipe_id or required_batches <= 0:
                continue

            covered_by_parent_recipe = sub_recipe_id in printed_recipe_owner and printed_recipe_owner.get(sub_recipe_id) != root_recipe_id
            enriched_row = dict(sub_row)
            enriched_row['covered_by_parent_recipe'] = covered_by_parent_recipe
            enriched_row['covered_by_recipe_name'] = root_recipe_name_map.get(printed_recipe_owner.get(sub_recipe_id)) if covered_by_parent_recipe else None
            root_subrecipe_rows.append(enriched_row)
            if covered_by_parent_recipe:
                continue

            sub_card = build_sub_card(
                sub_recipe_id,
                sub_row.get('required_qty'),
                sub_row.get('required_unit'),
                required_batches,
                {root_recipe_id, sub_recipe_id},
                root_recipe_id
            )
            if not sub_card:
                continue
            if sub_row.get('display_required'):
                sub_card['display_required'] = sub_row.get('display_required')
            sub_cards.append(sub_card)

        root_prep['subrecipe_rows'] = root_subrecipe_rows
        printed_recipe_owner[root_recipe_id] = root_recipe_id
        for card in sub_cards:
            mark_tree_owner(card, root_recipe_id)

        groups.append({
            'main_label': main_label,
            'main_labels': main_labels,
            'root': root_prep,
            'sub_cards': sub_cards
        })

    return groups

def parse_catering_pdf_to_template_items(file_stream):
    reader = PdfReader(file_stream)
    parsed = []
    current_major_section = 'Imported Menu'
    current_subsection = 'Items'
    current_package = ''
    current_item = None

    ignored_prefixes = (
        'Pricing is subject',
        'Receptions Serving',
        'Buffets Serving',
        'Listed prices are per person',
        'All are priced',
        'Price listed',
        'Place cards',
        'ALL DINNERS INCLUDE',
        'Consumption bar'
    )
    ignored_exact = {
        'Catering Menu',
        'THE START OF SOMETHING DELICIOUS'
    }

    def flush_current():
        nonlocal current_item
        if not current_item:
            return
        current_item['item_name'] = clean_menu_text(current_item.get('item_name'))
        current_item['menu_descriptor'] = clean_menu_text(current_item.get('menu_descriptor'))
        if current_item['item_name']:
            parsed.append(current_item)
        current_item = None

    for page in reader.pages:
        text = page.extract_text() or ''
        for raw_line in text.splitlines():
            line = clean_menu_text(raw_line)
            if not line:
                continue
            if line in ignored_exact or line.startswith(ignored_prefixes):
                continue

            upper = line.upper()
            if upper in (
                'BREAKFAST ENHANCEMENTS', 'REFRESH AND RENEW: BREAKOUT PACKAGES',
                'SNACK ATTACK', 'MORNING BEVERAGES', 'STATIONS - DAYTIME',
                'RECEPTIONS DISPLAYS', 'RECEPTIONS STATIONS', 'SMALL BITES',
                "HORS D'OEUVRES", 'BUFFET PACKAGES', 'CUSTOMIZE YOUR DINNER BUFFET',
                'PLATED 3-COURSE DINNER', 'LATE NIGHT SNACKS',
                'FOXTOWN PREMIUM BEVERAGE PACKAGE', 'FOXTOWN STANDARD BEVERAGE PACKAGE',
                'BEER, WINE & SODA PACKAGES', 'PACKAGE ENHANCEMENTS'
            ):
                flush_current()
                current_major_section = line.title()
                current_subsection = 'Items'
                current_package = ''
                continue

            if upper in BANQUET_PACKAGE_SUBSECTIONS:
                flush_current()
                current_subsection = line.title()
                continue

            if upper.startswith('CHOICE OF ') or upper.startswith('CHOOSE ') or upper.endswith(' OPTIONS'):
                continue

            package_match = re.match(r'^([A-Z0-9 &\'’\-\(\)\.]+?)\s+\$\s*([0-9]+(?:\.[0-9]+)?|Market Price)$', line)
            if package_match and len(package_match.group(1).split()) >= 2:
                flush_current()
                current_package = clean_menu_text(package_match.group(1).title())
                current_subsection = 'Items'
                continue

            item_with_price = re.match(r'^(.*?)\s+\$\s*([0-9]+(?:\.[0-9]+)?|Market Price)$', line)
            if item_with_price:
                flush_current()
                section_parts = [current_major_section]
                if current_package:
                    section_parts.append(current_package)
                if current_subsection and current_subsection != 'Items':
                    section_parts.append(current_subsection)
                section = ' · '.join(section_parts)
                current_item = {
                    'menu_section': section,
                    'item_name': clean_menu_text(item_with_price.group(1)),
                    'menu_descriptor': '',
                    'price_text': item_with_price.group(2)
                }
                continue

            if '|' in line:
                flush_current()
                left, right = line.split('|', 1)
                section_parts = [current_major_section]
                if current_package:
                    section_parts.append(current_package)
                if current_subsection and current_subsection != 'Items':
                    section_parts.append(current_subsection)
                section = ' · '.join(section_parts)
                current_item = {
                    'menu_section': section,
                    'item_name': clean_menu_text(left),
                    'menu_descriptor': clean_menu_text(right),
                    'price_text': ''
                }
                continue

            is_heading_like = upper == line and len(line.split()) <= 8 and any(ch.isalpha() for ch in line)
            if is_heading_like and len(line) < 50:
                flush_current()
                current_subsection = line.title()
                continue

            if not current_item:
                section_parts = [current_major_section]
                if current_package:
                    section_parts.append(current_package)
                if current_subsection and current_subsection != 'Items':
                    section_parts.append(current_subsection)
                section = ' · '.join(section_parts)
                current_item = {
                    'menu_section': section,
                    'item_name': line,
                    'menu_descriptor': '',
                    'price_text': ''
                }
            else:
                if current_item.get('menu_descriptor'):
                    current_item['menu_descriptor'] = f"{current_item['menu_descriptor']} {line}".strip()
                else:
                    current_item['menu_descriptor'] = line

    flush_current()

    deduped = []
    seen = set()
    for item in parsed:
        key = (item.get('menu_section', '').lower(), item.get('item_name', '').lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped

def find_or_create_ingredient(cur, name, unit_value):
    cur.execute("SELECT id FROM ingredients WHERE LOWER(name) = LOWER(%s) LIMIT 1", (name,))
    existing = cur.fetchone()
    if existing:
        return existing['id'], False

    ingredient_id = generate_id('ing_')
    cur.execute("""
        INSERT INTO ingredients (id, name, unit)
        VALUES (%s, %s, %s)
    """, (
        ingredient_id,
        name,
        unit_value or None
    ))
    return ingredient_id, True

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

VENUE_DEFAULTS = [
    'Foxtown Brewing',
    "Renard's",
    'Interurban',
    'Heritage Meats',
    "11's Lounge",
    'Banquets',
    'Foxtown Landing'
]

BANQUET_MENU_SECTION_OPTIONS = [
    'Breakfast',
    'Breakouts',
    'Snacks',
    'Daytime Stations',
    'Reception Displays',
    'Reception Stations',
    'Small Bites',
    "Hors D'Oeuvres",
    'Buffet Packages',
    'Custom Buffet',
    'Plated Dinner',
    'Late Night Snacks',
    'Beverages',
    'Enhancements',
    'Other'
]

BANQUET_PACKAGE_SUBSECTIONS = {
    'STARTERS', 'ENTREES', 'SIDES', 'SWEETS', 'DESSERT', 'DESSERTS', 'COLD', 'WARM',
    'COLD APPETIZERS', 'WARM APPETIZERS'
}

def db_table_exists(cur, table_name):
    cur.execute("SELECT to_regclass(%s) AS table_ref", (table_name,))
    row = cur.fetchone() or {}
    return bool(row.get('table_ref'))

def db_column_exists(cur, table_name, column_name):
    if '.' in table_name:
        schema_name, plain_table = table_name.split('.', 1)
    else:
        schema_name, plain_table = 'public', table_name
    cur.execute("""
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = %s
          AND column_name = %s
        LIMIT 1
    """, (schema_name, plain_table, column_name))
    return cur.fetchone() is not None

def ensure_banquet_menu_item_base_yield_columns(cur):
    """Backfill banquet menu item sold-as fields on older databases before save."""
    if not db_table_exists(cur, 'public.banquet_menu_items'):
        return False

    cur.execute("""
        ALTER TABLE banquet_menu_items
        ADD COLUMN IF NOT EXISTS base_yield_qty NUMERIC DEFAULT 1
    """)
    cur.execute("""
        ALTER TABLE banquet_menu_items
        ADD COLUMN IF NOT EXISTS base_yield_unit TEXT DEFAULT 'each'
    """)
    cur.execute("""
        UPDATE banquet_menu_items
        SET base_yield_qty = 1
        WHERE base_yield_qty IS NULL
    """)
    cur.execute("""
        UPDATE banquet_menu_items
        SET base_yield_unit = 'each'
        WHERE base_yield_unit IS NULL OR TRIM(base_yield_unit) = ''
    """)
    return True

def get_active_venues(cur):
    if not db_table_exists(cur, 'public.venues'):
        return []
    cur.execute("""
        SELECT id, name, active, sort_order
        FROM venues
        WHERE active = TRUE
        ORDER BY sort_order NULLS LAST, name
    """)
    return cur.fetchall()

def get_recipe_venue_ids(cur, recipe_id):
    if not db_table_exists(cur, 'public.recipe_venues'):
        return []
    cur.execute("""
        SELECT venue_id
        FROM recipe_venues
        WHERE recipe_id = %s
        ORDER BY venue_id
    """, (recipe_id,))
    return [row['venue_id'] for row in cur.fetchall()]

def parse_recipe_venue_ids(request, cur, errors):
    raw_ids = request.form.getlist('recipe_venue_ids[]')
    candidate_ids = sorted({(value or '').strip() for value in raw_ids if (value or '').strip()})
    if not candidate_ids:
        return []
    if not db_table_exists(cur, 'public.venues'):
        return []
    cur.execute("SELECT id FROM venues WHERE id = ANY(%s)", (candidate_ids,))
    valid_ids = {row['id'] for row in cur.fetchall()}
    invalid_ids = [value for value in candidate_ids if value not in valid_ids]
    if invalid_ids:
        errors.append('One or more selected venues were invalid.')
    return [value for value in candidate_ids if value in valid_ids]

def normalize_quarter(value):
    if not value:
        return None
    value = value.strip().upper()
    if value in ('1', 'Q1'):
        return 'Q1'
    if value in ('2', 'Q2'):
        return 'Q2'
    if value in ('3', 'Q3'):
        return 'Q3'
    if value in ('4', 'Q4'):
        return 'Q4'
    return value
