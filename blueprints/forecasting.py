from datetime import timedelta

from db import get_cursor, get_db
from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from helpers.commissary import ensure_commissary_tables
from helpers.forecasting import (
    FORECASTING_DAY_FIELDS,
    build_forecasting_plan_rows,
    ensure_forecasting_schema,
    ensure_recipe_venue_link,
    get_active_menu_recipe_ids,
    get_existing_menu_items_by_recipe,
    get_forecasting_day_dates,
    get_forecasting_menu_item,
    get_forecasting_plan_by_week,
    get_forecasting_plan_lines,
    get_forecasting_week_window,
    get_or_create_forecasting_plan,
    group_forecasting_items_by_category,
    list_forecasting_menu_items,
    list_forecasting_recipes,
    normalize_forecasting_plan_status,
    save_forecasting_plan_lines,
    search_forecasting_recipes,
    summarize_forecasting_items,
    submit_forecasting_plan_to_commissary,
)
from helpers.menu import clean_menu_text
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


def _parse_plan_quantity(value, label, errors):
    text = (value or '').strip()
    if not text:
        return 0
    try:
        number = float(text)
    except ValueError:
        errors.append(f'{label} must be a number.')
        return 0
    if number < 0:
        errors.append(f'{label} cannot be negative.')
        return 0
    return number


def _parse_forecasting_plan_rows(form, active_items):
    active_by_id = {
        item.get('id'): item
        for item in active_items or []
        if item.get('id')
    }
    menu_item_ids = form.getlist('line_menu_item_id[]')
    units = form.getlist('line_unit[]')
    notes = form.getlist('line_notes[]')
    day_values = {
        day_key: form.getlist(f'line_{day_key}_qty[]')
        for day_key, _, _ in FORECASTING_DAY_FIELDS
    }
    rows = []
    errors = []
    for idx, menu_item_id in enumerate(menu_item_ids):
        item = active_by_id.get(menu_item_id)
        if not item:
            continue
        unit = clean_menu_text(units[idx] if idx < len(units) else '')
        row = {
            'menu_item_id': item.get('id'),
            'recipe_id': item.get('recipe_id'),
            'item_name': item.get('name') or 'Forecast item',
            'category': item.get('category') or 'Uncategorized',
            'unit': unit or item.get('yield_unit') or 'each',
            'notes': clean_menu_text(notes[idx] if idx < len(notes) else ''),
        }
        for day_key, label, _ in FORECASTING_DAY_FIELDS:
            values = day_values.get(day_key) or []
            row[f'{day_key}_qty'] = _parse_plan_quantity(
                values[idx] if idx < len(values) else '',
                f"{item.get('name') or 'Forecast item'} {label}",
                errors,
            )
        rows.append(row)
    return rows, errors


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


@bp.route('/forecasting/plan', methods=['GET', 'POST'])
@login_required
def forecasting_plan():
    conn = get_db()
    with get_cursor() as cur:
        ensure_forecasting_schema(cur)
        ensure_commissary_tables(cur)
        conn.commit()

        venues = [dict(row) for row in get_active_venues(cur)]
        selected_venue = _resolve_selected_venue(
            venues,
            request.form.get('venue_id') if request.method == 'POST' else request.args.get('venue_id'),
        )
        selected_venue_id = selected_venue.get('id') or ''
        selected_venue_name = selected_venue.get('name') or 'Forecast Outlet'
        week_start, week_end = get_forecasting_week_window(
            request.form.get('week_start') if request.method == 'POST' else request.args.get('week_start')
        )
        day_dates = get_forecasting_day_dates(week_start)
        plan = None
        active_items = []
        plan_rows = []

        if selected_venue_id:
            created_by = clean_menu_text(getattr(current_user, 'username', '') or 'chef')
            plan = get_forecasting_plan_by_week(cur, selected_venue_id, week_start)
            active_items = [
                item
                for item in list_forecasting_menu_items(cur, selected_venue_id)
                if bool(item.get('active'))
            ]

            if request.method == 'POST':
                intent = (request.form.get('intent') or 'save').strip().lower()
                notes = clean_menu_text(request.form.get('notes'))
                rows, errors = _parse_forecasting_plan_rows(request.form, active_items)
                if plan and normalize_forecasting_plan_status(plan.get('status')) == 'submitted':
                    errors.append('Submitted forecasts are locked. Create a new week to make another pull.')

                if errors:
                    flash(' '.join(sorted(set(errors))), 'error')
                    plan_rows = build_forecasting_plan_rows(active_items, rows)
                else:
                    try:
                        if not plan:
                            plan = get_or_create_forecasting_plan(cur, selected_venue_id, week_start, created_by=created_by)
                        save_forecasting_plan_lines(cur, plan['id'], rows, notes=notes)
                        if intent == 'submit':
                            plan = {
                                **plan,
                                'notes': notes,
                                'status': normalize_forecasting_plan_status(plan.get('status')),
                            }
                            result = submit_forecasting_plan_to_commissary(
                                cur,
                                plan,
                                venue_name=selected_venue_name,
                                submitted_by=created_by,
                            )
                            if not result.get('ok'):
                                conn.rollback()
                                flash(result.get('message') or 'Could not submit forecast.', 'error')
                            else:
                                conn.commit()
                                flash(
                                    f"Forecast submitted: {result.get('order_count', 0)} commissary orders "
                                    f"and {result.get('line_count', 0)} line items created.",
                                    'success',
                                )
                                return redirect(
                                    url_for(
                                        'commissary.commissary_planner',
                                        day=week_start.isoformat(),
                                        outlet=selected_venue_name,
                                        tab='production',
                                    )
                                )
                        else:
                            conn.commit()
                            flash('Forecast draft saved.', 'success')
                            return redirect(
                                url_for(
                                    'forecasting.forecasting_plan',
                                    venue_id=selected_venue_id,
                                    week_start=week_start.isoformat(),
                                )
                            )
                    except Exception:
                        conn.rollback()
                        raise

            if not plan_rows:
                saved_lines = get_forecasting_plan_lines(cur, plan.get('id') if plan else '')
                plan_rows = build_forecasting_plan_rows(active_items, saved_lines)

    venue_tabs = [
        {
            'id': venue.get('id') or '',
            'label': venue.get('name') or 'Venue',
            'url': url_for(
                'forecasting.forecasting_plan',
                venue_id=venue.get('id') or '',
                week_start=week_start.isoformat(),
            ),
        }
        for venue in venues
        if venue.get('id')
    ]
    prev_week_start = week_start - timedelta(days=7)
    next_week_start = week_start + timedelta(days=7)
    total_qty = sum(float(row.get('total_qty') or 0) for row in plan_rows)
    active_forecast_line_count = sum(1 for row in plan_rows if float(row.get('total_qty') or 0) > 0)

    return render_template(
        'forecasting_plan.html',
        venues=venues,
        venue_tabs=venue_tabs,
        selected_venue=selected_venue,
        selected_venue_id=selected_venue_id,
        selected_venue_name=selected_venue_name,
        plan=plan or {},
        plan_status=normalize_forecasting_plan_status((plan or {}).get('status')),
        plan_rows=plan_rows,
        week_start=week_start,
        week_end=week_end,
        prev_week_start=prev_week_start,
        next_week_start=next_week_start,
        day_dates=day_dates,
        total_qty=total_qty,
        active_forecast_line_count=active_forecast_line_count,
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
