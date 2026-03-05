from datetime import datetime, timezone

from config import PRICE_REFRESH_DAYS
from db import get_cursor, get_db
from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for
from flask_login import login_required
from helpers.shared import generate_id, handle_route_error, to_float
from helpers.units import normalize_unit

bp = Blueprint('ingredients', __name__)


def escape_like(value):
    return value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


@bp.errorhandler(Exception)
def handle_ingredients_error(error):
    return handle_route_error(error, 'ingredients')


@bp.route('/ingredients')
@login_required
def ingredients():
    only_needs_update = (request.args.get('needs_update') or '').strip().lower() in ('1', 'true', 'yes', 'on')

    with get_cursor() as cur:
        cur.execute(
            """
            SELECT * FROM ingredients
            ORDER BY category, name
            """
        )
        ingredients_list = cur.fetchall()

    now = datetime.now(timezone.utc)
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
    if only_needs_update:
        ingredients_list = [ingredient for ingredient in ingredients_list if ingredient.get('needs_price_update')]

    return render_template(
        'ingredients.html',
        ingredients=ingredients_list,
        needs_update_count=needs_update_count,
        price_refresh_days=PRICE_REFRESH_DAYS,
        only_needs_update=only_needs_update,
    )


@bp.route('/ingredients/new', methods=['GET', 'POST'])
@login_required
def new_ingredient():
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
            conn = get_db()

            try:
                with get_cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO ingredients (id, name, category, unit, vendor, vendor_code, g_code, cost_per_unit, price_updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CASE WHEN %s THEN NOW() ELSE NULL END)
                        """,
                        (
                            generate_id('ing_'),
                            name,
                            request.form.get('category') or None,
                            unit_value or None,
                            request.form.get('vendor') or None,
                            request.form.get('vendor_code') or None,
                            request.form.get('g_code') or None,
                            cost_value,
                            price_updated,
                        ),
                    )
                    conn.commit()
                flash('Ingredient created', 'success')
                return redirect(url_for('ingredients'))
            except Exception:
                conn.rollback()
                flash('Error saving ingredient', 'error')

    return render_template(
        'new_ingredient.html',
        ingredient={
            'name': '',
            'category': '',
            'unit': '',
            'vendor': '',
            'vendor_code': '',
            'g_code': '',
            'cost_per_unit': '',
        },
    )


@bp.route('/ingredients/<ingredient_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_ingredient(ingredient_id):
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        if not name:
            flash('Ingredient name is required', 'error')
            return redirect(url_for('edit_ingredient', ingredient_id=ingredient_id))

        conn = get_db()
        with get_cursor() as cur:
            cur.execute("SELECT cost_per_unit FROM ingredients WHERE id = %s", (ingredient_id,))
            existing = cur.fetchone() or {}
        existing_cost = to_float(existing.get('cost_per_unit'))
        new_cost_raw = request.form.get('cost_per_unit')
        new_cost = to_float(new_cost_raw) if new_cost_raw not in (None, '') else None
        cost_changed = (new_cost is not None) and (new_cost != existing_cost)

        unit_value = (request.form.get('unit') or '').strip()
        unit_value = normalize_unit(unit_value) or unit_value

        try:
            with get_cursor() as cur:
                cur.execute(
                    """
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
                    """,
                    (
                        request.form.get('name'),
                        request.form.get('category'),
                        unit_value or None,
                        request.form.get('vendor'),
                        request.form.get('vendor_code') or None,
                        request.form.get('g_code') or None,
                        new_cost,
                        cost_changed,
                        ingredient_id,
                    ),
                )
                conn.commit()

            flash('Ingredient updated successfully', 'success')
            return redirect(url_for('ingredients'))
        except Exception:
            conn.rollback()
            flash('Error updating ingredient', 'error')
            return redirect(url_for('edit_ingredient', ingredient_id=ingredient_id))

    with get_cursor() as cur:
        cur.execute("SELECT * FROM ingredients WHERE id = %s", (ingredient_id,))
        ingredient = cur.fetchone()

    if not ingredient:
        flash('Ingredient not found', 'error')
        return redirect(url_for('ingredients'))

    return render_template('edit_ingredient.html', ingredient=ingredient)


@bp.route('/ingredients/<ingredient_id>/delete', methods=['POST'])
@login_required
def delete_ingredient(ingredient_id):
    conn = get_db()

    with get_cursor() as cur:
        cur.execute("SELECT id, name FROM ingredients WHERE id = %s", (ingredient_id,))
        ingredient = cur.fetchone()
        if not ingredient:
            flash('Ingredient not found', 'error')
            return redirect(url_for('ingredients'))

        cur.execute(
            """
            SELECT COUNT(*) AS usage_count
            FROM recipe_ingredients
            WHERE type = 'ingredient' AND item_id = %s
            """,
            (ingredient_id,),
        )
        usage = cur.fetchone() or {}
    usage_count = int(usage.get('usage_count') or 0)
    if usage_count > 0:
        flash(f"Can't delete {ingredient['name']} — used in {usage_count} recipe(s).", 'error')
        return redirect(url_for('edit_ingredient', ingredient_id=ingredient_id))

    try:
        with get_cursor() as cur:
            cur.execute("DELETE FROM ingredients WHERE id = %s", (ingredient_id,))
            conn.commit()
        flash('Ingredient deleted', 'success')
        return redirect(url_for('ingredients'))
    except Exception:
        conn.rollback()
        flash('Error deleting ingredient', 'error')


@bp.route('/api/ingredients/search')
@login_required
def api_ingredients_search():
    query = (request.args.get('q') or '').strip()
    try:
        limit = int(request.args.get('limit', 20))
    except (TypeError, ValueError):
        limit = 20
    limit = min(max(limit, 1), 100)

    like_query = f"%{escape_like(query)}%"
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT id, name, unit, category, vendor
            FROM ingredients
            WHERE (%s = '' OR name ILIKE %s ESCAPE '\\')
            ORDER BY name
            LIMIT %s
            """,
            (query, like_query, limit),
        )
        results = cur.fetchall()

    return jsonify({'results': results})
