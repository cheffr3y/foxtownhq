from db import get_cursor, get_db
from flask import Blueprint, render_template
from flask_login import login_required

from helpers.auth import require_role
from helpers.prep_helpers import ensure_prep_schema, get_prep_aggregation
from helpers.shared import handle_route_error

bp = Blueprint('prep', __name__, url_prefix='/prep')


@bp.before_request
def _require_chef():
    return require_role('chef')


@bp.errorhandler(Exception)
def handle_prep_error(error):
    return handle_route_error(error, 'prep')


@bp.route('/', strict_slashes=False)
@login_required
def prep_index():
    conn = get_db()
    with get_cursor() as cur:
        ensure_prep_schema(cur)
        conn.commit()
        prep_groups = get_prep_aggregation(cur)

    prep_form_count = sum(len(rows) for rows in prep_groups.values())
    return render_template(
        'prep/index.html',
        prep_groups=prep_groups,
        ingredient_count=len(prep_groups),
        prep_form_count=prep_form_count,
    )
