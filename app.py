import os
from datetime import date, datetime, timedelta

from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash

from blueprints.banquet import bp as banquet_bp
from blueprints.buffet import bp as buffet_bp
from blueprints.commissary import bp as commissary_bp
from blueprints.ingredients import bp as ingredients_bp
from blueprints.menu import bp as menu_bp
from blueprints.recipes import bp as recipes_bp
from config import PRICE_REFRESH_DAYS
from db import get_cursor, init_app as init_db_app
from helpers.auth import get_admin_config
from helpers.banquet import (
    auto_complete_past_banquet_events,
    build_banquet_datasets,
    resolve_banquet_venue,
)
from helpers.dashboard import (
    build_dashboard_view_model,
    get_dashboard_counts,
)
from helpers.shared import inject_helpers
from helpers.units import get_unit_system
from helpers.venues import get_active_venues

load_dotenv()

app = Flask(__name__)
secret_key = os.getenv('FLASK_SECRET_KEY')
if not secret_key:
    raise RuntimeError(
        'FLASK_SECRET_KEY environment variable is required. '
        'Generate one with: python -c "import secrets; print(secrets.token_hex(32))"'
    )
app.config['SECRET_KEY'] = secret_key
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
    today = date.today()
    week_end = today + timedelta(days=6)
    unit_system = get_unit_system()
    with get_cursor() as cur:
        counts = get_dashboard_counts(cur)
        venues = get_active_venues(cur)
        banquet_venue = resolve_banquet_venue(venues)
        selected_venue = banquet_venue.get('id') or ''
        dashboard_view = build_dashboard_view_model(
            cur,
            today,
            week_end,
            selected_venue,
            unit_system,
            counts['stale_price_count'],
        )

    return render_template(
        'dashboard.html',
        recipe_count=counts['recipe_count'],
        ingredient_count=counts['ingredient_count'],
        stale_price_count=counts['stale_price_count'],
        recent_rollout_count=counts['recent_rollout_count'],
        price_refresh_days=PRICE_REFRESH_DAYS,
        today=today,
        week_end=week_end,
        banquet_venue_name=banquet_venue.get('name') or 'Banquets',
        today_events=dashboard_view['today_events'],
        upcoming_events=dashboard_view['upcoming_events'],
        stale_badge_count=counts['stale_price_count'],
        production_board_live=dashboard_view['production_board_live'],
        production_board_active_tasks=dashboard_view['production_board_active_tasks'],
        production_pulse_items=dashboard_view['production_pulse_items'],
        production_pulse_total_batches=dashboard_view['production_pulse_total_batches'],
        production_pulse_total_amount=dashboard_view['production_pulse_total_amount'],
        production_pulse_total_subrecipes=dashboard_view['production_pulse_total_subrecipes'],
        production_pulse_event_count=dashboard_view['production_pulse_event_count'],
        pinned_recipes=dashboard_view['pinned_recipes'],
        recent_recipes=dashboard_view['recent_recipes'],
        system_health_items=dashboard_view['system_health_items'],
    )


@app.route('/search')
@login_required
def search():
    def escape_like(value):
        return value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')

    query = (request.args.get('q') or '').strip()
    recipes = []
    ingredients = []
    events = []

    if query:
        like_query = f'%{escape_like(query)}%'
        with get_cursor() as cur:
            cur.execute(
                """
                SELECT id, name, recipe_type, category
                FROM recipes
                WHERE name ILIKE %s ESCAPE '\\'
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
                WHERE name ILIKE %s ESCAPE '\\'
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
                WHERE e.name ILIKE %s ESCAPE '\\'
                ORDER BY e.event_date DESC NULLS LAST, e.name
                LIMIT 30
                """,
                (like_query,),
            )
            events = cur.fetchall()

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
    today = date.today()
    forecast_end = today + timedelta(days=2)
    with get_cursor() as cur:
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
        if not legacy_endpoint:
            continue

        view_func = flask_app.view_functions[rule.endpoint]
        existing_view = flask_app.view_functions.get(legacy_endpoint)
        if existing_view is not None and existing_view is not view_func:
            continue

        methods = sorted(m for m in rule.methods if m not in {'HEAD', 'OPTIONS'})
        flask_app.add_url_rule(
            rule.rule,
            endpoint=legacy_endpoint,
            view_func=view_func,
            methods=methods,
            defaults=rule.defaults,
        )


register_blueprint_with_legacy_endpoints(app, banquet_bp)
register_blueprint_with_legacy_endpoints(app, buffet_bp)
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
