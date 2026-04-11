from db import get_cursor, get_db
from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for
from flask_login import login_required

from helpers.forecasting import (
    ensure_forecasting_schema,
    ensure_recipe_venue_link,
    get_active_menu_recipe_ids,
    get_existing_menu_items_by_recipe,
    get_forecasting_menu_item,
    group_forecasting_items_by_category,
    list_forecasting_menu_items,
    list_forecasting_recipes,
    search_forecasting_recipes,
    summarize_forecasting_items,
)
from helpers.shared import generate_id, handle_route_error
from helpers.venues import get_active_venues

bp = Blueprint('forecasting', __name__)


@bp.errorhandler(Exception)
def handle_forecasting_error(error):
    return handle_route_error(error, 'forecasting')


def _resolve_selected_venue(venues, requested_id=''):
    requested = (requested_id or '').strip()
    selected = None
    if requested:
        selected = next((venue for venue in venues if venue.get('id') == requested), None)
    if not selected and venues:
        selected = venues[0]
    return selected or {}


def _dashboard_redirect(venue_id=''):
    if venue_id:
        return redirect(url_for('forecasting.forecasting_dashboard', venue_id=venue_id))
    return redirect(url_for('forecasting.forecasting_dashboard'))


def _wants_json_response():
    return 'application/json' in (request.headers.get('Accept') or '').lower()


def _normalize_id_list(values):
    normalized = []
    seen = set()
    for value in values or []:
        candidate = (value or '').strip()
        if candidate and candidate not in seen:
            normalized.append(candidate)
            seen.add(candidate)
    return normalized


def _render_add_page(cur, selected_venue_id='', errors=None, selected_recipe_ids=None):
    venues = [dict(row) for row in get_active_venues(cur)]
    selected_venue = _resolve_selected_venue(venues, selected_venue_id)
    effective_venue_id = selected_venue.get('id') or ''
    recipes = list_forecasting_recipes(cur)
    already_added_ids = get_active_menu_recipe_ids(cur, effective_venue_id)
    return render_template(
        'forecasting_add.html',
        venues=venues,
        selected_venue=selected_venue,
        selected_venue_id=effective_venue_id,
        recipes=recipes,
        already_added_ids=already_added_ids,
        selected_recipe_ids=selected_recipe_ids or [],
        errors=errors or [],
    )


@bp.route('/forecasting')
@login_required
def forecasting_dashboard():
    conn = get_db()
    with get_cursor() as cur:
        ensure_forecasting_schema(cur)
        conn.commit()
        venues = [dict(row) for row in get_active_venues(cur)]
        selected_venue = _resolve_selected_venue(venues, request.args.get('venue_id'))
        selected_venue_id = selected_venue.get('id') or ''
        items = list_forecasting_menu_items(cur, selected_venue_id)

    active_items = [item for item in items if bool(item.get('active'))]
    archived_items = [item for item in items if not bool(item.get('active'))]
    summary = summarize_forecasting_items(items)
    venue_tabs = [
        {
            'id': venue.get('id') or '',
            'label': venue.get('name') or 'Venue',
            'url': url_for('forecasting.forecasting_dashboard', venue_id=venue.get('id') or ''),
        }
        for venue in venues
        if venue.get('id')
    ]

    return render_template(
        'forecasting_dashboard.html',
        venues=venues,
        venue_tabs=venue_tabs,
        selected_venue=selected_venue,
        selected_venue_id=selected_venue_id,
        active_items_by_category=group_forecasting_items_by_category(active_items),
        archived_items=archived_items,
        active_count=summary['active_count'],
        archived_count=summary['archived_count'],
        with_recipe=summary['with_recipe'],
        missing_recipe=summary['missing_recipe'],
        completion_pct=summary['completion_pct'],
    )


