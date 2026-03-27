import re
import traceback
from collections import defaultdict
from datetime import date, datetime, timedelta

from db import get_cursor, get_db
from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required
from helpers.db_helpers import db_table_exists
from helpers.menu import clean_menu_text
from helpers.shared import generate_id, handle_route_error, parse_float_field, to_float
from helpers.units import format_number, get_unit_system
from helpers.venues import get_active_venues

bp = Blueprint('buffet', __name__)


@bp.errorhandler(Exception)
def handle_buffet_error(error):
    return handle_route_error(error, 'buffet')


BUFFET_STATUS_CHOICES = [
    ('planning', 'Planning'),
    ('confirmed', 'Confirmed'),
    ('completed', 'Completed'),
    ('cancelled', 'Cancelled'),
]

BUFFET_STATION_OPTIONS = [
    'Omelette Station',
    'Carving Station',
    'Entrée',
    'Brunch Line',
    'Salad Bar',
    'Dessert Bar',
    'Charcuterie & Cheese',
    'Soup',
    'Beverages',
    'Other',
]

DIETARY_FLAGS = ['is_gf', 'is_v', 'is_vg', 'is_df', 'is_nf']
DIETARY_LABELS = {'is_gf': 'GF', 'is_v': 'V', 'is_vg': 'VG', 'is_df': 'DF', 'is_nf': 'NF'}
LINE_FIELD_PATTERN = re.compile(r'^lines\[(\d+)\]\[([a-z_]+)\]$')


def buffet_tables_ready(cur):
    return db_table_exists(cur, 'public.buffet_events') and db_table_exists(cur, 'public.buffet_event_lines')


def get_buffet_event(cur, event_id):
    cur.execute(
        """
        SELECT *
        FROM buffet_events
        WHERE id = %s
        """,
        (event_id,),
    )
    return cur.fetchone()


def get_buffet_event_lines(cur, event_id):
    cur.execute(
        """
        SELECT
            bel.*,
            r.name AS recipe_name,
            r.recipe_type,
            r.yield_qty,
            r.yield_unit
        FROM buffet_event_lines bel
        LEFT JOIN recipes r ON r.id = bel.recipe_id
        WHERE bel.event_id = %s
        ORDER BY bel.sort_order, bel.id
        """,
        (event_id,),
    )
    return cur.fetchall()


def get_guest_total(event):
    if not event:
        return 0
    return int(
        to_float(event.get('guests_adult'))
        + to_float(event.get('guests_child'))
        + to_float(event.get('guests_senior'))
        + to_float(event.get('guests_comp'))
    )


def get_recipe_options(cur):
    cur.execute(
        """
        SELECT id, name, recipe_type, yield_qty, yield_unit
        FROM recipes
        ORDER BY name
        """
    )
    return cur.fetchall()


def parse_buffet_lines(request):
    indexed_rows = defaultdict(dict)
    for key in request.form.keys():
        match = LINE_FIELD_PATTERN.match(key)
        if not match:
            continue
        indexed_rows[int(match.group(1))][match.group(2)] = request.form.get(key)

    lines = []
    for row_index in sorted(indexed_rows):
        raw_line = indexed_rows[row_index]
        dish_name = clean_menu_text(raw_line.get('dish_name'))
        if not dish_name:
            continue

        line = {
            'station': clean_menu_text(raw_line.get('station')) or None,
            'dish_name': dish_name,
            'description': clean_menu_text(raw_line.get('description')) or None,
            'vessel': clean_menu_text(raw_line.get('vessel')) or None,
            'foh_talking_points': clean_menu_text(raw_line.get('foh_talking_points')) or None,
            'recipe_id': clean_menu_text(raw_line.get('recipe_id')) or None,
            'serving_size_qty': parse_float_field(
                raw_line.get('serving_size_qty'),
                'Serving size',
                [],
                required=False,
                min_value=0,
            ),
            'serving_size_unit': clean_menu_text(raw_line.get('serving_size_unit')) or None,
        }
        for flag in DIETARY_FLAGS:
            line[flag] = bool(raw_line.get(flag))
        lines.append(line)
    return lines


