from collections import defaultdict
from datetime import date, datetime, timedelta

from helpers.banquet import derive_prep_family_label
from helpers.db_helpers import db_table_exists
from helpers.formatting import collect_ingredient_usage_from_components, flatten_components_for_pdf, normalize_match_key, split_instruction_steps
from helpers.menu import clean_menu_text
from helpers.recipes import (
    build_component_tree,
    collect_batch_recipe_usage_from_components,
    collect_direct_ingredients_for_prep,
    collect_direct_subrecipes_for_prep,
    collect_ingredients_from_components,
    get_recipe_by_id,
    get_recipe_total_cost,
    normalize_recipe_type,
    ratio_from_line_quantity,
)
from helpers.shared import parse_float_field, to_float
from helpers.units import convert_cost_per_unit, convert_count_units, convert_quantity_between_units, normalize_count_unit, normalize_unit, smart_quantity
from helpers.venues import get_active_venues

def get_commissary_week_window(week_start_raw=''):
    today = date.today()
    start_date = today
    text = (week_start_raw or '').strip()
    if text:
        try:
            start_date = datetime.strptime(text, '%Y-%m-%d').date()
        except ValueError:
            start_date = today
    week_start = start_date - timedelta(days=start_date.weekday())
    week_end = week_start + timedelta(days=6)
    return week_start, week_end

COMMISSARY_ACTIVE_STATUSES = ('draft', 'submitted', 'confirmed', 'in_production')

COMMISSARY_STATUS_CHOICES = [
    ('draft', 'Draft'),
    ('submitted', 'Submitted'),
    ('confirmed', 'Confirmed'),
    ('in_production', 'In Production'),
    ('completed', 'Completed'),
    ('cancelled', 'Cancelled'),
]

COMMISSARY_SOURCE_CHOICES = [
    ('outlet_request', 'Outlet Request'),
    ('standing_prep', 'Standing Prep'),
    ('chef_add', 'Chef Add'),
]

DEFAULT_COMMISSARY_OUTLET = 'Foxtown Brewing'

def commissary_tables_ready(cur):
    required = (
        'public.commissary_orders',
        'public.commissary_order_lines',
    )
    return all(db_table_exists(cur, table_name) for table_name in required)


def normalize_commissary_status(status):
    raw = (status or '').strip().lower()
    if raw == 'pending':
        return 'submitted'
    valid_statuses = {choice[0] for choice in COMMISSARY_STATUS_CHOICES}
    return raw if raw in valid_statuses else 'draft'


def normalize_commissary_source(source):
    raw = (source or '').strip().lower()
    valid_sources = {choice[0] for choice in COMMISSARY_SOURCE_CHOICES}
    return raw if raw in valid_sources else 'outlet_request'


def resolve_commissary_rollup_quantity(quantity, quantity_unit, production_log=None):
    requested_qty = to_float(quantity)
    requested_unit = quantity_unit or 'each'
    log = production_log or {}
    actual_qty = to_float(log.get('actual_yield'))
    actual_unit = clean_menu_text(log.get('actual_yield_unit')) or requested_unit

    if log.get('signed_off'):
        if actual_qty > 0:
            return actual_qty, actual_unit, 'actual'
        return requested_qty, requested_unit, 'planned'

    if log.get('pending_rollup'):
        if actual_qty > 0:
            return actual_qty, actual_unit, 'actual'
        return 0, actual_unit, 'pending'

    return requested_qty, requested_unit, 'planned'


def resolve_commissary_remaining_quantity(quantity, quantity_unit, production_log=None):
    planned_qty = to_float(quantity)
    planned_unit = quantity_unit or 'each'
    log = production_log or {}
    actual_qty = to_float(log.get('actual_yield'))
    actual_unit = clean_menu_text(log.get('actual_yield_unit')) or planned_unit
    if actual_qty <= 0:
        return planned_qty, planned_unit

    converted_actual = None
    if actual_unit == planned_unit:
        converted_actual = actual_qty
    else:
        converted_actual = convert_quantity_between_units(actual_qty, actual_unit, planned_unit)
        if converted_actual is None:
            converted_actual = convert_count_units(actual_qty, actual_unit, planned_unit)

    if converted_actual is None:
        return planned_qty, planned_unit

    return max(planned_qty - converted_actual, 0), planned_unit

