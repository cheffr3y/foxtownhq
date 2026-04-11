from db import get_cursor, get_db
from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for
from flask_login import login_required

from helpers.forecasting import (
    FORECASTING_CATEGORY_OPTIONS,
    ensure_forecasting_schema,
    ensure_recipe_venue_link,
    get_forecasting_menu_item,
    get_forecasting_recipe,
    group_forecasting_items_by_category,
    list_forecasting_menu_items,
    search_forecasting_recipes,
    summarize_forecasting_menu_items,
)
from helpers.menu import clean_menu_text
from helpers.shared import generate_id, handle_route_error
from helpers.venues import get_active_venues

bp = Blueprint('forecasting', __name__)


@bp.errorhandler(Exception)
def handle_forecasting_error(error):
    return handle_route_error(error, 'forecasting')


def _parse_sort_order(value, errors):
    text = (value or '').strip()
    if not text:
        return 0
    try:
        parsed = int(text)
    except (TypeError, ValueError):
        errors.append('Sort order must be a whole number.')
        return 0
    if parsed < 0:
        errors.append('Sort order must be zero or greater.')
        return 0
    return parsed


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


def _build_form_state(base=None, venue_id=''):
    base = base or {}
    return {
        'name': base.get('name') or '',
        'venue_id': base.get('venue_id') or venue_id or '',
        'category': base.get('category') or '',
        'description': base.get('description') or '',
        'recipe_id': base.get('recipe_id') or '',
        'sort_order': base.get('sort_order') if base.get('sort_order') is not None else 0,
        'selected_recipe_name': base.get('selected_recipe_name') or base.get('recipe_name') or '',
        'selected_recipe_type': base.get('selected_recipe_type') or base.get('recipe_type') or '',
        'selected_recipe_category': base.get('selected_recipe_category') or base.get('recipe_category') or '',
        'selected_recipe_venue_names': base.get('selected_recipe_venue_names') or base.get('venue_names') or '',
    }


def _hydrate_selected_recipe(cur, form_state):
    recipe_id = (form_state.get('recipe_id') or '').strip()
    if not recipe_id:
        form_state['selected_recipe_name'] = ''
        form_state['selected_recipe_type'] = ''
        form_state['selected_recipe_category'] = ''
        form_state['selected_recipe_venue_names'] = ''
        return None

    recipe = get_forecasting_recipe(cur, recipe_id)
    if not recipe:
        return None

    form_state['selected_recipe_name'] = recipe.get('name') or ''
    form_state['selected_recipe_type'] = recipe.get('recipe_type') or ''
    form_state['selected_recipe_category'] = recipe.get('category') or ''
    form_state['selected_recipe_venue_names'] = recipe.get('venue_names') or ''
    return recipe


