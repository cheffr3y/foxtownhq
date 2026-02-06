import os
import uuid
import hmac
from io import BytesIO
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, send_file
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import RealDictCursor
from werkzeug.security import check_password_hash
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY')

# Database connection
def get_db():
    conn = psycopg2.connect(os.getenv('DATABASE_URL'))
    return conn

# Admin auth (single user for MVP)
def get_admin_config():
    username = os.getenv('ADMIN_USERNAME')
    password_hash = os.getenv('ADMIN_PASSWORD_HASH')
    password = os.getenv('ADMIN_PASSWORD')
    if not username or (not password_hash and not password):
        return None
    return {
        'username': username,
        'password_hash': password_hash,
        'password': password
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
        'weight': ['oz'],
        'volume': ['fl oz'],
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

PRICE_REFRESH_DAYS = 56
RECIPE_Q_FACTOR_PERCENT = to_float(os.getenv('RECIPE_Q_FACTOR_PERCENT') or 5)

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
    system = request.args.get('units')
    if system in ('auto', 'metric', 'imperial'):
        session['unit_system'] = system
    return session.get('unit_system', 'auto')

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

@app.context_processor
def inject_helpers():
    return {
        'smart_quantity': smart_quantity,
        'unit_system': get_unit_system(),
        'recipe_type_choices': RECIPE_TYPE_CHOICES,
        'ingredient_categories': INGREDIENT_CATEGORIES,
        'recipe_categories': RECIPE_CATEGORIES
    }

# Recipe helpers
def get_recipe_by_id(cur, recipe_id):
    cur.execute(
        "SELECT id, name, category, yield_qty, yield_unit, instructions, source_venue, equipment, recipe_type FROM recipes WHERE id = %s",
        (recipe_id,)
    )
    return cur.fetchone()

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
    section_values=None
):
    menu_items = []
    menu_groups = []
    menu_groups = []
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

        popularity = None
        if popularity_values and idx < len(popularity_values):
            try:
                popularity = int(float(popularity_values[idx]))
            except (TypeError, ValueError):
                popularity = None
            if popularity is not None and popularity <= 0:
                popularity = None

        base_cost = get_recipe_total_cost(cur, recipe_id, unit_system)
        item_total = base_cost * batches
        total_cost += item_total

        section = None
        if section_values and idx < len(section_values):
            section = (section_values[idx] or '').strip() or None

        menu_items.append({
            'recipe': recipe,
            'recipe_id': recipe_id,
            'batches': batches,
            'base_cost': base_cost,
            'item_total': item_total,
            'menu_price': menu_price,
            'target_food_cost_percent': target_percent,
            'popularity_score': popularity,
            'menu_section': section
        })

    if not menu_items:
        errors.append('Add at least one recipe to calculate a cost.')

    return menu_items, total_cost, errors