def _parse_int_field(value, label, errors, default=0, min_value=0):
    text = (value or '').strip()
    if not text:
        return default
    try:
        number = int(text)
    except ValueError:
        errors.append(f'{label} must be a whole number.')
        return default
    if number < min_value:
        errors.append(f'{label} must be at least {min_value}.')
        return default
    return number


def _valid_status_values():
    return {choice[0] for choice in BUFFET_STATUS_CHOICES}


def _build_recipe_display_label(recipe):
    recipe_name = recipe.get('name') or 'Recipe'
    recipe_type = clean_menu_text(recipe.get('recipe_type')) if recipe.get('recipe_type') else ''
    return f'{recipe_name} ({recipe_type})' if recipe_type else recipe_name


def _prepare_recipe_options(recipe_rows):
    prepared = []
    for row in recipe_rows:
        item = dict(row)
        item['display_label'] = _build_recipe_display_label(item)
        prepared.append(item)
    return prepared


def _prepare_lines_for_form(lines, recipe_map):
    prepared = []
    for row in lines or []:
        item = dict(row)
        if item.get('recipe_id') and item.get('recipe_id') in recipe_map:
            item['recipe_display_label'] = recipe_map[item['recipe_id']]
        elif item.get('recipe_name'):
            item['recipe_display_label'] = _build_recipe_display_label(item)
        else:
            item['recipe_display_label'] = ''
        prepared.append(item)
    return prepared


def _build_event_form_state(source=None, **overrides):
    state = {
        'id': None,
        'name': '',
        'event_date': '',
        'venue_id': '',
        'building': '',
        'room': '',
        'service_timing': '',
        'ticket_adult': '',
        'ticket_child': '',
        'ticket_senior': '',
        'ticket_comp': '',
        'guests_adult': '',
        'guests_child': '',
        'guests_senior': '',
        'guests_comp': '',
        'dietary_notes': '',
        'notes': '',
        'status': 'planning',
    }
    if source:
        for key in state:
            if key in source:
                state[key] = source.get(key)
    state.update(overrides)
    return state