def _render_menu_item_form(cur, mode='new', item=None, form_state=None, errors=None, requested_venue_id=''):
    venues = [dict(row) for row in get_active_venues(cur)]
    venue_ids = {venue.get('id') for venue in venues if venue.get('id')}
    active_item = item if item and item.get('active') else None
    base_form_state = form_state or _build_form_state(active_item, requested_venue_id)
    if not base_form_state.get('venue_id'):
        base_form_state['venue_id'] = requested_venue_id or (active_item.get('venue_id') if active_item else '')
    selected_venue = _resolve_selected_venue(venues, base_form_state.get('venue_id') or requested_venue_id)
    if (not base_form_state.get('venue_id') or base_form_state.get('venue_id') not in venue_ids) and selected_venue:
        base_form_state['venue_id'] = selected_venue.get('id') or ''
    selected_recipe = _hydrate_selected_recipe(cur, base_form_state)
    selected_venue_id = base_form_state.get('venue_id') or selected_venue.get('id') or ''
    return render_template(
        'forecasting_menu_item_form.html',
        mode=mode,
        page_title='Edit Menu Item' if mode == 'edit' else 'Add Menu Item',
        form_state=base_form_state,
        venues=venues,
        category_options=FORECASTING_CATEGORY_OPTIONS,
        selected_recipe=selected_recipe,
        selected_venue_id=selected_venue_id,
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
        selected_venue_name = selected_venue.get('name') or 'No Active Venue'
        items = list_forecasting_menu_items(cur, selected_venue_id)
        summary = summarize_forecasting_menu_items(cur, selected_venue_id)

    return render_template(
        'forecasting_dashboard.html',
        venues=venues,
        selected_venue_id=selected_venue_id,
        selected_venue_name=selected_venue_name,
        items_by_category=group_forecasting_items_by_category(items),
        total_items=summary['total_items'],
        items_with_recipe=summary['items_with_recipe'],
        items_missing_recipe=summary['items_missing_recipe'],
        completion_pct=summary['completion_pct'],
    )


@bp.route('/forecasting/menu-items/new', methods=['GET', 'POST'])
@login_required
def forecasting_menu_item_new():
    conn = get_db()
    with get_cursor() as cur:
        ensure_forecasting_schema(cur)
        conn.commit()
        venues = [dict(row) for row in get_active_venues(cur)]
        valid_venue_ids = {venue.get('id') for venue in venues if venue.get('id')}
        requested_venue_id = request.values.get('venue_id') or ''

        if request.method == 'POST':
            errors = []
            form_state = _build_form_state(
                {
                    'name': clean_menu_text(request.form.get('name')),
                    'venue_id': (request.form.get('venue_id') or '').strip(),
                    'category': clean_menu_text(request.form.get('category')),
                    'description': clean_menu_text(request.form.get('description')),
                    'recipe_id': (request.form.get('recipe_id') or '').strip(),
                    'sort_order': _parse_sort_order(request.form.get('sort_order'), errors),
                },
                requested_venue_id,
            )

            if not form_state['name']:
                errors.append('Name is required.')
            if not form_state['venue_id']:
                errors.append('Venue is required.')
            elif form_state['venue_id'] not in valid_venue_ids:
                errors.append('Selected venue was invalid.')

            if not form_state['category']:
                errors.append('Category is required.')
            elif form_state['category'] not in FORECASTING_CATEGORY_OPTIONS:
                errors.append('Choose a valid category.')

            selected_recipe = None
            if form_state['recipe_id']:
                selected_recipe = _hydrate_selected_recipe(cur, form_state)
                if not selected_recipe:
                    errors.append('Selected recipe was invalid.')

            if errors:
                return _render_menu_item_form(
                    cur,
                    mode='new',
                    form_state=form_state,
                    errors=sorted(set(errors)),
                    requested_venue_id=form_state.get('venue_id') or requested_venue_id,
                )

            try:
                cur.execute(
                    """
                    INSERT INTO forecasting_menu_items (
                        id,
                        name,
                        category,
                        venue_id,
                        description,
                        recipe_id,
                        sort_order
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        generate_id('fmi_'),
                        form_state['name'],
                        form_state['category'],
                        form_state['venue_id'],
                        form_state['description'] or None,
                        form_state['recipe_id'] or None,
                        form_state['sort_order'],
                    ),
                )
                if form_state['recipe_id'] and form_state['venue_id']:
                    ensure_recipe_venue_link(cur, form_state['recipe_id'], form_state['venue_id'])
                conn.commit()
                flash('Menu item added.', 'success')
                return _dashboard_redirect(form_state['venue_id'])
            except Exception:
                conn.rollback()
                return _render_menu_item_form(
                    cur,
                    mode='new',
                    form_state=form_state,
                    errors=['Error saving menu item.'],
                    requested_venue_id=form_state.get('venue_id') or requested_venue_id,
                )

        return _render_menu_item_form(cur, mode='new', requested_venue_id=requested_venue_id)


@bp.route('/forecasting/menu-items/<item_id>/edit', methods=['GET', 'POST'])
@login_required
def forecasting_menu_item_edit(item_id):
    conn = get_db()
    with get_cursor() as cur:
        ensure_forecasting_schema(cur)
        conn.commit()
        item = get_forecasting_menu_item(cur, item_id)
        if not item or not item.get('active'):
            flash('Menu item not found.', 'error')
            return _dashboard_redirect(request.args.get('venue_id') or '')

        venues = [dict(row) for row in get_active_venues(cur)]
        valid_venue_ids = {venue.get('id') for venue in venues if venue.get('id')}

        if request.method == 'POST':
            errors = []
            form_state = _build_form_state(
                {
                    'name': clean_menu_text(request.form.get('name')),
                    'venue_id': (request.form.get('venue_id') or '').strip(),
                    'category': clean_menu_text(request.form.get('category')),
                    'description': clean_menu_text(request.form.get('description')),
                    'recipe_id': (request.form.get('recipe_id') or '').strip(),
                    'sort_order': _parse_sort_order(request.form.get('sort_order'), errors),
                },
                item.get('venue_id') or '',
            )

            if not form_state['name']:
                errors.append('Name is required.')
            if not form_state['venue_id']:
                errors.append('Venue is required.')
            elif form_state['venue_id'] not in valid_venue_ids:
                errors.append('Selected venue was invalid.')

            if not form_state['category']:
                errors.append('Category is required.')
            elif form_state['category'] not in FORECASTING_CATEGORY_OPTIONS:
                errors.append('Choose a valid category.')

            selected_recipe = None
            if form_state['recipe_id']:
                selected_recipe = _hydrate_selected_recipe(cur, form_state)
                if not selected_recipe:
                    errors.append('Selected recipe was invalid.')

            if errors:
                return _render_menu_item_form(
                    cur,
                    mode='edit',
                    item=item,
                    form_state=form_state,
                    errors=sorted(set(errors)),
                    requested_venue_id=form_state.get('venue_id') or item.get('venue_id') or '',
                )

            try:
                cur.execute(
                    """
                    UPDATE forecasting_menu_items
                    SET
                        name = %s,
                        category = %s,
                        venue_id = %s,
                        description = %s,
                        recipe_id = %s,
                        sort_order = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (
                        form_state['name'],
                        form_state['category'],
                        form_state['venue_id'],
                        form_state['description'] or None,
                        form_state['recipe_id'] or None,
                        form_state['sort_order'],
                        item_id,
                    ),
                )
                if form_state['recipe_id'] and form_state['venue_id']:
                    ensure_recipe_venue_link(cur, form_state['recipe_id'], form_state['venue_id'])
                conn.commit()
                flash('Menu item updated.', 'success')
                return _dashboard_redirect(form_state['venue_id'])
            except Exception:
                conn.rollback()
                return _render_menu_item_form(
                    cur,
                    mode='edit',
                    item=item,
                    form_state=form_state,
                    errors=['Error saving menu item.'],
                    requested_venue_id=form_state.get('venue_id') or item.get('venue_id') or '',
                )

        return _render_menu_item_form(
            cur,
            mode='edit',
            item=item,
            requested_venue_id=item.get('venue_id') or '',
        )


@bp.route('/forecasting/menu-items/<item_id>/delete', methods=['POST'])
@login_required
def forecasting_menu_item_delete(item_id):
    conn = get_db()
    with get_cursor() as cur:
        ensure_forecasting_schema(cur)
        conn.commit()
        item = get_forecasting_menu_item(cur, item_id)
        redirect_venue_id = (request.form.get('venue_id') or '').strip()
        if not item or not item.get('active'):
            flash('Menu item not found.', 'error')
            return _dashboard_redirect(redirect_venue_id)

        try:
            cur.execute(
                """
                UPDATE forecasting_menu_items
                SET active = FALSE,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (item_id,),
            )
            conn.commit()
            flash('Menu item removed.', 'success')
        except Exception:
            conn.rollback()
            flash('Error removing menu item.', 'error')

        return _dashboard_redirect(redirect_venue_id or item.get('venue_id') or '')


@bp.route('/api/forecasting/recipes/search')
@login_required
def api_forecasting_recipes_search():
    query = (request.args.get('q') or '').strip()
    venue_id = (request.args.get('venue_id') or '').strip()
    with get_cursor() as cur:
        results = search_forecasting_recipes(cur, query=query, venue_id=venue_id, limit=20)
    return jsonify(results)
