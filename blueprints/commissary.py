from flask import Blueprint
from flask_login import login_required
from psycopg2.extras import RealDictCursor

from helpers.common import *

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


@bp.route('/commissary-planner')
@login_required
def commissary_planner():
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    ensure_commissary_tables(cur)
    conn.commit()
    start_date, end_date = get_banquet_date_window(request.args.get('start_date'), request.args.get('end_date'))
    selected_outlet = clean_menu_text(request.args.get('outlet'))
    outlet_options = get_commissary_outlet_options(cur)

    datasets = build_commissary_datasets(cur, start_date, end_date, selected_outlet, get_unit_system())
    orders = datasets.get('orders', [])
    open_statuses = set(COMMISSARY_ACTIVE_STATUSES)

    metrics = {
        'open_orders': sum(1 for order in orders if (order.get('status') or '').lower() in open_statuses),
        'completed_orders': sum(1 for order in orders if (order.get('status') or '').lower() == 'completed'),
    }

    cur.close()
    return render_template(
        'commissary_planner.html',
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        selected_outlet=selected_outlet,
        outlet_options=outlet_options,
        datasets=datasets,
        orders=orders,
        metrics=metrics
    )


@bp.route('/commissary-planner/orders/new', methods=['GET', 'POST'])
@login_required
def commissary_order_new():
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

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
                    """, (
                        order_id,
                        recipe_id,
                        item_name,
                        line.get('quantity'),
                        line.get('quantity_unit') or 'each',
                        line.get('notes'),
                        idx
                    ))

                conn.commit()
                flash('Commissary order created.', 'success')
                cur.close()
                return redirect(url_for('commissary_order_edit', order_id=order_id))
            except Exception:
                conn.rollback()
                flash('Error creating commissary order.', 'error')

    cur.close()
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
    cur = conn.cursor(cursor_factory=RealDictCursor)

    ensure_commissary_tables(cur)
    conn.commit()
    existing = get_commissary_order(cur, order_id)
    if not existing:
        cur.close()
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
                    """, (
                        order_id,
                        recipe_id,
                        item_name,
                        line.get('quantity'),
                        line.get('quantity_unit') or 'each',
                        line.get('notes'),
                        idx
                    ))

                conn.commit()
                flash('Commissary order updated.', 'success')
                cur.close()
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

        cur.close()
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
    cur.close()
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
    cur = conn.cursor(cursor_factory=RealDictCursor)

    ensure_commissary_tables(cur)
    conn.commit()
    cur.execute("SELECT id, outlet, needed_date FROM outlet_orders WHERE id = %s", (order_id,))
    order = cur.fetchone()
    if not order:
        cur.close()
        flash('Commissary order not found.', 'error')
        return redirect(url_for('commissary_planner'))

    try:
        cur.execute("DELETE FROM outlet_orders WHERE id = %s", (order_id,))
        conn.commit()
        flash(f"Deleted commissary order for {order.get('outlet')} on {order.get('needed_date')}.", 'success')
    except Exception:
        conn.rollback()
        flash('Error deleting commissary order.', 'error')
    cur.close()
    return redirect(url_for('commissary_planner'))


@bp.route('/commissary-planner/packet/print')
@login_required
def commissary_packet_print():
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    ensure_commissary_tables(cur)
    conn.commit()
    selected_outlet = clean_menu_text(request.args.get('outlet'))
    start_date, end_date = get_banquet_date_window(request.args.get('start_date'), request.args.get('end_date'))
    include_shopping_raw = request.args.get('include_shopping')
    if include_shopping_raw is None:
        include_shopping = True
    else:
        include_shopping = (include_shopping_raw or '').strip().lower() in ('1', 'true', 'yes', 'on')
    datasets = build_commissary_datasets(cur, start_date, end_date, selected_outlet, get_unit_system())
    for prep in datasets.get('weekly_prep', []):
        prep['instruction_steps'] = split_instruction_steps(prep.get('instructions'))

    cur.close()
    return render_template(
        'commissary_packet_print.html',
        selected_outlet=selected_outlet,
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        include_shopping=include_shopping,
        generated_at=datetime.now().strftime('%b %d, %Y %I:%M %p'),
        datasets=datasets
    )