def _insert_buffet_line(cur, event_id, line, sort_order):
    cur.execute(
        """
        INSERT INTO buffet_event_lines (
            event_id,
            station,
            dish_name,
            description,
            vessel,
            foh_talking_points,
            recipe_id,
            serving_size_qty,
            serving_size_unit,
            is_gf,
            is_v,
            is_vg,
            is_df,
            is_nf,
            sort_order
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            event_id,
            line.get('station'),
            line.get('dish_name'),
            line.get('description'),
            line.get('vessel'),
            line.get('foh_talking_points'),
            line.get('recipe_id'),
            line.get('serving_size_qty'),
            line.get('serving_size_unit'),
            bool(line.get('is_gf')),
            bool(line.get('is_v')),
            bool(line.get('is_vg')),
            bool(line.get('is_df')),
            bool(line.get('is_nf')),
            sort_order,
        ),
    )


@bp.route('/buffet-planner')
@login_required
def buffet_planner():
    with get_cursor() as cur:
        if not buffet_tables_ready(cur):
            flash('Buffet tables are missing. Run migrations first.', 'error')
            return render_template(
                'buffet_planner.html',
                events=[],
                upcoming_events=[],
                past_events=[],
                status_choices=BUFFET_STATUS_CHOICES,
            )

        cur.execute(
            """
            SELECT
                e.*,
                COALESCE(e.guests_adult, 0)
                + COALESCE(e.guests_child, 0)
                + COALESCE(e.guests_senior, 0)
                + COALESCE(e.guests_comp, 0) AS guest_total
            FROM buffet_events e
            ORDER BY e.event_date DESC, e.created_at DESC, e.id DESC
            """
        )
        events = cur.fetchall()

        today_value = date.today()
        cur.execute(
            """
            SELECT
                e.*,
                COALESCE(e.guests_adult, 0)
                + COALESCE(e.guests_child, 0)
                + COALESCE(e.guests_senior, 0)
                + COALESCE(e.guests_comp, 0) AS guest_total
            FROM buffet_events e
            WHERE e.event_date >= %s
            ORDER BY e.event_date ASC, e.created_at ASC, e.id ASC
            """,
            (today_value,),
        )
        upcoming_events = cur.fetchall()

        cur.execute(
            """
            SELECT
                e.*,
                COALESCE(e.guests_adult, 0)
                + COALESCE(e.guests_child, 0)
                + COALESCE(e.guests_senior, 0)
                + COALESCE(e.guests_comp, 0) AS guest_total
            FROM buffet_events e
            WHERE e.event_date < %s
            ORDER BY e.event_date DESC, e.created_at DESC, e.id DESC
            LIMIT 20
            """,
            (today_value,),
        )
        past_events = cur.fetchall()

    return render_template(
        'buffet_planner.html',
        events=events,
        upcoming_events=upcoming_events,
        past_events=past_events,
        status_choices=BUFFET_STATUS_CHOICES,
    )


@bp.route('/buffet-planner/events/new', methods=['GET', 'POST'])
@login_required
def buffet_event_new():
    conn = get_db()
    with get_cursor() as cur:
        if not buffet_tables_ready(cur):
            flash('Buffet tables are missing. Run migrations first.', 'error')
            return redirect(url_for('buffet_planner'))

        recipe_options = _prepare_recipe_options(get_recipe_options(cur))
        recipe_map = {row['id']: row['display_label'] for row in recipe_options if row.get('id')}
        event = _build_event_form_state()
        lines = []

        if request.method == 'POST':
            errors = []
            name = clean_menu_text(request.form.get('name'))
            event_date_raw = (request.form.get('event_date') or '').strip()
            status = clean_menu_text(request.form.get('status') or 'planning').lower() or 'planning'
            venue_id = clean_menu_text(request.form.get('venue_id')) or None

            ticket_adult = parse_float_field(request.form.get('ticket_adult'), 'Adult ticket price', errors, required=False, min_value=0) or 0
            ticket_child = parse_float_field(request.form.get('ticket_child'), 'Child ticket price', errors, required=False, min_value=0) or 0
            ticket_senior = parse_float_field(request.form.get('ticket_senior'), 'Senior ticket price', errors, required=False, min_value=0) or 0

            ticket_comp = _parse_int_field(request.form.get('ticket_comp'), 'Comp tickets', errors, default=0, min_value=0)
            guests_adult = _parse_int_field(request.form.get('guests_adult'), 'Adult guests', errors, default=0, min_value=0)
            guests_child = _parse_int_field(request.form.get('guests_child'), 'Child guests', errors, default=0, min_value=0)
            guests_senior = _parse_int_field(request.form.get('guests_senior'), 'Senior guests', errors, default=0, min_value=0)
            guests_comp = _parse_int_field(request.form.get('guests_comp'), 'Comp guests', errors, default=0, min_value=0)

            if not name:
                errors.append('Event name is required.')

            try:
                event_date = datetime.strptime(event_date_raw, '%Y-%m-%d').date()
            except ValueError:
                event_date = None
                errors.append('Event date is required and must be in YYYY-MM-DD format.')

            if status not in _valid_status_values():
                errors.append('Status is invalid.')
                status = 'planning'

            lines = _prepare_lines_for_form(parse_buffet_lines(request), recipe_map)
            event = _build_event_form_state(
                event,
                name=name,
                event_date=event_date_raw,
                venue_id=venue_id or '',
                building=clean_menu_text(request.form.get('building')),
                room=clean_menu_text(request.form.get('room')),
                service_timing=clean_menu_text(request.form.get('service_timing')),
                ticket_adult=request.form.get('ticket_adult') or '',
                ticket_child=request.form.get('ticket_child') or '',
                ticket_senior=request.form.get('ticket_senior') or '',
                ticket_comp=request.form.get('ticket_comp') or '',
                guests_adult=request.form.get('guests_adult') or '',
                guests_child=request.form.get('guests_child') or '',
                guests_senior=request.form.get('guests_senior') or '',
                guests_comp=request.form.get('guests_comp') or '',
                dietary_notes=clean_menu_text(request.form.get('dietary_notes')),
                notes=clean_menu_text(request.form.get('notes')),
                status=status,
            )

            if errors:
                flash(' '.join(sorted(set(errors))), 'error')
            else:
                event_id = generate_id('buf_')
                try:
                    cur.execute(
                        """
                        INSERT INTO buffet_events (
                            id,
                            name,
                            event_date,
                            venue_id,
                            building,
                            room,
                            service_timing,
                            ticket_adult,
                            ticket_child,
                            ticket_senior,
                            ticket_comp,
                            guests_adult,
                            guests_child,
                            guests_senior,
                            guests_comp,
                            dietary_notes,
                            notes,
                            status
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            event_id,
                            name,
                            event_date,
                            venue_id,
                            event.get('building') or None,
                            event.get('room') or None,
                            event.get('service_timing') or None,
                            ticket_adult,
                            ticket_child,
                            ticket_senior,
                            ticket_comp,
                            guests_adult,
                            guests_child,
                            guests_senior,
                            guests_comp,
                            event.get('dietary_notes') or None,
                            event.get('notes') or None,
                            status,
                        ),
                    )

                    for idx, line in enumerate(lines):
                        _insert_buffet_line(cur, event_id, line, idx)

                    conn.commit()
                    flash('Buffet event created.', 'success')
                    return redirect(url_for('buffet_event_edit', event_id=event_id))
                except Exception:
                    traceback.print_exc()
                    conn.rollback()
                    flash('Error creating buffet event.', 'error')

    return render_template(
        'buffet_event_form.html',
        page_title='New Buffet Event',
        mode='new',
        event=event,
        lines=lines,
        recipe_options=recipe_options,
        station_options=BUFFET_STATION_OPTIONS,
        status_choices=BUFFET_STATUS_CHOICES,
        dietary_labels=DIETARY_LABELS,
        guest_total=get_guest_total(event),
    )