def apply_menu_pricing(menu_items, default_target_percent=20):
    total_weight = 0
    weighted_cost = 0
    weighted_price = 0

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
        else:
            item['food_cost_percent'] = None

        popularity = item.get('popularity_score')
        try:
            popularity = int(popularity) if popularity is not None else None
        except (TypeError, ValueError):
            popularity = None
        if not popularity or popularity <= 0:
            popularity = 5
        item['popularity_score'] = popularity

        if menu_price and menu_price > 0:
            weighted_cost += item['base_cost'] * popularity
            weighted_price += menu_price * popularity
            total_weight += popularity

    theoretical_food_cost = None
    weighted_avg_price = None
    weighted_avg_cost = None
    if total_weight > 0 and weighted_price > 0:
        theoretical_food_cost = (weighted_cost / weighted_price) * 100
        weighted_avg_price = weighted_price / total_weight
        weighted_avg_cost = weighted_cost / total_weight

    return {
        'theoretical_food_cost_percent': theoretical_food_cost,
        'weighted_avg_price': weighted_avg_price,
        'weighted_avg_cost': weighted_avg_cost,
        'total_weight': total_weight
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
        items_sorted = sorted(items, key=lambda item: (item.get('recipe', {}).get('name') or '').lower())
        grouped_list.append({'section': section, 'items': items_sorted})

    return grouped_list

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

def collect_ingredients_from_components(components, totals):
    for item in components:
        if item.get('type') == 'ingredient':
            ing_id = item.get('item_id')
            unit = (item.get('unit') or '').strip()
            qty = to_float(item.get('scaled_quantity'))
            if ing_id:
                key = (ing_id, unit)
                totals[key] = totals.get(key, 0) + qty
        if item.get('children'):
            collect_ingredients_from_components(item['children'], totals)

def collect_subrecipes_from_components(components, subrecipes):
    for item in components:
        if item.get('type') == 'recipe' and item.get('item_id'):
            subrecipes.add(item['item_id'])
        if item.get('children'):
            collect_subrecipes_from_components(item['children'], subrecipes)

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

# Simple user class for authentication
class User(UserMixin):
    def __init__(self, id, username):
        self.id = id
        self.username = username

# Flask-Login setup
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

@login_manager.user_loader
def load_user(user_id):
    config = get_admin_config()
    username = config['username'] if config else 'admin'
    return User(user_id, username)  # Simplified for now

# Routes
@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        config = get_admin_config()
        if not config:
            flash('Admin credentials are not configured yet.', 'error')
            return render_template('login.html')

        username = request.form.get('username')
        password = request.form.get('password')
        
        is_user_match = username == config['username']
        is_pass_match = False
        if config['password_hash']:
            is_pass_match = check_password_hash(config['password_hash'], password or '')
        else:
            is_pass_match = hmac.compare_digest(password or '', config['password'] or '')

        if is_user_match and is_pass_match:
            user = User('1', username)
            login_user(user)
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid credentials', 'error')
    
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html')

@app.route('/recipes')
@login_required
def recipes():
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # Get all recipes with ingredient count
    cur.execute("""
        SELECT r.*, COUNT(ri.id) as ingredient_count
        FROM recipes r
        LEFT JOIN recipe_ingredients ri ON r.id = ri.recipe_id
        GROUP BY r.id
        ORDER BY r.name
    """)
    
    recipes_list = cur.fetchall()
    cur.close()
    conn.close()
    
    return render_template('recipes.html', recipes=recipes_list)

@app.route('/menu-costing', methods=['GET', 'POST'])
@login_required
def menu_costing():
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    unit_system = get_unit_system()

    cur.execute("""
        SELECT id, name, yield_qty, yield_unit, recipe_type
        FROM recipes
        ORDER BY name
    """)
    recipes_list = cur.fetchall()

    menu_items = []
    total_cost = 0
    q_factor_percent = 5
    q_amount = 0
    grand_total = 0

    if request.method == 'POST':
        q_factor_raw = (request.form.get('q_factor_percent') or '').strip()
        if q_factor_raw:
            q_factor_percent = q_factor_raw
        recipe_ids = request.form.getlist('menu_recipe_id[]')
        batch_values = request.form.getlist('menu_batches[]')
        menu_items, total_cost, errors = parse_menu_items(cur, unit_system, recipe_ids, batch_values)
        q_factor_percent, q_amount, grand_total = compute_q_factor(total_cost, q_factor_percent)
        if errors:
            flash(' '.join(sorted(set(errors))), 'error')
    else:
        q_factor_percent, q_amount, grand_total = compute_q_factor(total_cost, q_factor_percent)

    cur.close()
    conn.close()

    return render_template(
        'menu_costing.html',
        recipes=recipes_list,
        menu_items=menu_items,
        total_cost=total_cost,
        q_factor_percent=q_factor_percent,
        q_amount=q_amount,
        grand_total=grand_total
    )

@app.route('/menu-rollouts')
@login_required
def menu_rollouts():
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT mr.*, COUNT(mri.id) as item_count
        FROM menu_rollouts mr
        LEFT JOIN menu_rollout_items mri ON mr.id = mri.rollout_id
        WHERE mr.is_one_off = FALSE
        GROUP BY mr.id
        ORDER BY mr.year DESC NULLS LAST, mr.quarter DESC NULLS LAST, mr.venue, mr.name
    """)
    rollouts = cur.fetchall()

    cur.close()
    conn.close()

    return render_template('menu_rollouts.html', rollouts=rollouts)

MENU_SECTION_OPTIONS = [
    'Appetizers',
    'Salads',
    'Wraps',
    'BBQ Dinner',
    'Handhelds',
    'Fish Fry Friday',
    'Entrees',
    'Sides',
    'Desserts'
]

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

@app.route('/menu-rollouts/new', methods=['GET', 'POST'])
@login_required
def menu_rollout_new():
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    unit_system = get_unit_system()

    cur.execute("""
        SELECT id, name, yield_qty, yield_unit, recipe_type
        FROM recipes
        WHERE recipe_type = 'menu' OR recipe_type IS NULL
        ORDER BY name
    """)
    recipes_list = cur.fetchall()

    menu_items = []
    total_cost = 0
    ingredient_master = []
    ingredient_total_cost = 0
    batch_recipes = []
    q_factor_percent = 5
    default_target_percent = 20
    q_amount = 0
    grand_total = 0
    rollout_data = {
        'name': '',
        'venue': '',
        'year': '',
        'quarter': '',
        'notes': '',
        'q_factor_percent': q_factor_percent,
        'target_food_cost_percent': default_target_percent
    }
    pricing_summary = {
        'theoretical_food_cost_percent': None,
        'weighted_avg_price': None,
        'weighted_avg_cost': None,
        'total_weight': 0
    }

    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        venue = (request.form.get('venue') or '').strip()
        year_value = (request.form.get('year') or '').strip()
        quarter = normalize_quarter(request.form.get('quarter'))
        notes = (request.form.get('notes') or '').strip()
        q_factor_raw = (request.form.get('q_factor_percent') or '').strip()
        q_factor_percent = q_factor_raw if q_factor_raw else q_factor_percent
        target_raw = (request.form.get('target_food_cost_percent') or '').strip()
        default_target_percent = target_raw if target_raw else default_target_percent

        rollout_data = {
            'name': name,
            'venue': venue,
            'year': year_value,
            'quarter': quarter,
            'notes': notes,
            'q_factor_percent': q_factor_percent,
            'target_food_cost_percent': default_target_percent
        }

        year = int(year_value) if year_value.isdigit() else None
        if not name and venue and year and quarter:
            name = f"{venue} {quarter} {year}"

        recipe_ids = request.form.getlist('menu_recipe_id[]')
        recipe_names = request.form.getlist('menu_recipe_name[]')
        batch_values = request.form.getlist('menu_batches[]')
        menu_prices = request.form.getlist('menu_price[]')
        target_values = request.form.getlist('menu_target_percent[]')
        popularity_values = request.form.getlist('menu_popularity[]')
        section_values = request.form.getlist('menu_section[]')
        menu_items, total_cost, errors = parse_menu_items(
            cur,
            unit_system,
            recipe_ids,
            batch_values,
            menu_prices,
            target_values,
            popularity_values,
            default_target_percent,
            section_values
        )
        for idx, recipe_id in enumerate(recipe_ids):
            name = (recipe_names[idx] if idx < len(recipe_names) else '').strip()
            if name and not recipe_id:
                errors.append(f'Recipe "{name}" was not found. Select it from the list.')
        q_factor_percent, q_amount, grand_total = compute_q_factor(total_cost, q_factor_percent)
        pricing_summary = apply_menu_pricing(menu_items, default_target_percent)
        if menu_items:
            ingredient_master, ingredient_total_cost, batch_recipes = build_rollout_breakdown(
                cur,
                unit_system,
                menu_items
            )
            menu_groups = group_menu_items(menu_items)
            menu_groups = group_menu_items(menu_items)

        if not name:
            errors.append('Menu name is required.')

        if errors:
            flash(' '.join(sorted(set(errors))), 'error')
        else:
            rollout_id = generate_id('menu_')
            try:
                cur.execute("""
                    INSERT INTO menu_rollouts (id, name, venue, year, quarter, notes, q_factor_percent, target_food_cost_percent, is_one_off)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, FALSE)
                """, (
                    rollout_id,
                    name,
                    venue or None,
                    year,
                    quarter or None,
                    notes or None,
                    to_float(q_factor_percent),
                    to_float(default_target_percent)
                ))

                for item in menu_items:
                    cur.execute("""
                        INSERT INTO menu_rollout_items (id, rollout_id, recipe_id, batches, menu_price, target_food_cost_percent, popularity_score, menu_section)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        generate_id('mri_'),
                        rollout_id,
                        item['recipe_id'],
                        item['batches'],
                        item.get('menu_price'),
                        item.get('target_food_cost_percent'),
                        item.get('popularity_score'),
                        item.get('menu_section')
                    ))

                conn.commit()
                cur.close()
                conn.close()
                flash('Menu rollout created', 'success')
                return redirect(url_for('menu_rollout_edit', rollout_id=rollout_id))
            except Exception:
                conn.rollback()
                flash('Error saving menu rollout', 'error')

    cur.close()
    conn.close()

    return render_template(
        'menu_rollout_form.html',
        mode='new',
        rollout=rollout_data,
        recipes=recipes_list,
        menu_items=menu_items,
        menu_groups=menu_groups,
        total_cost=total_cost,
        q_factor_percent=q_factor_percent,
        q_amount=q_amount,
        grand_total=grand_total,
        pricing_summary=pricing_summary,
        default_target_percent=to_float(default_target_percent) or 20,
        ingredient_master=ingredient_master,
        ingredient_total_cost=ingredient_total_cost,
        batch_recipes=batch_recipes,
        section_options=MENU_SECTION_OPTIONS
    )

