import os
from datetime import date, datetime, timedelta

from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from psycopg2.extras import RealDictCursor
from werkzeug.security import check_password_hash

from blueprints.banquet import bp as banquet_bp
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
    auto_complete_past_banquet_events(cur, selected_venue)
    datasets = build_banquet_datasets(cur, today, week_end, selected_venue, get_unit_system())

    today_events = [event for event in datasets.get('events', []) if event.get('event_date') == today]
    upcoming_events = [
        event for event in datasets.get('events', [])
        if event.get('event_date') and today < event.get('event_date') <= week_end
    ]

    events_with_missing_menus = [
        event for event in datasets.get('events', [])
        if event.get('event_date')
        and event.get('event_date') <= (today + timedelta(days=3))
        and int(event.get('line_count') or 0) == 0
    ]

    attention_flags = []
    if events_with_missing_menus:
        first_event = events_with_missing_menus[0]
        attention_flags.append({
            'title': 'Events Missing Menu Items',
            'count_text': str(len(events_with_missing_menus)),
            'detail': 'Events within 72 hours have no menu lines attached.',
            'href': url_for('banquet_event_edit', event_id=first_event.get('id')),
            'tone': 'amber',
        })
    if stale_price_count:
        attention_flags.append({
            'title': 'Ingredient Price Refresh Needed',
            'count_text': str(stale_price_count),
            'detail': f'Ingredients are older than {PRICE_REFRESH_DAYS or 56} days.',
            'href': url_for('ingredients'),
            'tone': 'slate',
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
        attention_flags=attention_flags,
    )


@app.route('/production-board')
@login_required
def production_board():
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    today = date.today()
    venues = get_active_venues(cur)
    banquet_venue = resolve_banquet_venue(venues)
    selected_venue = banquet_venue.get('id') or ''

    auto_complete_past_banquet_events(cur, selected_venue)
    datasets = build_banquet_datasets(cur, today, today, selected_venue, get_unit_system())
    today_events = datasets.get('events', [])

    cur.close()

    return render_template(
        'production_board.html',
        today=today,
        current_time=datetime.now(),
        venue_name=banquet_venue.get('name') or 'Banquets',
        today_events=today_events,
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
register_blueprint_with_legacy_endpoints(app, recipes_bp)
register_blueprint_with_legacy_endpoints(app, ingredients_bp)
register_blueprint_with_legacy_endpoints(app, menu_bp)


if __name__ == '__main__':
    debug_flag = os.getenv('FLASK_DEBUG', '').lower() in ('1', 'true', 'yes')
    debug_env = os.getenv('FLASK_ENV', '').lower() == 'development'
    debug = debug_flag or debug_env
    port = int(os.getenv('PORT', 5000))
    app.run(debug=debug, host='0.0.0.0', port=port)