def ensure_commissary_tables(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS commissary_orders (
            id TEXT PRIMARY KEY,
            outlet TEXT NOT NULL,
            needed_date DATE NOT NULL,
            status TEXT DEFAULT 'draft',
            source TEXT DEFAULT 'outlet_request',
            notes TEXT,
            created_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS commissary_order_lines (
            id BIGSERIAL PRIMARY KEY,
            order_id TEXT NOT NULL REFERENCES commissary_orders(id) ON DELETE CASCADE,
            recipe_id TEXT REFERENCES recipes(id) ON DELETE SET NULL,
            item_name TEXT,
            quantity NUMERIC NOT NULL DEFAULT 1,
            quantity_unit TEXT DEFAULT 'each',
            prep_start_date DATE,
            prep_end_date DATE,
            sort_order INTEGER DEFAULT 0,
            notes TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS commissary_production_logs (
            id BIGSERIAL PRIMARY KEY,
            line_id BIGINT REFERENCES commissary_order_lines(id) ON DELETE CASCADE,
            production_date DATE NOT NULL,
            assigned_to TEXT,
            made_by TEXT,
            signed_off BOOLEAN NOT NULL DEFAULT FALSE,
            signed_off_by TEXT,
            tasted_by TEXT,
            production_notes TEXT,
            signed_off_at TIMESTAMP,
            actual_yield NUMERIC,
            actual_yield_unit TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (line_id, production_date)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS commissary_standing_items (
            id BIGSERIAL PRIMARY KEY,
            recipe_id TEXT REFERENCES recipes(id) ON DELETE SET NULL,
            item_name TEXT,
            default_quantity NUMERIC NOT NULL DEFAULT 1,
            default_unit TEXT DEFAULT 'each',
            frequency TEXT NOT NULL DEFAULT 'daily',
            active BOOLEAN NOT NULL DEFAULT TRUE,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS commissary_transfers (
            id BIGSERIAL PRIMARY KEY,
            production_date DATE NOT NULL,
            from_location TEXT NOT NULL DEFAULT 'Commissary',
            to_outlet TEXT NOT NULL,
            transferred_by TEXT,
            transfer_method TEXT NOT NULL DEFAULT 'delivery',
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS commissary_transfer_lines (
            id BIGSERIAL PRIMARY KEY,
            transfer_id BIGINT NOT NULL REFERENCES commissary_transfers(id) ON DELETE CASCADE,
            production_log_id BIGINT REFERENCES commissary_production_logs(id) ON DELETE SET NULL,
            item_name TEXT NOT NULL,
            quantity NUMERIC NOT NULL DEFAULT 0,
            quantity_unit TEXT,
            notes TEXT
        )
    """)

    cur.execute("ALTER TABLE commissary_orders ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'draft'")
    cur.execute("ALTER TABLE commissary_orders ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'outlet_request'")
    cur.execute("ALTER TABLE commissary_orders ADD COLUMN IF NOT EXISTS notes TEXT")
    cur.execute("ALTER TABLE commissary_orders ADD COLUMN IF NOT EXISTS created_by TEXT")
    cur.execute("ALTER TABLE commissary_orders ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    cur.execute("ALTER TABLE commissary_orders ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")

    cur.execute("ALTER TABLE commissary_order_lines ADD COLUMN IF NOT EXISTS item_name TEXT")
    cur.execute("ALTER TABLE commissary_order_lines ADD COLUMN IF NOT EXISTS quantity_unit TEXT DEFAULT 'each'")
    cur.execute("ALTER TABLE commissary_order_lines ADD COLUMN IF NOT EXISTS prep_start_date DATE")
    cur.execute("ALTER TABLE commissary_order_lines ADD COLUMN IF NOT EXISTS prep_end_date DATE")
    cur.execute("ALTER TABLE commissary_order_lines ADD COLUMN IF NOT EXISTS notes TEXT")
    cur.execute("ALTER TABLE commissary_order_lines ADD COLUMN IF NOT EXISTS sort_order INTEGER DEFAULT 0")
    cur.execute("ALTER TABLE commissary_order_lines ALTER COLUMN quantity SET DEFAULT 1")
    cur.execute("""
        UPDATE commissary_order_lines
        SET quantity_unit = 'each'
        WHERE quantity_unit IS NULL OR TRIM(quantity_unit) = ''
    """)
    cur.execute("""
        UPDATE commissary_order_lines line
        SET prep_start_date = o.needed_date
        FROM commissary_orders o
        WHERE line.order_id = o.id
          AND line.prep_start_date IS NULL
    """)
    cur.execute("""
        UPDATE commissary_order_lines line
        SET prep_end_date = COALESCE(line.prep_start_date, o.needed_date)
        FROM commissary_orders o
        WHERE line.order_id = o.id
          AND line.prep_end_date IS NULL
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_commissary_orders_needed_date ON commissary_orders (needed_date)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_commissary_orders_status ON commissary_orders (status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_commissary_orders_source ON commissary_orders (source)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_commissary_order_lines_order_id ON commissary_order_lines (order_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_commissary_order_lines_recipe_id ON commissary_order_lines (recipe_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_commissary_order_lines_prep_start ON commissary_order_lines (prep_start_date)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_commissary_order_lines_prep_end ON commissary_order_lines (prep_end_date)")

    cur.execute("ALTER TABLE commissary_production_logs ADD COLUMN IF NOT EXISTS order_id TEXT")
    cur.execute("ALTER TABLE commissary_production_logs ADD COLUMN IF NOT EXISTS order_item_id BIGINT")
    cur.execute("ALTER TABLE commissary_production_logs ADD COLUMN IF NOT EXISTS line_id BIGINT")
    cur.execute("ALTER TABLE commissary_production_logs ADD COLUMN IF NOT EXISTS production_date DATE")
    cur.execute("ALTER TABLE commissary_production_logs ADD COLUMN IF NOT EXISTS assigned_to TEXT")
    cur.execute("ALTER TABLE commissary_production_logs ADD COLUMN IF NOT EXISTS made_by TEXT")
    cur.execute("ALTER TABLE commissary_production_logs ADD COLUMN IF NOT EXISTS signed_off BOOLEAN NOT NULL DEFAULT FALSE")
    cur.execute("ALTER TABLE commissary_production_logs ADD COLUMN IF NOT EXISTS signed_off_by TEXT")
    cur.execute("ALTER TABLE commissary_production_logs ADD COLUMN IF NOT EXISTS tasted_by TEXT")
    cur.execute("ALTER TABLE commissary_production_logs ADD COLUMN IF NOT EXISTS notes TEXT")
    cur.execute("ALTER TABLE commissary_production_logs ADD COLUMN IF NOT EXISTS production_notes TEXT")
    cur.execute("ALTER TABLE commissary_production_logs ADD COLUMN IF NOT EXISTS signed_off_at TIMESTAMP")
    cur.execute("ALTER TABLE commissary_production_logs ADD COLUMN IF NOT EXISTS actual_yield NUMERIC")
    cur.execute("ALTER TABLE commissary_production_logs ADD COLUMN IF NOT EXISTS actual_yield_unit TEXT")
    cur.execute("ALTER TABLE commissary_production_logs ADD COLUMN IF NOT EXISTS pending_rollup BOOLEAN NOT NULL DEFAULT FALSE")
    cur.execute("ALTER TABLE commissary_production_logs ADD COLUMN IF NOT EXISTS cancelled_line BOOLEAN NOT NULL DEFAULT FALSE")
    cur.execute("ALTER TABLE commissary_production_logs ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    cur.execute("ALTER TABLE commissary_production_logs ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    cur.execute("ALTER TABLE commissary_production_logs ALTER COLUMN order_id DROP NOT NULL")
    cur.execute("ALTER TABLE commissary_production_logs ALTER COLUMN order_item_id DROP NOT NULL")
    cur.execute("""
        UPDATE commissary_production_logs
        SET signed_off = FALSE
        WHERE signed_off IS NULL
    """)
    cur.execute("""
        UPDATE commissary_production_logs
        SET line_id = order_item_id
        WHERE line_id IS NULL AND order_item_id IS NOT NULL
    """)
    cur.execute("""
        UPDATE commissary_production_logs
        SET production_notes = notes
        WHERE COALESCE(TRIM(production_notes), '') = '' AND COALESCE(TRIM(notes), '') <> ''
    """)
    cur.execute("""
        UPDATE commissary_production_logs
        SET pending_rollup = FALSE
        WHERE pending_rollup IS NULL
    """)
    cur.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    PARTITION BY line_id, production_date
                    ORDER BY COALESCE(updated_at, created_at, CURRENT_TIMESTAMP) DESC, id DESC
                ) AS rn
            FROM commissary_production_logs
            WHERE line_id IS NOT NULL
              AND production_date IS NOT NULL
        )
        DELETE FROM commissary_production_logs
        WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
    """
    )
    # Keep legacy index name untouched (it may point at order_item_id on older installs).
    # Create a distinct index name for (line_id, production_date), which ON CONFLICT expects.
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_comm_prod_logs_line_id_prod_date "
        "ON commissary_production_logs (line_id, production_date)"
    )
    cur.execute(
        """
        SELECT 1
        FROM pg_indexes
        WHERE schemaname = CURRENT_SCHEMA()
          AND indexname = 'idx_comm_prod_logs_line_id_prod_date'
        LIMIT 1
    """
    )
    if not cur.fetchone():
        cur.execute(
            """
            DELETE FROM commissary_production_logs a
            USING commissary_production_logs b
            WHERE a.line_id = b.line_id
              AND a.production_date = b.production_date
              AND a.id < b.id
        """
        )
        cur.execute(
            "CREATE UNIQUE INDEX idx_comm_prod_logs_line_id_prod_date "
            "ON commissary_production_logs (line_id, production_date)"
        )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_comm_prod_logs_order_id ON commissary_production_logs (order_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_comm_prod_logs_production_date ON commissary_production_logs (production_date)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_comm_prod_logs_assigned_to ON commissary_production_logs (assigned_to)")
    cur.execute(
        "ALTER TABLE commissary_transfer_lines "
        "ADD COLUMN IF NOT EXISTS production_log_id BIGINT REFERENCES commissary_production_logs(id) ON DELETE SET NULL"
    )

    cur.execute("CREATE INDEX IF NOT EXISTS idx_comm_standing_items_recipe_id ON commissary_standing_items (recipe_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_comm_standing_items_active ON commissary_standing_items (active)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_comm_transfers_production_date ON commissary_transfers (production_date)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_comm_transfer_lines_transfer_id ON commissary_transfer_lines (transfer_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_comm_transfer_lines_production_log_id ON commissary_transfer_lines (production_log_id)")

    legacy_orders_ready = db_table_exists(cur, 'public.outlet_orders')
    legacy_lines_ready = db_table_exists(cur, 'public.outlet_order_items')
    if legacy_orders_ready and legacy_lines_ready:
        cur.execute("SELECT COUNT(*) AS count FROM commissary_orders")
        needs_bootstrap = int((cur.fetchone() or {}).get('count') or 0) == 0
        if needs_bootstrap:
            cur.execute("""
                INSERT INTO commissary_orders (
                    id, outlet, needed_date, status, source, notes, created_at, updated_at
                )
                SELECT
                    o.id,
                    o.outlet,
                    o.needed_date,
                    CASE
                        WHEN LOWER(COALESCE(NULLIF(TRIM(o.status), ''), 'pending')) = 'pending' THEN 'submitted'
                        ELSE LOWER(COALESCE(NULLIF(TRIM(o.status), ''), 'draft'))
                    END AS status,
                    'outlet_request' AS source,
                    o.notes,
                    o.created_at,
                    o.updated_at
                FROM outlet_orders o
                ON CONFLICT (id) DO NOTHING
            """)
            cur.execute("""
                INSERT INTO commissary_order_lines (
                    id, order_id, recipe_id, item_name, quantity, quantity_unit, notes, sort_order
                )
                SELECT
                    oi.id,
                    oi.order_id,
                    oi.recipe_id,
                    oi.item_name,
                    oi.quantity,
                    COALESCE(NULLIF(TRIM(oi.quantity_unit), ''), 'each'),
                    oi.notes,
                    COALESCE(oi.sort_order, 0)
                FROM outlet_order_items oi
                JOIN commissary_orders co ON co.id = oi.order_id
                ON CONFLICT (id) DO NOTHING
            """)
            cur.execute("""
                SELECT setval(
                    pg_get_serial_sequence('commissary_order_lines', 'id'),
                    COALESCE((SELECT MAX(id) FROM commissary_order_lines), 1),
                    (SELECT COUNT(*) > 0 FROM commissary_order_lines)
                )
            """)

def get_commissary_outlet_options(cur):
    options = {DEFAULT_COMMISSARY_OUTLET}
    for venue in get_active_venues(cur):
        venue_name = clean_menu_text(venue.get('name'))
        if venue_name:
            options.add(venue_name)
    if db_table_exists(cur, 'public.commissary_orders'):
        cur.execute("""
            SELECT DISTINCT outlet
            FROM commissary_orders
            WHERE outlet IS NOT NULL AND TRIM(outlet) <> ''
            ORDER BY outlet
        """)
        for row in cur.fetchall():
            outlet_name = clean_menu_text(row.get('outlet'))
            if outlet_name:
                options.add(outlet_name)
    return sorted(options, key=lambda value: value.lower())

def get_commissary_order(cur, order_id):
    if not commissary_tables_ready(cur):
        return None
    cur.execute("""
        SELECT id,
               outlet,
               needed_date,
               status,
               source,
               notes,
               created_by,
               created_at,
               updated_at
        FROM commissary_orders
        WHERE id = %s
        LIMIT 1
    """, (order_id,))
    return cur.fetchone()

def get_commissary_order_lines(cur, order_id):
    if not commissary_tables_ready(cur):
        return []
    cur.execute("""
        SELECT line.id AS line_id,
               line.order_id,
               line.recipe_id,
               line.item_name,
               line.quantity,
               line.quantity_unit,
               line.prep_start_date,
               line.prep_end_date,
               line.notes AS line_notes,
               line.sort_order,
               o.needed_date,
               pl.assigned_to,
               pl.made_by AS production_made_by,
               pl.signed_off AS production_signed_off,
               pl.signed_off_by AS production_signed_off_by,
               pl.tasted_by AS production_tasted_by,
               COALESCE(pl.production_notes, pl.notes) AS production_notes,
               r.name AS recipe_name,
               r.yield_qty,
               r.yield_unit,
               r.recipe_type
        FROM commissary_order_lines line
        JOIN commissary_orders o ON o.id = line.order_id
        LEFT JOIN commissary_production_logs pl
               ON pl.line_id = line.id
              AND pl.production_date = o.needed_date
        LEFT JOIN recipes r ON r.id = line.recipe_id
        WHERE line.order_id = %s
        ORDER BY COALESCE(line.sort_order, 0), line.id
    """, (order_id,))
    return cur.fetchall()

def parse_commissary_order_lines(request, valid_recipe_ids):
    line_ids = request.form.getlist('line_id[]')
    recipe_ids = request.form.getlist('line_recipe_id[]')
    item_names = request.form.getlist('line_item_name[]')
    quantities = request.form.getlist('line_qty[]')
    quantity_units = request.form.getlist('line_unit[]')
    prep_starts = request.form.getlist('line_prep_start[]')
    prep_ends = request.form.getlist('line_prep_end[]')
    notes = request.form.getlist('line_notes[]')

    max_len = max(
        len(line_ids),
        len(recipe_ids),
        len(item_names),
        len(quantities),
        len(quantity_units),
        len(prep_starts),
        len(prep_ends),
        len(notes),
        0
    )
    rows = []
    errors = []
    for idx in range(max_len):
        line_id = (line_ids[idx] if idx < len(line_ids) else '').strip()
        recipe_id = (recipe_ids[idx] if idx < len(recipe_ids) else '').strip()
        item_name = clean_menu_text(item_names[idx] if idx < len(item_names) else '')
        qty_raw = (quantities[idx] if idx < len(quantities) else '').strip()
        qty_unit_raw = clean_menu_text(quantity_units[idx] if idx < len(quantity_units) else '')
        prep_start_raw = (prep_starts[idx] if idx < len(prep_starts) else '').strip()
        prep_end_raw = (prep_ends[idx] if idx < len(prep_ends) else '').strip()
        note = clean_menu_text(notes[idx] if idx < len(notes) else '')

        if not any([recipe_id, item_name, qty_raw, qty_unit_raw, prep_start_raw, prep_end_raw, note]):
            continue
        if recipe_id and recipe_id not in valid_recipe_ids:
            errors.append('One or more commissary line items reference an invalid recipe.')
            continue
        if not recipe_id and not item_name:
            errors.append('Each commissary line needs a recipe or item name.')
        qty = parse_float_field(qty_raw, 'Order quantity', errors, required=True, min_value=0.0001)
        if qty is None:
            continue
        prep_start_date = None
        prep_end_date = None
        if prep_start_raw:
            try:
                prep_start_date = datetime.strptime(prep_start_raw, '%Y-%m-%d').date()
            except ValueError:
                errors.append('Prep start date must be a valid date (YYYY-MM-DD).')
                continue
        if prep_end_raw:
            try:
                prep_end_date = datetime.strptime(prep_end_raw, '%Y-%m-%d').date()
            except ValueError:
                errors.append('Prep end date must be a valid date (YYYY-MM-DD).')
                continue
        if prep_start_date and not prep_end_date:
            prep_end_date = prep_start_date
        if prep_end_date and not prep_start_date:
            prep_start_date = prep_end_date
        if prep_start_date and prep_end_date and prep_end_date < prep_start_date:
            errors.append('Prep end date cannot be before prep start date.')
            continue
        normalized_unit = normalize_unit(qty_unit_raw) or normalize_count_unit(qty_unit_raw) or qty_unit_raw or None
        rows.append({
            'line_id': line_id or None,
            'recipe_id': recipe_id or None,
            'item_name': item_name or None,
            'quantity': qty,
            'quantity_unit': normalized_unit,
            'prep_start_date': prep_start_date,
            'prep_end_date': prep_end_date,
            'notes': note or None,
        })
    return rows, errors


def fetch_commissary_order_rows(cur, start_date, end_date, outlet=''):
    if not commissary_tables_ready(cur):
        return []
    logs_ready = db_table_exists(cur, 'public.commissary_production_logs')
    params = [start_date, end_date, end_date, start_date]
    outlet_filter_sql = ""
    if outlet:
        outlet_filter_sql = " AND o.outlet = %s"
        params.append(outlet)

    if logs_ready:
        production_select_sql = """
               pl.id AS production_log_id,
               pl.production_date,
               pl.assigned_to AS production_assigned_to,
               pl.made_by AS production_made_by,
               pl.signed_off AS production_signed_off,
               pl.signed_off_by AS production_signed_off_by,
               pl.tasted_by AS production_tasted_by,
               COALESCE(pl.production_notes, pl.notes) AS production_notes,
               pl.actual_yield,
               pl.actual_yield_unit,
               COALESCE(pl.pending_rollup, FALSE) AS production_pending_rollup,
               COALESCE(pl.cancelled_line, FALSE) AS production_cancelled_line,
               pl.updated_at AS production_updated_at,
        """
        production_join_sql = """
        LEFT JOIN commissary_production_logs pl
               ON pl.line_id = line.id
              AND pl.production_date = o.needed_date
        """
    else:
        production_select_sql = """
               NULL::BIGINT AS production_log_id,
               NULL::DATE AS production_date,
               NULL::TEXT AS production_assigned_to,
               NULL::TEXT AS production_made_by,
               NULL::BOOLEAN AS production_signed_off,
               NULL::TEXT AS production_signed_off_by,
               NULL::TEXT AS production_tasted_by,
               NULL::TEXT AS production_notes,
               NULL::NUMERIC AS actual_yield,
               NULL::TEXT AS actual_yield_unit,
               FALSE AS production_pending_rollup,
               FALSE AS production_cancelled_line,
               NULL::TIMESTAMP AS production_updated_at,
        """
        production_join_sql = ""

    cur.execute(f"""
        SELECT o.id AS order_id,
               o.outlet,
               o.needed_date,
               o.status,
               o.source,
               o.notes AS order_notes,
               o.created_by,
               o.created_at,
               o.updated_at,
               line.id AS line_id,
               line.recipe_id,
               line.item_name,
               line.quantity,
               line.quantity_unit,
               line.prep_start_date,
               line.prep_end_date,
               line.notes AS line_notes,
               line.sort_order,
               {production_select_sql}
               r.name AS recipe_name,
               r.category AS recipe_category,
               r.yield_qty,
               r.yield_unit,
               r.instructions,
               r.recipe_type
        FROM commissary_orders o
        LEFT JOIN commissary_order_lines line ON line.order_id = o.id
        {production_join_sql}
        LEFT JOIN recipes r ON r.id = line.recipe_id
        WHERE (
            o.needed_date BETWEEN %s AND %s
            OR (
                COALESCE(line.prep_start_date, o.needed_date) <= %s
                AND COALESCE(line.prep_end_date, line.prep_start_date, o.needed_date) >= %s
            )
        )
          AND COALESCE(NULLIF(TRIM(o.status), ''), 'draft') <> 'cancelled'
          {outlet_filter_sql}
        ORDER BY o.needed_date, o.outlet, o.created_at, COALESCE(line.sort_order, 0), line.id
    """, params)
    return cur.fetchall()

def build_commissary_datasets(cur, start_date, end_date, outlet='', unit_system='imperial'):
    rows = fetch_commissary_order_rows(cur, start_date, end_date, outlet)
    orders_map = {}
    ingredient_totals = {}
    ingredient_usage = defaultdict(set)
    batch_usage_qty = {}
    batch_usage_orders = defaultdict(set)
    batch_usage_items = defaultdict(set)
    batch_usage_assignees = defaultdict(set)
    order_daily_ingredients = defaultdict(lambda: defaultdict(float))

    for row in rows:
        order_id = row.get('order_id')
        if not order_id:
            continue
        if order_id not in orders_map:
            orders_map[order_id] = {
                'id': order_id,
                'outlet': row.get('outlet') or DEFAULT_COMMISSARY_OUTLET,
                'needed_date': row.get('needed_date'),
                'status': normalize_commissary_status(row.get('status')),
                'source': normalize_commissary_source(row.get('source')),
                'notes': row.get('order_notes'),
                'created_by': row.get('created_by'),
                'created_at': row.get('created_at'),
                'updated_at': row.get('updated_at'),
                'lines': [],
                'line_count': 0,
                'logged_line_count': 0,
                'signed_off_line_count': 0
            }

        if not row.get('line_id'):
            continue

        recipe_id = row.get('recipe_id')
        quantity = to_float(row.get('quantity'))
        quantity_unit = row.get('quantity_unit') or row.get('yield_unit') or 'each'
        item_name = clean_menu_text(row.get('item_name')) or row.get('recipe_name') or 'Item'
        production_log = {
            'id': row.get('production_log_id'),
            'date': row.get('production_date'),
            'assigned_to': clean_menu_text(row.get('production_assigned_to')),
            'made_by': clean_menu_text(row.get('production_made_by')),
            'signed_off': bool(row.get('production_signed_off')),
            'signed_off_by': clean_menu_text(row.get('production_signed_off_by')),
            'tasted_by': clean_menu_text(row.get('production_tasted_by')),
            'notes': clean_menu_text(row.get('production_notes')),
            'actual_yield': to_float(row.get('actual_yield')),
            'actual_yield_unit': row.get('actual_yield_unit'),
            'pending_rollup': bool(row.get('production_pending_rollup')),
            'cancelled_line': bool(row.get('production_cancelled_line')),
            'updated_at': row.get('production_updated_at')
        }
        has_production_log = any([
            production_log.get('assigned_to'),
            production_log.get('made_by'),
            production_log.get('signed_off'),
            production_log.get('signed_off_by'),
            production_log.get('tasted_by'),
            production_log.get('notes'),
            production_log.get('actual_yield'),
            production_log.get('pending_rollup'),
            production_log.get('cancelled_line'),
        ])

        rollup_quantity, rollup_quantity_unit, rollup_basis = resolve_commissary_rollup_quantity(
            quantity,
            quantity_unit,
            production_log,
        )

        line = {
            'id': row.get('line_id'),
            'recipe_id': recipe_id,
            'item_name': item_name,
            'recipe_name': row.get('recipe_name'),
            'recipe_type': normalize_recipe_type(row.get('recipe_type')),
            'quantity': quantity,
            'quantity_unit': quantity_unit,
            'rollup_quantity': rollup_quantity,
            'rollup_quantity_unit': rollup_quantity_unit,
            'rollup_basis': rollup_basis,
            'prep_start_date': row.get('prep_start_date') or orders_map[order_id].get('needed_date'),
            'prep_end_date': row.get('prep_end_date') or row.get('prep_start_date') or orders_map[order_id].get('needed_date'),
            'notes': row.get('line_notes'),
            'instructions': row.get('instructions') or '',
            'instruction_steps': split_instruction_steps(row.get('instructions')),
            'source': orders_map[order_id].get('source'),
            'estimated_cost_total': None,
            'estimated_cost_per_unit': None,
            'ratio': None,
            'components': [],
            'production_log': production_log,
            'has_production_log': has_production_log
        }

        if recipe_id:
            recipe = {
                'id': recipe_id,
                'name': row.get('recipe_name') or item_name,
                'yield_qty': row.get('yield_qty'),
                'yield_unit': row.get('yield_unit')
            }
            ratio = ratio_from_line_quantity(
                {
                    'quantity': rollup_quantity,
                    'quantity_unit': rollup_quantity_unit
                },
                recipe
            )
            line['ratio'] = ratio
            line_cost_total = get_recipe_total_cost(cur, recipe_id, unit_system, apply_q_factor=True) * ratio
            line['estimated_cost_total'] = line_cost_total
            line['estimated_cost_per_unit'] = line_cost_total / rollup_quantity if rollup_quantity > 0 else None

            recipe_yield_qty = to_float(recipe.get('yield_qty'))
            required_output_qty = (recipe_yield_qty * ratio) if recipe_yield_qty > 0 else ratio
            required_output_unit = recipe.get('yield_unit') or rollup_quantity_unit
            root_key = (recipe_id, required_output_unit)
            batch_usage_qty[root_key] = batch_usage_qty.get(root_key, 0) + required_output_qty
            batch_usage_orders[recipe_id].add(order_id)
            batch_usage_items[recipe_id].add(item_name)
            if production_log.get('assigned_to'):
                batch_usage_assignees[recipe_id].add(production_log.get('assigned_to'))

            components, _, _ = build_component_tree(cur, recipe_id, ratio, 0, set(), unit_system, apply_q_factor=False)
            line['components'] = components
            collect_ingredients_from_components(components, ingredient_totals)
            collect_ingredients_from_components(components, order_daily_ingredients[order_id])
            collect_batch_recipe_usage_from_components(
                components,
                batch_usage_qty,
                event_usage_map=batch_usage_orders,
                menu_item_usage_map=batch_usage_items,
                event_id=order_id,
                menu_item_name=item_name
            )
            collect_ingredient_usage_from_components(components, item_name, ingredient_usage)

        orders_map[order_id]['lines'].append(line)
        orders_map[order_id]['line_count'] += 1
        if has_production_log:
            orders_map[order_id]['logged_line_count'] += 1
        if production_log.get('signed_off'):
            orders_map[order_id]['signed_off_line_count'] += 1

    orders = sorted(
        orders_map.values(),
        key=lambda item: (
            (item.get('needed_date') or date.today()),
            (item.get('outlet') or '').lower(),
            (item.get('id') or '')
        )
    )
    order_label_map = {
        order.get('id'): f"{order.get('outlet') or DEFAULT_COMMISSARY_OUTLET} ({order.get('needed_date')})"
        for order in orders
    }

    ingredient_master = []
    ingredient_total_cost = 0
    if ingredient_totals:
        ingredient_ids = list({ing_id for ing_id, _ in ingredient_totals.keys() if ing_id})
        ingredient_map = {}
        if ingredient_ids:
            cur.execute("""
                SELECT id, name, unit, category, cost_per_unit, vendor, vendor_code, g_code
                FROM ingredients
                WHERE id = ANY(%s)
            """, (ingredient_ids,))
            ingredient_map = {row['id']: row for row in cur.fetchall()}

        for (ing_id, unit), qty in ingredient_totals.items():
            ingredient = ingredient_map.get(ing_id, {})
            cost_per_unit = convert_cost_per_unit(
                ingredient.get('cost_per_unit'),
                ingredient.get('unit'),
                unit
            )
            ext_cost = qty * cost_per_unit if cost_per_unit else 0
            ingredient_total_cost += ext_cost
            display = smart_quantity(qty, unit, unit_system)
            ingredient_master.append({
                'id': ing_id,
                'name': ingredient.get('name') or 'Unknown',
                'category': ingredient.get('category') or 'Uncategorized',
                'vendor': ingredient.get('vendor'),
                'vendor_code': ingredient.get('vendor_code'),
                'g_code': ingredient.get('g_code'),
                'quantity': qty,
                'unit': unit,
                'display_quantity': display['quantity'],
                'display_unit': display['unit'],
                'cost_per_unit': cost_per_unit,
                'ext_cost': ext_cost,
                'used_in': sorted(ingredient_usage.get(ing_id, set()))
            })
    ingredient_master.sort(key=lambda item: ((item.get('vendor') or '').lower(), (item.get('category') or '').lower(), (item.get('name') or '').lower()))

    weekly_prep = []
    if batch_usage_qty:
        batch_ids = list({recipe_id for recipe_id, _ in batch_usage_qty.keys() if recipe_id})
        recipe_map = {}
        if batch_ids:
            cur.execute("""
                SELECT
                    id,
                    name,
                    category,
                    yield_qty,
                    yield_unit,
                    instructions,
                    recipe_type,
                    menu_descriptor,
                    source_venue,
                    equipment,
                    station,
                    critical_steps,
                    storage_instructions,
                    shelf_life_days,
                    prep_time_minutes
                FROM recipes
                WHERE id = ANY(%s)
            """, (batch_ids,))
            recipe_map = {row['id']: row for row in cur.fetchall()}

        for (recipe_id, qty_unit), required_qty in batch_usage_qty.items():
            recipe = recipe_map.get(recipe_id)
            if not recipe:
                continue
            yield_qty = to_float(recipe.get('yield_qty'))
            yield_unit = recipe.get('yield_unit')
            required_qty_in_yield = required_qty
            if qty_unit and yield_unit and qty_unit != yield_unit:
                converted = convert_quantity_between_units(required_qty, qty_unit, yield_unit)
                if converted is not None:
                    required_qty_in_yield = converted

            required_batches = (required_qty_in_yield / yield_qty) if yield_qty > 0 else required_qty_in_yield
            components, total_cost, _ = build_component_tree(cur, recipe_id, required_batches, 0, set(), unit_system, apply_q_factor=False)
            ingredient_totals_for_batch = {}
            collect_direct_ingredients_for_prep(components, ingredient_totals_for_batch)
            subrecipe_totals_for_batch = {}
            collect_direct_subrecipes_for_prep(components, subrecipe_totals_for_batch)
            ingredient_rows = []
            for (ing_id, unit), qty in ingredient_totals_for_batch.items():
                display = smart_quantity(qty, unit, unit_system)
                ingredient_rows.append({
                    'ingredient_id': ing_id,
                    'name': next((item['name'] for item in ingredient_master if item['id'] == ing_id), 'Unknown'),
                    'quantity': qty,
                    'unit': unit,
                    'display_quantity': display['quantity'],
                    'display_unit': display['unit']
                })
            ingredient_rows.sort(key=lambda item: (item.get('name') or '').lower())

            subrecipe_rows = []
            for (sub_recipe_id, sub_unit), sub_qty in subrecipe_totals_for_batch.items():
                sub_recipe = recipe_map.get(sub_recipe_id)
                if not sub_recipe:
                    sub_recipe = get_recipe_by_id(cur, sub_recipe_id)
                if not sub_recipe:
                    continue
                sub_yield_qty = to_float(sub_recipe.get('yield_qty'))
                sub_yield_unit = sub_recipe.get('yield_unit') or sub_unit
                sub_required_qty_in_yield = sub_qty
                if sub_unit and sub_yield_unit and sub_unit != sub_yield_unit:
                    converted = convert_quantity_between_units(sub_qty, sub_unit, sub_yield_unit)
                    if converted is not None:
                        sub_required_qty_in_yield = converted
                sub_required_batches = (sub_required_qty_in_yield / sub_yield_qty) if sub_yield_qty > 0 else sub_required_qty_in_yield
                display = smart_quantity(sub_qty, sub_unit, unit_system)
                subrecipe_rows.append({
                    'recipe_id': sub_recipe_id,
                    'recipe_name': sub_recipe.get('name') or 'Sub-recipe',
                    'required_qty': sub_qty,
                    'required_unit': sub_unit,
                    'display_required': display,
                    'required_batches': sub_required_batches,
                    'yield_qty': sub_yield_qty,
                    'yield_unit': sub_yield_unit
                })
            subrecipe_rows.sort(key=lambda item: (item.get('recipe_name') or '').lower())

            weekly_prep.append({
                'recipe_id': recipe_id,
                'recipe_name': recipe.get('name'),
                'category': recipe.get('category'),
                'recipe_type': normalize_recipe_type(recipe.get('recipe_type')),
                'menu_descriptor': recipe.get('menu_descriptor'),
                'source_venue': recipe.get('source_venue'),
                'equipment': recipe.get('equipment'),
                'station': recipe.get('station'),
                'required_batches': required_batches,
                'required_qty': required_qty_in_yield,
                'required_unit': yield_unit or qty_unit,
                'display_required': smart_quantity(required_qty_in_yield, yield_unit or qty_unit, unit_system),
                'yield_qty': yield_qty,
                'yield_unit': yield_unit,
                'display_yield': smart_quantity(yield_qty, yield_unit, unit_system),
                'ingredient_rows': ingredient_rows,
                'subrecipe_rows': subrecipe_rows,
                'components': components,
                'component_count': len(flatten_components_for_pdf(components)),
                'instructions': recipe.get('instructions'),
                'instruction_steps': split_instruction_steps(recipe.get('instructions')),
                'critical_steps': split_instruction_steps(recipe.get('critical_steps')),
                'storage_lines': split_instruction_steps(recipe.get('storage_instructions')),
                'storage_instructions': recipe.get('storage_instructions'),
                'shelf_life_days': recipe.get('shelf_life_days'),
                'prep_time_minutes': recipe.get('prep_time_minutes'),
                'used_in_orders': sorted(batch_usage_orders.get(recipe_id, set())),
                'used_in_order_labels': [order_label_map.get(order_id, order_id) for order_id in sorted(batch_usage_orders.get(recipe_id, set()))],
                'used_in_items': sorted(batch_usage_items.get(recipe_id, set())),
                'assigned_cooks': sorted(batch_usage_assignees.get(recipe_id, set())),
                'estimated_cost': total_cost
            })
    for prep in weekly_prep:
        prep_family = derive_prep_family_label(prep.get('recipe_name'))
        prep['prep_family'] = prep_family
        prep['prep_family_key'] = normalize_match_key(prep_family)
    weekly_prep.sort(key=lambda item: (item.get('prep_family_key') or '', (item.get('recipe_name') or '').lower()))

    source_label_map = {value: label for value, label in COMMISSARY_SOURCE_CHOICES}
    source_display_order = ['outlet_request', 'chef_add', 'standing_prep']

    daily_groups = []
    by_day = defaultdict(list)
    for order in orders:
        order['source_label'] = source_label_map.get(order.get('source'), 'Outlet Request')
        by_day[order.get('needed_date')].append(order)
    for day in sorted(by_day.keys()):
        day_orders = by_day[day]
        for order in day_orders:
            ingredient_rows = []
            for (ing_id, unit), qty in order_daily_ingredients.get(order['id'], {}).items():
                display = smart_quantity(qty, unit, unit_system)
                ingredient_rows.append({
                    'ingredient_id': ing_id,
                    'name': next((item['name'] for item in ingredient_master if item['id'] == ing_id), 'Unknown'),
                    'quantity': qty,
                    'unit': unit,
                    'display_quantity': display['quantity'],
                    'display_unit': display['unit']
                })
            ingredient_rows.sort(key=lambda item: (item.get('name') or '').lower())
            order['daily_ingredients'] = ingredient_rows
        daily_groups.append({'date': day, 'orders': day_orders})

    logs_by_line_day = {}
    logs_by_line = defaultdict(dict)
    line_ids = [line.get('id') for order in orders for line in order.get('lines', []) if line.get('id')]
    if line_ids:
        log_history_start = start_date - timedelta(days=14)
        cur.execute(
            """
            SELECT
                line_id,
                production_date,
                assigned_to,
                made_by,
                signed_off,
                signed_off_by,
                tasted_by,
                COALESCE(production_notes, notes) AS production_notes,
                actual_yield,
                actual_yield_unit,
                COALESCE(pending_rollup, FALSE) AS pending_rollup,
                COALESCE(cancelled_line, FALSE) AS cancelled_line,
                updated_at
            FROM commissary_production_logs
            WHERE line_id = ANY(%s)
              AND production_date BETWEEN %s AND %s
        """,
            (line_ids, log_history_start, end_date),
        )
        for log_row in cur.fetchall():
            line_id = log_row.get('line_id')
            log_date = log_row.get('production_date')
            if not line_id or not log_date:
                continue
            log_payload = {
                'id': None,
                'date': log_date,
                'assigned_to': clean_menu_text(log_row.get('assigned_to')),
                'made_by': clean_menu_text(log_row.get('made_by')),
                'signed_off': bool(log_row.get('signed_off')),
                'signed_off_by': clean_menu_text(log_row.get('signed_off_by')),
                'tasted_by': clean_menu_text(log_row.get('tasted_by')),
                'notes': clean_menu_text(log_row.get('production_notes')),
                'actual_yield': to_float(log_row.get('actual_yield')),
                'actual_yield_unit': log_row.get('actual_yield_unit'),
                'pending_rollup': bool(log_row.get('pending_rollup')),
                'cancelled_line': bool(log_row.get('cancelled_line')),
                'updated_at': log_row.get('updated_at'),
            }
            logs_by_line_day[(line_id, log_date)] = log_payload
            logs_by_line[line_id][log_date] = log_payload

    daily_production_days = []
    day_cursor = start_date
    while day_cursor <= end_date:
        entry_rows = []
        for order in orders:
            source_key = normalize_commissary_source(order.get('source'))
            for line in order.get('lines', []):
                line_start = line.get('prep_start_date') or order.get('needed_date') or day_cursor
                line_end = line.get('prep_end_date') or line_start
                if line_end < line_start:
                    line_start, line_end = line_end, line_start
                production_log = logs_by_line_day.get((line.get('id'), day_cursor)) or {}
                prior_logs = logs_by_line.get(line.get('id'), {})
                latest_prior_log = None
                latest_prior_date = None
                if prior_logs:
                    prior_dates = [log_date for log_date in prior_logs.keys() if log_date < day_cursor]
                    if prior_dates:
                        latest_prior_date = max(prior_dates)
                        latest_prior_log = prior_logs.get(latest_prior_date) or None

                carryover_active = bool(
                    latest_prior_log
                    and latest_prior_log.get('pending_rollup')
                    and not latest_prior_log.get('signed_off')
                    and not latest_prior_log.get('cancelled_line')
                )
                is_within_window = line_start <= day_cursor <= line_end if line_start and line_end else False
                if not is_within_window and not carryover_active:
                    continue
                day_total = ((line_end - line_start).days + 1) if line_start and line_end else 1
                day_index = ((day_cursor - line_start).days + 1) if line_start else 1
                display_quantity = line.get('quantity')
                display_unit = line.get('quantity_unit') or 'each'
                carryover_label = ''
                if carryover_active:
                    remaining_qty, remaining_unit = resolve_commissary_remaining_quantity(
                        line.get('quantity'),
                        line.get('quantity_unit') or 'each',
                        latest_prior_log,
                    )
                    if remaining_qty <= 0:
                        continue
                    display_quantity = remaining_qty
                    display_unit = remaining_unit or display_unit
                    carryover_label = (
                        f"Carryover from {latest_prior_date.strftime('%a %-m/%-d')}"
                        if latest_prior_date else
                        'Carryover'
                    )
                entry_rows.append({
                    'source': source_key,
                    'source_label': source_label_map.get(source_key, 'Outlet Request'),
                    'item_name': line.get('item_name') or line.get('recipe_name') or 'Item',
                    'quantity': display_quantity,
                    'quantity_unit': display_unit,
                    'assigned_to': production_log.get('assigned_to') or '',
                    'made_by': production_log.get('made_by') or '',
                    'tasted_by': production_log.get('tasted_by') or '',
                    'signed_off_by': production_log.get('signed_off_by') or '',
                    'production_notes': production_log.get('notes') or '',
                    'actual_yield': production_log.get('actual_yield') or '',
                    'actual_yield_unit': production_log.get('actual_yield_unit') or (line.get('quantity_unit') or 'each'),
                    'pending_rollup': bool(production_log.get('pending_rollup')),
                    'cancelled_line': bool(production_log.get('cancelled_line')),
                    'signed_off': bool(production_log.get('signed_off')),
                    'outlet': order.get('outlet') or DEFAULT_COMMISSARY_OUTLET,
                    'order_id': order.get('id'),
                    'line_id': line.get('id'),
                    'notes': line.get('notes') or '',
                    'prep_start_date': line_start,
                    'prep_end_date': line_end,
                    'prep_day_index': day_index,
                    'prep_day_total': day_total,
                    'prep_day_label': f"Day {day_index}/{day_total}" if day_total > 1 else '',
                    'carryover_label': carryover_label,
                    'is_carryover': bool(carryover_label),
                    'is_standing_suggestion': False,
                })

        source_groups = []
        for source_key in source_display_order:
            source_entries = [entry for entry in entry_rows if entry.get('source') == source_key]
            if not source_entries:
                continue
            source_entries.sort(key=lambda entry: (entry.get('item_name') or '').lower())
            source_groups.append({
                'source': source_key,
                'label': source_label_map.get(source_key, source_key.replace('_', ' ').title()),
                'entries': source_entries,
            })
        daily_production_days.append({
            'date': day_cursor,
            'source_groups': source_groups,
            'entry_count': len(entry_rows),
        })
        day_cursor += timedelta(days=1)

    all_daily_line_entries = [
        entry
        for day in daily_production_days
        for source_group in day.get('source_groups', [])
        for entry in source_group.get('entries', [])
        if entry.get('line_id')
        and not entry.get('cancelled_line')
    ]
    total_lines = len(all_daily_line_entries)
    signed_off_line_count = sum(1 for entry in all_daily_line_entries if entry.get('signed_off'))
    logged_line_count = sum(
        1
        for entry in all_daily_line_entries
        if any(
            [
                entry.get('assigned_to'),
                entry.get('made_by'),
                entry.get('tasted_by'),
                entry.get('signed_off_by'),
                entry.get('production_notes'),
                entry.get('actual_yield'),
                entry.get('signed_off'),
            ]
        )
    )
    total_estimated_cost = sum(
        sum(line.get('estimated_cost_total', 0) or 0 for line in order.get('lines', []))
        for order in orders
    )

    return {
        'orders': orders,
        'daily_groups': daily_groups,
        'daily_production_days': daily_production_days,
        'source_choices': COMMISSARY_SOURCE_CHOICES,
        'shopping_ingredients': ingredient_master,
        'shopping_total_cost': ingredient_total_cost,
        'weekly_prep': weekly_prep,
        'order_count': len(orders),
        'line_count': total_lines,
        'logged_line_count': logged_line_count,
        'signed_off_line_count': signed_off_line_count,
        'total_estimated_cost': total_estimated_cost
    }

__all__ = [
    'get_commissary_week_window',
    'COMMISSARY_ACTIVE_STATUSES',
    'COMMISSARY_STATUS_CHOICES',
    'COMMISSARY_SOURCE_CHOICES',
    'DEFAULT_COMMISSARY_OUTLET',
    'commissary_tables_ready',
    'normalize_commissary_status',
    'normalize_commissary_source',
    'ensure_commissary_tables',
    'get_commissary_outlet_options',
    'get_commissary_order',
    'get_commissary_order_lines',
    'parse_commissary_order_lines',
    'fetch_commissary_order_rows',
    'build_commissary_datasets',
]