@bp.route('/forecasting/add', methods=['GET', 'POST'])
@login_required
def forecasting_add():
    conn = get_db()
    with get_cursor() as cur:
        ensure_forecasting_schema(cur)
        conn.commit()

        if request.method == 'POST':
            venues = [dict(row) for row in get_active_venues(cur)]
            valid_venue_ids = {venue.get('id') for venue in venues if venue.get('id')}
            venue_id = (request.form.get('venue_id') or '').strip()
            selected_recipe_ids = _normalize_id_list(request.form.getlist('recipe_ids[]'))
            errors = []

            if not venue_id:
                errors.append('Venue is required.')
            elif venue_id not in valid_venue_ids:
                errors.append('Selected venue was invalid.')

            if not selected_recipe_ids:
                errors.append('Select at least one recipe to add.')

            if errors:
                return _render_add_page(
                    cur,
                    selected_venue_id=venue_id,
                    errors=sorted(set(errors)),
                    selected_recipe_ids=selected_recipe_ids,
                )

            cur.execute(
                """
                SELECT
                    id,
                    name,
                    COALESCE(NULLIF(TRIM(category), ''), 'Uncategorized') AS category,
                    menu_descriptor
                FROM recipes
                WHERE id = ANY(%s)
                """,
                (selected_recipe_ids,),
            )
            recipe_map = {row['id']: dict(row) for row in cur.fetchall()}
            valid_recipe_ids = [recipe_id for recipe_id in selected_recipe_ids if recipe_id in recipe_map]

            if not valid_recipe_ids:
                return _render_add_page(
                    cur,
                    selected_venue_id=venue_id,
                    errors=['Selected recipes were invalid.'],
                    selected_recipe_ids=selected_recipe_ids,
                )

            existing_by_recipe = get_existing_menu_items_by_recipe(cur, venue_id, valid_recipe_ids)
            added_count = 0

            try:
                for recipe_id in valid_recipe_ids:
                    recipe = recipe_map[recipe_id]
                    existing = existing_by_recipe.get(recipe_id)
                    if existing and existing.get('active'):
                        continue

                    if existing:
                        cur.execute(
                            """
                            UPDATE forecasting_menu_items
                            SET
                                name = %s,
                                category = %s,
                                description = %s,
                                active = TRUE,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = %s
                            """,
                            (
                                recipe.get('name') or 'Menu Item',
                                recipe.get('category') or 'Uncategorized',
                                recipe.get('menu_descriptor') or None,
                                existing['id'],
                            ),
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO forecasting_menu_items (
                                id,
                                name,
                                category,
                                venue_id,
                                description,
                                recipe_id,
                                sort_order,
                                active
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, 0, TRUE)
                            """,
                            (
                                generate_id('fmi_'),
                                recipe.get('name') or 'Menu Item',
                                recipe.get('category') or 'Uncategorized',
                                venue_id,
                                recipe.get('menu_descriptor') or None,
                                recipe_id,
                            ),
                        )

                    ensure_recipe_venue_link(cur, recipe_id, venue_id)
                    added_count += 1

                conn.commit()
                flash(f'{added_count} items added to menu.', 'success')
                return _dashboard_redirect(venue_id)
            except Exception:
                conn.rollback()
                return _render_add_page(
                    cur,
                    selected_venue_id=venue_id,
                    errors=['Error adding recipes to the menu.'],
                    selected_recipe_ids=selected_recipe_ids,
                )

        return _render_add_page(cur, selected_venue_id=request.args.get('venue_id'))


@bp.route('/forecasting/menu-items/<item_id>/toggle', methods=['POST'])
@login_required
def forecasting_menu_item_toggle(item_id):
    conn = get_db()
    with get_cursor() as cur:
        ensure_forecasting_schema(cur)
        conn.commit()
        item = get_forecasting_menu_item(cur, item_id)
        venue_id = (request.form.get('venue_id') or '').strip() or (item.get('venue_id') if item else '')

        if not item:
            if _wants_json_response():
                return jsonify({'ok': False, 'error': 'Item not found.'}), 404
            flash('Item not found.', 'error')
            return _dashboard_redirect(venue_id)

        new_active = not bool(item.get('active'))
        try:
            cur.execute(
                """
                UPDATE forecasting_menu_items
                SET active = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (new_active, item_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            if _wants_json_response():
                return jsonify({'ok': False, 'error': 'Error updating item status.'}), 500
            flash('Error updating item status.', 'error')
            return _dashboard_redirect(venue_id)

    if _wants_json_response():
        return jsonify({'active': new_active})

    flash('Item restored.' if new_active else 'Item archived.', 'success')
    return _dashboard_redirect(venue_id)


@bp.route('/forecasting/menu-items/<item_id>/delete', methods=['POST'])
@login_required
def forecasting_menu_item_delete(item_id):
    conn = get_db()
    with get_cursor() as cur:
        ensure_forecasting_schema(cur)
        conn.commit()
        item = get_forecasting_menu_item(cur, item_id)
        venue_id = (request.form.get('venue_id') or '').strip() or (item.get('venue_id') if item else '')

        if not item:
            flash('Item not found.', 'error')
            return _dashboard_redirect(venue_id)

        try:
            cur.execute("DELETE FROM forecasting_menu_items WHERE id = %s", (item_id,))
            conn.commit()
            flash('Item removed.', 'success')
        except Exception:
            conn.rollback()
            flash('Error removing item.', 'error')

        return _dashboard_redirect(venue_id)


@bp.route('/api/forecasting/recipes/search')
@login_required
def api_forecasting_recipes_search():
    conn = get_db()
    with get_cursor() as cur:
        ensure_forecasting_schema(cur)
        conn.commit()
        results = search_forecasting_recipes(
            cur,
            query=(request.args.get('q') or '').strip(),
            venue_id=(request.args.get('venue_id') or '').strip(),
            limit=30,
        )
    return jsonify(results)