@app.route('/menu-rollouts/<rollout_id>', methods=['GET', 'POST'])
@login_required
def menu_rollout_edit(rollout_id):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    unit_system = get_unit_system()

    cur.execute("SELECT * FROM menu_rollouts WHERE id = %s", (rollout_id,))
    rollout = cur.fetchone()
    if not rollout:
        cur.close()
        conn.close()
        flash('Menu rollout not found', 'error')
        return redirect(url_for('menu_rollouts'))

    cur.execute("SELECT id, name, yield_qty, yield_unit, recipe_type FROM recipes ORDER BY name")
    recipes_list = cur.fetchall()

    menu_items = []
    total_cost = 0
    ingredient_master = []
    ingredient_total_cost = 0
    batch_recipes = []
    q_factor_percent = rollout.get('q_factor_percent') if rollout and rollout.get('q_factor_percent') is not None else 5
    default_target_percent = rollout.get('target_food_cost_percent') if rollout and rollout.get('target_food_cost_percent') is not None else 20
    q_amount = 0
    grand_total = 0
    pricing_summary = {
        'theoretical_food_cost_percent': None,
        'weighted_avg_price': None,
        'weighted_avg_cost': None,
        'total_weight': 0
    }

    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        venue = (request.form.get('venue') or '').strip()
        year_value = (request.form.get('year') or '').strip()
        quarter = normalize_quarter(request.form.get('quarter'))
        notes = (request.form.get('notes') or '').strip()
        q_factor_raw = (request.form.get('q_factor_percent') or '').strip()
        q_factor_percent = q_factor_raw if q_factor_raw else q_factor_percent
        target_raw = (request.form.get('target_food_cost_percent') or '').strip()
        default_target_percent = target_raw if target_raw else default_target_percent

        rollout = dict(rollout)
        rollout.update({
            'name': name,
            'venue': venue,
            'year': year_value,
            'quarter': quarter,
            'notes': notes,
            'q_factor_percent': q_factor_percent,
            'target_food_cost_percent': default_target_percent
        })

        year = int(year_value) if year_value.isdigit() else None
        if not name and venue and year and quarter:
            name = f"{venue} {quarter} {year}"

        recipe_ids = request.form.getlist('menu_recipe_id[]')
        recipe_names = request.form.getlist('menu_recipe_name[]')
        batch_values = request.form.getlist('menu_batches[]')
        menu_prices = request.form.getlist('menu_price[]')
        target_values = request.form.getlist('menu_target_percent[]')
        popularity_values = request.form.getlist('menu_popularity[]')
        section_values = request.form.getlist('menu_section[]')
        menu_items, total_cost, errors = parse_menu_items(
            cur,
            unit_system,
            recipe_ids,
            batch_values,
            menu_prices,
            target_values,
            popularity_values,
            default_target_percent,
            section_values
        )
        for idx, recipe_id in enumerate(recipe_ids):
            name = (recipe_names[idx] if idx < len(recipe_names) else '').strip()
            if name and not recipe_id:
                errors.append(f'Recipe "{name}" was not found. Select it from the list.')
        q_factor_percent, q_amount, grand_total = compute_q_factor(total_cost, q_factor_percent)
        pricing_summary = apply_menu_pricing(menu_items, default_target_percent)
        if menu_items:
            ingredient_master, ingredient_total_cost, batch_recipes = build_rollout_breakdown(
                cur,
                unit_system,
                menu_items
            )

        if not name:
            errors.append('Menu name is required.')

        if errors:
            flash(' '.join(sorted(set(errors))), 'error')
        else:
            try:
                cur.execute("""
                    UPDATE menu_rollouts
                    SET name = %s,
                        venue = %s,
                        year = %s,
                        quarter = %s,
                        notes = %s,
                        q_factor_percent = %s,
                        target_food_cost_percent = %s
                    WHERE id = %s
                """, (
                    name,
                    venue or None,
                    year,
                    quarter or None,
                    notes or None,
                    to_float(q_factor_percent),
                    to_float(default_target_percent),
                    rollout_id
                ))

                cur.execute("DELETE FROM menu_rollout_items WHERE rollout_id = %s", (rollout_id,))
                for item in menu_items:
                    cur.execute("""
                        INSERT INTO menu_rollout_items (id, rollout_id, recipe_id, batches, menu_price, target_food_cost_percent, popularity_score, menu_section)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        generate_id('mri_'),
                        rollout_id,
                        item['recipe_id'],
                        item['batches'],
                        item.get('menu_price'),
                        item.get('target_food_cost_percent'),
                        item.get('popularity_score'),
                        item.get('menu_section')
                    ))

                conn.commit()
                cur.close()
                conn.close()
                flash('Menu rollout updated', 'success')
                return redirect(url_for('menu_rollout_edit', rollout_id=rollout_id))
            except Exception:
                conn.rollback()
                flash('Error updating menu rollout', 'error')

    if request.method == 'GET':
        cur.execute("""
            SELECT mri.recipe_id,
                   mri.batches,
                   mri.menu_price,
                   mri.target_food_cost_percent,
                   mri.popularity_score,
                   mri.menu_section,
                   r.name
            FROM menu_rollout_items mri
            JOIN recipes r ON r.id = mri.recipe_id
            WHERE mri.rollout_id = %s
            ORDER BY r.name
        """, (rollout_id,))
        saved_items = cur.fetchall()

        recipe_ids = [row['recipe_id'] for row in saved_items]
        batch_values = [row['batches'] for row in saved_items]
        menu_prices = [row.get('menu_price') for row in saved_items]
        target_values = [row.get('target_food_cost_percent') for row in saved_items]
        popularity_values = [row.get('popularity_score') for row in saved_items]
        section_values = [row.get('menu_section') for row in saved_items]
        if recipe_ids:
            menu_items, total_cost, _ = parse_menu_items(
                cur,
                unit_system,
                recipe_ids,
                batch_values,
                menu_prices,
                target_values,
                popularity_values,
                default_target_percent,
                section_values
            )
            q_factor_percent, q_amount, grand_total = compute_q_factor(total_cost, q_factor_percent)
            pricing_summary = apply_menu_pricing(menu_items, default_target_percent)
            ingredient_master, ingredient_total_cost, batch_recipes = build_rollout_breakdown(
                cur,
                unit_system,
                menu_items
            )
            menu_groups = group_menu_items(menu_items)

    cur.close()
    conn.close()

    return render_template(
        'menu_rollout_form.html',
        mode='edit',
        rollout=rollout,
        recipes=recipes_list,
        menu_items=menu_items,
        menu_groups=menu_groups,
        total_cost=total_cost,
        q_factor_percent=q_factor_percent,
        q_amount=q_amount,
        grand_total=grand_total,
        pricing_summary=pricing_summary,
        default_target_percent=to_float(default_target_percent) or 20,
        ingredient_master=ingredient_master,
        ingredient_total_cost=ingredient_total_cost,
        batch_recipes=batch_recipes,
        section_options=MENU_SECTION_OPTIONS
    )

@app.route('/menu-rollouts/<rollout_id>/order-guide')
@login_required
def menu_rollout_order_guide(rollout_id):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    unit_system = get_unit_system()

    cur.execute("SELECT * FROM menu_rollouts WHERE id = %s", (rollout_id,))
    rollout = cur.fetchone()
    if not rollout:
        cur.close()
        conn.close()
        flash('Menu rollout not found', 'error')
        return redirect(url_for('menu_rollouts'))

    cur.execute("SELECT recipe_id, batches FROM menu_rollout_items WHERE rollout_id = %s", (rollout_id,))
    items = cur.fetchall()
    if not items:
        cur.close()
        conn.close()
        flash('Add recipes to this rollout before exporting an order guide.', 'error')
        return redirect(url_for('menu_rollout_edit', rollout_id=rollout_id))

    recipe_ids = [row['recipe_id'] for row in items]
    batch_values = [row['batches'] for row in items]
    menu_items, _, errors = parse_menu_items(cur, unit_system, recipe_ids, batch_values)
    if errors:
        cur.close()
        conn.close()
        flash(' '.join(sorted(set(errors))), 'error')
        return redirect(url_for('menu_rollout_edit', rollout_id=rollout_id))

    ingredient_master, _, _ = build_rollout_breakdown(cur, unit_system, menu_items)
    if not ingredient_master:
        cur.close()
        conn.close()
        flash('No ingredients found for this rollout.', 'error')
        return redirect(url_for('menu_rollout_edit', rollout_id=rollout_id))

    grouped = {}
    for ing in ingredient_master:
        vendor = ing.get('vendor') or 'Unassigned Vendor'
        category = ing.get('category') or 'Uncategorized'
        grouped.setdefault(vendor, {}).setdefault(category, []).append(ing)

    wb = Workbook()
    existing_titles = set()
    header_fill = PatternFill('solid', fgColor='E2E8F0')
    category_fill = PatternFill('solid', fgColor='DCFCE7')
    stripe_fill = PatternFill('solid', fgColor='F8FAFC')
    header_font = Font(bold=True, color='1F2937')
    category_font = Font(bold=True, color='14532D')
    align = Alignment(vertical='center')
    thin = Side(border_style='thin', color='E5E7EB')
    border = Border(top=thin, bottom=thin, left=thin, right=thin)

    headers = ["Ingredient", "Unit", "Vendor Code", "G-Code", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    column_widths = [36, 10, 16, 16, 10, 10, 10, 10, 10, 10]

    first_sheet = True
    for vendor, categories in sorted(grouped.items(), key=lambda item: item[0].lower()):
        ws = wb.active if first_sheet else wb.create_sheet()
        first_sheet = False
        ws.title = sanitize_sheet_title(vendor, existing_titles)

        ws.append(headers)
        for col_idx, _ in enumerate(headers, start=1):
            cell = ws.cell(row=1, column=col_idx)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = align
            cell.border = border
            ws.column_dimensions[cell.column_letter].width = column_widths[col_idx - 1]

        ws.freeze_panes = "A2"

        row_idx = 2
        for category, items_list in sorted(categories.items(), key=lambda item: item[0].lower()):
            ws.merge_cells(start_row=row_idx, start_column=1, end_row=row_idx, end_column=len(headers))
            cell = ws.cell(row=row_idx, column=1, value=category)
            cell.fill = category_fill
            cell.font = category_font
            cell.alignment = align
            cell.border = border
            row_idx += 1

            zebra = False
            for ing in sorted(items_list, key=lambda item: (item.get('name') or '').lower()):
                row_values = [
                    ing.get('name') or '',
                    ing.get('unit') or ing.get('display_unit') or '',
                    ing.get('vendor_code') or '',
                    ing.get('g_code') or '',
                    '', '', '', '', '', ''
                ]
                for col_idx, value in enumerate(row_values, start=1):
                    cell = ws.cell(row=row_idx, column=col_idx, value=value)
                    cell.alignment = align
                    cell.border = border
                    if zebra:
                        cell.fill = stripe_fill
                zebra = not zebra
                row_idx += 1

            row_idx += 1

    cur.close()
    conn.close()

    output = BytesIO()
    wb.save(output)
    output.seek(0)

    filename = f"{make_safe_filename(rollout.get('name') or 'order_guide')}_order_guide.xlsx"
    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=filename
    )

@app.route('/recipes/new', methods=['GET', 'POST'])
@login_required
def recipe_new():
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    if request.method == 'POST':
        errors = []
        name = (request.form.get('name') or '').strip()
        category = (request.form.get('category') or '').strip()
        yield_qty = (request.form.get('yield_qty') or '').strip()
        yield_unit = (request.form.get('yield_unit') or '').strip()
        yield_unit = normalize_unit(yield_unit) or yield_unit
        instructions = (request.form.get('instructions') or '').strip()
        recipe_type = infer_recipe_type(name, request.form.get('recipe_type'))

        if not name:
            errors.append('Recipe name is required.')

        ingredient_ids = request.form.getlist('ingredient_id[]')
        ingredient_names = request.form.getlist('ingredient_name[]')
        ingredient_qtys = request.form.getlist('ingredient_qty[]')
        ingredient_units = request.form.getlist('ingredient_unit[]')

        for idx, ing_id in enumerate(ingredient_ids):
            ing_id = (ing_id or '').strip()
            ing_name = (ingredient_names[idx] if idx < len(ingredient_names) else '').strip()
            if not ing_id and ing_name:
                errors.append(f'Ingredient \"{ing_name}\" was not found. Select it from the list or create it first.')

        if errors:
            flash(' '.join(sorted(set(errors))), 'error')
        else:
            recipe_id = generate_id('rec_')
            try:
                cur.execute("""
                    INSERT INTO recipes (id, name, category, yield_qty, yield_unit, instructions, recipe_type)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (
                    recipe_id,
                    name,
                    category or None,
                    yield_qty or None,
                    yield_unit or None,
                    instructions or None,
                    recipe_type
                ))

                # Ingredients
                for idx, ing_id in enumerate(ingredient_ids):
                    ing_id = (ing_id or '').strip()
                    if not ing_id:
                        continue
                    qty = (ingredient_qtys[idx] if idx < len(ingredient_qtys) else '').strip()
                    unit = (ingredient_units[idx] if idx < len(ingredient_units) else '').strip()
                    unit = normalize_unit(unit) or unit
                    if qty == '':
                        continue
                    cur.execute("""
                        INSERT INTO recipe_ingredients (id, recipe_id, type, item_id, quantity, unit)
                        VALUES (%s, %s, 'ingredient', %s, %s, %s)
                    """, (
                        generate_id('ri_'),
                        recipe_id,
                        ing_id,
                        qty,
                        unit or None
                    ))

                # Sub-recipes
                sub_ids = request.form.getlist('sub_recipe_id[]')
                sub_qtys = request.form.getlist('sub_recipe_qty[]')
                sub_units = request.form.getlist('sub_recipe_unit[]')

                for idx, sub_id in enumerate(sub_ids):
                    sub_id = (sub_id or '').strip()
                    if not sub_id:
                        continue
                    qty = (sub_qtys[idx] if idx < len(sub_qtys) else '').strip()
                    unit = (sub_units[idx] if idx < len(sub_units) else '').strip()
                    unit = normalize_unit(unit) or unit
                    if qty == '':
                        continue
                    cur.execute("""
                        INSERT INTO recipe_ingredients (id, recipe_id, type, item_id, quantity, unit)
                        VALUES (%s, %s, 'recipe', %s, %s, %s)
                    """, (
                        generate_id('ri_'),
                        recipe_id,
                        sub_id,
                        qty,
                        unit or None
                    ))

                conn.commit()
                cur.close()
                conn.close()

                flash('Recipe created', 'success')
                return redirect(url_for('recipe_detail', recipe_id=recipe_id))
            except Exception:
                conn.rollback()
                flash('Error creating recipe', 'error')

    # GET or validation error fallback
    cur.execute("SELECT id, name, unit, category FROM ingredients ORDER BY name")
    ingredients_list = cur.fetchall()

    cur.execute("""
        SELECT id, name, yield_qty, yield_unit, recipe_type
        FROM recipes
        WHERE recipe_type = 'batch' OR recipe_type IS NULL
        ORDER BY name
    """)
    recipes_list = cur.fetchall()

    cur.close()
    conn.close()

    return render_template(
        'recipe_form.html',
        mode='new',
        recipe={'name': '', 'category': '', 'yield_qty': '', 'yield_unit': '', 'instructions': '', 'recipe_type': ''},
        ingredients=ingredients_list,
        recipes=recipes_list,
        ingredient_items=[],
        subrecipe_items=[]
    )

