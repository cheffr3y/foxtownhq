from datetime import date, datetime, timedelta

from db import get_cursor, get_db
from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for
from flask_login import login_required

from helpers.commissary import (
    COMMISSARY_ACTIVE_STATUSES,
    COMMISSARY_STATUS_CHOICES,
    DEFAULT_COMMISSARY_OUTLET,
    build_commissary_datasets,
    build_commissary_prep_groups,
    ensure_commissary_tables,
    get_commissary_order,
    get_commissary_order_lines,
    get_commissary_outlet_options,
    get_commissary_week_window,
    parse_commissary_order_lines,
)
from helpers.formatting import split_instruction_steps
from helpers.menu import clean_menu_text
from helpers.recipes import normalize_recipe_type
from helpers.shared import generate_id, handle_route_error, to_float
from helpers.units import format_number, get_unit_system

bp = Blueprint('commissary', __name__)


@bp.errorhandler(Exception)
def handle_commissary_error(error):
    return handle_route_error(error, 'commissary')


def list_commissary_recipe_options(cur):
    cur.execute("""
        SELECT id,
               name,
               recipe_type,
               category,
               yield_qty,
               yield_unit
        FROM recipes
        ORDER BY CASE WHEN recipe_type = 'batch' THEN 0 WHEN recipe_type = 'menu' THEN 1 ELSE 2 END,
                 name
    """)
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


