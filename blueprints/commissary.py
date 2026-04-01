import io
import traceback
from datetime import date, datetime, timedelta

from db import get_cursor, get_db
from flask import Blueprint, Response, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.pdfgen import canvas
from xml.sax.saxutils import escape

from helpers.commissary import (
    COMMISSARY_SOURCE_CHOICES,
    COMMISSARY_STATUS_CHOICES,
    DEFAULT_COMMISSARY_OUTLET,
    build_commissary_datasets,
    ensure_commissary_tables,
    get_commissary_order,
    get_commissary_order_lines,
    get_commissary_outlet_options,
    get_commissary_week_window,
    normalize_commissary_source,
    normalize_commissary_status,
    parse_commissary_order_lines,
)
from helpers.menu import clean_menu_text
from helpers.recipes import build_component_tree, collect_ingredients_from_components, normalize_recipe_type, ratio_from_line_quantity
from helpers.shared import generate_id, handle_route_error, to_float
from helpers.units import format_number, get_unit_system, smart_quantity

bp = Blueprint('commissary', __name__)


@bp.errorhandler(Exception)
def handle_commissary_error(error):
    traceback.print_exc()
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
    daily_rows = []
    for day_group in datasets.get('daily_production_days', []) or []:
        for source_group in day_group.get('source_groups', []) or []:
            for entry in source_group.get('entries', []) or []:
                if entry.get('cancelled_line'):
                    continue
                if not entry.get('line_id'):
                    continue
                daily_rows.append(
                    {
                        'production_date': day_group.get('date'),
                        'outlet': entry.get('outlet') or DEFAULT_COMMISSARY_OUTLET,
                        'source_label': source_group.get('label') or entry.get('source_label') or 'Outlet Request',
                        'item_name': entry.get('item_name') or 'Item',
                        'quantity': entry.get('quantity'),
                        'quantity_unit': entry.get('quantity_unit') or 'each',
                        'assigned_to': entry.get('assigned_to') or '',
                        'made_by': entry.get('made_by') or '',
                        'tasted_by': entry.get('tasted_by') or '',
                        'production_notes': entry.get('production_notes') or '',
                        'signed_off': bool(entry.get('signed_off')),
                        'pending_rollup': bool(entry.get('pending_rollup')),
                        'cancelled_line': bool(entry.get('cancelled_line')),
                        'signed_off_by': entry.get('signed_off_by') or '',
                        'prep_day_label': entry.get('prep_day_label') or '',
                        'notes': entry.get('notes') or '',
                    }
                )

    total_lines = len(daily_rows)
    signed_off_lines = sum(1 for row in daily_rows if row.get('signed_off'))
    completion_rate = round((signed_off_lines / total_lines) * 100, 1) if total_lines else 0

    items_by_outlet = {}
    incomplete_lines = []
    deferred_lines = []
    for row in daily_rows:
        outlet_name = row.get('outlet') or DEFAULT_COMMISSARY_OUTLET
        items_by_outlet[outlet_name] = items_by_outlet.get(outlet_name, 0) + 1
        if not row.get('signed_off'):
            if row.get('pending_rollup'):
                deferred_lines.append(
                    {
                        'needed_date': row.get('production_date'),
                        'outlet': outlet_name,
                        'item_name': row.get('item_name'),
                        'reason': row.get('production_notes') or '',
                        'assigned_to': row.get('assigned_to') or '',
                    }
                )
            else:
                incomplete_lines.append(
                    {
                        'needed_date': row.get('production_date'),
                        'outlet': outlet_name,
                        'item_name': row.get('item_name'),
                        'assigned_to': row.get('assigned_to') or '',
                        'made_by': row.get('made_by') or '',
                        'tasted_by': row.get('tasted_by') or '',
                        'production_notes': row.get('production_notes') or '',
                        'notes': row.get('notes') or '',
                        'prep_day_label': row.get('prep_day_label') or '',
                    }
                )

    daily_rows.sort(
        key=lambda row: (
            row.get('production_date') or week_start,
            (row.get('outlet') or '').lower(),
            (row.get('item_name') or '').lower(),
        )
    )

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
        'daily_rows': daily_rows,
        'incomplete_lines': incomplete_lines,
        'deferred_lines': deferred_lines,
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
    pending_rollup = bool(payload.get('pending_rollup')) and not signed_off
    cancelled_line = bool(payload.get('cancelled_line'))

    if cancelled_line:
        signed_off = False
        pending_rollup = False

    has_values = any([
        assigned_to,
        made_by,
        tasted_by,
        signed_off_by,
        production_notes,
        actual_yield,
        actual_yield_unit,
        signed_off,
        pending_rollup,
        cancelled_line,
    ])

    if not has_values:
        cur.execute(
            """
            SELECT 1
            FROM commissary_production_logs
            WHERE line_id = %s
              AND production_date = %s
            LIMIT 1
        """,
            (line_id, production_date),
        )
        if not cur.fetchone():
            return False

    try:
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
                pending_rollup,
                cancelled_line,
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
                pending_rollup = EXCLUDED.pending_rollup,
                cancelled_line = EXCLUDED.cancelled_line,
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
                pending_rollup,
                cancelled_line,
                signed_off,
            ),
        )
    except Exception as exc:
        traceback.print_exc()
        print(f"commissary upsert ON CONFLICT fallback triggered: {type(exc).__name__}: {exc}")
        cur.execute(
            """
            UPDATE commissary_production_logs
            SET assigned_to = %s,
                made_by = %s,
                tasted_by = %s,
                signed_off = %s,
                signed_off_by = %s,
                production_notes = %s,
                actual_yield = %s,
                actual_yield_unit = %s,
                pending_rollup = %s,
                cancelled_line = %s,
                signed_off_at = CASE
                    WHEN %s THEN COALESCE(signed_off_at, CURRENT_TIMESTAMP)
                    ELSE NULL
                END,
                updated_at = CURRENT_TIMESTAMP
            WHERE line_id = %s
              AND production_date = %s
        """,
            (
                assigned_to or None,
                made_by or None,
                tasted_by or None,
                signed_off,
                signed_off_by or None,
                production_notes or None,
                actual_yield or None,
                actual_yield_unit or None,
                pending_rollup,
                cancelled_line,
                signed_off,
                line_id,
                production_date,
            ),
        )
        if cur.rowcount == 0:
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
                    pending_rollup,
                    cancelled_line,
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
                    %s,
                    %s,
                    CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE NULL END,
                    CURRENT_TIMESTAMP
                )
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
                    pending_rollup,
                    cancelled_line,
                    signed_off,
                ),
            )
    return True


