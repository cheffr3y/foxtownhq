import os
from datetime import date, datetime, timedelta

from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from psycopg2.extras import RealDictCursor
from werkzeug.security import check_password_hash

from blueprints.banquet import bp as banquet_bp
from blueprints.commissary import bp as commissary_bp
from blueprints.ingredients import bp as ingredients_bp
from blueprints.menu import bp as menu_bp
from blueprints.recipes import bp as recipes_bp
from config import PRICE_REFRESH_DAYS
from db import get_db, init_app as init_db_app
from helpers.common import (
    auto_complete_past_banquet_events,
    build_banquet_datasets,
    get_active_venues,
    get_admin_config,
    get_unit_system,
    inject_helpers,
    resolve_banquet_venue,
)

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY')
init_db_app(app)
app.context_processor(inject_helpers)


class User(UserMixin):
    def __init__(self, id, username):
        self.id = id
        self.username = username


login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'


@login_manager.user_loader
def load_user(user_id):
    config = get_admin_config()
    username = config['username'] if config else 'admin'
    return User(user_id, username)


@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    from flask import request

    if request.method == 'POST':
        config = get_admin_config()
        if not config:
            flash('Admin credentials are not configured yet.', 'error')
            return render_template('login.html')

        username = request.form.get('username')
        password = request.form.get('password')

        is_user_match = username == config['username']
        is_pass_match = check_password_hash(config['password_hash'], password or '')

        if is_user_match and is_pass_match:
            user = User('1', username)
            login_user(user)
            return redirect(url_for('dashboard'))

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
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute('SELECT COUNT(*) AS count FROM recipes')
    recipe_count = cur.fetchone()['count']

    cur.execute('SELECT COUNT(*) AS count FROM ingredients')
    ingredient_count = cur.fetchone()['count']

    stale_cutoff = datetime.utcnow() - timedelta(days=PRICE_REFRESH_DAYS)
    cur.execute(
        '''
        SELECT COUNT(*) AS count
        FROM ingredients
        WHERE price_updated_at IS NULL OR price_updated_at < %s
        ''',
        (stale_cutoff,),
    )
    stale_price_count = cur.fetchone()['count']

    recent_cutoff = datetime.utcnow() - timedelta(days=30)
    cur.execute(
        '''
        SELECT COUNT(*) AS count
        FROM menu_rollouts
        WHERE is_one_off = FALSE
          AND created_at >= %s
        ''',
        (recent_cutoff,),
    )
    recent_rollout_count = cur.fetchone()['count']

    today = date.today()
    week_end = today + timedelta(days=6)
    venues = get_active_venues(cur)
    banquet_venue = resolve_banquet_venue(venues)
    selected_venue = banquet_venue.get('id') or ''
    unit_system = get_unit_system()
    auto_complete_past_banquet_events(cur, selected_venue)
    datasets = build_banquet_datasets(cur, today, week_end, selected_venue, unit_system)

    def as_int(value):
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def as_float(value):
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    def format_quantity(value):
        number = as_float(value)
        if abs(number - round(number)) < 1e-9:
            return f"{int(round(number)):,}"
        return f"{number:,.2f}".rstrip('0').rstrip('.')

    def add_amount(accumulator, quantity, unit):
        qty = as_float(quantity)
        if qty <= 0:
            return
        clean_unit = (unit or '').strip() or 'units'
        key = clean_unit.lower()
        if key not in accumulator:
            accumulator[key] = {
                'unit': clean_unit,
                'qty': 0.0,
            }
        accumulator[key]['qty'] += qty

    def amount_label(accumulator, max_parts=2):
        if not accumulator:
            return '0 units'
        ordered = sorted(accumulator.values(), key=lambda item: (-as_float(item.get('qty')), (item.get('unit') or '').lower()))
        parts = [f"{format_quantity(item.get('qty'))} {item.get('unit')}" for item in ordered[:max_parts]]
        if len(ordered) > max_parts:
            parts.append(f"+{len(ordered) - max_parts} more")
        return ' · '.join(parts)

    def event_status_tone(status):
        key = (status or '').strip().lower()
        if key in ('confirmed', 'completed', 'executed'):
            return 'emerald'
        if key in ('cancelled', 'blocked'):
            return 'rose'
        return 'amber'

    def estimate_prep_completion(event):
        status_key = (event.get('status') or '').strip().lower()
        if status_key in ('completed', 'executed'):
            return 100
        if status_key in ('cancelled',):
            return 0

        line_count = as_int(event.get('line_count'))
        guest_count = max(1, as_int(event.get('guests')))
        expected_line_count = max(1, round(guest_count / 26))
        coverage_ratio = min(1.0, line_count / expected_line_count) if expected_line_count else 0.0

        if status_key == 'confirmed':
            return int(max(42, min(97, round(55 + (coverage_ratio * 42)))))
        return int(max(10, min(84, round(18 + (coverage_ratio * 62)))))

    def enrich_event(event):
        event_copy = dict(event)
        status_key = (event_copy.get('status') or 'planning').strip().lower()
        event_copy['status'] = status_key
        event_copy['status_tone'] = event_status_tone(status_key)
        event_copy['prep_completion'] = estimate_prep_completion(event_copy)
        event_copy['is_confirmed'] = status_key == 'confirmed'
        return event_copy

    today_events = [enrich_event(event) for event in datasets.get('events', []) if event.get('event_date') == today]
    upcoming_events = [
        enrich_event(event) for event in datasets.get('events', [])
        if event.get('event_date') and today < event.get('event_date') <= week_end
    ]

    events_with_missing_menus = [
        event for event in today_events + upcoming_events
        if event.get('event_date')
        and event.get('event_date') <= (today + timedelta(days=3))
        and int(event.get('line_count') or 0) == 0
    ]

    # Production Board live pulse (next 48h in selected venue)
    board_window_end = today + timedelta(days=2)
    board_datasets = build_banquet_datasets(cur, today, board_window_end, selected_venue, unit_system)
    production_board_active_tasks = sum(
        as_int(event.get('line_count'))
        for event in board_datasets.get('events', [])
        if (event.get('status') or '').strip().lower() not in ('completed', 'executed', 'cancelled')
    )
    production_board_live = production_board_active_tasks > 0

    # Production Pulse: aggregate sub-recipes for next 48h across all banquets.
    pulse_datasets = build_banquet_datasets(cur, today, board_window_end, '', unit_system)
    pulse_map = {}
    for prep in pulse_datasets.get('weekly_prep', []) or []:
        root_name = prep.get('recipe_name') or 'Prep Item'
        sub_rows = prep.get('subrecipe_rows') or []

        if not sub_rows and prep.get('recipe_id'):
            fallback_key = f"root:{prep.get('recipe_id')}"
            if fallback_key not in pulse_map:
                pulse_map[fallback_key] = {
                    'recipe_id': prep.get('recipe_id'),
                    'recipe_name': prep.get('recipe_name') or 'Prep Item',
                    'required_batches': 0.0,
                    'required_amounts': {},
                    'used_in_preps': set(),
                }
            pulse_map[fallback_key]['required_batches'] += as_float(prep.get('required_batches'))
            add_amount(
                pulse_map[fallback_key]['required_amounts'],
                prep.get('required_qty'),
                prep.get('required_unit') or prep.get('yield_unit'),
            )
            pulse_map[fallback_key]['used_in_preps'].add(root_name)

        for sub in sub_rows:
            recipe_id = sub.get('recipe_id')
            recipe_name = sub.get('recipe_name') or 'Sub-recipe'
            if recipe_id:
                pulse_key = f"sub:{recipe_id}"
            else:
                pulse_key = f"sub-name:{recipe_name.strip().lower()}"
            if pulse_key not in pulse_map:
                pulse_map[pulse_key] = {
                    'recipe_id': recipe_id,
                    'recipe_name': recipe_name,
                    'required_batches': 0.0,
                    'required_amounts': {},
                    'used_in_preps': set(),
                }
            pulse_map[pulse_key]['required_batches'] += as_float(sub.get('required_batches'))
            add_amount(
                pulse_map[pulse_key]['required_amounts'],
                sub.get('required_qty'),
                sub.get('required_unit') or sub.get('yield_unit'),
            )
            pulse_map[pulse_key]['used_in_preps'].add(root_name)

    production_pulse_items = []
    production_pulse_amounts = {}
    for row in pulse_map.values():
        row_amounts = row.get('required_amounts') or {}
        for amount in row_amounts.values():
            add_amount(production_pulse_amounts, amount.get('qty'), amount.get('unit'))
        production_pulse_items.append({
            'recipe_id': row.get('recipe_id'),
            'recipe_name': row.get('recipe_name'),
            'required_batches': round(as_float(row.get('required_batches')), 2),
            'required_amount_label': amount_label(row_amounts),
            'used_in_preps': sorted(row.get('used_in_preps') or []),
        })
    production_pulse_items.sort(key=lambda item: (-as_float(item.get('required_batches')), (item.get('recipe_name') or '').lower()))
    production_pulse_total_batches = round(sum(as_float(item.get('required_batches')) for item in production_pulse_items), 2)
    production_pulse_total_amount = amount_label(production_pulse_amounts, max_parts=3)
    production_pulse_total_subrecipes = len(production_pulse_items)
    production_pulse_items = production_pulse_items[:8]
    production_pulse_event_count = as_int(pulse_datasets.get('event_count'))

    # Pinned recipes = most-used in active week window; Recent = fallback quick access.
    recipe_usage = {}
    for event in datasets.get('events', []) or []:
        for line in event.get('lines', []) or []:
            line_recipes = line.get('recipes') or ([line.get('recipe')] if line.get('recipe') else [])
            for line_recipe in line_recipes:
                recipe_id = line_recipe.get('id')
                if not recipe_id:
                    continue
                if recipe_id not in recipe_usage:
                    recipe_usage[recipe_id] = {
                        'id': recipe_id,
                        'name': line_recipe.get('name') or line.get('menu_item_name') or 'Recipe',
                        'hit_count': 0,
                        'source': 'Pinned',
                    }
                recipe_usage[recipe_id]['hit_count'] += 1

    pinned_recipes = sorted(
        recipe_usage.values(),
        key=lambda row: (-as_int(row.get('hit_count')), (row.get('name') or '').lower())
    )[:8]
    pinned_recipe_ids = {row.get('id') for row in pinned_recipes if row.get('id')}

    cur.execute("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'recipes'
          AND column_name = ANY(%s)
    """, (['updated_at', 'created_at'],))
    recipe_columns = {row.get('column_name') for row in cur.fetchall()}
    if 'updated_at' in recipe_columns and 'created_at' in recipe_columns:
        recent_order_expr = 'COALESCE(updated_at, created_at)'
    elif 'updated_at' in recipe_columns:
        recent_order_expr = 'updated_at'
    elif 'created_at' in recipe_columns:
        recent_order_expr = 'created_at'
    else:
        recent_order_expr = 'id'

    cur.execute(f"""
        SELECT id, name, recipe_type, category
        FROM recipes
        ORDER BY {recent_order_expr} DESC NULLS LAST, name
        LIMIT 16
    """)
    recent_recipes = []
    for row in cur.fetchall():
        if row.get('id') in pinned_recipe_ids:
            continue
        recent_recipes.append({
            'id': row.get('id'),
            'name': row.get('name') or 'Recipe',
            'recipe_type': row.get('recipe_type') or 'other',
            'category': row.get('category') or '',
            'source': 'Recent',
        })
        if len(recent_recipes) >= 8:
            break

    system_health_items = []
    if production_board_live:
        system_health_items.append({
            'label': 'Production Board Live',
            'count_text': str(production_board_active_tasks),
            'detail': 'active prep tasks',
            'tone': 'emerald',
            'href': url_for('production_board'),
        })
    if events_with_missing_menus:
        first_event = events_with_missing_menus[0]
        system_health_items.append({
            'label': 'Events Missing Menu Items',
            'count_text': str(len(events_with_missing_menus)),
            'detail': 'Events within 72 hours have no menu lines attached.',
            'href': url_for('banquet_event_edit', event_id=first_event.get('id')),
            'tone': 'amber',
        })
    if stale_price_count:
        system_health_items.append({
            'label': 'Ingredient Price Refresh Needed',
            'count_text': str(stale_price_count),
            'detail': f'Ingredients are older than {PRICE_REFRESH_DAYS or 56} days.',
            'href': url_for('ingredients', needs_update='1'),
            'tone': 'rose',
        })
    if not system_health_items:
        system_health_items.append({
            'label': 'System Health',
            'count_text': 'All Clear',
            'detail': 'No urgent pricing or coverage risks in the next 72 hours.',
            'href': url_for('dashboard'),
            'tone': 'emerald',
        })

    cur.close()

    return render_template(
        'dashboard.html',
        recipe_count=recipe_count,
        ingredient_count=ingredient_count,
        stale_price_count=stale_price_count,
        recent_rollout_count=recent_rollout_count,
        price_refresh_days=PRICE_REFRESH_DAYS,
        today=today,
        week_end=week_end,
        banquet_venue_name=banquet_venue.get('name') or 'Banquets',
        today_events=today_events,
        upcoming_events=upcoming_events,
        stale_badge_count=stale_price_count,
        production_board_live=production_board_live,
        production_board_active_tasks=production_board_active_tasks,
        production_pulse_items=production_pulse_items,
        production_pulse_total_batches=production_pulse_total_batches,
        production_pulse_total_amount=production_pulse_total_amount,
        production_pulse_total_subrecipes=production_pulse_total_subrecipes,
        production_pulse_event_count=production_pulse_event_count,
        pinned_recipes=pinned_recipes,
        recent_recipes=recent_recipes,
        system_health_items=system_health_items,
    )


@app.route('/search')
@login_required
def search():
    query = (request.args.get('q') or '').strip()
    recipes = []
    ingredients = []
    events = []

    if query:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        like_query = f'%{query}%'

        cur.execute(
            """
            SELECT id, name, recipe_type, category
            FROM recipes
            WHERE name ILIKE %s
            ORDER BY name
            LIMIT 30
            """,
            (like_query,),
        )
        recipes = cur.fetchall()

        cur.execute(
            """
            SELECT id, name, category, unit
            FROM ingredients
            WHERE name ILIKE %s
            ORDER BY name
            LIMIT 30
            """,
            (like_query,),
        )
        ingredients = cur.fetchall()

        cur.execute(
            """
            SELECT
                e.id,
                e.name,
                e.event_date,
                e.guest_count AS guests,
                e.status,
                COALESCE(v.name, e.building, 'Banquet') AS venue_name
            FROM banquet_events e
            LEFT JOIN venues v ON v.id = e.venue_id
            WHERE e.name ILIKE %s
            ORDER BY e.event_date DESC NULLS LAST, e.name
            LIMIT 30
            """,
            (like_query,),
        )
        events = cur.fetchall()
        cur.close()

    return render_template(
        'search_results.html',
        search_query=query,
        recipes=recipes,
        ingredients=ingredients,
        events=events,
    )


@app.route('/production-board')
@login_required
def production_board():
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    today = date.today()
    forecast_end = today + timedelta(days=2)
    venues = get_active_venues(cur)
    banquet_venue = resolve_banquet_venue(venues)
    selected_venue = banquet_venue.get('id') or ''

    auto_complete_past_banquet_events(cur, selected_venue)
    datasets = build_banquet_datasets(cur, today, forecast_end, selected_venue, get_unit_system())
    forecast_events = datasets.get('events', [])
    max_line_count = max((int(event.get('line_count') or 0) for event in forecast_events), default=0)
    max_guest_count = max((int(event.get('guests') or 0) for event in forecast_events), default=0)

    day_columns = []
    for offset in range(3):
        day = today + timedelta(days=offset)
        label = 'Active Today' if offset == 0 else ('Tomorrow' if offset == 1 else 'Day +2')
        events = [event for event in forecast_events if event.get('event_date') == day]

        enriched_events = []
        for event in events:
            guest_count = int(event.get('guests') or 0)
            line_count = int(event.get('line_count') or 0)
            progress_percent = 0
            if max_line_count > 0 and line_count > 0:
                progress_percent = max(14, min(100, round((line_count / max_line_count) * 100)))

            if guest_count >= 200:
                volume_tone = 'critical'
            elif guest_count >= 100:
                volume_tone = 'high'
            elif guest_count >= 50:
                volume_tone = 'medium'
            else:
                volume_tone = 'low'

            if max_guest_count > 0 and guest_count > 0:
                guest_scale = 1 + (guest_count / max_guest_count) * 0.9
            else:
                guest_scale = 1

            event_copy = dict(event)
            event_copy['guest_count'] = guest_count
            event_copy['line_count'] = line_count
            event_copy['progress_percent'] = progress_percent
            event_copy['volume_tone'] = volume_tone
            event_copy['guest_scale'] = round(guest_scale, 2)
            event_copy['line_preview'] = (event.get('lines') or [])[:4]
            event_copy['remaining_line_count'] = max(0, line_count - 4)
            event_copy['is_large_warning'] = offset == 2 and guest_count >= 100
            enriched_events.append(event_copy)

        day_columns.append({
            'offset': offset,
            'label': label,
            'date': day,
            'events': enriched_events,
            'event_count': len(enriched_events),
            'guest_total': sum(event.get('guest_count') or 0 for event in enriched_events),
            'line_total': sum(event.get('line_count') or 0 for event in enriched_events),
        })

    cur.close()

    return render_template(
        'production_board.html',
        today=today,
        current_time=datetime.now(),
        venue_name=banquet_venue.get('name') or 'Banquets',
        day_columns=day_columns,
        last_updated=datetime.now(),
    )


def register_blueprint_with_legacy_endpoints(flask_app, blueprint):
    flask_app.register_blueprint(blueprint)
    prefix = f'{blueprint.name}.'
    for rule in list(flask_app.url_map.iter_rules()):
        if not rule.endpoint.startswith(prefix):
            continue

        legacy_endpoint = rule.endpoint[len(prefix) :]
        if not legacy_endpoint or legacy_endpoint in flask_app.view_functions:
            continue

        methods = sorted(m for m in rule.methods if m not in {'HEAD', 'OPTIONS'})
        flask_app.add_url_rule(
            rule.rule,
            endpoint=legacy_endpoint,
            view_func=flask_app.view_functions[rule.endpoint],
            methods=methods,
        )


register_blueprint_with_legacy_endpoints(app, banquet_bp)
register_blueprint_with_legacy_endpoints(app, commissary_bp)
register_blueprint_with_legacy_endpoints(app, recipes_bp)
register_blueprint_with_legacy_endpoints(app, ingredients_bp)
register_blueprint_with_legacy_endpoints(app, menu_bp)


if __name__ == '__main__':
    debug_flag = os.getenv('FLASK_DEBUG', '').lower() in ('1', 'true', 'yes')
    debug_env = os.getenv('FLASK_ENV', '').lower() == 'development'
    debug = debug_flag or debug_env
    port = int(os.getenv('PORT', 5000))
    app.run(debug=debug, host='0.0.0.0', port=port)