def upsert_commissary_line_production_log(cur, order_id, line_id, production_date, line):
    if not line_id or not production_date:
        return False

    made_by = clean_menu_text(line.get('production_made_by'))
    tasted_by = clean_menu_text(line.get('production_tasted_by'))
    signed_off_by = clean_menu_text(line.get('production_signed_off_by'))
    production_notes = clean_menu_text(line.get('production_notes'))
    signed_off = bool(line.get('production_signed_off'))
    has_values = any([made_by, tasted_by, signed_off_by, production_notes, signed_off])

    if has_values:
        cur.execute("""
            INSERT INTO commissary_production_logs (
                order_id,
                order_item_id,
                production_date,
                made_by,
                signed_off,
                signed_off_by,
                tasted_by,
                notes,
                signed_off_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE NULL END)
            ON CONFLICT (order_item_id, production_date)
            DO UPDATE SET
                made_by = EXCLUDED.made_by,
                signed_off = EXCLUDED.signed_off,
                signed_off_by = EXCLUDED.signed_off_by,
                tasted_by = EXCLUDED.tasted_by,
                notes = EXCLUDED.notes,
                signed_off_at = CASE
                    WHEN EXCLUDED.signed_off THEN COALESCE(commissary_production_logs.signed_off_at, CURRENT_TIMESTAMP)
                    ELSE NULL
                END,
                updated_at = CURRENT_TIMESTAMP
        """, (
            order_id,
            line_id,
            production_date,
            made_by or None,
            signed_off,
            signed_off_by or None,
            tasted_by or None,
            production_notes or None,
            signed_off
        ))
        return True

    cur.execute("""
        DELETE FROM commissary_production_logs
        WHERE order_item_id = %s AND production_date = %s
    """, (line_id, production_date))
    return False


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

        all_daily_groups = datasets.get('daily_groups', []) or []
        if view_mode == 'day':
            filtered_daily_groups = [group for group in all_daily_groups if group.get('date') == selected_day]
        else:
            filtered_daily_groups = all_daily_groups

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
        filtered_daily_groups=filtered_daily_groups,
        orders=orders,
        metrics=metrics,
        selected_day=selected_day.isoformat(),
        view_mode=view_mode
    )


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

        order = {
            'outlet': DEFAULT_COMMISSARY_OUTLET,
            'needed_date': '',
            'status': 'pending',
            'notes': ''
        }
        lines = []

        if request.method == 'POST':
            errors = []
            outlet = clean_menu_text(request.form.get('outlet'))
            needed_date_raw = (request.form.get('needed_date') or '').strip()
            status = (request.form.get('status') or 'pending').strip().lower()
            notes = clean_menu_text(request.form.get('notes'))

            valid_statuses = {choice[0] for choice in COMMISSARY_STATUS_CHOICES}
            if status not in valid_statuses:
                status = 'pending'

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
                'notes': notes
            }

            if errors:
                flash(' '.join(sorted(set(errors))), 'error')
            else:
                order_id = generate_id('cor_')
                try:
                    cur.execute("""
                        INSERT INTO outlet_orders (id, outlet, needed_date, status, notes)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (
                        order_id,
                        outlet,
                        needed_date,
                        status,
                        notes or None
                    ))

                    for idx, line in enumerate(lines):
                        recipe_id = line.get('recipe_id')
                        item_name = line.get('item_name') or recipe_name_map.get(recipe_id) or None
                        cur.execute("""
                            INSERT INTO outlet_order_items (
                                order_id,
                                recipe_id,
                                item_name,
                                quantity,
                                quantity_unit,
                                notes,
                                sort_order
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            RETURNING id
                        """, (
                            order_id,
                            recipe_id,
                            item_name,
                            line.get('quantity'),
                            line.get('quantity_unit') or 'each',
                            line.get('notes'),
                            idx
                        ))
                        inserted_row = cur.fetchone() or {}
                        line_id = inserted_row.get('id')
                        upsert_commissary_line_production_log(cur, order_id, line_id, needed_date, line)

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
        status_options=COMMISSARY_STATUS_CHOICES
    )


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
            status = (request.form.get('status') or 'pending').strip().lower()
            notes = clean_menu_text(request.form.get('notes'))

            valid_statuses = {choice[0] for choice in COMMISSARY_STATUS_CHOICES}
            if status not in valid_statuses:
                status = 'pending'

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
                    'notes': notes
                }
            else:
                try:
                    cur.execute("""
                        UPDATE outlet_orders
                        SET outlet = %s,
                            needed_date = %s,
                            status = %s,
                            notes = %s,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = %s
                    """, (
                        outlet,
                        needed_date,
                        status,
                        notes or None,
                        order_id
                    ))

                    cur.execute("DELETE FROM outlet_order_items WHERE order_id = %s", (order_id,))
                    for idx, line in enumerate(lines):
                        recipe_id = line.get('recipe_id')
                        item_name = line.get('item_name') or recipe_name_map.get(recipe_id) or None
                        cur.execute("""
                            INSERT INTO outlet_order_items (
                                order_id,
                                recipe_id,
                                item_name,
                                quantity,
                                quantity_unit,
                                notes,
                                sort_order
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            RETURNING id
                        """, (
                            order_id,
                            recipe_id,
                            item_name,
                            line.get('quantity'),
                            line.get('quantity_unit') or 'each',
                            line.get('notes'),
                            idx
                        ))
                        inserted_row = cur.fetchone() or {}
                        line_id = inserted_row.get('id')
                        upsert_commissary_line_production_log(cur, order_id, line_id, needed_date, line)

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
                        'notes': notes
                    }

            return render_template(
                'commissary_order_form.html',
                page_title='Edit Commissary Order',
                order=order,
                lines=lines,
                recipe_options=recipe_options,
                outlet_options=outlet_options,
                status_options=COMMISSARY_STATUS_CHOICES
            )

        lines = get_commissary_order_lines(cur, order_id)
        order = {
            **existing,
            'needed_date': existing.get('needed_date').isoformat() if existing.get('needed_date') else ''
        }

    return render_template(
        'commissary_order_form.html',
        page_title='Edit Commissary Order',
        order=order,
        lines=lines,
        recipe_options=recipe_options,
        outlet_options=outlet_options,
        status_options=COMMISSARY_STATUS_CHOICES
    )


@bp.route('/commissary-planner/orders/<order_id>/delete', methods=['POST'])
@login_required
def commissary_order_delete(order_id):
    conn = get_db()
    with get_cursor() as cur:
        ensure_commissary_tables(cur)
        conn.commit()
        cur.execute("SELECT id, outlet, needed_date FROM outlet_orders WHERE id = %s", (order_id,))
        order = cur.fetchone()
        if not order:
            flash('Commissary order not found.', 'error')
            return redirect(url_for('commissary_planner'))

        try:
            cur.execute("DELETE FROM outlet_orders WHERE id = %s", (order_id,))
            conn.commit()
            flash(f"Deleted commissary order for {order.get('outlet')} on {order.get('needed_date')}.", 'success')
        except Exception:
            conn.rollback()
            flash('Error deleting commissary order.', 'error')
    return redirect(url_for('commissary_planner'))


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

        cur.execute("""
            SELECT oi.id AS line_id,
                   oi.order_id,
                   o.outlet,
                   o.needed_date
            FROM outlet_order_items oi
            JOIN outlet_orders o ON o.id = oi.order_id
            WHERE oi.id = %s
            LIMIT 1
        """, (line_id,))
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

        made_by = clean_menu_text(request.form.get('made_by'))
        tasted_by = clean_menu_text(request.form.get('tasted_by'))
        signed_off_by = clean_menu_text(request.form.get('signed_off_by'))
        production_notes = clean_menu_text(request.form.get('production_notes'))
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
            saved = upsert_commissary_line_production_log(cur, line.get('order_id'), line_id, production_date, {
                'production_made_by': made_by,
                'production_tasted_by': tasted_by,
                'production_signed_off_by': signed_off_by,
                'production_notes': production_notes,
                'production_signed_off': signed_off
            })
            if saved:
                cur.execute("""
                    SELECT updated_at
                    FROM commissary_production_logs
                    WHERE order_item_id = %s
                      AND production_date = %s
                    LIMIT 1
                """, (line_id, production_date))
                saved_row = cur.fetchone() or {}
                updated_at = saved_row.get('updated_at')
                response_payload = {
                    'ok': True,
                    'saved': True,
                    'signed_off': signed_off,
                    'updated_at': updated_at.isoformat() if updated_at else None,
                    'message': 'Production log updated.'
                }
                if not is_ajax:
                    flash('Production log updated.', 'success')
            else:
                response_payload = {
                    'ok': True,
                    'saved': False,
                    'signed_off': False,
                    'updated_at': None,
                    'message': 'Production log cleared.'
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
            'week_start': week_start or (production_date.isoformat() if production_date else '')
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
        prep_groups=prep_groups
    )