@app.route('/recipes/<recipe_id>/edit', methods=['GET', 'POST'])
@login_required
def recipe_edit(recipe_id):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    recipe = get_recipe_by_id(cur, recipe_id)
    if not recipe:
        cur.close()
        conn.close()
        flash('Recipe not found', 'error')
        return redirect(url_for('recipes'))

    if request.method == 'POST':
        errors = []
        name = (request.form.get('name') or '').strip()
        category = (request.form.get('category') or '').strip()
        yield_qty = (request.form.get('yield_qty') or '').strip()
        yield_unit = (request.form.get('yield_unit') or '').strip()
        yield_unit = normalize_unit(yield_unit) or yield_unit
        instructions = (request.form.get('instructions') or '').strip()
        recipe_type = infer_recipe_type(name, request.form.get('recipe_type'))

        if not name:
            errors.append('Recipe name is required.')

        ingredient_ids = request.form.getlist('ingredient_id[]')
        ingredient_names = request.form.getlist('ingredient_name[]')
        ingredient_qtys = request.form.getlist('ingredient_qty[]')
        ingredient_units = request.form.getlist('ingredient_unit[]')

        for idx, ing_id in enumerate(ingredient_ids):
            ing_id = (ing_id or '').strip()
            ing_name = (ingredient_names[idx] if idx < len(ingredient_names) else '').strip()
            if not ing_id and ing_name:
                errors.append(f'Ingredient \"{ing_name}\" was not found. Select it from the list or create it first.')

        if errors:
            flash(' '.join(sorted(set(errors))), 'error')
        else:
            try:
                cur.execute("""
                    UPDATE recipes
                    SET name = %s,
                        category = %s,
                        yield_qty = %s,
                        yield_unit = %s,
                        instructions = %s,
                        recipe_type = %s
                    WHERE id = %s
                """, (
                    name,
                    category or None,
                    yield_qty or None,
                    yield_unit or None,
                    instructions or None,
                    recipe_type,
                    recipe_id
                ))

                cur.execute("DELETE FROM recipe_ingredients WHERE recipe_id = %s", (recipe_id,))

                for idx, ing_id in enumerate(ingredient_ids):
                    ing_id = (ing_id or '').strip()
                    if not ing_id:
                        continue
                    qty = (ingredient_qtys[idx] if idx < len(ingredient_qtys) else '').strip()
                    unit = (ingredient_units[idx] if idx < len(ingredient_units) else '').strip()
                    unit = normalize_unit(unit) or unit
                    if qty == '':
                        continue
                    cur.execute("""
                        INSERT INTO recipe_ingredients (id, recipe_id, type, item_id, quantity, unit)
                        VALUES (%s, %s, 'ingredient', %s, %s, %s)
                    """, (
                        generate_id('ri_'),
                        recipe_id,
                        ing_id,
                        qty,
                        unit or None
                    ))

                sub_ids = request.form.getlist('sub_recipe_id[]')
                sub_qtys = request.form.getlist('sub_recipe_qty[]')
                sub_units = request.form.getlist('sub_recipe_unit[]')

                for idx, sub_id in enumerate(sub_ids):
                    sub_id = (sub_id or '').strip()
                    if not sub_id:
                        continue
                    qty = (sub_qtys[idx] if idx < len(sub_qtys) else '').strip()
                    unit = (sub_units[idx] if idx < len(sub_units) else '').strip()
                    unit = normalize_unit(unit) or unit
                    if qty == '':
                        continue
                    cur.execute("""
                        INSERT INTO recipe_ingredients (id, recipe_id, type, item_id, quantity, unit)
                        VALUES (%s, %s, 'recipe', %s, %s, %s)
                    """, (
                        generate_id('ri_'),
                        recipe_id,
                        sub_id,
                        qty,
                        unit or None
                    ))

                conn.commit()
                cur.close()
                conn.close()

                flash('Recipe updated', 'success')
                return redirect(url_for('recipe_detail', recipe_id=recipe_id))
            except Exception:
                conn.rollback()
                flash('Error updating recipe', 'error')

    # GET or validation error fallback
    components = get_recipe_components(cur, recipe_id)
    ingredient_items = [c for c in components if c.get('type') == 'ingredient']
    subrecipe_items = [c for c in components if c.get('type') == 'recipe']

    cur.execute("SELECT id, name, unit, category FROM ingredients ORDER BY name")
    ingredients_list = cur.fetchall()

    cur.execute("""
        SELECT id, name, yield_qty, yield_unit, recipe_type
        FROM recipes
        WHERE id != %s AND (recipe_type = 'batch' OR recipe_type IS NULL)
        ORDER BY name
    """, (recipe_id,))
    recipes_list = cur.fetchall()

    cur.close()
    conn.close()

    return render_template(
        'recipe_form.html',
        mode='edit',
        recipe=recipe,
        ingredients=ingredients_list,
        recipes=recipes_list,
        ingredient_items=ingredient_items,
        subrecipe_items=subrecipe_items
    )

