import io
from datetime import date, datetime, timedelta

from db import get_cursor, get_db
from flask import Blueprint, Response, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from helpers.commissary import (
    COMMISSARY_ACTIVE_STATUSES,
    COMMISSARY_SOURCE_CHOICES,
    COMMISSARY_STATUS_CHOICES,
    DEFAULT_COMMISSARY_OUTLET,
    build_commissary_datasets,
    build_commissary_prep_groups,
    ensure_commissary_tables,
    get_commissary_order,
    get_commissary_order_lines,
    get_commissary_outlet_options,
    get_commissary_standing_items,
    get_commissary_week_window,
    normalize_commissary_source,
    normalize_commissary_status,
    parse_commissary_order_lines,
    parse_commissary_standing_item_form,
)
from helpers.formatting import split_instruction_steps
from helpers.menu import clean_menu_text
from helpers.recipes import build_component_tree, collect_ingredients_from_components, normalize_recipe_type, ratio_from_line_quantity
from helpers.shared import generate_id, handle_route_error, to_float
from helpers.units import format_number, get_unit_system, smart_quantity

bp = Blueprint('commissary', __name__)


@bp.errorhandler(Exception)
def handle_commissary_error(error):
    return handle_route_error(error, 'commissary')


def get_stats_window(preset, start_raw='', end_raw=''):
    today = date.today()
    if preset == 'custom':
        try:
            start_date = datetime.strptime((start_raw or '').strip(), '%Y-%m-%d').date()
        except ValueError:
            start_date = today - timedelta(days=6)
        try:
            end_date = datetime.strptime((end_raw or '').strip(), '%Y-%m-%d').date()
        except ValueError:
            end_date = today
    elif preset == 'month':
        start_date = today.replace(day=1)
        end_date = today
    elif preset == 'quarter':
        quarter_start_month = ((today.month - 1) // 3) * 3 + 1
        start_date = today.replace(month=quarter_start_month, day=1)
        end_date = today
    elif preset == 'year':
        start_date = today.replace(month=1, day=1)
        end_date = today
    else:
        preset = 'week'
        start_date = today - timedelta(days=today.weekday())
        end_date = today
    if start_date > end_date:
        start_date, end_date = end_date, start_date
    return preset, start_date, end_date


def get_review_window(week_start_raw=''):
    today = date.today()
    if week_start_raw:
        try:
            selected = datetime.strptime((week_start_raw or '').strip(), '%Y-%m-%d').date()
        except ValueError:
            selected = today
    else:
        selected = today
    week_start = selected - timedelta(days=selected.weekday())
    week_end = week_start + timedelta(days=6)
    return week_start, week_end


def build_weekly_review_context(cur, week_start, week_end, selected_outlet=''):
    datasets = build_commissary_datasets(cur, week_start, week_end, selected_outlet, get_unit_system())
    orders = datasets.get('orders', [])
    total_lines = datasets.get('line_count', 0)
    signed_off_lines = datasets.get('signed_off_line_count', 0)
    completion_rate = round((signed_off_lines / total_lines) * 100, 1) if total_lines else 0

    items_by_outlet = {}
    incomplete_lines = []
    for order in orders:
        outlet_name = order.get('outlet') or DEFAULT_COMMISSARY_OUTLET
        items_by_outlet[outlet_name] = items_by_outlet.get(outlet_name, 0) + int(order.get('line_count') or 0)
        for line in order.get('lines', []):
            production_log = line.get('production_log') or {}
            if production_log.get('signed_off'):
                continue
            incomplete_lines.append({
                'needed_date': order.get('needed_date'),
                'outlet': outlet_name,
                'item_name': line.get('item_name') or line.get('recipe_name') or 'Item',
                'assigned_to': production_log.get('assigned_to') or '',
                'notes': production_log.get('notes') or line.get('notes') or '',
            })

    cur.execute(
        """
        SELECT
            t.id,
            t.production_date,
            t.from_location,
            t.to_outlet,
            t.transferred_by,
            t.transfer_method,
            t.notes,
            COUNT(line.id) AS line_count
        FROM commissary_transfers t
        LEFT JOIN commissary_transfer_lines line ON line.transfer_id = t.id
        WHERE t.production_date BETWEEN %s AND %s
          AND (%s = '' OR t.to_outlet = %s)
        GROUP BY t.id
        ORDER BY t.production_date, t.created_at
    """,
        (week_start, week_end, selected_outlet or '', selected_outlet or ''),
    )
    transfers = cur.fetchall()

    outlet_rows = [{'outlet': outlet, 'line_count': count} for outlet, count in items_by_outlet.items()]
    outlet_rows.sort(key=lambda row: (-row.get('line_count', 0), (row.get('outlet') or '').lower()))

    return {
        'datasets': datasets,
        'orders': orders,
        'total_lines': total_lines,
        'signed_off_lines': signed_off_lines,
        'completion_rate': completion_rate,
        'items_by_outlet': outlet_rows,
        'incomplete_lines': incomplete_lines,
        'transfers': transfers,
    }


def list_commissary_recipe_options(cur):
    cur.execute(
        """
        SELECT id,
               name,
               recipe_type,
               category,
               yield_qty,
               yield_unit
        FROM recipes
        ORDER BY CASE WHEN recipe_type = 'batch' THEN 0 WHEN recipe_type = 'menu' THEN 1 ELSE 2 END,
                 name
    """
    )
    rows = cur.fetchall()
    for row in rows:
        recipe_type = normalize_recipe_type(row.get('recipe_type')) or (row.get('recipe_type') or 'other')
        type_label = str(recipe_type).upper()
        display = f"{row.get('name') or 'Recipe'} ({type_label})"
        yield_qty = to_float(row.get('yield_qty'))
        yield_unit = row.get('yield_unit') or ''
        if yield_qty > 0 and yield_unit:
            display = f"{display} - yield {format_number(yield_qty)} {yield_unit}"
        row['display_label'] = display
    return rows


def list_transfer_production_log_options(cur, production_date, outlet=''):
    if not production_date:
        return []
    cur.execute(
        """
        SELECT
            pl.id AS production_log_id,
            pl.line_id,
            pl.production_date,
            pl.assigned_to,
            pl.made_by,
            pl.signed_off,
            COALESCE(line.item_name, r.name, 'Item') AS item_name,
            COALESCE(pl.actual_yield, line.quantity, 0) AS quantity,
            COALESCE(NULLIF(TRIM(pl.actual_yield_unit), ''), NULLIF(TRIM(line.quantity_unit), ''), 'each') AS quantity_unit,
            o.outlet
        FROM commissary_production_logs pl
        JOIN commissary_order_lines line ON line.id = pl.line_id
        JOIN commissary_orders o ON o.id = line.order_id
        LEFT JOIN recipes r ON r.id = line.recipe_id
        WHERE pl.production_date = %s
          AND (%s = '' OR o.outlet = %s)
        ORDER BY o.outlet, LOWER(COALESCE(line.item_name, r.name, '')), pl.id
    """,
        (production_date, outlet or '', outlet or ''),
    )
    rows = cur.fetchall()
    for row in rows:
        qty = to_float(row.get('quantity'))
        unit = row.get('quantity_unit') or 'each'
        row['display_label'] = (
            f"#{row.get('production_log_id')} - "
            f"{row.get('item_name')} ({qty} {unit}) - {row.get('outlet')}"
        )
    return rows


def upsert_commissary_line_production_log(cur, line_id, production_date, payload):
    if not line_id or not production_date:
        return False

    assigned_to = clean_menu_text(payload.get('assigned_to'))
    made_by = clean_menu_text(payload.get('made_by'))
    tasted_by = clean_menu_text(payload.get('tasted_by'))
    signed_off_by = clean_menu_text(payload.get('signed_off_by'))
    production_notes = clean_menu_text(payload.get('production_notes'))
    actual_yield = to_float(payload.get('actual_yield'))
    actual_yield_unit = clean_menu_text(payload.get('actual_yield_unit'))
    signed_off = bool(payload.get('signed_off'))

    has_values = any([
        assigned_to,
        made_by,
        tasted_by,
        signed_off_by,
        production_notes,
        actual_yield,
        actual_yield_unit,
        signed_off,
    ])

    if has_values:
        cur.execute(
            """
            INSERT INTO commissary_production_logs (
                line_id,
                production_date,
                assigned_to,
                made_by,
                tasted_by,
                signed_off,
                signed_off_by,
                production_notes,
                actual_yield,
                actual_yield_unit,
                signed_off_at,
                updated_at
            )
            VALUES (
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE NULL END,
                CURRENT_TIMESTAMP
            )
            ON CONFLICT (line_id, production_date)
            DO UPDATE SET
                assigned_to = EXCLUDED.assigned_to,
                made_by = EXCLUDED.made_by,
                tasted_by = EXCLUDED.tasted_by,
                signed_off = EXCLUDED.signed_off,
                signed_off_by = EXCLUDED.signed_off_by,
                production_notes = EXCLUDED.production_notes,
                actual_yield = EXCLUDED.actual_yield,
                actual_yield_unit = EXCLUDED.actual_yield_unit,
                signed_off_at = CASE
                    WHEN EXCLUDED.signed_off THEN COALESCE(commissary_production_logs.signed_off_at, CURRENT_TIMESTAMP)
                    ELSE NULL
                END,
                updated_at = CURRENT_TIMESTAMP
        """,
            (
                line_id,
                production_date,
                assigned_to or None,
                made_by or None,
                tasted_by or None,
                signed_off,
                signed_off_by or None,
                production_notes or None,
                actual_yield or None,
                actual_yield_unit or None,
                signed_off,
            ),
        )
        return True

    cur.execute(
        """
        DELETE FROM commissary_production_logs
        WHERE line_id = %s AND production_date = %s
    """,
        (line_id, production_date),
    )
    return False


@bp.route('/commissary')
@bp.route('/commissary-planner')
@login_required
def commissary_planner():
    conn = get_db()
    with get_cursor() as cur:
        ensure_commissary_tables(cur)
        conn.commit()

        week_start_raw = (request.args.get('week_start') or request.args.get('start_date') or '').strip()
        selected_day_raw = (request.args.get('day') or '').strip()
        view_mode = (request.args.get('view') or 'day').strip().lower()
        if view_mode not in ('day', 'week'):
            view_mode = 'day'

        start_date, end_date = get_commissary_week_window(week_start_raw)
        selected_outlet = clean_menu_text(request.args.get('outlet'))
        selected_units = get_unit_system()
        outlet_options = get_commissary_outlet_options(cur)

        datasets = build_commissary_datasets(cur, start_date, end_date, selected_outlet, selected_units)

        selected_day = None
        if selected_day_raw:
            try:
                selected_day = datetime.strptime(selected_day_raw, '%Y-%m-%d').date()
            except ValueError:
                selected_day = None
        if not selected_day:
            today = date.today()
            selected_day = today if start_date <= today <= end_date else start_date
        if selected_day < start_date or selected_day > end_date:
            selected_day = start_date

        all_daily_days = datasets.get('daily_production_days', []) or []
        if view_mode == 'day':
            filtered_daily_days = [day_data for day_data in all_daily_days if day_data.get('date') == selected_day]
        else:
            filtered_daily_days = all_daily_days

        orders = datasets.get('orders', [])
        open_statuses = set(COMMISSARY_ACTIVE_STATUSES)

        metrics = {
            'open_orders': sum(1 for order in orders if (order.get('status') or '').lower() in open_statuses),
            'completed_orders': sum(1 for order in orders if (order.get('status') or '').lower() == 'completed'),
            'logged_lines': datasets.get('logged_line_count', 0),
            'signed_off_lines': datasets.get('signed_off_line_count', 0),
        }

        prev_week_start = start_date - timedelta(days=7)
        next_week_start = start_date + timedelta(days=7)
        current_week_start, _ = get_commissary_week_window('')

    return render_template(
        'commissary_planner.html',
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        week_start=start_date.isoformat(),
        prev_week_start=prev_week_start.isoformat(),
        next_week_start=next_week_start.isoformat(),
        current_week_start=current_week_start.isoformat(),
        selected_outlet=selected_outlet,
        selected_units=selected_units,
        outlet_options=outlet_options,
        datasets=datasets,
        filtered_daily_days=filtered_daily_days,
        orders=orders,
        metrics=metrics,
        selected_day=selected_day.isoformat(),
        view_mode=view_mode,
        source_options=COMMISSARY_SOURCE_CHOICES,
    )


@bp.route('/commissary/orders/new', methods=['GET', 'POST'])
@bp.route('/commissary-planner/orders/new', methods=['GET', 'POST'])
@login_required
def commissary_order_new():
    conn = get_db()
    with get_cursor() as cur:
        ensure_commissary_tables(cur)
        conn.commit()

        recipe_options = list_commissary_recipe_options(cur)
        recipe_name_map = {row['id']: row.get('name') for row in recipe_options if row.get('id')}
        valid_recipe_ids = {row['id'] for row in recipe_options if row.get('id')}
        outlet_options = get_commissary_outlet_options(cur)

        default_source = normalize_commissary_source(request.args.get('source') or 'outlet_request')
        order = {
            'outlet': DEFAULT_COMMISSARY_OUTLET,
            'needed_date': '',
            'status': 'draft',
            'source': default_source,
            'notes': '',
        }
        lines = []

        if request.method == 'POST':
            errors = []
            outlet = clean_menu_text(request.form.get('outlet'))
            needed_date_raw = (request.form.get('needed_date') or '').strip()
            status = normalize_commissary_status(request.form.get('status'))
            source = normalize_commissary_source(request.form.get('source'))
            notes = clean_menu_text(request.form.get('notes'))

            needed_date = None
            try:
                needed_date = datetime.strptime(needed_date_raw, '%Y-%m-%d').date()
            except ValueError:
                errors.append('Needed date is required.')

            if not outlet:
                errors.append('Outlet is required.')

            lines, line_errors = parse_commissary_order_lines(request, valid_recipe_ids)
            errors.extend(line_errors)
            if not lines:
                errors.append('Add at least one commissary line item.')

            order = {
                'outlet': outlet,
                'needed_date': needed_date_raw,
                'status': status,
                'source': source,
                'notes': notes,
            }

            if errors:
                flash(' '.join(sorted(set(errors))), 'error')
            else:
                order_id = generate_id('cor_')
                created_by = clean_menu_text(getattr(current_user, 'username', '') or 'chef')
                try:
                    cur.execute(
                        """
                        INSERT INTO commissary_orders (
                            id,
                            outlet,
                            needed_date,
                            status,
                            source,
                            notes,
                            created_by
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                        (
                            order_id,
                            outlet,
                            needed_date,
                            status,
                            source,
                            notes or None,
                            created_by or None,
                        ),
                    )

                    for idx, line in enumerate(lines):
                        recipe_id = line.get('recipe_id')
                        item_name = line.get('item_name') or recipe_name_map.get(recipe_id) or None
                        cur.execute(
                            """
                            INSERT INTO commissary_order_lines (
                                order_id,
                                recipe_id,
                                item_name,
                                quantity,
                                quantity_unit,
                                notes,
                                sort_order
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                            (
                                order_id,
                                recipe_id,
                                item_name,
                                line.get('quantity'),
                                line.get('quantity_unit') or 'each',
                                line.get('notes'),
                                idx,
                            ),
                        )

                    conn.commit()
                    flash('Commissary order created.', 'success')
                    return redirect(url_for('commissary_order_edit', order_id=order_id))
                except Exception:
                    conn.rollback()
                    flash('Error creating commissary order.', 'error')

    return render_template(
        'commissary_order_form.html',
        page_title='New Commissary Order',
        order=order,
        lines=lines,
        recipe_options=recipe_options,
        outlet_options=outlet_options,
        status_options=COMMISSARY_STATUS_CHOICES,
        source_options=COMMISSARY_SOURCE_CHOICES,
    )


@bp.route('/commissary/orders/<order_id>/edit', methods=['GET', 'POST'])
@bp.route('/commissary-planner/orders/<order_id>/edit', methods=['GET', 'POST'])
@login_required
def commissary_order_edit(order_id):
    conn = get_db()
    with get_cursor() as cur:
        ensure_commissary_tables(cur)
        conn.commit()

        existing = get_commissary_order(cur, order_id)
        if not existing:
            flash('Commissary order not found.', 'error')
            return redirect(url_for('commissary_planner'))

        recipe_options = list_commissary_recipe_options(cur)
        recipe_name_map = {row['id']: row.get('name') for row in recipe_options if row.get('id')}
        valid_recipe_ids = {row['id'] for row in recipe_options if row.get('id')}
        outlet_options = get_commissary_outlet_options(cur)

        if request.method == 'POST':
            errors = []
            outlet = clean_menu_text(request.form.get('outlet'))
            needed_date_raw = (request.form.get('needed_date') or '').strip()
            status = normalize_commissary_status(request.form.get('status'))
            source = normalize_commissary_source(request.form.get('source'))
            notes = clean_menu_text(request.form.get('notes'))

            needed_date = None
            try:
                needed_date = datetime.strptime(needed_date_raw, '%Y-%m-%d').date()
            except ValueError:
                errors.append('Needed date is required.')

            if not outlet:
                errors.append('Outlet is required.')

            lines, line_errors = parse_commissary_order_lines(request, valid_recipe_ids)
            errors.extend(line_errors)
            if not lines:
                errors.append('Add at least one commissary line item.')

            if errors:
                flash(' '.join(sorted(set(errors))), 'error')
                order = {
                    **existing,
                    'outlet': outlet,
                    'needed_date': needed_date_raw,
                    'status': status,
                    'source': source,
                    'notes': notes,
                }
            else:
                try:
                    cur.execute(
                        """
                        UPDATE commissary_orders
                        SET outlet = %s,
                            needed_date = %s,
                            status = %s,
                            source = %s,
                            notes = %s,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = %s
                    """,
                        (
                            outlet,
                            needed_date,
                            status,
                            source,
                            notes or None,
                            order_id,
                        ),
                    )

                    cur.execute("DELETE FROM commissary_order_lines WHERE order_id = %s", (order_id,))
                    for idx, line in enumerate(lines):
                        recipe_id = line.get('recipe_id')
                        item_name = line.get('item_name') or recipe_name_map.get(recipe_id) or None
                        cur.execute(
                            """
                            INSERT INTO commissary_order_lines (
                                order_id,
                                recipe_id,
                                item_name,
                                quantity,
                                quantity_unit,
                                notes,
                                sort_order
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                            (
                                order_id,
                                recipe_id,
                                item_name,
                                line.get('quantity'),
                                line.get('quantity_unit') or 'each',
                                line.get('notes'),
                                idx,
                            ),
                        )

                    conn.commit()
                    flash('Commissary order updated.', 'success')
                    return redirect(url_for('commissary_order_edit', order_id=order_id))
                except Exception:
                    conn.rollback()
                    flash('Error updating commissary order.', 'error')
                    order = {
                        **existing,
                        'outlet': outlet,
                        'needed_date': needed_date_raw,
                        'status': status,
                        'source': source,
                        'notes': notes,
                    }

            return render_template(
                'commissary_order_form.html',
                page_title='Edit Commissary Order',
                order=order,
                lines=lines,
                recipe_options=recipe_options,
                outlet_options=outlet_options,
                status_options=COMMISSARY_STATUS_CHOICES,
                source_options=COMMISSARY_SOURCE_CHOICES,
            )

        lines = get_commissary_order_lines(cur, order_id)
        order = {
            **existing,
            'status': normalize_commissary_status(existing.get('status')),
            'source': normalize_commissary_source(existing.get('source')),
            'needed_date': existing.get('needed_date').isoformat() if existing.get('needed_date') else '',
        }

    return render_template(
        'commissary_order_form.html',
        page_title='Edit Commissary Order',
        order=order,
        lines=lines,
        recipe_options=recipe_options,
        outlet_options=outlet_options,
        status_options=COMMISSARY_STATUS_CHOICES,
        source_options=COMMISSARY_SOURCE_CHOICES,
    )


@bp.route('/commissary/orders/<order_id>/delete', methods=['POST'])
@bp.route('/commissary-planner/orders/<order_id>/delete', methods=['POST'])
@login_required
def commissary_order_delete(order_id):
    conn = get_db()
    with get_cursor() as cur:
        ensure_commissary_tables(cur)
        conn.commit()
        cur.execute("SELECT id, outlet, needed_date FROM commissary_orders WHERE id = %s", (order_id,))
        order = cur.fetchone()
        if not order:
            flash('Commissary order not found.', 'error')
            return redirect(url_for('commissary_planner'))

        try:
            cur.execute("DELETE FROM commissary_orders WHERE id = %s", (order_id,))
            conn.commit()
            flash(f"Deleted commissary order for {order.get('outlet')} on {order.get('needed_date')}.", 'success')
        except Exception:
            conn.rollback()
            flash('Error deleting commissary order.', 'error')
    return redirect(url_for('commissary_planner'))


@bp.route('/commissary/assign/<int:line_id>', methods=['POST'])
@bp.route('/commissary-planner/assign/<int:line_id>', methods=['POST'])
@login_required
def commissary_line_assignment_update(line_id):
    conn = get_db()
    is_ajax = (
        (request.form.get('ajax') or '').strip().lower() in ('1', 'true', 'yes', 'on')
        or (request.headers.get('X-Requested-With') or '').strip().lower() == 'xmlhttprequest'
        or 'application/json' in (request.headers.get('Accept') or '')
    )
    with get_cursor() as cur:
        ensure_commissary_tables(cur)
        conn.commit()

        cur.execute(
            """
            SELECT line.id AS line_id,
                   o.needed_date
            FROM commissary_order_lines line
            JOIN commissary_orders o ON o.id = line.order_id
            WHERE line.id = %s
            LIMIT 1
        """,
            (line_id,),
        )
        line = cur.fetchone()
        if not line:
            if is_ajax:
                return jsonify({'ok': False, 'error': 'Commissary line not found.'}), 404
            flash('Commissary line not found.', 'error')
            return redirect(url_for('commissary_planner'))

        production_date_raw = (request.form.get('production_date') or '').strip()
        try:
            production_date = datetime.strptime(production_date_raw, '%Y-%m-%d').date() if production_date_raw else line.get('needed_date')
        except ValueError:
            production_date = line.get('needed_date')

        assigned_to = clean_menu_text(request.form.get('assigned_to'))

        try:
            cur.execute(
                """
                INSERT INTO commissary_production_logs (
                    line_id,
                    production_date,
                    assigned_to,
                    updated_at
                )
                VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (line_id, production_date)
                DO UPDATE SET
                    assigned_to = EXCLUDED.assigned_to,
                    updated_at = CURRENT_TIMESTAMP
            """,
                (line_id, production_date, assigned_to or None),
            )
            conn.commit()
            if is_ajax:
                return jsonify({'ok': True, 'assigned_to': assigned_to or '', 'message': 'Cook assignment updated.'})
            flash('Cook assignment updated.', 'success')
        except Exception:
            conn.rollback()
            if is_ajax:
                return jsonify({'ok': False, 'error': 'Could not update cook assignment.'}), 500
            flash('Could not update cook assignment.', 'error')

        redirect_params = {
            'week_start': (request.form.get('week_start') or '').strip() or (production_date.isoformat() if production_date else ''),
        }
        selected_day = (request.form.get('day') or '').strip()
        if selected_day:
            redirect_params['day'] = selected_day
        selected_outlet = clean_menu_text(request.form.get('outlet'))
        if selected_outlet:
            redirect_params['outlet'] = selected_outlet
        view_mode = (request.form.get('view') or '').strip().lower()
        if view_mode in ('day', 'week'):
            redirect_params['view'] = view_mode
        return redirect(url_for('commissary_planner', **redirect_params))


@bp.route('/commissary/logs/<int:line_id>', methods=['POST'])
@bp.route('/commissary-planner/logs/<int:line_id>', methods=['POST'])
@login_required
def commissary_production_log_update(line_id):
    conn = get_db()
    is_ajax = (
        (request.form.get('ajax') or '').strip().lower() in ('1', 'true', 'yes', 'on')
        or (request.headers.get('X-Requested-With') or '').strip().lower() == 'xmlhttprequest'
        or 'application/json' in (request.headers.get('Accept') or '')
    )
    with get_cursor() as cur:
        ensure_commissary_tables(cur)
        conn.commit()

        cur.execute(
            """
            SELECT line.id AS line_id,
                   line.order_id,
                   o.outlet,
                   o.needed_date
            FROM commissary_order_lines line
            JOIN commissary_orders o ON o.id = line.order_id
            WHERE line.id = %s
            LIMIT 1
        """,
            (line_id,),
        )
        line = cur.fetchone()
        if not line:
            if is_ajax:
                return jsonify({'ok': False, 'error': 'Commissary production line not found.'}), 404
            flash('Commissary production line not found.', 'error')
            return redirect(url_for('commissary_planner'))

        production_date_raw = (request.form.get('production_date') or '').strip()
        try:
            production_date = datetime.strptime(production_date_raw, '%Y-%m-%d').date() if production_date_raw else line.get('needed_date')
        except ValueError:
            production_date = line.get('needed_date')

        assigned_to = clean_menu_text(request.form.get('assigned_to'))
        made_by = clean_menu_text(request.form.get('made_by'))
        tasted_by = clean_menu_text(request.form.get('tasted_by'))
        signed_off_by = clean_menu_text(request.form.get('signed_off_by'))
        production_notes = clean_menu_text(request.form.get('production_notes'))
        actual_yield_raw = (request.form.get('actual_yield') or '').strip()
        actual_yield_unit = clean_menu_text(request.form.get('actual_yield_unit'))
        signed_off = (request.form.get('signed_off') or '').strip().lower() in ('1', 'true', 'on', 'yes')

        week_start = (request.form.get('week_start') or '').strip()
        selected_day = (request.form.get('day') or '').strip()
        view_mode = (request.form.get('view') or '').strip().lower()
        selected_outlet = clean_menu_text(request.form.get('outlet'))
        selected_units = (request.form.get('units') or 'auto').strip().lower()
        if selected_units not in ('auto', 'imperial', 'metric', 'hybrid'):
            selected_units = 'auto'

        response_payload = None
        try:
            saved = upsert_commissary_line_production_log(
                cur,
                line_id,
                production_date,
                {
                    'assigned_to': assigned_to,
                    'made_by': made_by,
                    'tasted_by': tasted_by,
                    'signed_off_by': signed_off_by,
                    'production_notes': production_notes,
                    'actual_yield': actual_yield_raw,
                    'actual_yield_unit': actual_yield_unit,
                    'signed_off': signed_off,
                },
            )
            if saved:
                cur.execute(
                    """
                    SELECT updated_at
                    FROM commissary_production_logs
                    WHERE line_id = %s
                      AND production_date = %s
                    LIMIT 1
                """,
                    (line_id, production_date),
                )
                saved_row = cur.fetchone() or {}
                updated_at = saved_row.get('updated_at')
                response_payload = {
                    'ok': True,
                    'saved': True,
                    'signed_off': signed_off,
                    'updated_at': updated_at.isoformat() if updated_at else None,
                    'message': 'Production log updated.',
                }
                if not is_ajax:
                    flash('Production log updated.', 'success')
            else:
                response_payload = {
                    'ok': True,
                    'saved': False,
                    'signed_off': False,
                    'updated_at': None,
                    'message': 'Production log cleared.',
                }
                if not is_ajax:
                    flash('Production log cleared.', 'success')
            conn.commit()
        except Exception:
            conn.rollback()
            if is_ajax:
                return jsonify({'ok': False, 'error': 'Could not save production log.'}), 500
            flash('Could not save production log.', 'error')

        redirect_params = {
            'week_start': week_start or (production_date.isoformat() if production_date else ''),
        }
        if selected_outlet:
            redirect_params['outlet'] = selected_outlet
        if selected_units:
            redirect_params['units'] = selected_units
        if selected_day:
            redirect_params['day'] = selected_day
        if view_mode in ('day', 'week'):
            redirect_params['view'] = view_mode
        if is_ajax:
            return jsonify(response_payload or {'ok': True, 'saved': False, 'message': 'No changes saved.'})
        return redirect(url_for('commissary_planner', **redirect_params))


@bp.route('/commissary/log/bulk-signoff', methods=['POST'])
@login_required
def commissary_production_log_bulk_signoff():
    conn = get_db()
    is_ajax = (
        (request.form.get('ajax') or '').strip().lower() in ('1', 'true', 'yes', 'on')
        or (request.headers.get('X-Requested-With') or '').strip().lower() == 'xmlhttprequest'
        or 'application/json' in (request.headers.get('Accept') or '')
    )
    selected_outlet = clean_menu_text(request.form.get('outlet'))
    remaining = (request.form.get('remaining') or '1').strip()
    view_mode = (request.form.get('mode') or 'cards').strip().lower()
    if view_mode not in ('cards', 'table'):
        view_mode = 'cards'
    selected_date_raw = (request.form.get('production_date') or '').strip()
    signed_off_by = clean_menu_text(request.form.get('signed_off_by')) or clean_menu_text(getattr(current_user, 'username', '') or 'Chef')
    try:
        selected_date = datetime.strptime(selected_date_raw, '%Y-%m-%d').date() if selected_date_raw else date.today()
    except ValueError:
        selected_date = date.today()

    with get_cursor() as cur:
        ensure_commissary_tables(cur)
        cur.execute(
            """
            SELECT line.id AS line_id
            FROM commissary_order_lines line
            JOIN commissary_orders o ON o.id = line.order_id
            LEFT JOIN commissary_production_logs pl
                   ON pl.line_id = line.id
                  AND pl.production_date = %s
            WHERE o.needed_date = %s
              AND (%s = '' OR o.outlet = %s)
              AND COALESCE(NULLIF(TRIM(o.status), ''), 'draft') <> 'cancelled'
              AND COALESCE(pl.signed_off, FALSE) = FALSE
            ORDER BY line.id
        """,
            (selected_date, selected_date, selected_outlet or '', selected_outlet or ''),
        )
        line_ids = [row.get('line_id') for row in cur.fetchall() if row.get('line_id')]

        try:
            for line_id in line_ids:
                cur.execute(
                    """
                    INSERT INTO commissary_production_logs (
                        line_id,
                        production_date,
                        signed_off,
                        signed_off_by,
                        signed_off_at,
                        updated_at
                    )
                    VALUES (%s, %s, TRUE, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT (line_id, production_date)
                    DO UPDATE SET
                        signed_off = TRUE,
                        signed_off_by = EXCLUDED.signed_off_by,
                        signed_off_at = COALESCE(commissary_production_logs.signed_off_at, CURRENT_TIMESTAMP),
                        updated_at = CURRENT_TIMESTAMP
                """,
                    (line_id, selected_date, signed_off_by or None),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            if is_ajax:
                return jsonify({'ok': False, 'error': 'Bulk sign-off failed.'}), 500
            flash('Bulk sign-off failed.', 'error')
            return redirect(
                url_for(
                    'commissary_production_log',
                    log_date=selected_date.isoformat(),
                    outlet=selected_outlet or None,
                    remaining=remaining,
                    mode=view_mode,
                )
            )

    if is_ajax:
        return jsonify({'ok': True, 'updated_count': len(line_ids), 'message': f'Signed off {len(line_ids)} remaining item(s).'})
    flash(f'Signed off {len(line_ids)} remaining item(s).', 'success')
    return redirect(
        url_for(
            'commissary_production_log',
            log_date=selected_date.isoformat(),
            outlet=selected_outlet or None,
            remaining=remaining,
            mode=view_mode,
        )
    )


@bp.route('/commissary/log')
@bp.route('/commissary/log/<log_date>')
@login_required
def commissary_production_log(log_date=None):
    selected_date_raw = (log_date or request.args.get('date') or '').strip()
    selected_outlet = clean_menu_text(request.args.get('outlet'))
    show_remaining_only = (request.args.get('remaining') or '1').strip().lower() in ('1', 'true', 'yes', 'on')
    view_mode = (request.args.get('mode') or 'cards').strip().lower()
    if view_mode not in ('cards', 'table'):
        view_mode = 'cards'
    try:
        selected_date = datetime.strptime(selected_date_raw, '%Y-%m-%d').date() if selected_date_raw else date.today()
    except ValueError:
        selected_date = date.today()

    with get_cursor() as cur:
        ensure_commissary_tables(cur)
        datasets = build_commissary_datasets(cur, selected_date, selected_date, selected_outlet, get_unit_system())
        outlet_options = get_commissary_outlet_options(cur)

    day_rows = datasets.get('daily_production_days', []) or []
    day_data = day_rows[0] if day_rows else {'source_groups': []}
    entries = []
    for source_group in day_data.get('source_groups', []):
        for entry in source_group.get('entries', []):
            if not entry.get('line_id'):
                continue
            entries.append({
                **entry,
                'source_label': source_group.get('label'),
            })

    total_count = len(entries)
    signed_count = sum(1 for entry in entries if entry.get('signed_off'))
    if show_remaining_only:
        entries = [entry for entry in entries if not entry.get('signed_off')]
    entries.sort(key=lambda entry: ((entry.get('signed_off') is True), (entry.get('item_name') or '').lower()))

    return render_template(
        'commissary_log.html',
        selected_date=selected_date.isoformat(),
        selected_outlet=selected_outlet,
        outlet_options=outlet_options,
        entries=entries,
        total_count=total_count,
        signed_count=signed_count,
        remaining_count=max(0, total_count - signed_count),
        show_remaining_only=show_remaining_only,
        view_mode=view_mode,
        current_user_name=clean_menu_text(getattr(current_user, 'username', '') or 'Chef'),
    )


@bp.route('/commissary/standing-prep', methods=['GET', 'POST'])
@login_required
def commissary_standing_prep():
    conn = get_db()
    with get_cursor() as cur:
        ensure_commissary_tables(cur)
        conn.commit()

        recipe_options = list_commissary_recipe_options(cur)
        valid_recipe_ids = {row['id'] for row in recipe_options if row.get('id')}

        if request.method == 'POST':
            payload, errors = parse_commissary_standing_item_form(request, valid_recipe_ids)
            if errors:
                flash(' '.join(sorted(set(errors))), 'error')
            else:
                try:
                    cur.execute(
                        """
                        INSERT INTO commissary_standing_items (
                            recipe_id,
                            item_name,
                            default_quantity,
                            default_unit,
                            frequency,
                            active,
                            notes
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                        (
                            payload.get('recipe_id'),
                            payload.get('item_name'),
                            payload.get('default_quantity'),
                            payload.get('default_unit'),
                            payload.get('frequency'),
                            payload.get('active'),
                            payload.get('notes'),
                        ),
                    )
                    conn.commit()
                    flash('Standing prep item added.', 'success')
                    return redirect(url_for('commissary_standing_prep'))
                except Exception:
                    conn.rollback()
                    flash('Could not add standing prep item.', 'error')

        items = get_commissary_standing_items(cur, active_only=False)

    return render_template(
        'commissary_standing_prep.html',
        page_title='Standing Prep Manager',
        recipe_options=recipe_options,
        items=items,
        editing_item=None,
    )


@bp.route('/commissary/standing-prep/<int:item_id>/edit', methods=['GET', 'POST'])
@login_required
def commissary_standing_prep_edit(item_id):
    conn = get_db()
    with get_cursor() as cur:
        ensure_commissary_tables(cur)
        conn.commit()

        recipe_options = list_commissary_recipe_options(cur)
        valid_recipe_ids = {row['id'] for row in recipe_options if row.get('id')}

        cur.execute(
            """
            SELECT
                item.id,
                item.recipe_id,
                item.item_name,
                item.default_quantity,
                item.default_unit,
                item.frequency,
                item.active,
                item.notes,
                r.name AS recipe_name
            FROM commissary_standing_items item
            LEFT JOIN recipes r ON r.id = item.recipe_id
            WHERE item.id = %s
            LIMIT 1
        """,
            (item_id,),
        )
        editing_item = cur.fetchone()
        if not editing_item:
            flash('Standing prep item not found.', 'error')
            return redirect(url_for('commissary_standing_prep'))

        if request.method == 'POST':
            payload, errors = parse_commissary_standing_item_form(request, valid_recipe_ids)
            if errors:
                flash(' '.join(sorted(set(errors))), 'error')
                editing_item = {
                    **editing_item,
                    **payload,
                }
            else:
                try:
                    cur.execute(
                        """
                        UPDATE commissary_standing_items
                        SET recipe_id = %s,
                            item_name = %s,
                            default_quantity = %s,
                            default_unit = %s,
                            frequency = %s,
                            active = %s,
                            notes = %s
                        WHERE id = %s
                    """,
                        (
                            payload.get('recipe_id'),
                            payload.get('item_name'),
                            payload.get('default_quantity'),
                            payload.get('default_unit'),
                            payload.get('frequency'),
                            payload.get('active'),
                            payload.get('notes'),
                            item_id,
                        ),
                    )
                    conn.commit()
                    flash('Standing prep item updated.', 'success')
                    return redirect(url_for('commissary_standing_prep'))
                except Exception:
                    conn.rollback()
                    flash('Could not update standing prep item.', 'error')

        items = get_commissary_standing_items(cur, active_only=False)

    return render_template(
        'commissary_standing_prep.html',
        page_title='Standing Prep Manager',
        recipe_options=recipe_options,
        items=items,
        editing_item=editing_item,
    )


@bp.route('/commissary/standing-prep/<int:item_id>/toggle', methods=['POST'])
@login_required
def commissary_standing_prep_toggle(item_id):
    conn = get_db()
    with get_cursor() as cur:
        ensure_commissary_tables(cur)
        conn.commit()
        try:
            cur.execute(
                """
                UPDATE commissary_standing_items
                SET active = NOT active
                WHERE id = %s
            """,
                (item_id,),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            flash('Could not update standing prep item state.', 'error')
    return redirect(url_for('commissary_standing_prep'))


@bp.route('/commissary/transfers', methods=['GET', 'POST'])
@login_required
def commissary_transfers():
    conn = get_db()
    with get_cursor() as cur:
        ensure_commissary_tables(cur)
        conn.commit()

        outlet_options = get_commissary_outlet_options(cur)
        selected_outlet_filter = clean_menu_text(request.args.get('outlet_filter') or request.form.get('outlet_filter'))
        default_transfer = {
            'production_date': date.today().isoformat(),
            'from_location': 'Commissary',
            'to_outlet': DEFAULT_COMMISSARY_OUTLET,
            'transferred_by': clean_menu_text(getattr(current_user, 'username', '') or 'Chef'),
            'transfer_method': 'delivery',
            'notes': '',
        }
        transfer_lines = []

        if request.method == 'POST':
            errors = []
            production_date_raw = (request.form.get('production_date') or '').strip()
            from_location = clean_menu_text(request.form.get('from_location')) or 'Commissary'
            to_outlet = clean_menu_text(request.form.get('to_outlet'))
            transferred_by = clean_menu_text(request.form.get('transferred_by'))
            transfer_method = (request.form.get('transfer_method') or 'delivery').strip().lower()
            notes = clean_menu_text(request.form.get('notes'))

            try:
                production_date = datetime.strptime(production_date_raw, '%Y-%m-%d').date()
            except ValueError:
                production_date = None
                errors.append('Production date is required.')

            if not to_outlet:
                errors.append('Destination outlet is required.')
            if transfer_method not in ('delivery', 'pickup'):
                transfer_method = 'delivery'

            production_log_options = list_transfer_production_log_options(cur, production_date, selected_outlet_filter) if production_date else []
            production_log_option_map = {str(row.get('production_log_id')): row for row in production_log_options if row.get('production_log_id')}
            item_names = request.form.getlist('line_item_name[]')
            quantities = request.form.getlist('line_qty[]')
            units = request.form.getlist('line_unit[]')
            line_notes = request.form.getlist('line_notes[]')
            production_log_ids = request.form.getlist('line_production_log_id[]')
            max_len = max(len(item_names), len(quantities), len(units), len(line_notes), len(production_log_ids), 0)
            for idx in range(max_len):
                item_name = clean_menu_text(item_names[idx] if idx < len(item_names) else '')
                qty_raw = (quantities[idx] if idx < len(quantities) else '').strip()
                unit = clean_menu_text(units[idx] if idx < len(units) else '')
                row_note = clean_menu_text(line_notes[idx] if idx < len(line_notes) else '')
                production_log_id_raw = (production_log_ids[idx] if idx < len(production_log_ids) else '').strip()
                source_log = None
                production_log_id = None
                if production_log_id_raw:
                    if not production_log_id_raw.isdigit():
                        errors.append('Production log reference must be valid.')
                        continue
                    production_log_id = int(production_log_id_raw)
                    source_log = production_log_option_map.get(production_log_id_raw)
                    if not source_log:
                        errors.append('Selected production log line was not found for this date/filter.')
                        continue
                if not any([item_name, qty_raw, unit, row_note, production_log_id]):
                    continue
                if source_log:
                    if not item_name:
                        item_name = clean_menu_text(source_log.get('item_name')) or item_name
                    if not qty_raw:
                        qty_raw = str(source_log.get('quantity') or '')
                    if not unit:
                        unit = clean_menu_text(source_log.get('quantity_unit')) or unit
                qty = to_float(qty_raw)
                if qty <= 0:
                    errors.append('Transfer line quantities must be greater than zero.')
                    continue
                transfer_lines.append({
                    'item_name': item_name or 'Transfer Item',
                    'quantity': qty,
                    'quantity_unit': unit or 'each',
                    'notes': row_note or None,
                    'production_log_id': production_log_id,
                })
            if not transfer_lines:
                errors.append('Add at least one transfer line item.')

            default_transfer = {
                'production_date': production_date_raw,
                'from_location': from_location,
                'to_outlet': to_outlet,
                'transferred_by': transferred_by,
                'transfer_method': transfer_method,
                'notes': notes,
            }

            if errors:
                flash(' '.join(sorted(set(errors))), 'error')
            else:
                try:
                    cur.execute(
                        """
                        INSERT INTO commissary_transfers (
                            production_date,
                            from_location,
                            to_outlet,
                            transferred_by,
                            transfer_method,
                            notes
                        )
                        VALUES (%s, %s, %s, %s, %s, %s)
                        RETURNING id
                    """,
                        (
                            production_date,
                            from_location,
                            to_outlet,
                            transferred_by or None,
                            transfer_method,
                            notes or None,
                        ),
                    )
                    transfer_id = (cur.fetchone() or {}).get('id')

                    for line in transfer_lines:
                        cur.execute(
                            """
                            INSERT INTO commissary_transfer_lines (
                                transfer_id,
                                production_log_id,
                                item_name,
                                quantity,
                                quantity_unit,
                                notes
                            )
                            VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                            (
                                transfer_id,
                                line.get('production_log_id'),
                                line.get('item_name'),
                                line.get('quantity'),
                                line.get('quantity_unit'),
                                line.get('notes'),
                            ),
                        )
                    conn.commit()
                    flash('Transfer logged.', 'success')
                    return redirect(url_for('commissary_transfers'))
                except Exception:
                    conn.rollback()
                    flash('Could not save transfer log.', 'error')

        cur.execute(
            """
            SELECT
                t.id,
                t.production_date,
                t.from_location,
                t.to_outlet,
                t.transferred_by,
                t.transfer_method,
                t.notes,
                t.created_at,
                COUNT(line.id) AS line_count
            FROM commissary_transfers t
            LEFT JOIN commissary_transfer_lines line ON line.transfer_id = t.id
            GROUP BY t.id
            ORDER BY t.production_date DESC, t.created_at DESC
            LIMIT 40
        """
        )
        transfers = cur.fetchall()
        transfer_ids = [row.get('id') for row in transfers if row.get('id')]
        lines_by_transfer = {}
        if transfer_ids:
            cur.execute(
                """
                SELECT
                    transfer_id,
                    production_log_id,
                    item_name,
                    quantity,
                    quantity_unit,
                    notes,
                    pl.line_id,
                    COALESCE(src_line.item_name, src_recipe.name, 'Item') AS source_item_name
                FROM commissary_transfer_lines
                LEFT JOIN commissary_production_logs pl ON pl.id = commissary_transfer_lines.production_log_id
                LEFT JOIN commissary_order_lines src_line ON src_line.id = pl.line_id
                LEFT JOIN recipes src_recipe ON src_recipe.id = src_line.recipe_id
                WHERE transfer_id = ANY(%s)
                ORDER BY transfer_id DESC, id
            """,
                (transfer_ids,),
            )
            for row in cur.fetchall():
                transfer_id = row.get('transfer_id')
                if transfer_id not in lines_by_transfer:
                    lines_by_transfer[transfer_id] = []
                lines_by_transfer[transfer_id].append(row)

        option_date_raw = (default_transfer.get('production_date') or '').strip()
        try:
            option_date = datetime.strptime(option_date_raw, '%Y-%m-%d').date() if option_date_raw else date.today()
        except ValueError:
            option_date = date.today()
        production_log_options = list_transfer_production_log_options(cur, option_date, selected_outlet_filter)

    return render_template(
        'commissary_transfers.html',
        transfer=default_transfer,
        transfer_lines=transfer_lines,
        outlet_options=outlet_options,
        selected_outlet_filter=selected_outlet_filter,
        production_log_options=production_log_options,
        transfers=transfers,
        lines_by_transfer=lines_by_transfer,
    )


def build_commissary_ingredient_usage_from_logs(cur, start_date, end_date, selected_outlet, unit_system):
    cur.execute(
        """
        SELECT
            pl.id AS production_log_id,
            pl.line_id,
            pl.actual_yield,
            pl.actual_yield_unit,
            pl.signed_off,
            pl.made_by,
            line.recipe_id,
            line.quantity AS line_quantity,
            line.quantity_unit AS line_quantity_unit,
            r.yield_qty,
            r.yield_unit
        FROM commissary_production_logs pl
        JOIN commissary_order_lines line ON line.id = pl.line_id
        JOIN commissary_orders o ON o.id = line.order_id
        LEFT JOIN recipes r ON r.id = line.recipe_id
        WHERE pl.production_date BETWEEN %s AND %s
          AND line.recipe_id IS NOT NULL
          AND COALESCE(NULLIF(TRIM(o.status), ''), 'draft') <> 'cancelled'
          AND (%s = '' OR o.outlet = %s)
          AND (
              COALESCE(pl.signed_off, FALSE) = TRUE
              OR COALESCE(pl.actual_yield, 0) > 0
              OR COALESCE(NULLIF(TRIM(pl.made_by), ''), '') <> ''
          )
    """,
        (start_date, end_date, selected_outlet or '', selected_outlet or ''),
    )
    log_rows = cur.fetchall()
    if not log_rows:
        return []

    ingredient_totals = {}
    component_cache = {}
    for row in log_rows:
        recipe_id = row.get('recipe_id')
        if not recipe_id:
            continue

        produced_qty = to_float(row.get('actual_yield'))
        produced_unit = clean_menu_text(row.get('actual_yield_unit'))
        if produced_qty <= 0:
            produced_qty = to_float(row.get('line_quantity'))
            produced_unit = clean_menu_text(row.get('line_quantity_unit'))
        if produced_qty <= 0:
            continue

        ratio = ratio_from_line_quantity(
            {
                'quantity': produced_qty,
                'quantity_unit': produced_unit,
            },
            {
                'yield_qty': row.get('yield_qty'),
                'yield_unit': row.get('yield_unit'),
            },
        )
        if ratio <= 0:
            continue

        cache_key = (recipe_id, round(ratio, 6))
        components = component_cache.get(cache_key)
        if components is None:
            components, _, _ = build_component_tree(
                cur,
                recipe_id,
                ratio,
                0,
                set(),
                unit_system,
                apply_q_factor=False,
            )
            component_cache[cache_key] = components

        collect_ingredients_from_components(components, ingredient_totals)

    if not ingredient_totals:
        return []

    ingredient_ids = sorted({ing_id for ing_id, _ in ingredient_totals.keys() if ing_id})
    ingredient_map = {}
    if ingredient_ids:
        cur.execute(
            """
            SELECT id, name, category
            FROM ingredients
            WHERE id = ANY(%s)
        """,
            (ingredient_ids,),
        )
        ingredient_map = {row['id']: row for row in cur.fetchall()}

    ingredient_usage_rows = []
    for (ing_id, unit), qty in ingredient_totals.items():
        ingredient = ingredient_map.get(ing_id, {})
        display = smart_quantity(qty, unit, unit_system)
        ingredient_usage_rows.append(
            {
                'id': ing_id,
                'name': ingredient.get('name') or 'Unknown',
                'category': ingredient.get('category') or 'Uncategorized',
                'quantity': qty,
                'unit': unit,
                'display_quantity': display.get('quantity'),
                'display_unit': display.get('unit') or unit,
            }
        )

    ingredient_usage_rows.sort(key=lambda row: to_float(row.get('quantity')), reverse=True)
    return ingredient_usage_rows


@bp.route('/commissary/stats')
@login_required
def commissary_stats():
    preset = (request.args.get('range') or 'week').strip().lower()
    selected_outlet = clean_menu_text(request.args.get('outlet'))
    preset, start_date, end_date = get_stats_window(
        preset,
        request.args.get('start_date'),
        request.args.get('end_date'),
    )

    with get_cursor() as cur:
        ensure_commissary_tables(cur)
        outlet_options = get_commissary_outlet_options(cur)
        unit_system = get_unit_system()
        ingredient_usage_rows = build_commissary_ingredient_usage_from_logs(
            cur,
            start_date,
            end_date,
            selected_outlet,
            unit_system,
        )

        params = [start_date, end_date]
        outlet_sql = ""
        if selected_outlet:
            outlet_sql = " AND o.outlet = %s"
            params.append(selected_outlet)

        cur.execute(
            f"""
            SELECT
                line.recipe_id,
                COALESCE(r.name, line.item_name, 'Item') AS recipe_name,
                COUNT(*)::BIGINT AS order_count,
                ARRAY_AGG(DISTINCT o.outlet ORDER BY o.outlet) AS outlets
            FROM commissary_orders o
            JOIN commissary_order_lines line ON line.order_id = o.id
            LEFT JOIN recipes r ON r.id = line.recipe_id
            WHERE o.needed_date BETWEEN %s AND %s
              AND COALESCE(NULLIF(TRIM(o.status), ''), 'draft') <> 'cancelled'
              {outlet_sql}
            GROUP BY line.recipe_id, COALESCE(r.name, line.item_name, 'Item')
            ORDER BY order_count DESC, recipe_name
            LIMIT 40
        """,
            params,
        )
        most_requested_count = cur.fetchall()

        cur.execute(
            f"""
            SELECT
                line.recipe_id,
                COALESCE(r.name, line.item_name, 'Item') AS recipe_name,
                COALESCE(NULLIF(TRIM(line.quantity_unit), ''), 'each') AS quantity_unit,
                SUM(COALESCE(line.quantity, 0))::NUMERIC AS total_quantity
            FROM commissary_orders o
            JOIN commissary_order_lines line ON line.order_id = o.id
            LEFT JOIN recipes r ON r.id = line.recipe_id
            WHERE o.needed_date BETWEEN %s AND %s
              AND COALESCE(NULLIF(TRIM(o.status), ''), 'draft') <> 'cancelled'
              {outlet_sql}
            GROUP BY line.recipe_id, COALESCE(r.name, line.item_name, 'Item'), COALESCE(NULLIF(TRIM(line.quantity_unit), ''), 'each')
            ORDER BY total_quantity DESC, recipe_name
            LIMIT 80
        """,
            params,
        )
        most_requested_volume = cur.fetchall()

    def classify_unit(unit):
        text = (unit or '').strip().lower()
        if text in {'g', 'gram', 'grams', 'kg', 'lb', 'lbs', 'oz', 'ounce', 'ounces'}:
            return 'weight'
        if text in {'ml', 'l', 'liter', 'liters', 'fl oz', 'floz', 'qt', 'quart', 'gal', 'gallon', 'cup', 'cups', 'tbsp', 'tsp'}:
            return 'volume'
        return 'count'

    for row in most_requested_volume:
        row['unit_group'] = classify_unit(row.get('quantity_unit'))

    return render_template(
        'commissary_stats.html',
        preset=preset,
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        selected_outlet=selected_outlet,
        outlet_options=outlet_options,
        ingredient_usage_rows=ingredient_usage_rows,
        most_requested_count=most_requested_count,
        most_requested_volume=most_requested_volume,
    )


@bp.route('/commissary/review')
@bp.route('/commissary/review/<week_start>')
@login_required
def commissary_review(week_start=None):
    selected_outlet = clean_menu_text(request.args.get('outlet'))
    week_start_input = (request.args.get('week_start') or week_start or '').strip()
    week_start_date, week_end_date = get_review_window(week_start_input)
    with get_cursor() as cur:
        ensure_commissary_tables(cur)
        outlet_options = get_commissary_outlet_options(cur)
        review = build_weekly_review_context(cur, week_start_date, week_end_date, selected_outlet)

    return render_template(
        'commissary_review.html',
        week_start=week_start_date.isoformat(),
        week_end=week_end_date.isoformat(),
        selected_outlet=selected_outlet,
        outlet_options=outlet_options,
        review=review,
    )


@bp.route('/commissary/review/<week_start>/pdf')
@login_required
def commissary_review_pdf(week_start):
    selected_outlet = clean_menu_text(request.args.get('outlet'))
    week_start_date, week_end_date = get_review_window(week_start)
    with get_cursor() as cur:
        ensure_commissary_tables(cur)
        review = build_weekly_review_context(cur, week_start_date, week_end_date, selected_outlet)

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    y = height - 48

    def line(text, size=10, gap=14):
        nonlocal y
        pdf.setFont('Helvetica', size)
        pdf.drawString(42, y, text)
        y -= gap
        if y < 60:
            pdf.showPage()
            y = height - 48

    pdf.setTitle(f'Commissary Weekly Review {week_start_date.isoformat()}')
    line('Foxtown HQ - Commissary Weekly Review', size=14, gap=20)
    line(f'Week: {week_start_date.isoformat()} to {week_end_date.isoformat()}', size=10)
    if selected_outlet:
        line(f'Outlet Filter: {selected_outlet}', size=10)
    line(f"Generated: {datetime.now().strftime('%b %d, %Y %I:%M %p')}", size=10, gap=18)

    line('Summary', size=12, gap=16)
    line(f"Total production lines: {review.get('total_lines', 0)}")
    line(f"Signed off lines: {review.get('signed_off_lines', 0)}")
    line(f"Completion rate: {review.get('completion_rate', 0)}%")
    line(f"Transfers logged: {len(review.get('transfers', []))}", gap=18)

    line('Items by Outlet', size=12, gap=16)
    if review.get('items_by_outlet'):
        for row in review.get('items_by_outlet', []):
            line(f"- {row.get('outlet')}: {row.get('line_count')} lines")
    else:
        line('- No outlet production lines in this week.')
    y -= 4

    line('Daily Breakdown', size=12, gap=16)
    if review.get('orders'):
        for order in review.get('orders', []):
            line_count = int(order.get('line_count') or 0)
            signed_count = int(order.get('signed_off_line_count') or 0)
            line(
                f"- {order.get('needed_date')} | {order.get('outlet')} | {line_count} lines | {signed_count} signed"
            )
    else:
        line('- No orders in this week.')
    y -= 4

    line('Transfers', size=12, gap=16)
    if review.get('transfers'):
        for transfer in review.get('transfers', []):
            line(
                f"- {transfer.get('production_date')} | {transfer.get('to_outlet')} | {transfer.get('line_count')} items | {transfer.get('transfer_method')}"
            )
    else:
        line('- No transfers in this week.')
    y -= 4

    line('Issues / Flags', size=12, gap=16)
    if review.get('incomplete_lines'):
        for line_row in review.get('incomplete_lines')[:40]:
            line(
                f"- {line_row.get('needed_date')} | {line_row.get('outlet')} | {line_row.get('item_name')} (assigned: {line_row.get('assigned_to') or 'unassigned'})"
            )
    else:
        line('- No unsigned production lines.')

    pdf.showPage()
    pdf.save()
    payload = buffer.getvalue()
    buffer.close()

    filename = f'commissary-review-{week_start_date.isoformat()}.pdf'
    return Response(
        payload,
        mimetype='application/pdf',
        headers={'Content-Disposition': f'attachment; filename={filename}'},
    )


@bp.route('/commissary/print')
@bp.route('/commissary-planner/packet/print')
@login_required
def commissary_packet_print():
    conn = get_db()
    with get_cursor() as cur:
        ensure_commissary_tables(cur)
        conn.commit()

        selected_outlet = clean_menu_text(request.args.get('outlet'))
        selected_units = get_unit_system()
        week_start_raw = (request.args.get('week_start') or request.args.get('start_date') or '').strip()
        start_date, end_date = get_commissary_week_window(week_start_raw)
        selected_day_raw = (request.args.get('day') or '').strip()
        if selected_day_raw:
            try:
                selected_day = datetime.strptime(selected_day_raw, '%Y-%m-%d').date()
                start_date = selected_day
                end_date = selected_day
            except ValueError:
                selected_day = None
        else:
            selected_day = None

        include_shopping_raw = request.args.get('include_shopping')
        if include_shopping_raw is None:
            include_shopping = False
        else:
            include_shopping = (include_shopping_raw or '').strip().lower() in ('1', 'true', 'yes', 'on')

        datasets = build_commissary_datasets(cur, start_date, end_date, selected_outlet, selected_units)
        for prep in datasets.get('weekly_prep', []):
            prep['instruction_steps'] = split_instruction_steps(prep.get('instructions'))
        prep_groups = build_commissary_prep_groups(cur, datasets, selected_units)
        checklist_lines = []
        for order in datasets.get('orders', []):
            for line in order.get('lines', []):
                production_log = line.get('production_log') or {}
                checklist_lines.append({
                    'item_name': line.get('item_name') or line.get('recipe_name') or 'Item',
                    'recipe_name': line.get('recipe_name') or '',
                    'quantity': line.get('quantity'),
                    'quantity_unit': line.get('quantity_unit') or 'each',
                    'outlet': order.get('outlet') or DEFAULT_COMMISSARY_OUTLET,
                    'assigned_to': production_log.get('assigned_to') or '',
                    'notes': line.get('notes') or '',
                })
        checklist_lines.sort(key=lambda row: ((row.get('outlet') or '').lower(), (row.get('item_name') or '').lower()))

    return render_template(
        'commissary_packet_print.html',
        selected_outlet=selected_outlet,
        selected_units=selected_units,
        week_start=start_date.isoformat(),
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        selected_day=selected_day.isoformat() if selected_day else '',
        include_shopping=include_shopping,
        generated_at=datetime.now().strftime('%b %d, %Y %I:%M %p'),
        datasets=datasets,
        checklist_lines=checklist_lines,
        prep_groups=prep_groups,
    )
