import os
import uuid
import hmac
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import RealDictCursor
from werkzeug.security import check_password_hash

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
        'weight': ['lb', 'oz'],
        'volume': ['gal', 'qt', 'pt', 'cup', 'fl oz', 'tbsp', 'tsp'],
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

RECIPE_TYPE_CHOICES = [
    ('menu', 'Plated (RM)'),
    ('batch', 'Batch (RB)')
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
        'recipe_type_choices': RECIPE_TYPE_CHOICES
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

def build_component_tree(cur, recipe_id, scale_ratio, depth, path, unit_system):
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
                    unit_system
                )
                item['children'] = child_items
                item['sub_total_cost'] = child_cost
                item['cycle'] = child_cycle
                has_cycle = has_cycle or child_cycle
                total_cost += child_cost
            else:
                item['children'] = []
                item['sub_total_cost'] = 0

        components.append(item)

    return components, total_cost, has_cycle

def get_recipe_total_cost(cur, recipe_id, unit_system):
    _, total_cost, _ = build_component_tree(cur, recipe_id, 1, 0, set(), unit_system)
    return total_cost

def parse_menu_items(cur, unit_system, recipe_ids, batch_values):
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

        base_cost = get_recipe_total_cost(cur, recipe_id, unit_system)
        item_total = base_cost * batches
        total_cost += item_total

        menu_items.append({
            'recipe': recipe,
            'recipe_id': recipe_id,
            'batches': batches,
            'base_cost': base_cost,
            'item_total': item_total
        })

    if not menu_items:
        errors.append('Add at least one recipe to calculate a cost.')

    return menu_items, total_cost, errors

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
    q_amount = 0
    grand_total = 0
    rollout_data = {'name': '', 'venue': '', 'year': '', 'quarter': '', 'notes': '', 'q_factor_percent': q_factor_percent}

    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        venue = (request.form.get('venue') or '').strip()
        year_value = (request.form.get('year') or '').strip()
        quarter = normalize_quarter(request.form.get('quarter'))
        notes = (request.form.get('notes') or '').strip()
        q_factor_raw = (request.form.get('q_factor_percent') or '').strip()
        q_factor_percent = q_factor_raw if q_factor_raw else q_factor_percent

        rollout_data = {
            'name': name,
            'venue': venue,
            'year': year_value,
            'quarter': quarter,
            'notes': notes,
            'q_factor_percent': q_factor_percent
        }

        year = int(year_value) if year_value.isdigit() else None
        if not name and venue and year and quarter:
            name = f"{venue} {quarter} {year}"

        recipe_ids = request.form.getlist('menu_recipe_id[]')
        batch_values = request.form.getlist('menu_batches[]')
        menu_items, total_cost, errors = parse_menu_items(cur, unit_system, recipe_ids, batch_values)
        q_factor_percent, q_amount, grand_total = compute_q_factor(total_cost, q_factor_percent)
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
            rollout_id = generate_id('menu_')
            try:
                cur.execute("""
                    INSERT INTO menu_rollouts (id, name, venue, year, quarter, notes, q_factor_percent, is_one_off)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, FALSE)
                """, (
                    rollout_id,
                    name,
                    venue or None,
                    year,
                    quarter or None,
                    notes or None,
                    to_float(q_factor_percent)
                ))

                for item in menu_items:
                    cur.execute("""
                        INSERT INTO menu_rollout_items (id, rollout_id, recipe_id, batches)
                        VALUES (%s, %s, %s, %s)
                    """, (
                        generate_id('mri_'),
                        rollout_id,
                        item['recipe_id'],
                        item['batches']
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
        total_cost=total_cost,
        q_factor_percent=q_factor_percent,
        q_amount=q_amount,
        grand_total=grand_total,
        ingredient_master=ingredient_master,
        ingredient_total_cost=ingredient_total_cost,
        batch_recipes=batch_recipes
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
    q_amount = 0
    grand_total = 0

    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        venue = (request.form.get('venue') or '').strip()
        year_value = (request.form.get('year') or '').strip()
        quarter = normalize_quarter(request.form.get('quarter'))
        notes = (request.form.get('notes') or '').strip()
        q_factor_raw = (request.form.get('q_factor_percent') or '').strip()
        q_factor_percent = q_factor_raw if q_factor_raw else q_factor_percent

        rollout = dict(rollout)
        rollout.update({
            'name': name,
            'venue': venue,
            'year': year_value,
            'quarter': quarter,
            'notes': notes,
            'q_factor_percent': q_factor_percent
        })

        year = int(year_value) if year_value.isdigit() else None
        if not name and venue and year and quarter:
            name = f"{venue} {quarter} {year}"

        recipe_ids = request.form.getlist('menu_recipe_id[]')
        batch_values = request.form.getlist('menu_batches[]')
        menu_items, total_cost, errors = parse_menu_items(cur, unit_system, recipe_ids, batch_values)
        q_factor_percent, q_amount, grand_total = compute_q_factor(total_cost, q_factor_percent)
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
                        q_factor_percent = %s
                    WHERE id = %s
                """, (
                    name,
                    venue or None,
                    year,
                    quarter or None,
                    notes or None,
                    to_float(q_factor_percent),
                    rollout_id
                ))

                cur.execute("DELETE FROM menu_rollout_items WHERE rollout_id = %s", (rollout_id,))
                for item in menu_items:
                    cur.execute("""
                        INSERT INTO menu_rollout_items (id, rollout_id, recipe_id, batches)
                        VALUES (%s, %s, %s, %s)
                    """, (
                        generate_id('mri_'),
                        rollout_id,
                        item['recipe_id'],
                        item['batches']
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
            SELECT mri.recipe_id, mri.batches, r.name
            FROM menu_rollout_items mri
            JOIN recipes r ON r.id = mri.recipe_id
            WHERE mri.rollout_id = %s
            ORDER BY r.name
        """, (rollout_id,))
        saved_items = cur.fetchall()

        recipe_ids = [row['recipe_id'] for row in saved_items]
        batch_values = [row['batches'] for row in saved_items]
        if recipe_ids:
            menu_items, total_cost, _ = parse_menu_items(cur, unit_system, recipe_ids, batch_values)
            q_factor_percent, q_amount, grand_total = compute_q_factor(total_cost, q_factor_percent)
            ingredient_master, ingredient_total_cost, batch_recipes = build_rollout_breakdown(
                cur,
                unit_system,
                menu_items
            )

    cur.close()
    conn.close()

    return render_template(
        'menu_rollout_form.html',
        mode='edit',
        rollout=rollout,
        recipes=recipes_list,
        menu_items=menu_items,
        total_cost=total_cost,
        q_factor_percent=q_factor_percent,
        q_amount=q_amount,
        grand_total=grand_total,
        ingredient_master=ingredient_master,
        ingredient_total_cost=ingredient_total_cost,
        batch_recipes=batch_recipes
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
    
    components, total_cost, _ = build_component_tree(cur, recipe_id, 1, 0, set(), unit_system)
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
        cost_per_yield=cost_per_yield
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

if __name__ == '__main__':
    debug_flag = os.getenv('FLASK_DEBUG', '').lower() in ('1', 'true', 'yes')
    debug_env = os.getenv('FLASK_ENV', '').lower() == 'development'
    debug = debug_flag or debug_env
    port = int(os.getenv('PORT', 5000))
    app.run(debug=debug, host='0.0.0.0', port=port)
