import os
from datetime import date, timedelta

from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash

from blueprints.forecasting import bp as forecasting_bp
from blueprints.ingredients import bp as ingredients_bp
from blueprints.recipes import bp as recipes_bp
from config import PRICE_REFRESH_DAYS
from db import get_cursor, get_db, init_app as init_db_app
from helpers.auth import get_user_by_id, get_user_by_username, role_required
from helpers.dashboard import (
    BREWPUB_VENUE_ID,
    build_dashboard_view_model,
    get_dashboard_counts,
    list_rd_queue_items,
)
from helpers.shared import inject_helpers
from helpers.units import get_unit_system

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

# Run schema migrations automatically at startup so the app never starts
# with a stale schema regardless of how gunicorn is invoked.
try:
    import scripts.migrate as _migrate
    _migrate.main()
except Exception as _e:
    import logging
    logging.getLogger(__name__).error("Startup migration failed: %s", _e)


class User(UserMixin):
    def __init__(self, id, username, role='cook'):
        self.id = id
        self.username = username
        self.role = role


login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'


@login_manager.user_loader
def load_user(user_id):
    try:
        with get_cursor() as cur:
            row = get_user_by_id(cur, user_id)
        if not row or not row.get('active'):
            return None
        return User(row['id'], row['username'], row['role'])
    except Exception:
        return None


@app.route('/')
def index():
    if current_user.is_authenticated:
        if current_user.role == 'cook':
            return redirect(url_for('recipes.recipes'))
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''

        row = None
        try:
            with get_cursor() as cur:
                row = get_user_by_username(cur, username)
        except Exception:
            pass

        if row and row.get('active') and check_password_hash(row['password_hash'], password):
            user = User(row['id'], row['username'], row['role'])
            login_user(user)
            if user.role == 'cook':
                return redirect(url_for('recipes.recipes'))
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
@role_required('chef')
def dashboard():
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)
    unit_system = get_unit_system()
    conn = get_db()
    with get_cursor() as cur:
        counts = get_dashboard_counts(cur)
        dashboard_view = build_dashboard_view_model(
            cur,
            today,
            week_start,
            week_end,
            unit_system,
            counts,
        )
        conn.commit()

    return render_template(
        'dashboard.html',
        today=today,
        week_start=week_start,
        week_end=week_end,
        price_refresh_days=PRICE_REFRESH_DAYS,
        **counts,
        **dashboard_view,
    )


@app.route('/recipes/rd-queue')
@login_required
@role_required('chef')
def rd_queue():
    with get_cursor() as cur:
        rd_queue_items = list_rd_queue_items(cur)

    return render_template(
        'rd_queue.html',
        rd_queue_items=rd_queue_items,
    )


@app.route('/forecasting/send', methods=['GET', 'POST'])
@login_required
def forecast_send():
    flash('Weekly forecast opened for Foxtown Brewing.', 'info')
    return redirect(url_for('forecasting.forecasting_plan', venue_id=BREWPUB_VENUE_ID))


@app.route('/search')
@login_required
@role_required('chef')
def search():
    def escape_like(value):
        return value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')

    query = (request.args.get('q') or '').strip()
    recipes = []
    ingredients = []

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

    return render_template(
        'search_results.html',
        search_query=query,
        recipes=recipes,
        ingredients=ingredients,
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


register_blueprint_with_legacy_endpoints(app, forecasting_bp)
register_blueprint_with_legacy_endpoints(app, recipes_bp)
register_blueprint_with_legacy_endpoints(app, ingredients_bp)


if __name__ == '__main__':
    debug_flag = os.getenv('FLASK_DEBUG', '').lower() in ('1', 'true', 'yes')
    debug_env = os.getenv('FLASK_ENV', '').lower() == 'development'
    debug = debug_flag or debug_env
    port = int(os.getenv('PORT', 5000))
    app.run(debug=debug, host='0.0.0.0', port=port)