@app.route('/recipe-generator', methods=['GET', 'POST'])
@login_required
def recipe_generator():
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("SELECT name FROM ingredients ORDER BY name")
    ingredient_names = [row['name'] for row in cur.fetchall()]

    data = {
        'name': '',
        'category': '',
        'venue': '',
        'recipe_type': '',
        'yield_qty': '',
        'yield_unit': '',
        'equipment': '',
        'ingredients': [{'name': '', 'amount': '', 'unit': '', 'notes': ''}],
        'steps': [{'description': '', 'time': ''}]
    }

    if request.method == 'POST':
        errors = []
        data['name'] = (request.form.get('name') or '').strip()
        data['category'] = (request.form.get('category') or '').strip()
        data['venue'] = (request.form.get('venue') or '').strip()
        data['recipe_type'] = (request.form.get('recipe_type') or '').strip()
        data['yield_qty'] = (request.form.get('yield_qty') or '').strip()
        data['yield_unit'] = (request.form.get('yield_unit') or '').strip()
        data['equipment'] = (request.form.get('equipment') or '').strip()

        recipe_type_value = infer_recipe_type(data['name'], data['recipe_type'])
        yield_qty_value = parse_float_field(data['yield_qty'], 'Yield quantity', errors, required=True, min_value=0.0001)
        yield_unit_value = normalize_unit(data['yield_unit']) or data['yield_unit']
        if not data['yield_unit']:
            errors.append('Yield unit is required.')

        ingredient_names_in = request.form.getlist('ingredient_name[]')
        ingredient_qtys = request.form.getlist('ingredient_qty[]')
        ingredient_units = request.form.getlist('ingredient_unit[]')
        ingredient_notes = request.form.getlist('ingredient_notes[]')

        data['ingredients'] = []
        for idx, ing_name in enumerate(ingredient_names_in):
            name = (ing_name or '').strip()
            qty = (ingredient_qtys[idx] if idx < len(ingredient_qtys) else '').strip()
            unit = (ingredient_units[idx] if idx < len(ingredient_units) else '').strip()
            notes = (ingredient_notes[idx] if idx < len(ingredient_notes) else '').strip()

            if not name and not qty and not unit and not notes:
                continue

            if not name:
                errors.append('Ingredient name is required.')
            qty_value = parse_float_field(qty, 'Ingredient quantity', errors, required=True, min_value=0.0001) if name else None
            if not unit:
                errors.append('Ingredient unit is required.')
            unit_value = normalize_unit(unit) or unit

            data['ingredients'].append({
                'name': name,
                'amount': qty,
                'unit': unit,
                'notes': notes
            })

        step_desc = request.form.getlist('step_desc[]')
        step_time = request.form.getlist('step_time[]')
        data['steps'] = []
        for idx, desc in enumerate(step_desc):
            description = (desc or '').strip()
            time_value = (step_time[idx] if idx < len(step_time) else '').strip()
            if not description and not time_value:
                continue
            if not description:
                errors.append('Each step needs a description.')
            data['steps'].append({
                'description': description,
                'time': time_value
            })

        if not data['ingredients']:
            errors.append('Add at least one ingredient.')

        if errors:
            flash(' '.join(sorted(set(errors))), 'error')
        else:
            recipe_id = generate_id('rec_')
            instructions_lines = []
            if data['equipment']:
                instructions_lines.append(f"Equipment: {data['equipment']}")
            if data['steps']:
                instructions_lines.append("Steps:")
                for idx, step in enumerate(data['steps']):
                    time_label = f" ({step['time']})" if step['time'] else ''
                    instructions_lines.append(f"{idx + 1}. {step['description']}{time_label}")
            instructions = '\n'.join(instructions_lines)

            try:
                cur.execute("""
                    INSERT INTO recipes (id, name, category, yield_qty, yield_unit, instructions, source_venue, equipment, recipe_type)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    recipe_id,
                    data['name'],
                    data['category'] or None,
                    yield_qty_value,
                    yield_unit_value or None,
                    instructions or None,
                    data['venue'] or None,
                    data['equipment'] or None,
                    recipe_type_value
                ))

                created_ingredients = 0
                for item in data['ingredients']:
                    unit_value = normalize_unit(item['unit']) or item['unit']
                    ingredient_id, created = find_or_create_ingredient(cur, item['name'], unit_value)
                    if created:
                        created_ingredients += 1
                    cur.execute("""
                        INSERT INTO recipe_ingredients (id, recipe_id, type, item_id, quantity, unit, notes)
                        VALUES (%s, %s, 'ingredient', %s, %s, %s, %s)
                    """, (
                        generate_id('ri_'),
                        recipe_id,
                        ingredient_id,
                        item['amount'],
                        unit_value or None,
                        item['notes'] or None
                    ))

                conn.commit()
                cur.close()
                conn.close()
                if created_ingredients:
                    flash(f'Recipe saved. {created_ingredients} new ingredient(s) were created.', 'success')
                else:
                    flash('Recipe saved.', 'success')
                return redirect(url_for('recipe_detail', recipe_id=recipe_id))
            except Exception:
                conn.rollback()
                flash('Error saving recipe', 'error')

    cur.close()
    conn.close()

    return render_template(
        'recipe_generator.html',
        ingredient_names=ingredient_names,
        data=data
    )

@app.route('/recipes/<recipe_id>')
@login_required
def recipe_detail(recipe_id):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    unit_system = get_unit_system()
    
    # Get recipe details
    recipe = get_recipe_by_id(cur, recipe_id)
    
    if not recipe:
        flash('Recipe not found', 'error')
        return redirect(url_for('recipes'))
    
    components, total_cost, _ = build_component_tree(cur, recipe_id, 1, 0, set(), unit_system, apply_q_factor=True)
    base_total_cost = None
    q_factor_amount = None
    q_factor_percent = RECIPE_Q_FACTOR_PERCENT
    if total_cost is not None and q_factor_percent and q_factor_percent > 0:
        divisor = 1 + (q_factor_percent / 100)
        base_total_cost = total_cost / divisor
        q_factor_amount = total_cost - base_total_cost
    elif total_cost is not None:
        base_total_cost = total_cost
    yield_qty_float = to_float(recipe.get('yield_qty'))
    cost_per_yield = None
    if total_cost and yield_qty_float > 0:
        cost_per_yield = total_cost / yield_qty_float
    
    cur.close()
    conn.close()
    
    return render_template(
        'recipe_detail.html',
        recipe=recipe,
        components=components,
        component_count=len(components),
        total_cost=total_cost,
        cost_per_yield=cost_per_yield,
        base_total_cost=base_total_cost,
        q_factor_percent=RECIPE_Q_FACTOR_PERCENT,
        q_factor_amount=q_factor_amount
    )

@app.route('/ingredients')
@login_required
def ingredients():
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # Get all ingredients
    cur.execute("""
        SELECT * FROM ingredients
        ORDER BY category, name
    """)
    
    ingredients_list = cur.fetchall()
    print(f"DEBUG: Found {len(ingredients_list)} ingredients")

    now = datetime.utcnow()
    needs_update_count = 0
    for ingredient in ingredients_list:
        updated_at = ingredient.get('price_updated_at')
        age_days = None
        if updated_at:
            if getattr(updated_at, 'tzinfo', None):
                updated_at = updated_at.replace(tzinfo=None)
            age_days = (now - updated_at).days
        ingredient['price_age_days'] = age_days
        ingredient['needs_price_update'] = (age_days is None) or (age_days >= PRICE_REFRESH_DAYS)
        if ingredient['needs_price_update']:
            needs_update_count += 1
    cur.close()
    conn.close()
    
    return render_template(
        'ingredients.html',
        ingredients=ingredients_list,
        needs_update_count=needs_update_count,
        price_refresh_days=PRICE_REFRESH_DAYS
    )

@app.route('/ingredients/new', methods=['GET', 'POST'])
@login_required
def new_ingredient():
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        if not name:
            flash('Ingredient name is required', 'error')
        else:
            unit_value = (request.form.get('unit') or '').strip()
            unit_value = normalize_unit(unit_value) or unit_value
            cost_raw = request.form.get('cost_per_unit')
            cost_value = to_float(cost_raw) if cost_raw not in (None, '') else None
            price_updated = cost_value is not None

            try:
                cur.execute("""
                    INSERT INTO ingredients (id, name, category, unit, vendor, vendor_code, g_code, cost_per_unit, price_updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CASE WHEN %s THEN NOW() ELSE NULL END)
                """, (
                    generate_id('ing_'),
                    name,
                    request.form.get('category') or None,
                    unit_value or None,
                    request.form.get('vendor') or None,
                    request.form.get('vendor_code') or None,
                    request.form.get('g_code') or None,
                    cost_value,
                    price_updated
                ))
                conn.commit()
                cur.close()
                conn.close()

                flash('Ingredient created', 'success')
                return redirect(url_for('ingredients'))
            except Exception as e:
                conn.rollback()
                flash('Error saving ingredient', 'error')

    cur.close()
    conn.close()

    return render_template(
        'new_ingredient.html',
        ingredient={
            'name': '',
            'category': '',
            'unit': '',
            'vendor': '',
            'vendor_code': '',
            'g_code': '',
            'cost_per_unit': ''
        }
    )

@app.route('/ingredients/<ingredient_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_ingredient(ingredient_id):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        if not name:
            cur.close()
            conn.close()
            flash('Ingredient name is required', 'error')
            return redirect(url_for('edit_ingredient', ingredient_id=ingredient_id))

        cur.execute("SELECT cost_per_unit FROM ingredients WHERE id = %s", (ingredient_id,))
        existing = cur.fetchone() or {}
        existing_cost = to_float(existing.get('cost_per_unit'))
        new_cost_raw = request.form.get('cost_per_unit')
        new_cost = to_float(new_cost_raw) if new_cost_raw not in (None, '') else None
        cost_changed = (new_cost is not None) and (new_cost != existing_cost)

        # Update ingredient
        unit_value = (request.form.get('unit') or '').strip()
        unit_value = normalize_unit(unit_value) or unit_value

        try:
            cur.execute("""
                UPDATE ingredients
                SET name = %s,
                    category = %s,
                    unit = %s,
                    vendor = %s,
                    vendor_code = %s,
                    g_code = %s,
                    cost_per_unit = %s,
                    price_updated_at = CASE WHEN %s THEN NOW() ELSE price_updated_at END
                WHERE id = %s
            """, (
                request.form.get('name'),
                request.form.get('category'),
                unit_value or None,
                request.form.get('vendor'),
                request.form.get('vendor_code') or None,
                request.form.get('g_code') or None,
                new_cost,
                cost_changed,
                ingredient_id
            ))
            conn.commit()
            cur.close()
            conn.close()
            
            flash('Ingredient updated successfully', 'success')
            return redirect(url_for('ingredients'))
        except Exception:
            conn.rollback()
            cur.close()
            conn.close()
            flash('Error updating ingredient', 'error')
            return redirect(url_for('edit_ingredient', ingredient_id=ingredient_id))
    
    # GET - show edit form
    cur.execute("SELECT * FROM ingredients WHERE id = %s", (ingredient_id,))
    ingredient = cur.fetchone()
    
    cur.close()
    conn.close()
    
    if not ingredient:
        flash('Ingredient not found', 'error')
        return redirect(url_for('ingredients'))
    
    return render_template('edit_ingredient.html', ingredient=ingredient)

@app.route('/ingredients/<ingredient_id>/delete', methods=['POST'])
@login_required
def delete_ingredient(ingredient_id):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("SELECT id, name FROM ingredients WHERE id = %s", (ingredient_id,))
    ingredient = cur.fetchone()
    if not ingredient:
        cur.close()
        conn.close()
        flash('Ingredient not found', 'error')
        return redirect(url_for('ingredients'))

    cur.execute("""
        SELECT COUNT(*) AS usage_count
        FROM recipe_ingredients
        WHERE type = 'ingredient' AND item_id = %s
    """, (ingredient_id,))
    usage = cur.fetchone() or {}
    usage_count = int(usage.get('usage_count') or 0)
    if usage_count > 0:
        cur.close()
        conn.close()
        flash(f"Can't delete {ingredient['name']} — used in {usage_count} recipe(s).", 'error')
        return redirect(url_for('edit_ingredient', ingredient_id=ingredient_id))

    try:
        cur.execute("DELETE FROM ingredients WHERE id = %s", (ingredient_id,))
        conn.commit()
        cur.close()
        conn.close()
        flash('Ingredient deleted', 'success')
        return redirect(url_for('ingredients'))
    except Exception:
        conn.rollback()
        cur.close()
        conn.close()
        flash('Error deleting ingredient', 'error')
        return redirect(url_for('edit_ingredient', ingredient_id=ingredient_id))

if __name__ == '__main__':
    debug_flag = os.getenv('FLASK_DEBUG', '').lower() in ('1', 'true', 'yes')
    debug_env = os.getenv('FLASK_ENV', '').lower() == 'development'
    debug = debug_flag or debug_env
    port = int(os.getenv('PORT', 5000))
    app.run(debug=debug, host='0.0.0.0', port=port)
