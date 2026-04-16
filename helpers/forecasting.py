from collections import defaultdict
from datetime import date, datetime, timedelta

from helpers.db_helpers import db_table_exists
from helpers.shared import generate_id, to_float


FORECASTING_DAY_FIELDS = (
    ('mon', 'Mon', 0),
    ('tue', 'Tue', 1),
    ('wed', 'Wed', 2),
    ('thu', 'Thu', 3),
    ('fri', 'Fri', 4),
    ('sat', 'Sat', 5),
    ('sun', 'Sun', 6),
)

FORECASTING_PLAN_STATUSES = {'draft', 'submitted', 'archived'}


def escape_like(value):
    return value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def ensure_forecasting_schema(cur):
    if not db_table_exists(cur, 'public.forecasting_menu_items'):
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS forecasting_menu_items (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'Uncategorized',
                venue_id TEXT REFERENCES venues(id) ON DELETE SET NULL,
                description TEXT,
                recipe_id TEXT REFERENCES recipes(id) ON DELETE SET NULL,
                sort_order INTEGER DEFAULT 0,
                active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

    cur.execute("ALTER TABLE forecasting_menu_items ADD COLUMN IF NOT EXISTS name TEXT")
    cur.execute("ALTER TABLE forecasting_menu_items ADD COLUMN IF NOT EXISTS category TEXT DEFAULT 'Uncategorized'")
    cur.execute("ALTER TABLE forecasting_menu_items ADD COLUMN IF NOT EXISTS venue_id TEXT")
    cur.execute("ALTER TABLE forecasting_menu_items ADD COLUMN IF NOT EXISTS description TEXT")
    cur.execute("ALTER TABLE forecasting_menu_items ADD COLUMN IF NOT EXISTS recipe_id TEXT")
    cur.execute("ALTER TABLE forecasting_menu_items ADD COLUMN IF NOT EXISTS sort_order INTEGER DEFAULT 0")
    cur.execute("ALTER TABLE forecasting_menu_items ADD COLUMN IF NOT EXISTS active BOOLEAN DEFAULT TRUE")
    cur.execute("ALTER TABLE forecasting_menu_items ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    cur.execute("ALTER TABLE forecasting_menu_items ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    cur.execute("ALTER TABLE forecasting_menu_items ALTER COLUMN category SET DEFAULT 'Uncategorized'")
    cur.execute("ALTER TABLE forecasting_menu_items ALTER COLUMN sort_order SET DEFAULT 0")
    cur.execute("ALTER TABLE forecasting_menu_items ALTER COLUMN active SET DEFAULT TRUE")
    cur.execute(
        """
        UPDATE forecasting_menu_items
        SET category = 'Uncategorized'
        WHERE category IS NULL OR TRIM(category) = ''
        """
    )
    cur.execute(
        """
        UPDATE forecasting_menu_items
        SET sort_order = 0
        WHERE sort_order IS NULL
        """
    )
    cur.execute(
        """
        UPDATE forecasting_menu_items
        SET active = TRUE
        WHERE active IS NULL
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_forecasting_menu_items_category
            ON forecasting_menu_items (category)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_forecasting_menu_items_venue_id
            ON forecasting_menu_items (venue_id)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_forecasting_menu_items_recipe_id
            ON forecasting_menu_items (recipe_id)
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS forecasting_plans (
            id TEXT PRIMARY KEY,
            venue_id TEXT REFERENCES venues(id) ON DELETE CASCADE,
            week_start DATE NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            notes TEXT,
            created_by TEXT,
            submitted_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute("ALTER TABLE forecasting_plans ADD COLUMN IF NOT EXISTS venue_id TEXT")
    cur.execute("ALTER TABLE forecasting_plans ADD COLUMN IF NOT EXISTS week_start DATE")
    cur.execute("ALTER TABLE forecasting_plans ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'draft'")
    cur.execute("ALTER TABLE forecasting_plans ADD COLUMN IF NOT EXISTS notes TEXT")
    cur.execute("ALTER TABLE forecasting_plans ADD COLUMN IF NOT EXISTS created_by TEXT")
    cur.execute("ALTER TABLE forecasting_plans ADD COLUMN IF NOT EXISTS submitted_at TIMESTAMP")
    cur.execute("ALTER TABLE forecasting_plans ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    cur.execute("ALTER TABLE forecasting_plans ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    cur.execute(
        """
        UPDATE forecasting_plans
        SET status = 'draft'
        WHERE status IS NULL OR TRIM(status) = ''
        """
    )
    cur.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_forecasting_plans_venue_week
            ON forecasting_plans (venue_id, week_start)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_forecasting_plans_status
            ON forecasting_plans (status)
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS forecasting_plan_lines (
            id BIGSERIAL PRIMARY KEY,
            plan_id TEXT NOT NULL REFERENCES forecasting_plans(id) ON DELETE CASCADE,
            menu_item_id TEXT REFERENCES forecasting_menu_items(id) ON DELETE SET NULL,
            recipe_id TEXT REFERENCES recipes(id) ON DELETE SET NULL,
            item_name TEXT NOT NULL,
            category TEXT,
            unit TEXT DEFAULT 'each',
            mon_qty NUMERIC NOT NULL DEFAULT 0,
            tue_qty NUMERIC NOT NULL DEFAULT 0,
            wed_qty NUMERIC NOT NULL DEFAULT 0,
            thu_qty NUMERIC NOT NULL DEFAULT 0,
            fri_qty NUMERIC NOT NULL DEFAULT 0,
            sat_qty NUMERIC NOT NULL DEFAULT 0,
            sun_qty NUMERIC NOT NULL DEFAULT 0,
            notes TEXT,
            sort_order INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute("ALTER TABLE forecasting_plan_lines ADD COLUMN IF NOT EXISTS plan_id TEXT")
    cur.execute("ALTER TABLE forecasting_plan_lines ADD COLUMN IF NOT EXISTS menu_item_id TEXT")
    cur.execute("ALTER TABLE forecasting_plan_lines ADD COLUMN IF NOT EXISTS recipe_id TEXT")
    cur.execute("ALTER TABLE forecasting_plan_lines ADD COLUMN IF NOT EXISTS item_name TEXT")
    cur.execute("ALTER TABLE forecasting_plan_lines ADD COLUMN IF NOT EXISTS category TEXT")
    cur.execute("ALTER TABLE forecasting_plan_lines ADD COLUMN IF NOT EXISTS unit TEXT DEFAULT 'each'")
    for day_key, _, _ in FORECASTING_DAY_FIELDS:
        cur.execute(f"ALTER TABLE forecasting_plan_lines ADD COLUMN IF NOT EXISTS {day_key}_qty NUMERIC DEFAULT 0")
        cur.execute(
            f"""
            UPDATE forecasting_plan_lines
            SET {day_key}_qty = 0
            WHERE {day_key}_qty IS NULL
            """
        )
    cur.execute("ALTER TABLE forecasting_plan_lines ADD COLUMN IF NOT EXISTS notes TEXT")
    cur.execute("ALTER TABLE forecasting_plan_lines ADD COLUMN IF NOT EXISTS sort_order INTEGER DEFAULT 0")
    cur.execute("ALTER TABLE forecasting_plan_lines ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    cur.execute("ALTER TABLE forecasting_plan_lines ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    cur.execute(
        """
        UPDATE forecasting_plan_lines
        SET unit = 'each'
        WHERE unit IS NULL OR TRIM(unit) = ''
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_forecasting_plan_lines_plan_id
            ON forecasting_plan_lines (plan_id)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_forecasting_plan_lines_menu_item
            ON forecasting_plan_lines (menu_item_id)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_forecasting_plan_lines_recipe_id
            ON forecasting_plan_lines (recipe_id)
        """
    )
    return True


def ensure_forecasting_pipeline_schema(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS commissary_pipeline (
            id BIGSERIAL PRIMARY KEY,
            item_name TEXT NOT NULL,
            quantity NUMERIC NOT NULL DEFAULT 0,
            unit TEXT DEFAULT 'each',
            status TEXT NOT NULL DEFAULT 'requested',
            due_date DATE,
            requested_by TEXT,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute("ALTER TABLE commissary_pipeline ADD COLUMN IF NOT EXISTS item_name TEXT")
    cur.execute("ALTER TABLE commissary_pipeline ADD COLUMN IF NOT EXISTS quantity NUMERIC DEFAULT 0")
    cur.execute("ALTER TABLE commissary_pipeline ADD COLUMN IF NOT EXISTS unit TEXT DEFAULT 'each'")
    cur.execute("ALTER TABLE commissary_pipeline ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'requested'")
    cur.execute("ALTER TABLE commissary_pipeline ADD COLUMN IF NOT EXISTS due_date DATE")
    cur.execute("ALTER TABLE commissary_pipeline ADD COLUMN IF NOT EXISTS requested_by TEXT")
    cur.execute("ALTER TABLE commissary_pipeline ADD COLUMN IF NOT EXISTS notes TEXT")
    cur.execute("ALTER TABLE commissary_pipeline ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    cur.execute("ALTER TABLE commissary_pipeline ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_commissary_pipeline_due_date
            ON commissary_pipeline (due_date)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_commissary_pipeline_status
            ON commissary_pipeline (status)
        """
    )
    return True


def normalize_forecasting_plan_status(status):
    normalized = (status or '').strip().lower()
    return normalized if normalized in FORECASTING_PLAN_STATUSES else 'draft'


def get_forecasting_week_window(week_start_raw=''):
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


def get_forecasting_day_dates(week_start):
    return [
        {
            'key': day_key,
            'label': label,
            'date': week_start + timedelta(days=offset),
            'qty_field': f'{day_key}_qty',
        }
        for day_key, label, offset in FORECASTING_DAY_FIELDS
    ]


def ensure_recipe_venue_link(cur, recipe_id, venue_id):
    if not recipe_id or not venue_id or not db_table_exists(cur, 'public.recipe_venues'):
        return False
    cur.execute(
        """
        INSERT INTO recipe_venues (recipe_id, venue_id)
        VALUES (%s, %s)
        ON CONFLICT (recipe_id, venue_id) DO NOTHING
        """,
        (recipe_id, venue_id),
    )
    return True


def get_forecasting_menu_item(cur, item_id):
    cur.execute(
        """
        SELECT
            id,
            name,
            COALESCE(NULLIF(TRIM(category), ''), 'Uncategorized') AS category,
            venue_id,
            description,
            recipe_id,
            COALESCE(sort_order, 0) AS sort_order,
            COALESCE(active, TRUE) AS active,
            created_at,
            updated_at
        FROM forecasting_menu_items
        WHERE id = %s
        LIMIT 1
        """,
        (item_id,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def sync_forecasting_menu_items_for_recipe(cur, recipe_id, name='', category='', description=''):
    if not recipe_id or not db_table_exists(cur, 'public.forecasting_menu_items'):
        return 0

    cur.execute(
        """
        UPDATE forecasting_menu_items
        SET
            name = %s,
            category = %s,
            description = %s,
            updated_at = CURRENT_TIMESTAMP
        WHERE recipe_id = %s
        """,
        (
            (name or '').strip() or 'Menu Item',
            (category or '').strip() or 'Uncategorized',
            (description or '').strip() or None,
            recipe_id,
        ),
    )
    return cur.rowcount or 0


def list_forecasting_menu_items(cur, venue_id=''):
    if not venue_id:
        return []
    cur.execute(
        """
        SELECT
            fmi.id,
            COALESCE(NULLIF(TRIM(r.name), ''), NULLIF(TRIM(fmi.name), ''), 'Menu Item') AS name,
            COALESCE(NULLIF(TRIM(r.category), ''), NULLIF(TRIM(fmi.category), ''), 'Uncategorized') AS category,
            COALESCE(fmi.active, TRUE) AS active,
            COALESCE(fmi.sort_order, 0) AS sort_order,
            fmi.recipe_id,
            COALESCE(NULLIF(TRIM(r.menu_descriptor), ''), fmi.description) AS description,
            r.name AS recipe_name,
            r.recipe_type,
            r.menu_descriptor,
            r.yield_qty,
            r.yield_unit
        FROM forecasting_menu_items fmi
        LEFT JOIN recipes r ON r.id = fmi.recipe_id
        WHERE fmi.venue_id = %s
        ORDER BY
            COALESCE(fmi.active, TRUE) DESC,
            COALESCE(NULLIF(TRIM(r.category), ''), NULLIF(TRIM(fmi.category), ''), 'Uncategorized') ASC,
            COALESCE(fmi.sort_order, 0) ASC,
            LOWER(COALESCE(NULLIF(TRIM(r.name), ''), NULLIF(TRIM(fmi.name), ''), 'Menu Item')) ASC
        """,
        (venue_id,),
    )
    return [dict(row) for row in cur.fetchall()]


def summarize_forecasting_items(items):
    active_items = [item for item in (items or []) if bool(item.get('active'))]
    active_count = len(active_items)
    archived_count = len(items or []) - active_count
    with_recipe = sum(1 for item in active_items if item.get('recipe_id'))
    missing_recipe = active_count - with_recipe
    completion_pct = int(round((with_recipe / active_count) * 100)) if active_count else 0
    return {
        'active_count': active_count,
        'archived_count': archived_count,
        'with_recipe': with_recipe,
        'missing_recipe': missing_recipe,
        'completion_pct': completion_pct,
    }


def group_forecasting_items_by_category(items):
    grouped = {}
    for item in items or []:
        category = (item.get('category') or 'Uncategorized').strip() or 'Uncategorized'
        grouped.setdefault(category, []).append(item)
    return grouped


def list_forecasting_recipes(cur):
    cur.execute(
        """
        SELECT
            id,
            name,
            COALESCE(NULLIF(TRIM(category), ''), 'Uncategorized') AS category,
            recipe_type,
            menu_descriptor,
            yield_qty,
            yield_unit
        FROM recipes
        ORDER BY LOWER(name) ASC
        """
    )
    return [dict(row) for row in cur.fetchall()]


def get_active_menu_recipe_ids(cur, venue_id=''):
    if not venue_id:
        return set()
    cur.execute(
        """
        SELECT recipe_id
        FROM forecasting_menu_items
        WHERE venue_id = %s
          AND COALESCE(active, TRUE) = TRUE
          AND recipe_id IS NOT NULL
        """,
        (venue_id,),
    )
    return {row.get('recipe_id') for row in cur.fetchall() if row.get('recipe_id')}


def get_existing_menu_items_by_recipe(cur, venue_id, recipe_ids):
    if not venue_id or not recipe_ids:
        return {}

    cur.execute(
        """
        SELECT
            recipe_id,
            id,
            COALESCE(active, TRUE) AS active
        FROM forecasting_menu_items
        WHERE venue_id = %s
          AND recipe_id = ANY(%s)
        ORDER BY
            recipe_id,
            COALESCE(active, TRUE) DESC,
            updated_at DESC NULLS LAST,
            created_at DESC NULLS LAST,
            id DESC
        """,
        (venue_id, recipe_ids),
    )
    existing = {}
    for row in cur.fetchall():
        recipe_id = row.get('recipe_id')
        if recipe_id and recipe_id not in existing:
            existing[recipe_id] = dict(row)
    return existing


def search_forecasting_recipes(cur, query='', venue_id='', limit=30):
    capped_limit = min(max(int(limit or 30), 1), 100)
    like_query = f"%{escape_like(query or '')}%"
    cur.execute(
        """
        SELECT
            r.id,
            r.name,
            r.recipe_type,
            COALESCE(NULLIF(TRIM(r.category), ''), 'Uncategorized') AS category,
            r.menu_descriptor,
            EXISTS (
                SELECT 1
                FROM forecasting_menu_items fmi
                WHERE fmi.recipe_id = r.id
                  AND fmi.venue_id = %s
                  AND COALESCE(fmi.active, TRUE) = TRUE
            ) AS already_added
        FROM recipes r
        WHERE (
            %s = '' OR
            r.name ILIKE %s ESCAPE '\\' OR
            COALESCE(r.menu_descriptor, '') ILIKE %s ESCAPE '\\'
        )
        ORDER BY LOWER(r.name) ASC
        LIMIT %s
        """,
        (venue_id or '', query or '', like_query, like_query, capped_limit),
    )
    return [dict(row) for row in cur.fetchall()]


def get_forecasting_plan_by_week(cur, venue_id, week_start):
    if not venue_id or not week_start:
        return None
    cur.execute(
        """
        SELECT
            id,
            venue_id,
            week_start,
            COALESCE(NULLIF(TRIM(status), ''), 'draft') AS status,
            notes,
            created_by,
            submitted_at,
            created_at,
            updated_at
        FROM forecasting_plans
        WHERE venue_id = %s
          AND week_start = %s
        LIMIT 1
        """,
        (venue_id, week_start),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def get_or_create_forecasting_plan(cur, venue_id, week_start, created_by=''):
    plan = get_forecasting_plan_by_week(cur, venue_id, week_start)
    if plan:
        plan['status'] = normalize_forecasting_plan_status(plan.get('status'))
        return plan

    plan_id = generate_id('fpl_')
    cur.execute(
        """
        INSERT INTO forecasting_plans (
            id,
            venue_id,
            week_start,
            status,
            created_by
        )
        VALUES (%s, %s, %s, 'draft', %s)
        """,
        (plan_id, venue_id, week_start, (created_by or '').strip() or None),
    )
    return get_forecasting_plan_by_week(cur, venue_id, week_start)


def get_forecasting_plan_lines(cur, plan_id):
    if not plan_id:
        return []
    cur.execute(
        """
        SELECT
            fpl.id,
            fpl.plan_id,
            fpl.menu_item_id,
            fpl.recipe_id,
            COALESCE(NULLIF(TRIM(fpl.item_name), ''), r.name, fmi.name, 'Forecast item') AS item_name,
            COALESCE(NULLIF(TRIM(fpl.category), ''), r.category, fmi.category, 'Uncategorized') AS category,
            COALESCE(NULLIF(TRIM(fpl.unit), ''), r.yield_unit, 'each') AS unit,
            COALESCE(fpl.mon_qty, 0) AS mon_qty,
            COALESCE(fpl.tue_qty, 0) AS tue_qty,
            COALESCE(fpl.wed_qty, 0) AS wed_qty,
            COALESCE(fpl.thu_qty, 0) AS thu_qty,
            COALESCE(fpl.fri_qty, 0) AS fri_qty,
            COALESCE(fpl.sat_qty, 0) AS sat_qty,
            COALESCE(fpl.sun_qty, 0) AS sun_qty,
            fpl.notes,
            COALESCE(fpl.sort_order, 0) AS sort_order,
            r.name AS recipe_name,
            r.yield_qty,
            r.yield_unit
        FROM forecasting_plan_lines fpl
        LEFT JOIN forecasting_menu_items fmi ON fmi.id = fpl.menu_item_id
        LEFT JOIN recipes r ON r.id = fpl.recipe_id
        WHERE fpl.plan_id = %s
        ORDER BY
            COALESCE(fpl.sort_order, 0),
            LOWER(COALESCE(NULLIF(TRIM(fpl.category), ''), r.category, fmi.category, 'Uncategorized')),
            LOWER(COALESCE(NULLIF(TRIM(fpl.item_name), ''), r.name, fmi.name, 'Forecast item'))
        """,
        (plan_id,),
    )
    rows = []
    for row in cur.fetchall():
        item = dict(row)
        item['total_qty'] = sum(to_float(item.get(f'{day_key}_qty')) for day_key, _, _ in FORECASTING_DAY_FIELDS)
        rows.append(item)
    return rows


def build_forecasting_plan_rows(active_items, saved_lines):
    saved_by_menu_item = {
        line.get('menu_item_id'): line
        for line in saved_lines or []
        if line.get('menu_item_id')
    }
    seen_saved_ids = set()
    rows = []

    for idx, item in enumerate(active_items or []):
        saved = saved_by_menu_item.get(item.get('id')) or {}
        if saved.get('id'):
            seen_saved_ids.add(saved['id'])
        row = {
            'id': saved.get('id'),
            'menu_item_id': item.get('id'),
            'recipe_id': item.get('recipe_id'),
            'item_name': item.get('name') or saved.get('item_name') or 'Forecast item',
            'category': item.get('category') or saved.get('category') or 'Uncategorized',
            'unit': saved.get('unit') or item.get('yield_unit') or 'each',
            'notes': saved.get('notes') or '',
            'sort_order': idx,
            'recipe_name': item.get('recipe_name'),
            'recipe_type': item.get('recipe_type'),
            'yield_qty': item.get('yield_qty'),
            'yield_unit': item.get('yield_unit'),
            'is_orphaned': False,
        }
        for day_key, _, _ in FORECASTING_DAY_FIELDS:
            row[f'{day_key}_qty'] = to_float(saved.get(f'{day_key}_qty'))
        row['total_qty'] = sum(to_float(row.get(f'{day_key}_qty')) for day_key, _, _ in FORECASTING_DAY_FIELDS)
        rows.append(row)

    for saved in saved_lines or []:
        if saved.get('id') in seen_saved_ids:
            continue
        row = dict(saved)
        row['is_orphaned'] = True
        row['total_qty'] = sum(to_float(row.get(f'{day_key}_qty')) for day_key, _, _ in FORECASTING_DAY_FIELDS)
        rows.append(row)

    return rows


def save_forecasting_plan_lines(cur, plan_id, rows, notes=''):
    cur.execute("DELETE FROM forecasting_plan_lines WHERE plan_id = %s", (plan_id,))
    for idx, row in enumerate(rows or []):
        cur.execute(
            """
            INSERT INTO forecasting_plan_lines (
                plan_id,
                menu_item_id,
                recipe_id,
                item_name,
                category,
                unit,
                mon_qty,
                tue_qty,
                wed_qty,
                thu_qty,
                fri_qty,
                sat_qty,
                sun_qty,
                notes,
                sort_order
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                plan_id,
                row.get('menu_item_id') or None,
                row.get('recipe_id') or None,
                (row.get('item_name') or '').strip() or 'Forecast item',
                (row.get('category') or '').strip() or 'Uncategorized',
                (row.get('unit') or '').strip() or 'each',
                to_float(row.get('mon_qty')),
                to_float(row.get('tue_qty')),
                to_float(row.get('wed_qty')),
                to_float(row.get('thu_qty')),
                to_float(row.get('fri_qty')),
                to_float(row.get('sat_qty')),
                to_float(row.get('sun_qty')),
                (row.get('notes') or '').strip() or None,
                idx,
            ),
        )
    cur.execute(
        """
        UPDATE forecasting_plans
        SET notes = %s,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
        """,
        ((notes or '').strip() or None, plan_id),
    )


def submit_forecasting_plan_to_commissary(cur, plan, venue_name='', submitted_by=''):
    if normalize_forecasting_plan_status(plan.get('status')) == 'submitted':
        return {
            'ok': False,
            'already_submitted': True,
            'message': 'This forecast has already been submitted.',
        }

    plan_id = plan.get('id')
    week_start = plan.get('week_start')
    lines = get_forecasting_plan_lines(cur, plan_id)
    day_dates = get_forecasting_day_dates(week_start)
    grouped_lines = defaultdict(list)
    for line in lines:
        for day in day_dates:
            qty = to_float(line.get(day['qty_field']))
            if qty <= 0:
                continue
            grouped_lines[day['date']].append(
                {
                    'recipe_id': line.get('recipe_id'),
                    'item_name': line.get('item_name') or line.get('recipe_name') or 'Forecast item',
                    'quantity': qty,
                    'unit': line.get('unit') or line.get('yield_unit') or 'each',
                    'notes': line.get('notes'),
                }
            )

    if not grouped_lines:
        return {
            'ok': False,
            'message': 'Add at least one forecast quantity before submitting.',
        }

    ensure_forecasting_pipeline_schema(cur)
    outlet = (venue_name or '').strip() or 'Forecast Outlet'
    requester = (submitted_by or '').strip() or None
    created_order_ids = []
    created_line_count = 0

    for needed_date in sorted(grouped_lines.keys()):
        order_id = generate_id('cor_')
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
            VALUES (%s, %s, %s, 'submitted', 'forecast', %s, %s)
            """,
            (
                order_id,
                outlet,
                needed_date,
                f"Forecast plan {plan_id} for week starting {week_start}",
                requester,
            ),
        )
        created_order_ids.append(order_id)

        for idx, line in enumerate(grouped_lines[needed_date]):
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
                    line.get('recipe_id') or None,
                    line.get('item_name') or 'Forecast item',
                    line.get('quantity'),
                    line.get('unit') or 'each',
                    needed_date,
                    needed_date,
                    line.get('notes') or None,
                    idx,
                ),
            )
            cur.execute(
                """
                INSERT INTO commissary_pipeline (
                    item_name,
                    quantity,
                    unit,
                    status,
                    due_date,
                    requested_by,
                    notes
                )
                VALUES (%s, %s, %s, 'requested', %s, %s, %s)
                """,
                (
                    line.get('item_name') or 'Forecast item',
                    line.get('quantity'),
                    line.get('unit') or 'each',
                    needed_date,
                    requester,
                    f"Forecast plan {plan_id}",
                ),
            )
            created_line_count += 1

    cur.execute(
        """
        UPDATE forecasting_plans
        SET status = 'submitted',
            submitted_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
        """,
        (plan_id,),
    )
    return {
        'ok': True,
        'order_ids': created_order_ids,
        'order_count': len(created_order_ids),
        'line_count': created_line_count,
    }


__all__ = [
    'FORECASTING_DAY_FIELDS',
    'build_forecasting_plan_rows',
    'ensure_forecasting_schema',
    'ensure_forecasting_pipeline_schema',
    'ensure_recipe_venue_link',
    'get_forecasting_day_dates',
    'get_forecasting_menu_item',
    'get_forecasting_plan_by_week',
    'get_forecasting_plan_lines',
    'get_forecasting_week_window',
    'get_existing_menu_items_by_recipe',
    'get_active_menu_recipe_ids',
    'get_or_create_forecasting_plan',
    'group_forecasting_items_by_category',
    'list_forecasting_menu_items',
    'list_forecasting_recipes',
    'normalize_forecasting_plan_status',
    'save_forecasting_plan_lines',
    'search_forecasting_recipes',
    'submit_forecasting_plan_to_commissary',
    'sync_forecasting_menu_items_for_recipe',
    'summarize_forecasting_items',
]