@bp.route('/buffet-planner/events/<event_id>/edit', methods=['GET', 'POST'])
@login_required
def buffet_event_edit(event_id):
    conn = get_db()
    with get_cursor() as cur:
        if not buffet_tables_ready(cur):
            flash('Buffet tables are missing. Run migrations first.', 'error')
            return redirect(url_for('buffet_planner'))

        event = get_buffet_event(cur, event_id)
        if not event:
            flash('Event not found.', 'error')
            return redirect(url_for('buffet_planner'))

        recipe_options = _prepare_recipe_options(get_recipe_options(cur))
        recipe_map = {row['id']: row['display_label'] for row in recipe_options if row.get('id')}

        if request.method == 'POST':
            errors = []
            name = clean_menu_text(request.form.get('name'))
            event_date_raw = (request.form.get('event_date') or '').strip()
            status = clean_menu_text(request.form.get('status') or 'planning').lower() or 'planning'
            venue_id = clean_menu_text(request.form.get('venue_id')) or event.get('venue_id') or None

            ticket_adult = parse_float_field(request.form.get('ticket_adult'), 'Adult ticket price', errors, required=False, min_value=0) or 0
            ticket_child = parse_float_field(request.form.get('ticket_child'), 'Child ticket price', errors, required=False, min_value=0) or 0
            ticket_senior = parse_float_field(request.form.get('ticket_senior'), 'Senior ticket price', errors, required=False, min_value=0) or 0

            ticket_comp = _parse_int_field(request.form.get('ticket_comp'), 'Comp tickets', errors, default=0, min_value=0)
            guests_adult = _parse_int_field(request.form.get('guests_adult'), 'Adult guests', errors, default=0, min_value=0)
            guests_child = _parse_int_field(request.form.get('guests_child'), 'Child guests', errors, default=0, min_value=0)
            guests_senior = _parse_int_field(request.form.get('guests_senior'), 'Senior guests', errors, default=0, min_value=0)
            guests_comp = _parse_int_field(request.form.get('guests_comp'), 'Comp guests', errors, default=0, min_value=0)

            if not name:
                errors.append('Event name is required.')

            try:
                event_date = datetime.strptime(event_date_raw, '%Y-%m-%d').date()
            except ValueError:
                event_date = None
                errors.append('Event date is required and must be in YYYY-MM-DD format.')

            if status not in _valid_status_values():
                errors.append('Status is invalid.')
                status = 'planning'

            lines = _prepare_lines_for_form(parse_buffet_lines(request), recipe_map)
            event = _build_event_form_state(
                event,
                id=event_id,
                name=name,
                event_date=event_date_raw,
                venue_id=venue_id or '',
                building=clean_menu_text(request.form.get('building')),
                room=clean_menu_text(request.form.get('room')),
                service_timing=clean_menu_text(request.form.get('service_timing')),
                ticket_adult=request.form.get('ticket_adult') or '',
                ticket_child=request.form.get('ticket_child') or '',
                ticket_senior=request.form.get('ticket_senior') or '',
                ticket_comp=request.form.get('ticket_comp') or '',
                guests_adult=request.form.get('guests_adult') or '',
                guests_child=request.form.get('guests_child') or '',
                guests_senior=request.form.get('guests_senior') or '',
                guests_comp=request.form.get('guests_comp') or '',
                dietary_notes=clean_menu_text(request.form.get('dietary_notes')),
                notes=clean_menu_text(request.form.get('notes')),
                status=status,
            )

            if errors:
                flash(' '.join(sorted(set(errors))), 'error')
            else:
                try:
                    cur.execute(
                        """
                        UPDATE buffet_events
                        SET name = %s,
                            event_date = %s,
                            venue_id = %s,
                            building = %s,
                            room = %s,
                            service_timing = %s,
                            ticket_adult = %s,
                            ticket_child = %s,
                            ticket_senior = %s,
                            ticket_comp = %s,
                            guests_adult = %s,
                            guests_child = %s,
                            guests_senior = %s,
                            guests_comp = %s,
                            dietary_notes = %s,
                            notes = %s,
                            status = %s,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = %s
                        """,
                        (
                            name,
                            event_date,
                            venue_id,
                            event.get('building') or None,
                            event.get('room') or None,
                            event.get('service_timing') or None,
                            ticket_adult,
                            ticket_child,
                            ticket_senior,
                            ticket_comp,
                            guests_adult,
                            guests_child,
                            guests_senior,
                            guests_comp,
                            event.get('dietary_notes') or None,
                            event.get('notes') or None,
                            status,
                            event_id,
                        ),
                    )
                    cur.execute("DELETE FROM buffet_event_lines WHERE event_id = %s", (event_id,))
                    for idx, line in enumerate(lines):
                        _insert_buffet_line(cur, event_id, line, idx)
                    conn.commit()
                    flash('Buffet event updated.', 'success')
                    return redirect(url_for('buffet_event_edit', event_id=event_id))
                except Exception:
                    traceback.print_exc()
                    conn.rollback()
                    flash('Error updating buffet event.', 'error')
        else:
            event = _build_event_form_state(event, id=event_id)
            lines = _prepare_lines_for_form(get_buffet_event_lines(cur, event_id), recipe_map)

    return render_template(
        'buffet_event_form.html',
        page_title='Edit Buffet Event',
        mode='edit',
        event=event,
        lines=lines,
        recipe_options=recipe_options,
        station_options=BUFFET_STATION_OPTIONS,
        status_choices=BUFFET_STATUS_CHOICES,
        dietary_labels=DIETARY_LABELS,
        guest_total=get_guest_total(event),
    )