@bp.route('/commissary')
@bp.route('/commissary-planner')
@login_required
def commissary_planner():
    conn = get_db()
    with get_cursor() as cur:
        ensure_commissary_tables(cur)
        conn.commit()

        selected_day_raw = (request.args.get('day') or '').strip()
        today = date.today()
        try:
            selected_day = datetime.strptime(selected_day_raw, '%Y-%m-%d').date() if selected_day_raw else today
        except ValueError:
            selected_day = today

        start_date, end_date = get_commissary_week_window(selected_day.isoformat())
        if selected_day < start_date or selected_day > end_date:
            selected_day = today if start_date <= today <= end_date else start_date

        signoff_day_raw = (request.args.get('signoff_day') or '').strip()
        try:
            signoff_day = datetime.strptime(signoff_day_raw, '%Y-%m-%d').date() if signoff_day_raw else None
        except ValueError:
            signoff_day = None
        if not signoff_day:
            signoff_day = today if start_date <= today <= end_date else selected_day
        if signoff_day < start_date or signoff_day > end_date:
            signoff_day = selected_day

        active_tab = (request.args.get('tab') or '').strip().lower()
        if active_tab not in ('production', 'signoff', 'transfers'):
            active_tab = 'signoff' if datetime.now().hour >= 14 else 'production'

        selected_outlet = clean_menu_text(request.args.get('outlet'))
        selected_units = get_unit_system()
        outlet_options = get_commissary_outlet_options(cur)

        datasets = build_commissary_datasets(cur, start_date, end_date, selected_outlet, selected_units)
        all_daily_days = datasets.get('daily_production_days', []) or []
        day_map = {day_row.get('date'): day_row for day_row in all_daily_days if day_row.get('date')}

        selected_day_data = day_map.get(selected_day) or {'source_groups': []}
        production_by_outlet = {}
        for source_group in selected_day_data.get('source_groups', []):
            for entry in source_group.get('entries', []):
                outlet_name = entry.get('outlet') or DEFAULT_COMMISSARY_OUTLET
                if outlet_name not in production_by_outlet:
                    production_by_outlet[outlet_name] = []
                production_by_outlet[outlet_name].append(
                    {
                        'item_name': entry.get('item_name') or 'Item',
                        'quantity': entry.get('quantity'),
                        'quantity_unit': entry.get('quantity_unit') or 'each',
                        'outlet': outlet_name,
                    }
                )
        production_groups = []
        for outlet_name in sorted(production_by_outlet.keys(), key=lambda text: text.lower()):
            entries = production_by_outlet.get(outlet_name) or []
            entries.sort(key=lambda row: (row.get('item_name') or '').lower())
            production_groups.append({'outlet': outlet_name, 'entries': entries})

        signoff_entries_by_day = {}
        week_signoff_summary = []
        day_cursor = start_date
        while day_cursor <= end_date:
            day_data = day_map.get(day_cursor) or {'source_groups': []}
            entries = []
            for source_group in day_data.get('source_groups', []):
                for entry in source_group.get('entries', []):
                    if not entry.get('line_id'):
                        continue
                    entries.append(
                        {
                            'line_id': entry.get('line_id'),
                            'item_name': entry.get('item_name') or 'Item',
                            'quantity': entry.get('quantity'),
                            'quantity_unit': entry.get('quantity_unit') or 'each',
                            'outlet': entry.get('outlet') or DEFAULT_COMMISSARY_OUTLET,
                            'signed_off': bool(entry.get('signed_off')),
                            'made_by': entry.get('made_by') or '',
                            'tasted_by': entry.get('tasted_by') or '',
                            'actual_yield': entry.get('actual_yield') or '',
                            'actual_yield_unit': entry.get('actual_yield_unit') or (entry.get('quantity_unit') or 'each'),
                            'pending_rollup': bool(entry.get('pending_rollup')),
                            'cancelled_line': bool(entry.get('cancelled_line')),
                            'production_notes': entry.get('production_notes') or '',
                        }
                    )
            entries.sort(
                key=lambda row: (
                    row.get('cancelled_line') is True,
                    row.get('signed_off') is True,
                    (row.get('item_name') or '').lower(),
                )
            )
            active_entries = [row for row in entries if not row.get('cancelled_line')]
            signed_count = sum(1 for row in active_entries if row.get('signed_off'))
            day_iso = day_cursor.isoformat()
            signoff_entries_by_day[day_iso] = entries
            week_signoff_summary.append(
                {
                    'date': day_cursor,
                    'date_iso': day_iso,
                    'day_label': day_cursor.strftime('%a'),
                    'entry_count': len(active_entries),
                    'signed_off_count': signed_count,
                }
            )
            day_cursor += timedelta(days=1)

    return render_template(
        'commissary_planner.html',
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        week_start=start_date.isoformat(),
        selected_outlet=selected_outlet,
        selected_units=selected_units,
        outlet_options=outlet_options,
        datasets=datasets,
        production_groups=production_groups,
        signoff_entries_by_day=signoff_entries_by_day,
        week_signoff_summary=week_signoff_summary,
        selected_day=selected_day.isoformat(),
        signoff_day=signoff_day.isoformat(),
        active_tab=active_tab,
        current_user_name=clean_menu_text(getattr(current_user, 'username', '') or 'Chef'),
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
                        prep_start_date = line.get('prep_start_date') or needed_date
                        prep_end_date = line.get('prep_end_date') or prep_start_date or needed_date
                        cur.execute(
                            """
                            INSERT INTO commissary_order_lines (
                                order_id,
                                recipe_id,
                                item_name,
                                quantity,
                                quantity_unit,
                                prep_start_date,
                                prep_end_date,
                                notes,
                                sort_order
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                            (
                                order_id,
                                recipe_id,
                                item_name,
                                line.get('quantity'),
                                line.get('quantity_unit') or 'each',
                                prep_start_date,
                                prep_end_date,
                                line.get('notes'),
                                idx,
                            ),
                        )

                    conn.commit()
                    flash('Commissary order created.', 'success')
                    return redirect(url_for('commissary_order_edit', order_id=order_id))
                except Exception as exc:
                    conn.rollback()
                    traceback.print_exc()
                    flash(f'Error creating commissary order: {type(exc).__name__}: {exc}', 'error')

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
                        prep_start_date = line.get('prep_start_date') or needed_date
                        prep_end_date = line.get('prep_end_date') or prep_start_date or needed_date
                        cur.execute(
                            """
                            INSERT INTO commissary_order_lines (
                                order_id,
                                recipe_id,
                                item_name,
                                quantity,
                                quantity_unit,
                                prep_start_date,
                                prep_end_date,
                                notes,
                                sort_order
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                            (
                                order_id,
                                recipe_id,
                                item_name,
                                line.get('quantity'),
                                line.get('quantity_unit') or 'each',
                                prep_start_date,
                                prep_end_date,
                                line.get('notes'),
                                idx,
                            ),
                        )

                    conn.commit()
                    flash('Commissary order updated.', 'success')
                    return redirect(url_for('commissary_order_edit', order_id=order_id))
                except Exception as exc:
                    conn.rollback()
                    traceback.print_exc()
                    flash(f'Error updating commissary order: {type(exc).__name__}: {exc}', 'error')
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
        except Exception as exc:
            conn.rollback()
            traceback.print_exc()
            flash(f'Error deleting commissary order: {type(exc).__name__}: {exc}', 'error')
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
        except Exception as exc:
            conn.rollback()
            traceback.print_exc()
            error_detail = f'Could not update cook assignment: {type(exc).__name__}: {exc}'
            if is_ajax:
                return jsonify({'ok': False, 'error': error_detail}), 500
            flash(error_detail, 'error')

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
        pending_rollup = (request.form.get('pending_rollup') or '').strip().lower() in ('1', 'true', 'on', 'yes')
        cancelled_line = (request.form.get('cancelled_line') or '').strip().lower() in ('1', 'true', 'on', 'yes')

        if cancelled_line:
            signed_off = False
            pending_rollup = False

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
                    'pending_rollup': pending_rollup,
                    'cancelled_line': cancelled_line,
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
                    'pending_rollup': pending_rollup,
                    'cancelled_line': cancelled_line,
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
                    'pending_rollup': False,
                    'cancelled_line': False,
                    'updated_at': None,
                    'message': 'Production log cleared.',
                }
                if not is_ajax:
                    flash('Production log cleared.', 'success')
            conn.commit()
        except Exception as exc:
            conn.rollback()
            traceback.print_exc()
            error_detail = f'Could not save production log: {type(exc).__name__}: {exc}'
            if is_ajax:
                return jsonify({'ok': False, 'error': error_detail}), 500
            flash(error_detail, 'error')

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
            WHERE %s BETWEEN COALESCE(line.prep_start_date, o.needed_date)
                         AND COALESCE(line.prep_end_date, line.prep_start_date, o.needed_date)
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
                        pending_rollup,
                        signed_off_at,
                        updated_at
                    )
                    VALUES (%s, %s, TRUE, %s, FALSE, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT (line_id, production_date)
                    DO UPDATE SET
                        signed_off = TRUE,
                        signed_off_by = EXCLUDED.signed_off_by,
                        pending_rollup = FALSE,
                        signed_off_at = COALESCE(commissary_production_logs.signed_off_at, CURRENT_TIMESTAMP),
                        updated_at = CURRENT_TIMESTAMP
                """,
                    (line_id, selected_date, signed_off_by or None),
                )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            traceback.print_exc()
            error_detail = f'Bulk sign-off failed: {type(exc).__name__}: {exc}'
            if is_ajax:
                return jsonify({'ok': False, 'error': error_detail}), 500
            flash(error_detail, 'error')
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
                except Exception as exc:
                    conn.rollback()
                    traceback.print_exc()
                    flash(f'Could not save transfer log: {type(exc).__name__}: {exc}', 'error')

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

    header_navy = colors.HexColor('#1B2A4A')
    alert_red = colors.HexColor('#D4432A')
    good_green = colors.HexColor('#2E7D32')
    warn_amber = colors.HexColor('#F9A825')
    light_gray = colors.HexColor('#F5F5F5')
    border_gray = colors.HexColor('#DDDDDD')
    muted_text = colors.HexColor('#4F5B6E')
    white = colors.white
    margin_x = 0.5 * inch
    margin_y = 0.4 * inch
    generated_at = datetime.now().strftime('%b %d, %Y %I:%M %p')

    def fmt_date(value):
        if not value:
            return '-'
        if isinstance(value, datetime):
            return value.strftime('%b %d, %Y')
        if isinstance(value, date):
            return value.strftime('%b %d, %Y')
        raw = str(value).strip()
        if not raw:
            return '-'
        try:
            parsed = datetime.strptime(raw[:10], '%Y-%m-%d')
            return parsed.strftime('%b %d, %Y')
        except ValueError:
            return raw

    def to_text(value, default='-'):
        text = str(value or '').strip()
        return text or default

    def shorten(value, limit):
        text = to_text(value, default='')
        if len(text) <= limit:
            return text
        return f"{text[: limit - 3].rstrip()}..."

    def html_text(value, default='-'):
        return escape(to_text(value, default)).replace('\n', '<br/>')

    def qty_label(row):
        qty = format_number(row.get('quantity'))
        unit = (row.get('quantity_unit') or 'each').strip() or 'each'
        return f'{qty} {unit}'

    base_style = ParagraphStyle(
        'pdf-base',
        fontName='Helvetica',
        fontSize=8.5,
        leading=10.2,
        textColor=colors.black,
    )
    table_header_style = ParagraphStyle(
        'pdf-table-header',
        parent=base_style,
        fontName='Helvetica-Bold',
        textColor=white,
        fontSize=8.4,
        leading=9.8,
    )
    section_title_style = ParagraphStyle(
        'pdf-section-title',
        parent=base_style,
        fontName='Helvetica-Bold',
        fontSize=11,
        leading=13,
        textColor=header_navy,
        spaceAfter=4,
    )
    subtle_note_style = ParagraphStyle(
        'pdf-subtle-note',
        parent=base_style,
        fontSize=8.2,
        textColor=muted_text,
    )
    badge_style = ParagraphStyle(
        'pdf-badge',
        parent=base_style,
        fontName='Helvetica-Bold',
        fontSize=9,
        textColor=good_green,
    )
    date_divider_style = ParagraphStyle(
        'pdf-date-divider',
        parent=base_style,
        fontName='Helvetica-Bold',
        textColor=header_navy,
        fontSize=8.8,
    )

    def p(value, style=base_style, default='-'):
        return Paragraph(html_text(value, default), style)

    def status_p(is_signed, is_deferred=False):
        if is_signed:
            return Paragraph('<font color="#2E7D32"><b>Signed</b></font>', base_style)
        if is_deferred:
            return Paragraph('<font color="#A0590A"><b>Deferred</b></font>', base_style)
        return Paragraph('<font color="#D4432A"><b>Pending</b></font>', base_style)

    def draw_footer(pdf_canvas, page_num, total_pages):
        page_width, _ = letter
        pdf_canvas.setStrokeColor(border_gray)
        pdf_canvas.setLineWidth(0.6)
        pdf_canvas.line(margin_x, margin_y + 11, page_width - margin_x, margin_y + 11)
        pdf_canvas.setFont('Helvetica', 8)
        pdf_canvas.setFillColor(muted_text)
        pdf_canvas.drawString(margin_x, margin_y + 2, 'Foxtown HQ - Commissary Weekly Review')
        pdf_canvas.drawRightString(page_width - margin_x, margin_y + 2, f'Page {page_num} of {total_pages}')

    class NumberedCanvas(canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._saved_page_states = []

        def showPage(self):
            self._saved_page_states.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total_pages = len(self._saved_page_states)
            for page_num, state in enumerate(self._saved_page_states, start=1):
                self.__dict__.update(state)
                draw_footer(self, page_num, total_pages)
                super().showPage()
            super().save()

    def draw_first_page_header(pdf_canvas, doc):
        page_width, page_height = letter
        content_w = page_width - (doc.leftMargin + doc.rightMargin)
        top_y = page_height - doc.topMargin
        banner_h = 84
        banner_bottom = top_y - banner_h

        pdf_canvas.saveState()
        pdf_canvas.setFillColor(header_navy)
        pdf_canvas.rect(doc.leftMargin, banner_bottom, content_w, banner_h, stroke=0, fill=1)

        pdf_canvas.setFillColor(white)
        pdf_canvas.setFont('Helvetica-Bold', 16)
        pdf_canvas.drawString(doc.leftMargin + 12, top_y - 27, 'FOXTOWN HQ')
        pdf_canvas.setFont('Helvetica', 10)
        pdf_canvas.drawString(doc.leftMargin + 12, top_y - 43, 'Commissary Weekly Review')

        pdf_canvas.setFont('Helvetica-Bold', 9)
        pdf_canvas.drawRightString(
            page_width - doc.rightMargin - 12,
            top_y - 23,
            f'Week: {fmt_date(week_start_date)} - {fmt_date(week_end_date)}',
        )
        pdf_canvas.setFont('Helvetica', 8)
        pdf_canvas.drawRightString(page_width - doc.rightMargin - 12, top_y - 38, f'Generated: {generated_at}')

        if selected_outlet:
            tag = shorten(selected_outlet, 38)
            badge_text = f'Outlet: {tag}'
            badge_w = min(220, max(90, (len(badge_text) * 4.35) + 14))
            badge_x = page_width - doc.rightMargin - 12
            badge_y = top_y - 61
            pdf_canvas.setFillColor(colors.HexColor('#32466E'))
            pdf_canvas.roundRect(badge_x - badge_w, badge_y - 8.5, badge_w, 15.5, 7, stroke=0, fill=1)
            pdf_canvas.setFillColor(white)
            pdf_canvas.setFont('Helvetica-Bold', 7.4)
            pdf_canvas.drawRightString(badge_x - 6, badge_y - 3.8, badge_text)

        kpi_y = banner_bottom - 9
        card_h = 66
        card_gap = 8
        card_w = (content_w - (card_gap * 3)) / 4.0
        completion_rate = float(review.get('completion_rate') or 0)
        if completion_rate >= 90:
            completion_color = good_green
        elif completion_rate >= 70:
            completion_color = warn_amber
        else:
            completion_color = alert_red
        kpis = [
            (format_number(review.get('total_lines') or 0), 'Total Production Lines', header_navy),
            (format_number(review.get('signed_off_lines') or 0), 'Signed Off', header_navy),
            (f"{format_number(completion_rate)}%", 'Completion Rate', completion_color),
            (format_number(len(review.get('transfers') or [])), 'Transfers Logged', header_navy),
        ]
        for idx, (value, label, value_color) in enumerate(kpis):
            card_x = doc.leftMargin + (idx * (card_w + card_gap))
            card_y = kpi_y - card_h
            pdf_canvas.setFillColor(light_gray)
            pdf_canvas.roundRect(card_x, card_y, card_w, card_h, 6, stroke=0, fill=1)
            pdf_canvas.setStrokeColor(border_gray)
            pdf_canvas.setLineWidth(0.55)
            pdf_canvas.roundRect(card_x, card_y, card_w, card_h, 6, stroke=1, fill=0)
            pdf_canvas.setFillColor(value_color)
            pdf_canvas.setFont('Helvetica-Bold', 18)
            pdf_canvas.drawString(card_x + 9, card_y + 37, str(value))
            pdf_canvas.setFillColor(muted_text)
            pdf_canvas.setFont('Helvetica', 8.2)
            pdf_canvas.drawString(card_x + 9, card_y + 20, label)

        pdf_canvas.restoreState()

    def draw_later_pages(pdf_canvas, _doc):
        pdf_canvas.saveState()
        pdf_canvas.restoreState()

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=margin_x,
        rightMargin=margin_x,
        topMargin=margin_y,
        bottomMargin=margin_y,
        title=f'Commissary Weekly Review {week_start_date.isoformat()}',
    )
    content_width = doc.width
    story = [Spacer(1, 172)]

    story.append(Paragraph('Items by Outlet', section_title_style))
    outlet_rows = review.get('items_by_outlet') or []
    total_lines = float(review.get('total_lines') or 0)
    outlet_data = [
        [Paragraph('Outlet', table_header_style), Paragraph('Lines', table_header_style), Paragraph('Share', table_header_style)]
    ]
    if outlet_rows:
        for outlet_row in outlet_rows[:8]:
            line_count = int(outlet_row.get('line_count') or 0)
            share_pct = (line_count / total_lines * 100) if total_lines else 0
            outlet_data.append(
                [
                    p(outlet_row.get('outlet') or '-'),
                    p(format_number(line_count)),
                    p(f"{format_number(round(share_pct, 1))}%"),
                ]
            )
    else:
        outlet_data.append([p('No outlet production lines this week.', default=''), p('-', default='-'), p('-', default='-')])
    outlet_table = Table(
        outlet_data,
        colWidths=[content_width * 0.56, content_width * 0.2, content_width * 0.24],
        repeatRows=1,
    )
    outlet_table.setStyle(
        TableStyle(
            [
                ('BACKGROUND', (0, 0), (-1, 0), header_navy),
                ('TEXTCOLOR', (0, 0), (-1, 0), white),
                ('ALIGN', (1, 1), (-1, -1), 'RIGHT'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [white, light_gray]),
                ('GRID', (0, 0), (-1, -1), 0.4, border_gray),
                ('LEFTPADDING', (0, 0), (-1, -1), 5),
                ('RIGHTPADDING', (0, 0), (-1, -1), 5),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(outlet_table)
    story.append(Spacer(1, 12))

    deferred_rows = review.get('deferred_lines') or []
    if deferred_rows:
        story.append(Paragraph('Deferred', section_title_style))
        deferred_data = [
            [
                Paragraph('Date', table_header_style),
                Paragraph('Item', table_header_style),
                Paragraph('Outlet', table_header_style),
                Paragraph('Reason', table_header_style),
            ]
        ]
        for row in deferred_rows[:15]:
            deferred_data.append(
                [
                    p(fmt_date(row.get('needed_date'))),
                    p(row.get('item_name') or 'Item'),
                    p(row.get('outlet') or '-'),
                    p(row.get('reason') or '-'),
                ]
            )
        deferred_table = Table(
            deferred_data,
            colWidths=[content_width * 0.16, content_width * 0.28, content_width * 0.2, content_width * 0.36],
            repeatRows=1,
        )
        deferred_table.setStyle(
            TableStyle(
                [
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#FFF8EE')),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#A0590A')),
                    ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [white, light_gray]),
                    ('GRID', (0, 0), (-1, -1), 0.4, border_gray),
                    ('LEFTPADDING', (0, 0), (-1, -1), 5),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 5),
                    ('TOPPADDING', (0, 0), (-1, -1), 3.5),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 3.5),
                ]
            )
        )
        story.append(deferred_table)
        remaining_deferred = len(deferred_rows) - min(len(deferred_rows), 15)
        if remaining_deferred > 0:
            story.append(Spacer(1, 4))
            story.append(
                Paragraph(
                    f'<font color="#A0590A"><b>and {format_number(remaining_deferred)} more deferred items...</b></font>',
                    subtle_note_style,
                )
            )
        story.append(Spacer(1, 12))

    story.append(Paragraph('Issues / Flags', section_title_style))
    incomplete_rows = review.get('incomplete_lines') or []
    if incomplete_rows:
        issue_rows = incomplete_rows[:15]
        issue_data = [
            [
                Paragraph('Date', table_header_style),
                Paragraph('Item', table_header_style),
                Paragraph('Outlet', table_header_style),
                Paragraph('Made By', table_header_style),
                Paragraph('Notes', table_header_style),
            ]
        ]
        for row in issue_rows:
            notes = shorten(row.get('production_notes') or row.get('notes') or '-', 140)
            issue_data.append(
                [
                    p(fmt_date(row.get('needed_date'))),
                    p(row.get('item_name') or 'Item'),
                    p(row.get('outlet') or '-'),
                    p(row.get('made_by') or row.get('assigned_to') or '-'),
                    p(notes),
                ]
            )
        issue_table = Table(
            issue_data,
            colWidths=[content_width * 0.14, content_width * 0.27, content_width * 0.16, content_width * 0.16, content_width * 0.27],
            repeatRows=1,
        )
        issue_table.setStyle(
            TableStyle(
                [
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#FDEAE7')),
                    ('TEXTCOLOR', (0, 0), (-1, 0), alert_red),
                    ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [white, light_gray]),
                    ('GRID', (0, 0), (-1, -1), 0.4, border_gray),
                    ('LEFTPADDING', (0, 0), (-1, -1), 5),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 5),
                    ('TOPPADDING', (0, 0), (-1, -1), 3.5),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 3.5),
                ]
            )
        )
        story.append(issue_table)
        remaining_count = len(incomplete_rows) - len(issue_rows)
        if remaining_count > 0:
            story.append(Spacer(1, 4))
            story.append(
                Paragraph(
                    f'<font color="#D4432A"><b>and {format_number(remaining_count)} more pending sign-off items...</b></font>',
                    subtle_note_style,
                )
            )
    else:
        badge = Table([[Paragraph('All items signed off', badge_style)]], colWidths=[2.3 * inch])
        badge.setStyle(
            TableStyle(
                [
                    ('BACKGROUND', (0, 0), (0, 0), colors.HexColor('#E7F4EA')),
                    ('BOX', (0, 0), (0, 0), 0.4, colors.HexColor('#C2E0C7')),
                    ('LEFTPADDING', (0, 0), (0, 0), 10),
                    ('RIGHTPADDING', (0, 0), (0, 0), 10),
                    ('TOPPADDING', (0, 0), (0, 0), 5),
                    ('BOTTOMPADDING', (0, 0), (0, 0), 5),
                ]
            )
        )
        story.append(badge)

    daily_rows = review.get('daily_rows') or []
    transfers = review.get('transfers') or []
    if daily_rows or transfers:
        story.append(PageBreak())

    if daily_rows:
        story.append(Paragraph('Daily Production Detail', section_title_style))
        daily_data = [
            [
                Paragraph('Date', table_header_style),
                Paragraph('Item', table_header_style),
                Paragraph('Qty', table_header_style),
                Paragraph('Made By', table_header_style),
                Paragraph('Tasted By', table_header_style),
                Paragraph('Status', table_header_style),
                Paragraph('Notes', table_header_style),
            ]
        ]
        daily_styles = [
            ('BACKGROUND', (0, 0), (-1, 0), header_navy),
            ('TEXTCOLOR', (0, 0), (-1, 0), white),
            ('GRID', (0, 0), (-1, -1), 0.35, border_gray),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
            ('TOPPADDING', (0, 0), (-1, -1), 3.5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3.5),
        ]
        current_date = None
        table_row_index = 1
        zebra = False
        for row in daily_rows:
            row_date = row.get('production_date')
            if row_date != current_date:
                current_date = row_date
                daily_data.append(
                    [
                        Paragraph(html_text(fmt_date(row_date)), date_divider_style),
                        Paragraph('', base_style),
                        Paragraph('', base_style),
                        Paragraph('', base_style),
                        Paragraph('', base_style),
                        Paragraph('', base_style),
                        Paragraph('', base_style),
                    ]
                )
                daily_styles.extend(
                    [
                        ('SPAN', (0, table_row_index), (-1, table_row_index)),
                        ('BACKGROUND', (0, table_row_index), (-1, table_row_index), colors.HexColor('#EBEEF4')),
                    ]
                )
                table_row_index += 1

            notes = row.get('production_notes') or row.get('notes') or '-'
            daily_data.append(
                [
                    p(fmt_date(row.get('production_date'))),
                    p(row.get('item_name') or 'Item'),
                    p(qty_label(row)),
                    p(row.get('made_by') or '-'),
                    p(row.get('tasted_by') or '-'),
                    status_p(bool(row.get('signed_off')), bool(row.get('pending_rollup'))),
                    p(notes),
                ]
            )
            if zebra:
                daily_styles.append(('BACKGROUND', (0, table_row_index), (-1, table_row_index), light_gray))
            zebra = not zebra
            table_row_index += 1

        daily_table = Table(
            daily_data,
            colWidths=[0.88 * inch, 1.75 * inch, 0.58 * inch, 0.82 * inch, 0.82 * inch, 0.63 * inch, 2.02 * inch],
            repeatRows=1,
        )
        daily_table.setStyle(TableStyle(daily_styles))
        story.append(daily_table)

    if transfers:
        if daily_rows:
            story.append(Spacer(1, 12))
        story.append(Paragraph('Transfers', section_title_style))
        transfer_data = [
            [
                Paragraph('Date', table_header_style),
                Paragraph('To Outlet', table_header_style),
                Paragraph('Items', table_header_style),
                Paragraph('Method', table_header_style),
            ]
        ]
        for transfer in transfers:
            transfer_data.append(
                [
                    p(fmt_date(transfer.get('production_date'))),
                    p(transfer.get('to_outlet') or '-'),
                    p(format_number(transfer.get('line_count') or 0)),
                    p(transfer.get('transfer_method') or '-'),
                ]
            )
        transfer_table = Table(
            transfer_data,
            colWidths=[1.0 * inch, 3.0 * inch, 0.8 * inch, 2.7 * inch],
            repeatRows=1,
        )
        transfer_table.setStyle(
            TableStyle(
                [
                    ('BACKGROUND', (0, 0), (-1, 0), header_navy),
                    ('TEXTCOLOR', (0, 0), (-1, 0), white),
                    ('ALIGN', (2, 1), (2, -1), 'RIGHT'),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [white, light_gray]),
                    ('GRID', (0, 0), (-1, -1), 0.35, border_gray),
                    ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                    ('LEFTPADDING', (0, 0), (-1, -1), 5),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 5),
                    ('TOPPADDING', (0, 0), (-1, -1), 4),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ]
            )
        )
        story.append(transfer_table)

    doc.build(story, onFirstPage=draw_first_page_header, onLaterPages=draw_later_pages, canvasmaker=NumberedCanvas)
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
    )


@bp.route('/commissary/recipe-cards')
@login_required
def commissary_recipe_cards():
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

        datasets = build_commissary_datasets(cur, start_date, end_date, selected_outlet, selected_units)

    manifest_window = selected_day.isoformat() if selected_day else f"{start_date.isoformat()} to {end_date.isoformat()}"

    return render_template(
        'commissary_recipe_cards_print.html',
        selected_outlet=selected_outlet,
        selected_units=selected_units,
        week_start=start_date.isoformat(),
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        selected_day=selected_day.isoformat() if selected_day else '',
        manifest_window=manifest_window,
        generated_at=datetime.now().strftime('%b %d, %Y %I:%M %p'),
        datasets=datasets,
    )


@bp.route('/commissary-planner/packet/pdf')
@login_required
def commissary_packet_pdf():
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
        checklist_lines = []
        for order in datasets.get('orders', []):
            for line in order.get('lines', []):
                production_log = line.get('production_log') or {}
                checklist_lines.append(
                    {
                        'item_name': line.get('item_name') or line.get('recipe_name') or 'Item',
                        'recipe_name': line.get('recipe_name') or '',
                        'quantity': line.get('quantity'),
                        'quantity_unit': line.get('quantity_unit') or 'each',
                        'outlet': order.get('outlet') or DEFAULT_COMMISSARY_OUTLET,
                        'assigned_to': production_log.get('assigned_to') or '',
                        'notes': line.get('notes') or '',
                        'components': line.get('components') or [],
                        'instruction_steps': line.get('instruction_steps') or [],
                        'production_log': production_log,
                    }
                )
        checklist_lines.sort(key=lambda row: ((row.get('outlet') or '').lower(), (row.get('item_name') or '').lower()))

    header_black = colors.HexColor('#1A1A1A')
    light_gray = colors.HexColor('#F5F5F5')
    border_gray = colors.HexColor('#D9D9D9')
    muted_text = colors.HexColor('#4F5B6E')
    subrecipe_gray = colors.HexColor('#888888')
    white = colors.white
    margin_x = 0.5 * inch
    margin_y = 0.4 * inch
    generated_at = datetime.now().strftime('%b %d, %Y %I:%M %p')

    def fmt_date(value):
        if not value:
            return '-'
        if isinstance(value, datetime):
            return value.strftime('%b %d, %Y')
        if isinstance(value, date):
            return value.strftime('%b %d, %Y')
        raw = str(value).strip()
        if not raw:
            return '-'
        try:
            parsed = datetime.strptime(raw[:10], '%Y-%m-%d')
            return parsed.strftime('%b %d, %Y')
        except ValueError:
            return raw

    def to_text(value, default='-'):
        text = str(value or '').strip()
        return text or default

    def shorten(value, limit):
        text = to_text(value, default='')
        if len(text) <= limit:
            return text
        return f"{text[: limit - 3].rstrip()}..."

    def html_text(value, default='-'):
        return escape(to_text(value, default)).replace('\n', '<br/>')

    def qty_label_values(quantity, unit):
        qty = format_number(quantity)
        unit = (unit or 'each').strip() or 'each'
        return f'{qty} {unit}'

    def qty_label(row):
        return qty_label_values(row.get('quantity'), row.get('quantity_unit'))

    def actual_yield_label(log):
        qty = to_float(log.get('actual_yield'))
        unit = clean_menu_text(log.get('actual_yield_unit')) or ''
        if qty > 0:
            return qty_label_values(qty, unit or 'each')
        return '-'

    def component_qty_label(quantity, unit):
        qty_text = str(quantity or '').strip() or '—'
        unit_text = str(unit or '').strip()
        if qty_text == '—' or not unit_text:
            return qty_text
        return f'{qty_text} {unit_text}'

    def display_label(display):
        if not display:
            return '-'
        qty = to_text(display.get('quantity'), default='')
        unit = to_text(display.get('unit'), default='')
        text = f'{qty} {unit}'.strip()
        return text or '-'

    def signoff_notes_markup(line):
        parts = []
        if line.get('notes'):
            parts.append(f'<b>Line:</b> {html_text(line.get("notes"), "")}')
        production_notes = (line.get('production_log') or {}).get('notes')
        if production_notes:
            parts.append(f'<b>Prod:</b> {html_text(production_notes, "")}')
        return '<br/>'.join(parts) if parts else '-'

    unit_label = 'Auto'
    if selected_units == 'imperial':
        unit_label = 'Imperial'
    elif selected_units == 'metric':
        unit_label = 'Metric'
    elif selected_units == 'hybrid':
        unit_label = 'Grams + qt/gal/fl oz'

    if selected_day:
        range_label = f'Day {fmt_date(selected_day)}'
    else:
        range_label = f'{fmt_date(start_date)} - {fmt_date(end_date)}'

    outlet_names = sorted({row.get('outlet') or DEFAULT_COMMISSARY_OUTLET for row in checklist_lines}, key=lambda value: value.lower())
    outlet_label = selected_outlet or ', '.join(outlet_names) or 'All Outlets'

    base_style = ParagraphStyle(
        'packet-pdf-base',
        fontName='Helvetica',
        fontSize=8,
        leading=9.5,
        textColor=colors.black,
    )
    small_style = ParagraphStyle(
        'packet-pdf-small',
        parent=base_style,
        fontSize=7.6,
        leading=8.8,
    )
    table_header_style = ParagraphStyle(
        'packet-pdf-table-header',
        parent=small_style,
        fontName='Helvetica-Bold',
        textColor=white,
        fontSize=7.3,
        leading=8.5,
    )
    section_title_style = ParagraphStyle(
        'packet-pdf-section-title',
        parent=base_style,
        fontName='Helvetica-Bold',
        fontSize=11.5,
        leading=13.5,
        textColor=header_black,
        spaceAfter=3,
    )
    muted_style = ParagraphStyle(
        'packet-pdf-muted',
        parent=small_style,
        textColor=muted_text,
    )
    signoff_cell_style = ParagraphStyle(
        'packet-pdf-signoff-cell',
        parent=small_style,
        leading=8.8,
    )
    signoff_notes_style = ParagraphStyle(
        'packet-pdf-signoff-notes',
        parent=small_style,
        leading=8.6,
    )
    outlet_header_style = ParagraphStyle(
        'packet-pdf-outlet-header',
        parent=base_style,
        fontName='Helvetica-Bold',
        fontSize=10.5,
        leading=12.5,
        textColor=header_black,
        spaceBefore=8,
        spaceAfter=3,
    )
    checklist_item_style = ParagraphStyle(
        'packet-pdf-checklist-item',
        parent=base_style,
        fontName='Helvetica-Bold',
        fontSize=11,
        leading=13.2,
        spaceAfter=1,
    )
    checklist_subrecipe_style = ParagraphStyle(
        'packet-pdf-checklist-subrecipe',
        parent=base_style,
        fontSize=9,
        leading=10.8,
        textColor=subrecipe_gray,
        leftIndent=20,
    )
    card_title_style = ParagraphStyle(
        'packet-pdf-card-title',
        parent=base_style,
        fontName='Helvetica-Bold',
        fontSize=12,
        leading=14,
        textColor=header_black,
        spaceAfter=1,
    )
    card_meta_style = ParagraphStyle(
        'packet-pdf-card-meta',
        parent=small_style,
        textColor=muted_text,
        spaceAfter=4,
    )
    card_component_style = ParagraphStyle(
        'packet-pdf-card-component',
        parent=small_style,
        leading=8.8,
    )
    card_subcomponent_style = ParagraphStyle(
        'packet-pdf-card-subcomponent',
        parent=small_style,
        leading=8.8,
        textColor=subrecipe_gray,
    )
    method_step_style = ParagraphStyle(
        'packet-pdf-method-step',
        parent=base_style,
        fontSize=9,
        leading=11.2,
        spaceAfter=2,
    )
    empty_note_style = ParagraphStyle(
        'packet-pdf-empty-note',
        parent=base_style,
        fontSize=8.2,
        leading=9.6,
        textColor=muted_text,
    )
    master_kicker_style = ParagraphStyle(
        'packet-pdf-master-kicker',
        parent=small_style,
        fontName='Helvetica-Bold',
        fontSize=7.2,
        leading=8.6,
        textColor=muted_text,
    )
    master_title_style = ParagraphStyle(
        'packet-pdf-master-title',
        parent=base_style,
        fontName='Helvetica-Bold',
        fontSize=24,
        leading=24,
        textColor=header_black,
        spaceAfter=2,
    )
    master_meta_style = ParagraphStyle(
        'packet-pdf-master-meta',
        parent=small_style,
        fontName='Helvetica-Bold',
        fontSize=7.2,
        leading=8.6,
        textColor=muted_text,
        spaceAfter=4,
    )
    master_description_style = ParagraphStyle(
        'packet-pdf-master-description',
        parent=base_style,
        fontSize=8.5,
        leading=10.6,
        textColor=muted_text,
        spaceAfter=6,
    )
    master_stat_label_style = ParagraphStyle(
        'packet-pdf-master-stat-label',
        parent=small_style,
        fontName='Helvetica-Bold',
        fontSize=6.4,
        leading=7.4,
        textColor=muted_text,
    )
    master_stat_value_style = ParagraphStyle(
        'packet-pdf-master-stat-value',
        parent=base_style,
        fontName='Helvetica-Bold',
        fontSize=15,
        leading=16.5,
        textColor=header_black,
    )
    master_panel_title_style = ParagraphStyle(
        'packet-pdf-master-panel-title',
        parent=base_style,
        fontName='Helvetica-Bold',
        fontSize=8.6,
        leading=10,
        textColor=header_black,
        spaceAfter=3,
    )
    master_panel_body_style = ParagraphStyle(
        'packet-pdf-master-panel-body',
        parent=small_style,
        fontSize=7.8,
        leading=9.5,
        textColor=colors.black,
    )
    master_timeline_title_style = ParagraphStyle(
        'packet-pdf-master-timeline-title',
        parent=base_style,
        fontName='Helvetica-Bold',
        fontSize=9.4,
        leading=11,
        textColor=header_black,
        spaceAfter=4,
    )

    def p(value, style=base_style, default='-'):
        return Paragraph(html_text(value, default), style)

    def paragraph_from_markup(value, style):
        return Paragraph(value, style)

    def draw_page_header(pdf_canvas, doc):
        page_width, page_height = letter
        top_y = page_height - 22
        pdf_canvas.saveState()
        pdf_canvas.setFillColor(header_black)
        pdf_canvas.setFont('Helvetica-Bold', 11)
        pdf_canvas.drawString(doc.leftMargin, top_y, 'Commissary Production Packet')
        pdf_canvas.setFillColor(muted_text)
        pdf_canvas.setFont('Helvetica', 7.3)
        detail = f"{shorten(outlet_label, 60)} | {range_label}"
        pdf_canvas.drawString(doc.leftMargin, top_y - 10, detail)
        pdf_canvas.drawRightString(page_width - doc.rightMargin, top_y - 10, f'Auto Generated {generated_at}')
        pdf_canvas.setStrokeColor(border_gray)
        pdf_canvas.setLineWidth(0.6)
        pdf_canvas.line(doc.leftMargin, top_y - 14.5, page_width - doc.rightMargin, top_y - 14.5)
        pdf_canvas.restoreState()

    def draw_footer(pdf_canvas, page_num, total_pages):
        page_width, _ = letter
        footer_y = 16
        pdf_canvas.setStrokeColor(border_gray)
        pdf_canvas.setLineWidth(0.6)
        pdf_canvas.line(margin_x, footer_y + 10, page_width - margin_x, footer_y + 10)
        pdf_canvas.setFont('Helvetica', 7.5)
        pdf_canvas.setFillColor(muted_text)
        pdf_canvas.drawString(margin_x, footer_y + 2, 'Foxtown HQ')
        pdf_canvas.drawRightString(page_width - margin_x, footer_y + 2, f'Page {page_num} of {total_pages}')

    class NumberedCanvas(canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._saved_page_states = []

        def showPage(self):
            self._saved_page_states.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total_pages = len(self._saved_page_states)
            for page_num, state in enumerate(self._saved_page_states, start=1):
                self.__dict__.update(state)
                draw_footer(self, page_num, total_pages)
                super().showPage()
            super().save()

    def draw_first_page(pdf_canvas, doc):
        draw_page_header(pdf_canvas, doc)

    def draw_later_pages(pdf_canvas, doc):
        draw_page_header(pdf_canvas, doc)

    def styled_table(data, col_widths, align_right_cols=None, repeat_rows=1, header_color=header_black):
        table = Table(data, colWidths=col_widths, repeatRows=repeat_rows)
        commands = [
            ('BACKGROUND', (0, 0), (-1, 0), header_color),
            ('TEXTCOLOR', (0, 0), (-1, 0), white),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [white, light_gray]),
            ('GRID', (0, 0), (-1, -1), 0.4, border_gray),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
            ('TOPPADDING', (0, 0), (-1, -1), 3.5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3.5),
        ]
        if align_right_cols:
            for idx in align_right_cols:
                commands.append(('ALIGN', (idx, 1), (idx, -1), 'RIGHT'))
        table.setStyle(TableStyle(commands))
        return table

    def divider(width):
        line = Table([[Paragraph('', base_style)]], colWidths=[width])
        line.setStyle(
            TableStyle(
                [
                    ('LINEBELOW', (0, 0), (-1, 0), 0.6, border_gray),
                    ('LEFTPADDING', (0, 0), (-1, -1), 0),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                    ('TOPPADDING', (0, 0), (-1, -1), 0),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
                ]
            )
        )
        return line

    def checklist_subrecipe_flowables(components, depth=1):
        flowables = []
        if not components:
            return flowables
        for component in components:
            comp_type = component.get('type')
            if comp_type == 'option_group':
                flowables.extend(checklist_subrecipe_flowables(component.get('children') or [], depth))
                continue
            if comp_type != 'ingredient':
                comp_name = component.get('name') or component.get('item_name') or component.get('recipe_name') or 'Sub-recipe'
                comp_qty = component.get('display_quantity')
                if comp_qty in (None, ''):
                    comp_qty = component.get('quantity')
                if comp_qty in (None, ''):
                    comp_qty = component.get('scaled_quantity_display')
                comp_unit = component.get('display_unit') or component.get('unit') or ''
                style = ParagraphStyle(
                    f'packet-pdf-checklist-subrecipe-{depth}',
                    parent=checklist_subrecipe_style,
                    leftIndent=20 * depth,
                )
                flowables.append(
                    Paragraph(
                        f'{escape(comp_name)} &mdash; {escape(component_qty_label(comp_qty, comp_unit))}',
                        style,
                    )
                )
            flowables.extend(checklist_subrecipe_flowables(component.get('children') or [], depth + 1))
        return flowables

    def recipe_component_rows(components, depth=0):
        rows = []
        if not components:
            return rows
        for component in components:
            comp_type = component.get('type')
            if comp_type == 'option_group':
                rows.extend(recipe_component_rows(component.get('children') or [], depth))
                continue
            comp_name = component.get('name') or component.get('item_name') or component.get('recipe_name') or 'Component'
            comp_qty = component.get('display_quantity')
            if comp_qty in (None, ''):
                comp_qty = component.get('quantity')
            if comp_qty in (None, ''):
                comp_qty = component.get('scaled_quantity_display')
            comp_unit = component.get('display_unit') or component.get('unit') or '—'
            name_style = card_component_style if comp_type == 'ingredient' else card_subcomponent_style
            qty_style = card_component_style if comp_type == 'ingredient' else card_subcomponent_style
            name_paragraph_style = ParagraphStyle(
                f'packet-pdf-card-name-{comp_type or "component"}-{depth}',
                parent=name_style,
                leftIndent=depth * 12,
            )
            rows.append(
                [
                    Paragraph(escape(comp_name), name_paragraph_style),
                    Paragraph(html_text(comp_qty, '—'), qty_style),
                    Paragraph(html_text(comp_unit, '—'), qty_style),
                ]
            )
            rows.extend(recipe_component_rows(component.get('children') or [], depth + 1))
        return rows

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=margin_x,
        rightMargin=margin_x,
        topMargin=0.9 * inch,
        bottomMargin=0.5 * inch,
        title='Commissary Production Packet',
    )
    story = [Spacer(1, 2)]

    story.append(Paragraph('Daily Production Summary', section_title_style))
    story.append(Paragraph(range_label, muted_style))
    story.append(Spacer(1, 6))
    if checklist_lines:
        signoff_data = [
            [
                Paragraph('Item', table_header_style),
                Paragraph('Requested Qty', table_header_style),
                Paragraph('Yield Qty', table_header_style),
                Paragraph('Made By', table_header_style),
                Paragraph('Notes', table_header_style),
            ]
        ]
        for line in checklist_lines:
            production_log = line.get('production_log') or {}
            item_detail = f'<b>{escape(line.get("item_name") or "Item")}</b>'
            if line.get('recipe_name') and line.get('recipe_name') != line.get('item_name'):
                item_detail += f'<br/><font color="#6B7280">Recipe: {escape(line.get("recipe_name") or "")}</font>'
            item_detail += f'<br/><font color="#6B7280">{escape(line.get("outlet") or DEFAULT_COMMISSARY_OUTLET)}</font>'
            signoff_data.append(
                [
                    paragraph_from_markup(item_detail, signoff_cell_style),
                    p(qty_label(line), signoff_cell_style),
                    p(actual_yield_label(production_log), signoff_cell_style),
                    p(production_log.get('made_by') or '-', signoff_cell_style),
                    paragraph_from_markup(signoff_notes_markup(line), signoff_notes_style),
                ]
            )
        signoff_table = styled_table(
            signoff_data,
            col_widths=[
                doc.width * 0.34,
                doc.width * 0.14,
                doc.width * 0.14,
                doc.width * 0.12,
                doc.width * 0.26,
            ],
            align_right_cols=[1, 2],
        )
        story.append(signoff_table)
    else:
        story.append(Paragraph('No production items found for this date window.', empty_note_style))

    story.append(PageBreak())

    story.append(Paragraph('Production Checklist', section_title_style))
    story.append(Paragraph('Unified production list grouped by outlet.', muted_style))
    story.append(Spacer(1, 6))
    if checklist_lines:
        outlet_groups = {}
        for line in checklist_lines:
            outlet_groups.setdefault(line.get('outlet') or DEFAULT_COMMISSARY_OUTLET, []).append(line)
        for outlet in sorted(outlet_groups.keys(), key=lambda value: value.lower()):
            story.append(Paragraph(escape(outlet), outlet_header_style))
            for line in outlet_groups[outlet]:
                story.append(
                    Paragraph(
                        f'[ ] {escape(line.get("item_name") or "Item")} &mdash; {escape(qty_label(line))}',
                        checklist_item_style,
                    )
                )
                for sub_flowable in checklist_subrecipe_flowables(line.get('components') or []):
                    story.append(sub_flowable)
                story.append(Spacer(1, 2))
    else:
        story.append(Paragraph('No production items found for this date window.', empty_note_style))

    story.append(PageBreak())

    story.append(Paragraph('Scaled Recipe Master Sheets', section_title_style))
    story.append(Paragraph('Recipe database sheets projected into commissary prep output.', muted_style))
    story.append(Spacer(1, 6))
    weekly_prep = datasets.get('weekly_prep') or []
    if weekly_prep:
        for idx, prep in enumerate(weekly_prep):
            if idx > 0:
                story.append(PageBreak())

            story.append(Paragraph('Kitchen Ops / Commissary Batch', master_kicker_style))
            story.append(Paragraph(escape(prep.get('recipe_name') or 'Recipe'), master_title_style))
            story.append(Paragraph(
                escape(
                    f"RECIPE ID: {(prep.get('recipe_id') or 'RECIPE').upper()} · "
                    f"STATION: {(prep.get('station') or 'GENERAL').upper()}"
                ),
                master_meta_style,
            ))
            story.append(Paragraph(
                escape(prep.get('menu_descriptor') or 'Scaled commissary production master generated from active order demand.'),
                master_description_style,
            ))

            stat_cells = []
            for label, value in [
                ('Required Output', display_label(prep.get('display_required'))),
                ('Base Yield', display_label(prep.get('display_yield'))),
                ('Scaled Run', f"{format_number(prep.get('required_batches'))}x"),
                ('Component Rows', format_number(prep.get('component_count') or 0)),
            ]:
                stat_cells.append(
                    Paragraph(
                        f"<font size='6.4' color='#4F5B6E'><b>{escape(label.upper())}</b></font><br/>"
                        f"<font size='15'><b>{escape(value)}</b></font>",
                        base_style,
                    )
                )
            stat_table = Table([stat_cells], colWidths=[doc.width * 0.25] * 4)
            stat_table.setStyle(
                TableStyle(
                    [
                        ('BOX', (0, 0), (-1, -1), 0.5, header_black),
                        ('INNERGRID', (0, 0), (-1, -1), 0.5, border_gray),
                        ('LEFTPADDING', (0, 0), (-1, -1), 8),
                        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
                        ('TOPPADDING', (0, 0), (-1, -1), 8),
                        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
                        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                    ]
                )
            )
            story.append(stat_table)
            story.append(Spacer(1, 8))

            component_rows = recipe_component_rows(prep.get('components') or [])
            component_data = [
                [
                    Paragraph('Ingredient / Sub-Recipe', table_header_style),
                    Paragraph('Qty', table_header_style),
                    Paragraph('Unit', table_header_style),
                ]
            ]
            if component_rows:
                component_data.extend(component_rows)
            else:
                component_data.append(
                    [
                        Paragraph('No scaled component data available.', small_style),
                        Paragraph('—', small_style),
                        Paragraph('—', small_style),
                    ]
                )
            component_table = styled_table(
                component_data,
                col_widths=[doc.width * 0.68, doc.width * 0.16, doc.width * 0.16],
                align_right_cols=[1],
            )
            story.append(component_table)
            story.append(Spacer(1, 8))

            critical_steps = prep.get('critical_steps') or []
            if critical_steps:
                critical_markup = '<b>Critical Yield Alerts</b><br/>' + '<br/>'.join(
                    escape(f"{step_index:02d}. {step}") for step_index, step in enumerate(critical_steps, start=1)
                )
            else:
                critical_markup = '<b>Critical Yield Alerts</b><br/>No critical steps are logged yet.'
            storage_lines = prep.get('storage_lines') or []
            handling_lines = [
                f"Prep Time: {prep.get('prep_time_minutes')} min" if prep.get('prep_time_minutes') is not None else 'Prep Time: Not logged',
                f"Shelf Life: {prep.get('shelf_life_days')} days" if prep.get('shelf_life_days') is not None else 'Shelf Life: Not logged',
                f"Station: {prep.get('station') or 'General'}",
                f"Equipment: {prep.get('equipment') or 'Not specified'}",
                f"Assigned Cooks: {', '.join(prep.get('assigned_cooks') or []) or 'Open assignment'}",
                f"Storage: {' / '.join(storage_lines) if storage_lines else 'Not logged'}",
            ]
            if prep.get('used_in_order_labels'):
                handling_lines.append(f"Order Targets: {', '.join(prep['used_in_order_labels'][:6])}")
            detail_table = Table(
                [[
                    Paragraph(critical_markup, master_panel_body_style),
                    Paragraph('<b>Prep And Handling</b><br/>' + '<br/>'.join(escape(line) for line in handling_lines), master_panel_body_style),
                ]],
                colWidths=[doc.width * 0.5, doc.width * 0.5],
            )
            detail_table.setStyle(
                TableStyle(
                    [
                        ('BACKGROUND', (0, 0), (0, 0), colors.HexColor('#EDF2F4')),
                        ('BACKGROUND', (1, 0), (1, 0), colors.HexColor('#E1E9EE')),
                        ('BOX', (0, 0), (-1, -1), 0.5, border_gray),
                        ('INNERGRID', (0, 0), (-1, -1), 0.5, border_gray),
                        ('LEFTPADDING', (0, 0), (-1, -1), 8),
                        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
                        ('TOPPADDING', (0, 0), (-1, -1), 8),
                        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
                        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                    ]
                )
            )
            story.append(detail_table)
            story.append(Spacer(1, 8))

            steps = prep.get('instruction_steps') or []
            if steps:
                story.append(Paragraph('Production Timeline', master_timeline_title_style))
                timeline_data = []
                for step_index, step in enumerate(steps, start=1):
                    timeline_data.append([
                        Paragraph(f'<b>{step_index:02d}</b>', small_style),
                        Paragraph(html_text(step, ''), method_step_style),
                    ])
                timeline_table = Table(timeline_data, colWidths=[doc.width * 0.08, doc.width * 0.92])
                timeline_table.setStyle(
                    TableStyle(
                        [
                            ('BOX', (0, 0), (-1, -1), 0.5, border_gray),
                            ('INNERGRID', (0, 0), (-1, -1), 0.5, border_gray),
                            ('LEFTPADDING', (0, 0), (-1, -1), 8),
                            ('RIGHTPADDING', (0, 0), (-1, -1), 8),
                            ('TOPPADDING', (0, 0), (-1, -1), 6),
                            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                        ]
                    )
                )
                story.append(timeline_table)
            else:
                story.append(Paragraph('No method provided.', empty_note_style))
    else:
        story.append(Paragraph('No recipe master sheets are available for this date window.', empty_note_style))

    doc.build(story, onFirstPage=draw_first_page, onLaterPages=draw_later_pages, canvasmaker=NumberedCanvas)
    payload = buffer.getvalue()
    buffer.close()

    day_suffix = f"-{selected_day.isoformat()}" if selected_day else ''
    filename = f"commissary-packet-{start_date.isoformat()}{day_suffix}.pdf"
    return Response(
        payload,
        mimetype='application/pdf',
        headers={'Content-Disposition': f'attachment; filename={filename}'},
    )
