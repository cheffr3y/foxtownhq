import io
import math
import re
import traceback
from collections import defaultdict
from datetime import date, datetime, timedelta
from html import escape

from db import get_cursor, get_db
from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from flask_login import login_required
from helpers.db_helpers import db_column_exists, db_table_exists
from helpers.formatting import make_safe_filename
from helpers.menu import clean_menu_text
from helpers.recipes import (
    build_component_tree,
    collect_direct_subrecipes_for_prep,
    collect_ingredients_from_components,
    get_recipe_total_cost,
)
from helpers.shared import generate_id, handle_route_error, parse_float_field, to_float
from helpers.units import convert_cost_per_unit, convert_quantity_between_units, format_number, get_unit_system, smart_quantity
from helpers.venues import get_active_venues
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

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
BUFFET_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS buffet_events (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        event_date DATE NOT NULL,
        venue_id TEXT REFERENCES venues(id) ON DELETE SET NULL,
        building TEXT,
        room TEXT,
        service_timing TEXT,
        ticket_adult NUMERIC DEFAULT 0,
        ticket_child NUMERIC DEFAULT 0,
        ticket_senior NUMERIC DEFAULT 0,
        ticket_comp INTEGER DEFAULT 0,
        guests_adult INTEGER DEFAULT 0,
        guests_child INTEGER DEFAULT 0,
        guests_senior INTEGER DEFAULT 0,
        guests_comp INTEGER DEFAULT 0,
        dietary_notes TEXT,
        notes TEXT,
        status TEXT NOT NULL DEFAULT 'planning',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS buffet_event_lines (
        id BIGSERIAL PRIMARY KEY,
        event_id TEXT NOT NULL REFERENCES buffet_events(id) ON DELETE CASCADE,
        station TEXT,
        dish_name TEXT NOT NULL,
        description TEXT,
        vessel TEXT,
        foh_talking_points TEXT,
        recipe_id TEXT REFERENCES recipes(id) ON DELETE SET NULL,
        serving_size_qty NUMERIC,
        serving_size_unit TEXT,
        is_gf BOOLEAN NOT NULL DEFAULT FALSE,
        is_v BOOLEAN NOT NULL DEFAULT FALSE,
        is_vg BOOLEAN NOT NULL DEFAULT FALSE,
        is_df BOOLEAN NOT NULL DEFAULT FALSE,
        is_nf BOOLEAN NOT NULL DEFAULT FALSE,
        sort_order INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    ALTER TABLE buffet_event_lines
      ADD COLUMN IF NOT EXISTS direct_ingredient_id TEXT REFERENCES ingredients(id) ON DELETE SET NULL,
      ADD COLUMN IF NOT EXISTS direct_ingredient_unit TEXT
    """,
    "CREATE INDEX IF NOT EXISTS idx_buffet_event_lines_event_id ON buffet_event_lines (event_id, sort_order)",
    "CREATE INDEX IF NOT EXISTS idx_buffet_events_event_date ON buffet_events (event_date)",
    """
    CREATE TABLE IF NOT EXISTS buffet_order_items (
        id BIGSERIAL PRIMARY KEY,
        event_id TEXT NOT NULL REFERENCES buffet_events(id) ON DELETE CASCADE,
        ingredient_id TEXT REFERENCES ingredients(id) ON DELETE SET NULL,
        ingredient_name TEXT NOT NULL,
        total_qty NUMERIC,
        total_unit TEXT,
        pack_size NUMERIC,
        pack_unit TEXT,
        pack_type TEXT DEFAULT 'oz',
        cases_needed NUMERIC,
        vendor TEXT,
        notes TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_buffet_order_items_event_id ON buffet_order_items (event_id)",
]


def ensure_buffet_schema(cur):
    has_events = db_table_exists(cur, 'public.buffet_events')
    has_lines = db_table_exists(cur, 'public.buffet_event_lines')
    has_order_items = db_table_exists(cur, 'public.buffet_order_items')
    changed = False

    if not (has_events and has_lines and has_order_items):
        for statement in BUFFET_SCHEMA_STATEMENTS:
            cur.execute(statement)
        changed = True

    if db_table_exists(cur, 'public.buffet_event_lines') and not db_column_exists(cur, 'public.buffet_event_lines', 'direct_ingredient_id'):
        cur.execute(
            """
            ALTER TABLE buffet_event_lines
              ADD COLUMN IF NOT EXISTS direct_ingredient_id TEXT REFERENCES ingredients(id) ON DELETE SET NULL,
              ADD COLUMN IF NOT EXISTS direct_ingredient_unit TEXT
            """
        )
        changed = True

    return changed


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
            bel.direct_ingredient_id,
            bel.direct_ingredient_unit,
            r.name AS recipe_name,
            r.recipe_type,
            r.yield_qty,
            r.yield_unit,
            di.name AS direct_ingredient_name
        FROM buffet_event_lines bel
        LEFT JOIN recipes r ON r.id = bel.recipe_id
        LEFT JOIN ingredients di ON di.id = bel.direct_ingredient_id
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


def get_ingredient_options(cur):
    cur.execute(
        """
        SELECT id, name, unit, category
        FROM ingredients
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
        line['direct_ingredient_id'] = clean_menu_text(raw_line.get('direct_ingredient_id')) or None
        line['direct_ingredient_unit'] = clean_menu_text(raw_line.get('direct_ingredient_unit')) or None
        if line['direct_ingredient_id']:
            line['recipe_id'] = None
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


def _prepare_lines_for_form(lines, recipe_map, ingredient_map=None):
    prepared = []
    for row in lines or []:
        item = dict(row)
        if item.get('recipe_id') and item.get('recipe_id') in recipe_map:
            item['recipe_display_label'] = recipe_map[item['recipe_id']]
        elif item.get('recipe_name'):
            item['recipe_display_label'] = _build_recipe_display_label(item)
        else:
            item['recipe_display_label'] = ''
        direct_ingredient_id = item.get('direct_ingredient_id')
        item['direct_ingredient_display'] = (
            item.get('direct_ingredient_name')
            or (ingredient_map or {}).get(direct_ingredient_id)
            or ''
        )
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
    has_direct = db_column_exists(cur, 'public.buffet_event_lines', 'direct_ingredient_id')

    if has_direct:
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
                direct_ingredient_id,
                direct_ingredient_unit,
                serving_size_qty,
                serving_size_unit,
                is_gf,
                is_v,
                is_vg,
                is_df,
                is_nf,
                sort_order
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                event_id,
                line.get('station'),
                line.get('dish_name'),
                line.get('description'),
                line.get('vessel'),
                line.get('foh_talking_points'),
                line.get('recipe_id'),
                line.get('direct_ingredient_id'),
                line.get('direct_ingredient_unit'),
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
        return

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


def build_buffet_prep_sheet(cur, event_id, unit_system='imperial'):
    event = get_buffet_event(cur, event_id)
    guest_total = get_guest_total(event)
    lines = get_buffet_event_lines(cur, event_id)

    grouped = defaultdict(list)
    no_recipe_lines = []
    direct_ingredient_lines = []
    station_pull_specs = defaultdict(lambda: {'recipes': [], 'subrecipes': {}, 'ingredients': {}, 'dish_names': set()})
    station_summary_map = defaultdict(
        lambda: {
            'station': 'Other',
            'line_count': 0,
            'linked_line_count': 0,
            'recipe_line_count': 0,
            'direct_line_count': 0,
            'unlinked_line_count': 0,
            'costed_line_count': 0,
            'estimated_cost_total': 0.0,
        }
    )
    all_cards = []
    recipe_total_cost_cache = {}
    total_estimated_cost = 0.0
    total_costed_lines = 0

    direct_ingredient_ids = sorted({line.get('direct_ingredient_id') for line in lines if line.get('direct_ingredient_id')})
    direct_ingredient_map = {}
    if direct_ingredient_ids:
        cur.execute(
            """
            SELECT id, name, unit, cost_per_unit
            FROM ingredients
            WHERE id = ANY(%s)
            """,
            (direct_ingredient_ids,),
        )
        direct_ingredient_map = {row['id']: row for row in cur.fetchall()}

    for line in lines:
        station_name = line.get('station') or 'Other'
        station_summary = station_summary_map[station_name]
        station_summary['station'] = station_name
        station_summary['line_count'] += 1

        recipe_id = line.get('recipe_id')
        direct_ingredient_id = line.get('direct_ingredient_id')
        if not recipe_id and not direct_ingredient_id:
            no_recipe_lines.append(line.get('dish_name') or 'Unnamed Dish')
            station_summary['unlinked_line_count'] += 1
            continue
        if direct_ingredient_id:
            station_summary['linked_line_count'] += 1
            station_summary['direct_line_count'] += 1
            serving_size_qty = to_float(line.get('serving_size_qty')) or 1
            total_qty = serving_size_qty * guest_total
            unit = (line.get('direct_ingredient_unit') or line.get('serving_size_unit') or '').strip()
            display_total = smart_quantity(total_qty, unit, unit_system)
            ingredient = direct_ingredient_map.get(direct_ingredient_id, {})
            estimated_cost_total = None
            if ingredient.get('cost_per_unit') is not None:
                converted_cost = convert_cost_per_unit(
                    ingredient.get('cost_per_unit'),
                    ingredient.get('unit'),
                    unit or ingredient.get('unit'),
                )
                estimated_cost_total = total_qty * converted_cost
                station_summary['estimated_cost_total'] += estimated_cost_total
                station_summary['costed_line_count'] += 1
                total_estimated_cost += estimated_cost_total
                total_costed_lines += 1
            direct_line = {
                'dish_name': line.get('dish_name') or 'Dish',
                'station': station_name,
                'ingredient_id': direct_ingredient_id,
                'ingredient_name': line.get('direct_ingredient_name') or ingredient.get('name') or 'Ingredient',
                'total_qty': total_qty,
                'unit': unit,
                'display_total_qty': display_total.get('quantity'),
                'display_total_unit': display_total.get('unit'),
                'estimated_cost_total': estimated_cost_total,
                'estimated_cost_per_guest': (estimated_cost_total / guest_total) if estimated_cost_total is not None and guest_total > 0 else None,
            }
            for flag in DIETARY_FLAGS:
                direct_line[flag] = bool(line.get(flag))
            direct_ingredient_lines.append(direct_line)
            station_pull_specs[station_name]['dish_names'].add(direct_line['dish_name'])
            key = (direct_ingredient_id, unit)
            station_pull_specs[station_name]['ingredients'][key] = station_pull_specs[station_name]['ingredients'].get(key, 0) + total_qty
            continue

        station_summary['linked_line_count'] += 1
        station_summary['recipe_line_count'] += 1
        serving_qty = to_float(line.get('serving_size_qty')) or 1
        serving_unit = (line.get('serving_size_unit') or '').strip()
        yield_unit = (line.get('yield_unit') or '').strip()
        total_needed_qty = serving_qty * guest_total
        total_needed_unit = serving_unit
        recipe_yield_raw = to_float(line.get('yield_qty'))
        recipe_yield = recipe_yield_raw or 1
        if serving_unit and yield_unit and serving_unit != yield_unit:
            converted = convert_quantity_between_units(total_needed_qty, serving_unit, yield_unit)
            if converted is not None:
                total_needed_qty = converted
                total_needed_unit = yield_unit
        batches_needed = math.ceil(total_needed_qty / recipe_yield) if recipe_yield > 0 else 0
        total_yield = batches_needed * recipe_yield
        display_total_needed = smart_quantity(total_needed_qty, total_needed_unit, unit_system)
        display_total_yield = smart_quantity(total_yield, yield_unit, unit_system)
        scale_ratio = (total_needed_qty / recipe_yield_raw) if recipe_yield_raw and recipe_yield_raw > 0 else None
        estimated_cost_total = None
        if scale_ratio is not None:
            if recipe_id not in recipe_total_cost_cache:
                recipe_total_cost_cache[recipe_id] = get_recipe_total_cost(cur, recipe_id, unit_system, apply_q_factor=True)
            estimated_cost_total = recipe_total_cost_cache[recipe_id] * scale_ratio
            station_summary['estimated_cost_total'] += estimated_cost_total
            station_summary['costed_line_count'] += 1
            total_estimated_cost += estimated_cost_total
            total_costed_lines += 1

        card = {
            'dish_name': line.get('dish_name') or 'Dish',
            'station': station_name,
            'recipe_id': recipe_id,
            'recipe_name': line.get('recipe_name') or 'Recipe',
            'yield_qty': recipe_yield,
            'yield_unit': yield_unit,
            'serving_size_qty': serving_qty,
            'serving_size_unit': serving_unit or yield_unit,
            'total_needed_qty': total_needed_qty,
            'total_needed_unit': total_needed_unit,
            'display_total_needed_qty': display_total_needed.get('quantity'),
            'display_total_needed_unit': display_total_needed.get('unit'),
            'batches_needed': batches_needed,
            'total_yield': total_yield,
            'display_total_yield_qty': display_total_yield.get('quantity'),
            'display_total_yield_unit': display_total_yield.get('unit'),
            'scale_ratio': scale_ratio,
            'estimated_cost_total': estimated_cost_total,
            'estimated_cost_per_guest': (estimated_cost_total / guest_total) if estimated_cost_total is not None and guest_total > 0 else None,
            'assigned_to': '',
            'status': 'pending',
        }
        for flag in DIETARY_FLAGS:
            card[flag] = bool(line.get(flag))
        grouped[card['station']].append(card)
        all_cards.append(card)
        station_pull_specs[station_name]['dish_names'].add(card['dish_name'])
        station_pull_specs[station_name]['recipes'].append(
            {
                'dish_name': card['dish_name'],
                'recipe_id': recipe_id,
                'recipe_name': card['recipe_name'],
                'required_qty': total_needed_qty,
                'required_unit': total_needed_unit,
                'display_required_qty': display_total_needed.get('quantity'),
                'display_required_unit': display_total_needed.get('unit'),
                'yield_qty': recipe_yield,
                'yield_unit': yield_unit,
                'batches_needed': batches_needed,
                'display_batches_needed': format_number(batches_needed),
                'estimated_cost_total': estimated_cost_total,
            }
        )

        if scale_ratio is not None:
            line_subrecipe_totals = {}
            line_ingredient_totals = {}
            components, _, _ = build_component_tree(cur, recipe_id, scale_ratio, depth=0, path=set(), unit_system=unit_system, apply_q_factor=False)
            collect_direct_subrecipes_for_prep(components, line_subrecipe_totals)
            # Buffet station checklists should include raw ingredients from nested
            # sub-recipes, not just the top-level recipe leaves.
            collect_ingredients_from_components(components, line_ingredient_totals)
            if line_subrecipe_totals or line_ingredient_totals:
                station_name = card['station']
                station_pull_specs[station_name]['dish_names'].add(card['dish_name'])
                for key, qty in line_subrecipe_totals.items():
                    station_pull_specs[station_name]['subrecipes'][key] = station_pull_specs[station_name]['subrecipes'].get(key, 0) + qty
                for key, qty in line_ingredient_totals.items():
                    station_pull_specs[station_name]['ingredients'][key] = station_pull_specs[station_name]['ingredients'].get(key, 0) + qty

    sorted_grouped = {}
    for station in sorted(grouped):
        sorted_grouped[station] = sorted(grouped[station], key=lambda row: (row.get('dish_name') or '').lower())
    direct_ingredient_lines.sort(key=lambda row: ((row.get('station') or '').lower(), (row.get('dish_name') or '').lower()))

    prep_station_groups = []
    if station_pull_specs:
        recipe_ids = sorted(
            {
                recipe_id
                for station in station_pull_specs.values()
                for recipe_id, _unit in station['subrecipes'].keys()
                if recipe_id
            }
        )
        recipe_map = {}
        if recipe_ids:
            cur.execute(
                """
                SELECT id, name, yield_qty, yield_unit
                FROM recipes
                WHERE id = ANY(%s)
                """,
                (recipe_ids,),
            )
            recipe_map = {row['id']: row for row in cur.fetchall()}

        ingredient_ids = sorted(
            {
                ingredient_id
                for station in station_pull_specs.values()
                for ingredient_id, _unit in station['ingredients'].keys()
                if ingredient_id
            }
        )
        ingredient_map = {}
        if ingredient_ids:
            cur.execute(
                """
                SELECT id, name, unit, category
                FROM ingredients
                WHERE id = ANY(%s)
                """,
                (ingredient_ids,),
            )
            ingredient_map = {row['id']: row for row in cur.fetchall()}

        for station_name in sorted(station_pull_specs):
            station_spec = station_pull_specs[station_name]
            recipe_rows = sorted(
                station_spec['recipes'],
                key=lambda item: ((item.get('dish_name') or '').lower(), (item.get('recipe_name') or '').lower()),
            )
            subrecipe_rows = []
            for (subrecipe_id, required_unit), required_qty in sorted(
                station_spec['subrecipes'].items(),
                key=lambda item: (recipe_map.get(item[0][0], {}).get('name') or '').lower(),
            ):
                recipe = recipe_map.get(subrecipe_id, {})
                yield_qty = to_float(recipe.get('yield_qty'))
                yield_unit = (recipe.get('yield_unit') or required_unit or '').strip()
                required_qty_in_yield = required_qty
                if required_unit and yield_unit and required_unit != yield_unit:
                    converted = convert_quantity_between_units(required_qty, required_unit, yield_unit)
                    if converted is not None:
                        required_qty_in_yield = converted
                required_batches = (required_qty_in_yield / yield_qty) if yield_qty > 0 else None
                display_required = smart_quantity(required_qty_in_yield, yield_unit or required_unit, unit_system)
                subrecipe_rows.append(
                    {
                        'recipe_id': subrecipe_id,
                        'recipe_name': recipe.get('name') or 'Sub-recipe',
                        'required_qty': required_qty_in_yield,
                        'required_unit': yield_unit or required_unit,
                        'required_batches': required_batches,
                        'display_required_batches': format_number(required_batches) if required_batches is not None else '',
                        'yield_qty': yield_qty,
                        'yield_unit': yield_unit,
                        'display_required_qty': display_required.get('quantity'),
                        'display_required_unit': display_required.get('unit'),
                    }
                )

            ingredient_rows = []
            for (ingredient_id, unit), total_qty in sorted(
                station_spec['ingredients'].items(),
                key=lambda item: (ingredient_map.get(item[0][0], {}).get('name') or '').lower(),
            ):
                ingredient = ingredient_map.get(ingredient_id, {})
                display_total = smart_quantity(total_qty, unit, unit_system)
                ingredient_rows.append(
                    {
                        'ingredient_id': ingredient_id,
                        'ingredient_name': ingredient.get('name') or 'Ingredient',
                        'total_qty': total_qty,
                        'unit': unit,
                        'display_total_qty': display_total.get('quantity'),
                        'display_total_unit': display_total.get('unit'),
                    }
                )

            if recipe_rows or subrecipe_rows or ingredient_rows:
                prep_station_groups.append(
                    {
                        'station': station_name,
                        'dish_names': sorted(station_spec['dish_names']),
                        'recipes': recipe_rows,
                        'subrecipes': subrecipe_rows,
                        'ingredients': ingredient_rows,
                    }
                )

    station_costs = []
    for station_name in sorted(station_summary_map):
        summary = dict(station_summary_map[station_name])
        linked_line_count = int(summary.get('linked_line_count') or 0)
        if linked_line_count <= 0:
            continue
        costed_line_count = int(summary.get('costed_line_count') or 0)
        estimated_cost_total = to_float(summary.get('estimated_cost_total'))
        summary['avg_cost_per_line'] = (estimated_cost_total / costed_line_count) if costed_line_count > 0 else None
        summary['avg_cost_per_guest'] = (estimated_cost_total / guest_total) if guest_total > 0 else None
        station_costs.append(summary)

    station_cost_map = {row['station']: row for row in station_costs}
    total_linked_lines = len(all_cards) + len(direct_ingredient_lines)

    return {
        'stations': sorted_grouped,
        'guest_total': guest_total,
        'total_cards': len(all_cards),
        'no_recipe_lines': no_recipe_lines,
        'direct_ingredient_lines': direct_ingredient_lines,
        'prep_station_groups': prep_station_groups,
        'station_costs': station_costs,
        'station_cost_map': station_cost_map,
        'total_linked_lines': total_linked_lines,
        'total_costed_lines': total_costed_lines,
        'uncosted_linked_lines': max(total_linked_lines - total_costed_lines, 0),
        'total_estimated_cost': total_estimated_cost,
        'avg_estimated_cost_per_linked_line': (total_estimated_cost / total_costed_lines) if total_costed_lines > 0 else None,
        'avg_estimated_cost_per_guest': (total_estimated_cost / guest_total) if guest_total > 0 else None,
    }


def _cases_needed_for_item(total_qty, total_unit, pack_size, pack_unit, pack_type):
    pack_size_value = to_float(pack_size)
    if pack_size_value <= 0:
        return None

    pack_type_value = (pack_type or 'oz').strip().lower() or 'oz'
    if pack_type_value == 'fixed':
        return math.ceil(pack_size_value)

    total_qty_value = to_float(total_qty)
    if total_qty_value <= 0:
        return 0

    if pack_type_value == 'pc':
        return math.ceil(total_qty_value / pack_size_value)

    converted_total = convert_quantity_between_units(total_qty_value, total_unit, pack_unit or 'oz')
    if converted_total is None:
        converted_total = convert_quantity_between_units(total_qty_value, total_unit, 'oz')
    if converted_total is None:
        return None
    return math.ceil(converted_total / pack_size_value)


def build_buffet_order_rollup(cur, event_id, unit_system='imperial'):
    event = get_buffet_event(cur, event_id)
    guest_total = get_guest_total(event)
    lines = get_buffet_event_lines(cur, event_id)
    totals = {}

    for line in lines:
        recipe_id = line.get('recipe_id')
        direct_ingredient_id = line.get('direct_ingredient_id')

        if recipe_id:
            serving_size_qty = to_float(line.get('serving_size_qty')) or 1
            recipe_yield = to_float(line.get('yield_qty'))
            if recipe_yield <= 0:
                continue

            scale_ratio = (serving_size_qty * guest_total) / recipe_yield
            components, _, _ = build_component_tree(cur, recipe_id, scale_ratio, depth=0, path=set(), unit_system=unit_system)
            collect_ingredients_from_components(components, totals)
        elif direct_ingredient_id:
            serving_size_qty = to_float(line.get('serving_size_qty')) or 1
            total_qty = serving_size_qty * guest_total
            unit = (line.get('direct_ingredient_unit') or line.get('serving_size_unit') or '').strip()
            key = (direct_ingredient_id, unit)
            totals[key] = totals.get(key, 0) + total_qty

    cur.execute(
        """
        SELECT
            boi.*,
            i.name AS ingredient_table_name,
            i.vendor AS ingredient_vendor,
            i.unit AS ingredient_default_unit
        FROM buffet_order_items boi
        LEFT JOIN ingredients i ON i.id = boi.ingredient_id
        WHERE boi.event_id = %s
        ORDER BY boi.id
        """,
        (event_id,),
    )
    saved_override_rows = cur.fetchall()
    saved_override_map = {}
    for row in saved_override_rows:
        ingredient_id = row.get('ingredient_id')
        total_unit = (row.get('total_unit') or '').strip()
        if ingredient_id:
            saved_override_map[(ingredient_id, total_unit)] = row

    ingredient_ids = sorted({ingredient_id for ingredient_id, _unit in totals.keys() if ingredient_id})
    ingredient_map = {}
    if ingredient_ids:
        cur.execute(
            """
            SELECT id, name, unit, category, cost_per_unit, vendor, vendor_code, g_code
            FROM ingredients
            WHERE id = ANY(%s)
            """,
            (ingredient_ids,),
        )
        ingredient_map = {row['id']: row for row in cur.fetchall()}

    order_items = []
    for (ingredient_id, total_unit), total_qty in totals.items():
        ingredient = ingredient_map.get(ingredient_id, {})
        override = saved_override_map.get((ingredient_id, total_unit), {})
        pack_size = override.get('pack_size')
        pack_unit = override.get('pack_unit') or total_unit or ingredient.get('unit') or ''
        pack_type = override.get('pack_type') or 'oz'
        cases_needed = _cases_needed_for_item(total_qty, total_unit, pack_size, pack_unit, pack_type)
        display_total = smart_quantity(total_qty, total_unit, unit_system)
        order_items.append(
            {
                'ingredient_id': ingredient_id,
                'ingredient_name': ingredient.get('name') or override.get('ingredient_name') or 'Unknown',
                'total_qty': total_qty,
                'total_unit': total_unit,
                'display_total_qty': display_total.get('quantity'),
                'display_total_unit': display_total.get('unit'),
                'pack_size': pack_size,
                'pack_unit': pack_unit,
                'pack_type': pack_type,
                'cases_needed': cases_needed,
                'vendor': override.get('vendor') or ingredient.get('vendor') or '',
                'notes': override.get('notes') or '',
                'pack_cost': None,
            }
        )

    order_items.sort(key=lambda item: ((item.get('vendor') or '').lower(), (item.get('ingredient_name') or '').lower()))
    return order_items


@bp.route('/buffet-planner')
@login_required
def buffet_planner():
    conn = get_db()
    with get_cursor() as cur:
        if ensure_buffet_schema(cur):
            conn.commit()
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
        if ensure_buffet_schema(cur):
            conn.commit()
        if not buffet_tables_ready(cur):
            flash('Buffet tables are missing. Run migrations first.', 'error')
            return redirect(url_for('buffet_planner'))

        recipe_options = _prepare_recipe_options(get_recipe_options(cur))
        recipe_map = {row['id']: row['display_label'] for row in recipe_options if row.get('id')}
        ingredient_options = get_ingredient_options(cur)
        ingredient_map = {row['id']: row.get('name') or '' for row in ingredient_options if row.get('id')}
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

            lines = _prepare_lines_for_form(parse_buffet_lines(request), recipe_map, ingredient_map)
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
        ingredient_options=ingredient_options,
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
        if ensure_buffet_schema(cur):
            conn.commit()
        if not buffet_tables_ready(cur):
            flash('Buffet tables are missing. Run migrations first.', 'error')
            return redirect(url_for('buffet_planner'))

        event = get_buffet_event(cur, event_id)
        if not event:
            flash('Event not found.', 'error')
            return redirect(url_for('buffet_planner'))

        recipe_options = _prepare_recipe_options(get_recipe_options(cur))
        recipe_map = {row['id']: row['display_label'] for row in recipe_options if row.get('id')}
        ingredient_options = get_ingredient_options(cur)
        ingredient_map = {row['id']: row.get('name') or '' for row in ingredient_options if row.get('id')}

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

            lines = _prepare_lines_for_form(parse_buffet_lines(request), recipe_map, ingredient_map)
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
            lines = _prepare_lines_for_form(get_buffet_event_lines(cur, event_id), recipe_map, ingredient_map)

    return render_template(
        'buffet_event_form.html',
        page_title='Edit Buffet Event',
        mode='edit',
        event=event,
        lines=lines,
        recipe_options=recipe_options,
        ingredient_options=ingredient_options,
        station_options=BUFFET_STATION_OPTIONS,
        status_choices=BUFFET_STATUS_CHOICES,
        dietary_labels=DIETARY_LABELS,
        guest_total=get_guest_total(event),
    )


@bp.route('/buffet-planner/events/<event_id>/prep')
@login_required
def buffet_event_prep(event_id):
    conn = get_db()
    with get_cursor() as cur:
        if ensure_buffet_schema(cur):
            conn.commit()
        if not buffet_tables_ready(cur):
            flash('Buffet tables are missing. Run migrations first.', 'error')
            return redirect(url_for('buffet_planner'))
        event = get_buffet_event(cur, event_id)
        if not event:
            flash('Event not found.', 'error')
            return redirect(url_for('buffet_planner'))
        unit_system = get_unit_system()
        prep_data = build_buffet_prep_sheet(cur, event_id, unit_system)
        guest_total = get_guest_total(event)
        return render_template(
            'buffet_prep.html',
            event=event,
            guest_total=guest_total,
            stations=prep_data['stations'],
            no_recipe_lines=prep_data['no_recipe_lines'],
            direct_ingredient_lines=prep_data['direct_ingredient_lines'],
            prep_station_groups=prep_data['prep_station_groups'],
            total_cards=prep_data['total_cards'],
            station_costs=prep_data['station_costs'],
            station_cost_map=prep_data['station_cost_map'],
            total_linked_lines=prep_data['total_linked_lines'],
            total_costed_lines=prep_data['total_costed_lines'],
            uncosted_linked_lines=prep_data['uncosted_linked_lines'],
            total_estimated_cost=prep_data['total_estimated_cost'],
            avg_estimated_cost_per_linked_line=prep_data['avg_estimated_cost_per_linked_line'],
            avg_estimated_cost_per_guest=prep_data['avg_estimated_cost_per_guest'],
        )


@bp.route('/buffet-planner/events/<event_id>/prep/pdf')
@login_required
def buffet_prep_pdf(event_id):
    conn = get_db()
    with get_cursor() as cur:
        if ensure_buffet_schema(cur):
            conn.commit()
        if not buffet_tables_ready(cur):
            flash('Buffet tables are missing. Run migrations first.', 'error')
            return redirect(url_for('buffet_planner'))
        event = get_buffet_event(cur, event_id)
        if not event:
            flash('Event not found.', 'error')
            return redirect(url_for('buffet_planner'))

        prep_data = build_buffet_prep_sheet(cur, event_id, get_unit_system())

    header_navy = colors.HexColor('#1B2A4A')
    border_gray = colors.HexColor('#DDDDDD')
    light_gray = colors.HexColor('#F5F5F5')
    muted_text = colors.HexColor('#4F5B6E')
    white = colors.white
    margin_x = 0.5 * inch
    margin_y = 0.5 * inch
    generated_at = datetime.now().strftime('%b %d, %Y %I:%M %p')

    base_style = ParagraphStyle(
        'buffet-pdf-base',
        fontName='Helvetica',
        fontSize=8.5,
        leading=10.2,
        textColor=colors.black,
    )
    table_header_style = ParagraphStyle(
        'buffet-pdf-table-header',
        parent=base_style,
        fontName='Helvetica-Bold',
        textColor=white,
        fontSize=8.2,
        leading=9.5,
    )
    section_title_style = ParagraphStyle(
        'buffet-pdf-section-title',
        parent=base_style,
        fontName='Helvetica-Bold',
        fontSize=12,
        leading=14,
        textColor=header_navy,
        spaceAfter=4,
    )
    note_style = ParagraphStyle(
        'buffet-pdf-note',
        parent=base_style,
        textColor=muted_text,
    )
    strong_style = ParagraphStyle(
        'buffet-pdf-strong',
        parent=base_style,
        fontName='Helvetica-Bold',
    )

    def to_text(value, default='-'):
        text = str(value or '').strip()
        return text or default

    def html_text(value, default='-'):
        return escape(to_text(value, default)).replace('\n', '<br/>')

    def p(value, style=base_style, default='-'):
        return Paragraph(html_text(value, default), style)

    def fmt_qty(qty, unit=''):
        qty_text = qty.strip() if isinstance(qty, str) else format_number(qty)
        unit_text = (unit or '').strip()
        return f'{qty_text} {unit_text}'.strip()

    def draw_header(pdf_canvas, doc):
        page_width, page_height = letter
        header_text = f"Buffet Prep Sheet - {event.get('name') or 'Event'} | {event.get('event_date')} | {prep_data['guest_total']} guests"
        pdf_canvas.saveState()
        pdf_canvas.setStrokeColor(border_gray)
        pdf_canvas.setLineWidth(0.6)
        pdf_canvas.line(doc.leftMargin, page_height - 42, page_width - doc.rightMargin, page_height - 42)
        pdf_canvas.setFillColor(header_navy)
        pdf_canvas.setFont('Helvetica-Bold', 11)
        pdf_canvas.drawString(doc.leftMargin, page_height - 32, header_text[:140])
        pdf_canvas.restoreState()

    def draw_footer(pdf_canvas, page_num, total_pages):
        page_width, _ = letter
        pdf_canvas.setStrokeColor(border_gray)
        pdf_canvas.setLineWidth(0.6)
        pdf_canvas.line(margin_x, margin_y + 12, page_width - margin_x, margin_y + 12)
        pdf_canvas.setFont('Helvetica', 8)
        pdf_canvas.setFillColor(muted_text)
        pdf_canvas.drawString(margin_x, margin_y + 2, f'Foxtown HQ | Page {page_num} of {total_pages} | Generated {generated_at}')

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
        draw_header(pdf_canvas, doc)

    def draw_later_pages(pdf_canvas, doc):
        draw_header(pdf_canvas, doc)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=margin_x,
        rightMargin=margin_x,
        topMargin=0.7 * inch,
        bottomMargin=0.55 * inch,
        title=f"Buffet Prep Sheet {event.get('name') or 'Event'}",
    )

    story = [Spacer(1, 0.15 * inch)]
    station_names = list(prep_data['stations'].keys())
    col_widths = [1.55 * inch, 1.3 * inch, 0.8 * inch, 0.45 * inch, 0.65 * inch, 0.8 * inch, 0.75 * inch, 0.9 * inch, 0.25 * inch]

    if prep_data['station_costs']:
        summary_note = 'Buffet menu lines are rolled into station totals here for build-average planning.'
        if prep_data.get('uncosted_linked_lines'):
            summary_note += f" {format_number(prep_data['uncosted_linked_lines'])} linked line(s) are missing enough cost data to be included."
        story.append(Paragraph('Theo by Station', section_title_style))
        story.append(Paragraph(
            summary_note,
            note_style,
        ))
        story.append(Spacer(1, 0.08 * inch))
        summary_rows = [[
            Paragraph('Station', table_header_style),
            Paragraph('Linked Lines', table_header_style),
            Paragraph('Recipe Lines', table_header_style),
            Paragraph('Direct Lines', table_header_style),
            Paragraph('Theo Spend', table_header_style),
            Paragraph('Avg / Costed Line', table_header_style),
            Paragraph('Avg / Guest', table_header_style),
        ]]
        for row in prep_data['station_costs']:
            summary_rows.append(
                [
                    p(row.get('station') or 'Other'),
                    p(format_number(row.get('linked_line_count'))),
                    p(format_number(row.get('recipe_line_count'))),
                    p(format_number(row.get('direct_line_count'))),
                    p(f"${format_number(row.get('estimated_cost_total') or 0)}"),
                    p(f"${format_number(row.get('avg_cost_per_line'))}" if row.get('avg_cost_per_line') is not None else '—'),
                    p(f"${format_number(row.get('avg_cost_per_guest'))}" if row.get('avg_cost_per_guest') is not None else '—'),
                ]
            )
        summary_table = Table(
            summary_rows,
            colWidths=[1.55 * inch, 0.8 * inch, 0.8 * inch, 0.8 * inch, 1.0 * inch, 1.0 * inch, 1.0 * inch],
            repeatRows=1,
        )
        summary_table.setStyle(
            TableStyle(
                [
                    ('BACKGROUND', (0, 0), (-1, 0), header_navy),
                    ('TEXTCOLOR', (0, 0), (-1, 0), white),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [white, light_gray]),
                    ('GRID', (0, 0), (-1, -1), 0.35, border_gray),
                    ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                    ('LEFTPADDING', (0, 0), (-1, -1), 4),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 4),
                    ('TOPPADDING', (0, 0), (-1, -1), 4),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ]
            )
        )
        story.append(summary_table)

    for station_index, station_name in enumerate(station_names):
        if station_index > 0 or (station_index == 0 and prep_data['station_costs']):
            story.append(PageBreak())

        story.append(Paragraph(escape(station_name), section_title_style))
        station_cost = prep_data['station_cost_map'].get(station_name) or {}
        if station_cost:
            story.append(Paragraph(
                f"Theo spend: ${format_number(station_cost.get('estimated_cost_total') or 0)}",
                note_style,
            ))
            story.append(Spacer(1, 0.04 * inch))
        station_rows = [
            [
                Paragraph('Dish Name', table_header_style),
                Paragraph('Recipe', table_header_style),
                Paragraph('Serving Size', table_header_style),
                Paragraph('Guests', table_header_style),
                Paragraph('Batches Needed', table_header_style),
                Paragraph('Total Yield', table_header_style),
                Paragraph('Theo Spend', table_header_style),
                Paragraph('Assigned To', table_header_style),
                Paragraph('&#10003;', table_header_style),
            ]
        ]

        for card in prep_data['stations'][station_name]:
            station_rows.append(
                [
                    p(card.get('dish_name') or 'Dish'),
                    p(card.get('recipe_name') or 'Recipe'),
                    p(fmt_qty(card.get('serving_size_qty'), card.get('serving_size_unit'))),
                    p(format_number(prep_data['guest_total'])),
                    Paragraph(f"<b>{escape(format_number(card.get('batches_needed')))}</b>", strong_style),
                    p(fmt_qty(card.get('display_total_yield_qty'), card.get('display_total_yield_unit'))),
                    p(f"${format_number(card.get('estimated_cost_total'))}" if card.get('estimated_cost_total') is not None else '—'),
                    p(' ', default=' '),
                    p(' ', default=' '),
                ]
            )

        station_table = Table(station_rows, colWidths=col_widths, repeatRows=1)
        station_table.setStyle(
            TableStyle(
                [
                    ('BACKGROUND', (0, 0), (-1, 0), header_navy),
                    ('TEXTCOLOR', (0, 0), (-1, 0), white),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [white, light_gray]),
                    ('GRID', (0, 0), (-1, -1), 0.35, border_gray),
                    ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                    ('LEFTPADDING', (0, 0), (-1, -1), 4),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 4),
                    ('TOPPADDING', (0, 0), (-1, -1), 4),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                    ('ALIGN', (3, 1), (6, -1), 'CENTER'),
                    ('ALIGN', (8, 1), (8, -1), 'CENTER'),
                ]
            )
        )
        story.append(station_table)

    has_previous_sections = bool(station_names or prep_data['station_costs'])

    if prep_data['direct_ingredient_lines']:
        if has_previous_sections:
            story.append(PageBreak())
        story.append(Paragraph('Direct Ingredient Lines', section_title_style))
        story.append(Paragraph(
            'These lines scale directly from serving size times guest count and do not use recipe batch math.',
            note_style,
        ))
        story.append(Spacer(1, 0.08 * inch))
        direct_rows = [[
            Paragraph('Station', table_header_style),
            Paragraph('Dish Name', table_header_style),
            Paragraph('Ingredient', table_header_style),
            Paragraph('Total Needed', table_header_style),
            Paragraph('Theo Spend', table_header_style),
        ]]
        for row in prep_data['direct_ingredient_lines']:
            direct_rows.append(
                [
                    p(row.get('station') or 'Other'),
                    p(row.get('dish_name') or 'Dish'),
                    p(row.get('ingredient_name') or 'Ingredient'),
                    p(fmt_qty(row.get('display_total_qty'), row.get('display_total_unit'))),
                    p(f"${format_number(row.get('estimated_cost_total'))}" if row.get('estimated_cost_total') is not None else '—'),
                ]
            )
        direct_table = Table(direct_rows, colWidths=[1.2 * inch, 2.0 * inch, 2.0 * inch, 1.1 * inch, 1.0 * inch], repeatRows=1)
        direct_table.setStyle(
            TableStyle(
                [
                    ('BACKGROUND', (0, 0), (-1, 0), header_navy),
                    ('TEXTCOLOR', (0, 0), (-1, 0), white),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [white, light_gray]),
                    ('GRID', (0, 0), (-1, -1), 0.35, border_gray),
                    ('LEFTPADDING', (0, 0), (-1, -1), 5),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 5),
                    ('TOPPADDING', (0, 0), (-1, -1), 4),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ]
            )
        )
        story.append(direct_table)
        has_previous_sections = True

    if prep_data['prep_station_groups']:
        if has_previous_sections:
            story.append(PageBreak())
        story.append(Paragraph('Pulls by Station', section_title_style))
        story.append(Paragraph(
            'Scaled recipes, sub-recipes, and raw-ingredient pulls are broken out by buffet station here for station-level prep.',
            note_style,
        ))
        story.append(Spacer(1, 0.08 * inch))
        for station_index, station_group in enumerate(prep_data['prep_station_groups']):
            if station_index > 0:
                story.append(Spacer(1, 0.12 * inch))
            story.append(Paragraph(escape(station_group.get('station') or 'Other'), strong_style))
            if station_group.get('dish_names'):
                story.append(Paragraph(
                    escape(', '.join(station_group['dish_names'])),
                    note_style,
                ))
            story.append(Spacer(1, 0.04 * inch))

            if station_group.get('recipes'):
                recipe_rows = [[
                    Paragraph('Dish', table_header_style),
                    Paragraph('Scaled Recipe', table_header_style),
                    Paragraph('Total Needed', table_header_style),
                    Paragraph('Yield', table_header_style),
                    Paragraph('Batches', table_header_style),
                    Paragraph('Theo Spend', table_header_style),
                ]]
                for row in station_group['recipes']:
                    recipe_rows.append(
                        [
                            p(row.get('dish_name') or 'Dish'),
                            p(row.get('recipe_name') or 'Recipe'),
                            p(fmt_qty(row.get('display_required_qty'), row.get('display_required_unit'))),
                            p(fmt_qty(row.get('yield_qty'), row.get('yield_unit'))),
                            p(row.get('display_batches_needed') or '—'),
                            p(f"${format_number(row.get('estimated_cost_total'))}" if row.get('estimated_cost_total') is not None else '—'),
                        ]
                    )
                recipe_table = Table(recipe_rows, colWidths=[1.8 * inch, 1.9 * inch, 1.1 * inch, 0.9 * inch, 0.7 * inch, 0.9 * inch], repeatRows=1)
                recipe_table.setStyle(
                    TableStyle(
                        [
                            ('BACKGROUND', (0, 0), (-1, 0), header_navy),
                            ('TEXTCOLOR', (0, 0), (-1, 0), white),
                            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [white, light_gray]),
                            ('GRID', (0, 0), (-1, -1), 0.35, border_gray),
                            ('LEFTPADDING', (0, 0), (-1, -1), 5),
                            ('RIGHTPADDING', (0, 0), (-1, -1), 5),
                            ('TOPPADDING', (0, 0), (-1, -1), 4),
                            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                        ]
                    )
                )
                story.append(recipe_table)
                story.append(Spacer(1, 0.06 * inch))

            if station_group.get('subrecipes'):
                subrecipe_rows = [[
                    Paragraph('Sub-Recipe', table_header_style),
                    Paragraph('Total Needed', table_header_style),
                    Paragraph('Yield', table_header_style),
                    Paragraph('Batches', table_header_style),
                ]]
                for row in station_group['subrecipes']:
                    subrecipe_rows.append(
                        [
                            p(row.get('recipe_name') or 'Sub-recipe'),
                            p(fmt_qty(row.get('display_required_qty'), row.get('display_required_unit'))),
                            p(fmt_qty(row.get('yield_qty'), row.get('yield_unit'))),
                            p(row.get('display_required_batches') or '—'),
                        ]
                    )
                subrecipe_table = Table(subrecipe_rows, colWidths=[3.0 * inch, 1.5 * inch, 1.3 * inch, 1.0 * inch], repeatRows=1)
                subrecipe_table.setStyle(
                    TableStyle(
                        [
                            ('BACKGROUND', (0, 0), (-1, 0), header_navy),
                            ('TEXTCOLOR', (0, 0), (-1, 0), white),
                            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [white, light_gray]),
                            ('GRID', (0, 0), (-1, -1), 0.35, border_gray),
                            ('LEFTPADDING', (0, 0), (-1, -1), 5),
                            ('RIGHTPADDING', (0, 0), (-1, -1), 5),
                            ('TOPPADDING', (0, 0), (-1, -1), 4),
                            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                        ]
                    )
                )
                story.append(subrecipe_table)
                story.append(Spacer(1, 0.06 * inch))

            if station_group.get('ingredients'):
                ingredient_rows = [[
                    Paragraph('Ingredient', table_header_style),
                    Paragraph('Total Needed', table_header_style),
                ]]
                for row in station_group['ingredients']:
                    ingredient_rows.append(
                        [
                            p(row.get('ingredient_name') or 'Ingredient'),
                            p(fmt_qty(row.get('display_total_qty'), row.get('display_total_unit'))),
                        ]
                    )
                ingredient_table = Table(ingredient_rows, colWidths=[4.9 * inch, 2.4 * inch], repeatRows=1)
                ingredient_table.setStyle(
                    TableStyle(
                        [
                            ('BACKGROUND', (0, 0), (-1, 0), header_navy),
                            ('TEXTCOLOR', (0, 0), (-1, 0), white),
                            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [white, light_gray]),
                            ('GRID', (0, 0), (-1, -1), 0.35, border_gray),
                            ('LEFTPADDING', (0, 0), (-1, -1), 5),
                            ('RIGHTPADDING', (0, 0), (-1, -1), 5),
                            ('TOPPADDING', (0, 0), (-1, -1), 4),
                            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                        ]
                    )
                )
                story.append(ingredient_table)
        has_previous_sections = True

    if prep_data['no_recipe_lines']:
        if has_previous_sections:
            story.append(PageBreak())
        story.append(Paragraph('Items Without Recipe Links', section_title_style))
        story.append(Paragraph(
            'The following dishes are missing both recipe links and direct ingredients, so they were not included in the prep calculations.',
            note_style,
        ))
        story.append(Spacer(1, 0.08 * inch))
        missing_rows = [[Paragraph('Dish Name', table_header_style)]]
        for dish_name in prep_data['no_recipe_lines']:
            missing_rows.append([p(dish_name)])
        missing_table = Table(missing_rows, colWidths=[7.3 * inch], repeatRows=1)
        missing_table.setStyle(
            TableStyle(
                [
                    ('BACKGROUND', (0, 0), (0, 0), colors.HexColor('#FFF8EE')),
                    ('TEXTCOLOR', (0, 0), (0, 0), colors.HexColor('#A0590A')),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [white, light_gray]),
                    ('GRID', (0, 0), (-1, -1), 0.35, border_gray),
                    ('LEFTPADDING', (0, 0), (-1, -1), 5),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 5),
                    ('TOPPADDING', (0, 0), (-1, -1), 4),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ]
            )
        )
        story.append(missing_table)

    doc.build(story, onFirstPage=draw_first_page, onLaterPages=draw_later_pages, canvasmaker=NumberedCanvas)
    payload = buffer.getvalue()
    buffer.close()

    event_name_safe = make_safe_filename(event.get('name') or 'buffet_event')
    filename = f"{event_name_safe}_{event.get('event_date')}_buffet_prep.pdf"
    return Response(
        payload,
        mimetype='application/pdf',
        headers={'Content-Disposition': f'attachment; filename={filename}'},
    )


@bp.route('/buffet-planner/events/<event_id>/order')
@login_required
def buffet_event_order(event_id):
    conn = get_db()
    with get_cursor() as cur:
        if ensure_buffet_schema(cur):
            conn.commit()
        if not buffet_tables_ready(cur):
            flash('Buffet tables are missing. Run migrations first.', 'error')
            return redirect(url_for('buffet_planner'))
        if not db_table_exists(cur, 'public.buffet_order_items'):
            flash('Buffet order table is missing. Run migrations first.', 'error')
            return redirect(url_for('buffet_event_edit', event_id=event_id))

        event = get_buffet_event(cur, event_id)
        if not event:
            flash('Event not found.', 'error')
            return redirect(url_for('buffet_planner'))

        guest_total = get_guest_total(event)
        order_items = build_buffet_order_rollup(cur, event_id, get_unit_system())
        total_ext_cost = sum((to_float(item.get('cases_needed')) * to_float(item.get('pack_cost'))) for item in order_items if item.get('pack_cost'))

    return render_template(
        'buffet_order.html',
        event=event,
        guest_total=guest_total,
        order_items=order_items,
        total_ext_cost=total_ext_cost,
    )


@bp.route('/buffet-planner/events/<event_id>/order/save', methods=['POST'])
@login_required
def buffet_order_save(event_id):
    conn = get_db()
    with get_cursor() as cur:
        if ensure_buffet_schema(cur):
            conn.commit()
        if not buffet_tables_ready(cur):
            flash('Buffet tables are missing. Run migrations first.', 'error')
            return redirect(url_for('buffet_planner'))
        if not db_table_exists(cur, 'public.buffet_order_items'):
            flash('Buffet order table is missing. Run migrations first.', 'error')
            return redirect(url_for('buffet_event_edit', event_id=event_id))

        event = get_buffet_event(cur, event_id)
        if not event:
            flash('Event not found.', 'error')
            return redirect(url_for('buffet_planner'))

        indexed_rows = defaultdict(dict)
        for key in request.form.keys():
            match = LINE_FIELD_PATTERN.match(key.replace('items[', 'lines['))
            if not match:
                continue
            indexed_rows[int(match.group(1))][match.group(2)] = request.form.get(key)

        order_items = build_buffet_order_rollup(cur, event_id, get_unit_system())
        order_map = {(item.get('ingredient_id'), item.get('total_unit')): item for item in order_items}

        try:
            cur.execute("DELETE FROM buffet_order_items WHERE event_id = %s", (event_id,))
            for row_index in sorted(indexed_rows):
                raw = indexed_rows[row_index]
                ingredient_id = clean_menu_text(raw.get('ingredient_id')) or None
                ingredient_name = clean_menu_text(raw.get('ingredient_name')) or 'Unknown'
                total_unit = clean_menu_text(raw.get('total_unit')) or ''
                rollup_item = order_map.get((ingredient_id, total_unit), {})
                total_qty = rollup_item.get('total_qty')
                pack_size = parse_float_field(raw.get('pack_size'), 'Pack size', [], required=False, min_value=0)
                pack_unit = clean_menu_text(raw.get('pack_unit')) or None
                pack_type = clean_menu_text(raw.get('pack_type') or 'oz').lower() or 'oz'
                vendor = clean_menu_text(raw.get('vendor')) or None
                notes = clean_menu_text(raw.get('notes')) or None
                cases_needed = _cases_needed_for_item(total_qty, total_unit, pack_size, pack_unit or total_unit, pack_type)

                cur.execute(
                    """
                    INSERT INTO buffet_order_items (
                        event_id,
                        ingredient_id,
                        ingredient_name,
                        total_qty,
                        total_unit,
                        pack_size,
                        pack_unit,
                        pack_type,
                        cases_needed,
                        vendor,
                        notes,
                        updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                    """,
                    (
                        event_id,
                        ingredient_id,
                        ingredient_name,
                        total_qty,
                        total_unit or None,
                        pack_size,
                        pack_unit,
                        pack_type,
                        cases_needed,
                        vendor,
                        notes,
                    ),
                )

            conn.commit()
            flash('Buffet order overrides saved.', 'success')
        except Exception:
            traceback.print_exc()
            conn.rollback()
            flash('Error saving buffet order overrides.', 'error')

    return redirect(url_for('buffet_event_order', event_id=event_id))


@bp.route('/buffet-planner/events/<event_id>/order/pdf')
@login_required
def buffet_order_pdf(event_id):
    conn = get_db()
    with get_cursor() as cur:
        if ensure_buffet_schema(cur):
            conn.commit()
        if not buffet_tables_ready(cur):
            flash('Buffet tables are missing. Run migrations first.', 'error')
            return redirect(url_for('buffet_planner'))
        if not db_table_exists(cur, 'public.buffet_order_items'):
            flash('Buffet order table is missing. Run migrations first.', 'error')
            return redirect(url_for('buffet_event_edit', event_id=event_id))

        event = get_buffet_event(cur, event_id)
        if not event:
            flash('Event not found.', 'error')
            return redirect(url_for('buffet_planner'))

        guest_total = get_guest_total(event)
        order_items = build_buffet_order_rollup(cur, event_id, get_unit_system())

    header_navy = colors.HexColor('#1B2A4A')
    border_gray = colors.HexColor('#DDDDDD')
    light_gray = colors.HexColor('#F5F5F5')
    muted_text = colors.HexColor('#4F5B6E')
    white = colors.white
    margin_x = 0.5 * inch
    margin_y = 0.5 * inch
    generated_at = datetime.now().strftime('%b %d, %Y %I:%M %p')

    base_style = ParagraphStyle('buffet-order-pdf-base', fontName='Helvetica', fontSize=8.3, leading=10, textColor=colors.black)
    table_header_style = ParagraphStyle('buffet-order-pdf-head', parent=base_style, fontName='Helvetica-Bold', textColor=white, fontSize=8)
    section_title_style = ParagraphStyle('buffet-order-pdf-section', parent=base_style, fontName='Helvetica-Bold', textColor=header_navy, fontSize=11, leading=13)

    def html_text(value, default='-'):
        text = str(value or '').strip() or default
        return escape(text).replace('\n', '<br/>')

    def p(value, style=base_style, default='-'):
        return Paragraph(html_text(value, default), style)

    def draw_header(pdf_canvas, doc):
        page_width, page_height = letter
        header_text = f"Order Calculator - {event.get('name') or 'Event'} | {event.get('event_date')} | {guest_total} guests"
        pdf_canvas.saveState()
        pdf_canvas.setStrokeColor(border_gray)
        pdf_canvas.setLineWidth(0.6)
        pdf_canvas.line(doc.leftMargin, page_height - 42, page_width - doc.rightMargin, page_height - 42)
        pdf_canvas.setFillColor(header_navy)
        pdf_canvas.setFont('Helvetica-Bold', 11)
        pdf_canvas.drawString(doc.leftMargin, page_height - 32, header_text[:140])
        pdf_canvas.restoreState()

    def draw_footer(pdf_canvas, page_num, total_pages):
        page_width, _ = letter
        pdf_canvas.setStrokeColor(border_gray)
        pdf_canvas.setLineWidth(0.6)
        pdf_canvas.line(margin_x, margin_y + 12, page_width - margin_x, margin_y + 12)
        pdf_canvas.setFont('Helvetica', 8)
        pdf_canvas.setFillColor(muted_text)
        pdf_canvas.drawString(margin_x, margin_y + 2, f'Foxtown HQ | Page {page_num} of {total_pages} | Generated {generated_at}')

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
        draw_header(pdf_canvas, doc)

    def draw_later_pages(pdf_canvas, doc):
        draw_header(pdf_canvas, doc)

    vendor_groups = defaultdict(list)
    for item in order_items:
        vendor_groups[item.get('vendor') or 'Unassigned Vendor'].append(item)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=margin_x,
        rightMargin=margin_x,
        topMargin=0.7 * inch,
        bottomMargin=0.55 * inch,
        title=f"Buffet Order {event.get('name') or 'Event'}",
    )

    story = [Spacer(1, 0.15 * inch)]
    col_widths = [0.25 * inch, 2.2 * inch, 1.0 * inch, 0.8 * inch, 0.6 * inch, 0.8 * inch, 1.1 * inch, 1.0 * inch]

    for vendor_index, vendor in enumerate(sorted(vendor_groups)):
        if vendor_index > 0:
            story.append(Spacer(1, 0.12 * inch))
        story.append(Paragraph(escape(vendor), section_title_style))
        table_rows = [[
            Paragraph('&#10003;', table_header_style),
            Paragraph('Ingredient', table_header_style),
            Paragraph('Total Needed', table_header_style),
            Paragraph('Pack Size', table_header_style),
            Paragraph('Pack Type', table_header_style),
            Paragraph('Cases Needed', table_header_style),
            Paragraph('Vendor', table_header_style),
            Paragraph('Notes', table_header_style),
        ]]
        for item in vendor_groups[vendor]:
            pack_size_text = format_number(item.get('pack_size')) if item.get('pack_size') else '-'
            if item.get('pack_unit'):
                pack_size_text = f"{pack_size_text} {item.get('pack_unit')}".strip()
            cases_needed = format_number(item.get('cases_needed')) if item.get('cases_needed') is not None else '—'
            table_rows.append([
                p(' ', default=' '),
                p(item.get('ingredient_name')),
                p(f"{item.get('display_total_qty')} {item.get('display_total_unit')}".strip()),
                p(pack_size_text),
                p(item.get('pack_type') or 'oz'),
                p(cases_needed),
                p(item.get('vendor') or 'Unassigned Vendor'),
                p(item.get('notes') or ''),
            ])
        order_table = Table(table_rows, colWidths=col_widths, repeatRows=1)
        order_table.setStyle(
            TableStyle(
                [
                    ('BACKGROUND', (0, 0), (-1, 0), header_navy),
                    ('TEXTCOLOR', (0, 0), (-1, 0), white),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [white, light_gray]),
                    ('GRID', (0, 0), (-1, -1), 0.35, border_gray),
                    ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                    ('LEFTPADDING', (0, 0), (-1, -1), 4),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 4),
                    ('TOPPADDING', (0, 0), (-1, -1), 4),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                    ('ALIGN', (0, 1), (0, -1), 'CENTER'),
                    ('ALIGN', (4, 1), (5, -1), 'CENTER'),
                ]
            )
        )
        story.append(order_table)

    if not order_items:
        story.append(Paragraph('No recipe-linked ingredient rollup is available for this event yet.', base_style))

    doc.build(story, onFirstPage=draw_first_page, onLaterPages=draw_later_pages, canvasmaker=NumberedCanvas)
    payload = buffer.getvalue()
    buffer.close()

    event_name_safe = make_safe_filename(event.get('name') or 'buffet_event')
    filename = f"{event_name_safe}_{event.get('event_date')}_buffet_order.pdf"
    return Response(
        payload,
        mimetype='application/pdf',
        headers={'Content-Disposition': f'attachment; filename={filename}'},
    )


@bp.route('/buffet-planner/events/<event_id>/delete', methods=['POST'])
@login_required
def buffet_event_delete(event_id):
    conn = get_db()
    with get_cursor() as cur:
        if ensure_buffet_schema(cur):
            conn.commit()
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
        if ensure_buffet_schema(cur):
            conn.commit()
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