@bp.route('/buffet-planner/events/<event_id>/delete', methods=['POST'])
@login_required
def buffet_event_delete(event_id):
    conn = get_db()
    with get_cursor() as cur:
        if not buffet_tables_ready(cur):
            flash('Buffet tables are missing. Run migrations first.', 'error')
            return redirect(url_for('buffet_planner'))

        event = get_buffet_event(cur, event_id)
        if not event:
            flash('Event not found.', 'error')
            return redirect(url_for('buffet_planner'))

        try:
            cur.execute("DELETE FROM buffet_events WHERE id = %s", (event_id,))
            conn.commit()
            flash(f"Deleted event: {event.get('name') or event_id}", 'success')
        except Exception:
            traceback.print_exc()
            conn.rollback()
            flash('Error deleting buffet event.', 'error')

    return redirect(url_for('buffet_planner'))


@bp.route('/buffet-planner/events/<event_id>/duplicate', methods=['POST'])
@login_required
def buffet_event_duplicate(event_id):
    conn = get_db()
    with get_cursor() as cur:
        if not buffet_tables_ready(cur):
            flash('Buffet tables are missing. Run migrations first.', 'error')
            return redirect(url_for('buffet_planner'))

        source_event = get_buffet_event(cur, event_id)
        if not source_event:
            flash('Event not found.', 'error')
            return redirect(url_for('buffet_planner'))

        duplicate_name = clean_menu_text(request.form.get('duplicate_name'))
        if not duplicate_name:
            duplicate_name = f"{source_event.get('name') or 'Event'} (Copy)"

        duplicate_date_raw = (request.form.get('duplicate_event_date') or '').strip()
        try:
            duplicate_date = datetime.strptime(duplicate_date_raw, '%Y-%m-%d').date()
        except ValueError:
            flash('Duplicate date is required in YYYY-MM-DD format.', 'error')
            return redirect(url_for('buffet_event_edit', event_id=event_id))

        source_lines = get_buffet_event_lines(cur, event_id)
        new_event_id = generate_id('buf_')
        try:
            cur.execute(
                """
                INSERT INTO buffet_events (
                    id,
                    name,
                    event_date,
                    venue_id,
                    building,
                    room,
                    service_timing,
                    ticket_adult,
                    ticket_child,
                    ticket_senior,
                    ticket_comp,
                    guests_adult,
                    guests_child,
                    guests_senior,
                    guests_comp,
                    dietary_notes,
                    notes,
                    status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    new_event_id,
                    duplicate_name,
                    duplicate_date,
                    source_event.get('venue_id'),
                    source_event.get('building'),
                    source_event.get('room'),
                    source_event.get('service_timing'),
                    source_event.get('ticket_adult') or 0,
                    source_event.get('ticket_child') or 0,
                    source_event.get('ticket_senior') or 0,
                    source_event.get('ticket_comp') or 0,
                    source_event.get('guests_adult') or 0,
                    source_event.get('guests_child') or 0,
                    source_event.get('guests_senior') or 0,
                    source_event.get('guests_comp') or 0,
                    source_event.get('dietary_notes'),
                    source_event.get('notes'),
                    source_event.get('status') or 'planning',
                ),
            )

            for line in source_lines:
                _insert_buffet_line(cur, new_event_id, line, int(line.get('sort_order') or 0))

            conn.commit()
            flash(f'Duplicated event as: {duplicate_name}', 'success')
            return redirect(url_for('buffet_event_edit', event_id=new_event_id))
        except Exception:
            traceback.print_exc()
            conn.rollback()
            flash('Error duplicating buffet event.', 'error')
            return redirect(url_for('buffet_event_edit', event_id=event_id))
