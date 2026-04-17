import csv
import io
import os
import re
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta

from helpers.db_helpers import db_table_exists
from helpers.formatting import normalize_match_key
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
TOAST_PRODUCT_MIX_ALL_LEVELS_FILE = 'all levels.csv'


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
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS forecasting_sales_mix_imports (
            id TEXT PRIMARY KEY,
            venue_id TEXT REFERENCES venues(id) ON DELETE SET NULL,
            source_filename TEXT,
            sales_start DATE,
            sales_end DATE,
            source_format TEXT DEFAULT 'toast_product_mix',
            item_count INTEGER DEFAULT 0,
            food_item_count INTEGER DEFAULT 0,
            total_qty NUMERIC DEFAULT 0,
            total_net_sales NUMERIC DEFAULT 0,
            imported_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute("ALTER TABLE forecasting_sales_mix_imports ADD COLUMN IF NOT EXISTS venue_id TEXT")
    cur.execute("ALTER TABLE forecasting_sales_mix_imports ADD COLUMN IF NOT EXISTS source_filename TEXT")
    cur.execute("ALTER TABLE forecasting_sales_mix_imports ADD COLUMN IF NOT EXISTS sales_start DATE")
    cur.execute("ALTER TABLE forecasting_sales_mix_imports ADD COLUMN IF NOT EXISTS sales_end DATE")
    cur.execute("ALTER TABLE forecasting_sales_mix_imports ADD COLUMN IF NOT EXISTS source_format TEXT DEFAULT 'toast_product_mix'")
    cur.execute("ALTER TABLE forecasting_sales_mix_imports ADD COLUMN IF NOT EXISTS item_count INTEGER DEFAULT 0")
    cur.execute("ALTER TABLE forecasting_sales_mix_imports ADD COLUMN IF NOT EXISTS food_item_count INTEGER DEFAULT 0")
    cur.execute("ALTER TABLE forecasting_sales_mix_imports ADD COLUMN IF NOT EXISTS total_qty NUMERIC DEFAULT 0")
    cur.execute("ALTER TABLE forecasting_sales_mix_imports ADD COLUMN IF NOT EXISTS total_net_sales NUMERIC DEFAULT 0")
    cur.execute("ALTER TABLE forecasting_sales_mix_imports ADD COLUMN IF NOT EXISTS imported_by TEXT")
    cur.execute("ALTER TABLE forecasting_sales_mix_imports ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_forecasting_sales_mix_imports_venue
            ON forecasting_sales_mix_imports (venue_id, sales_start DESC, created_at DESC)
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS forecasting_sales_mix_items (
            id BIGSERIAL PRIMARY KEY,
            import_id TEXT NOT NULL REFERENCES forecasting_sales_mix_imports(id) ON DELETE CASCADE,
            source_type TEXT,
            menu TEXT,
            menu_group TEXT,
            subgroup TEXT,
            item_name TEXT NOT NULL,
            size_modifier TEXT,
            modifier_text TEXT,
            item_tags TEXT,
            qty_sold NUMERIC DEFAULT 0,
            avg_price NUMERIC,
            gross_sales NUMERIC DEFAULT 0,
            discount_amount NUMERIC DEFAULT 0,
            refund_amount NUMERIC DEFAULT 0,
            void_amount NUMERIC DEFAULT 0,
            net_sales NUMERIC DEFAULT 0,
            tax NUMERIC DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute("ALTER TABLE forecasting_sales_mix_items ADD COLUMN IF NOT EXISTS import_id TEXT")
    cur.execute("ALTER TABLE forecasting_sales_mix_items ADD COLUMN IF NOT EXISTS source_type TEXT")
    cur.execute("ALTER TABLE forecasting_sales_mix_items ADD COLUMN IF NOT EXISTS menu TEXT")
    cur.execute("ALTER TABLE forecasting_sales_mix_items ADD COLUMN IF NOT EXISTS menu_group TEXT")
    cur.execute("ALTER TABLE forecasting_sales_mix_items ADD COLUMN IF NOT EXISTS subgroup TEXT")
    cur.execute("ALTER TABLE forecasting_sales_mix_items ADD COLUMN IF NOT EXISTS item_name TEXT")
    cur.execute("ALTER TABLE forecasting_sales_mix_items ADD COLUMN IF NOT EXISTS size_modifier TEXT")
    cur.execute("ALTER TABLE forecasting_sales_mix_items ADD COLUMN IF NOT EXISTS modifier_text TEXT")
    cur.execute("ALTER TABLE forecasting_sales_mix_items ADD COLUMN IF NOT EXISTS item_tags TEXT")
    cur.execute("ALTER TABLE forecasting_sales_mix_items ADD COLUMN IF NOT EXISTS qty_sold NUMERIC DEFAULT 0")
    cur.execute("ALTER TABLE forecasting_sales_mix_items ADD COLUMN IF NOT EXISTS avg_price NUMERIC")
    cur.execute("ALTER TABLE forecasting_sales_mix_items ADD COLUMN IF NOT EXISTS gross_sales NUMERIC DEFAULT 0")
    cur.execute("ALTER TABLE forecasting_sales_mix_items ADD COLUMN IF NOT EXISTS discount_amount NUMERIC DEFAULT 0")
    cur.execute("ALTER TABLE forecasting_sales_mix_items ADD COLUMN IF NOT EXISTS refund_amount NUMERIC DEFAULT 0")
    cur.execute("ALTER TABLE forecasting_sales_mix_items ADD COLUMN IF NOT EXISTS void_amount NUMERIC DEFAULT 0")
    cur.execute("ALTER TABLE forecasting_sales_mix_items ADD COLUMN IF NOT EXISTS net_sales NUMERIC DEFAULT 0")
    cur.execute("ALTER TABLE forecasting_sales_mix_items ADD COLUMN IF NOT EXISTS tax NUMERIC DEFAULT 0")
    cur.execute("ALTER TABLE forecasting_sales_mix_items ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_forecasting_sales_mix_items_import
            ON forecasting_sales_mix_items (import_id)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_forecasting_sales_mix_items_name
            ON forecasting_sales_mix_items (LOWER(item_name))
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS forecasting_sales_mix_mappings (
            id BIGSERIAL PRIMARY KEY,
            venue_id TEXT REFERENCES venues(id) ON DELETE CASCADE,
            pos_menu TEXT,
            pos_menu_group TEXT,
            pos_item_name TEXT NOT NULL,
            menu_item_id TEXT REFERENCES forecasting_menu_items(id) ON DELETE CASCADE,
            multiplier NUMERIC NOT NULL DEFAULT 1,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            notes TEXT,
            updated_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute("ALTER TABLE forecasting_sales_mix_mappings ADD COLUMN IF NOT EXISTS venue_id TEXT")
    cur.execute("ALTER TABLE forecasting_sales_mix_mappings ADD COLUMN IF NOT EXISTS pos_menu TEXT")
    cur.execute("ALTER TABLE forecasting_sales_mix_mappings ADD COLUMN IF NOT EXISTS pos_menu_group TEXT")
    cur.execute("ALTER TABLE forecasting_sales_mix_mappings ADD COLUMN IF NOT EXISTS pos_item_name TEXT")
    cur.execute("ALTER TABLE forecasting_sales_mix_mappings ADD COLUMN IF NOT EXISTS menu_item_id TEXT")
    cur.execute("ALTER TABLE forecasting_sales_mix_mappings ADD COLUMN IF NOT EXISTS multiplier NUMERIC DEFAULT 1")
    cur.execute("ALTER TABLE forecasting_sales_mix_mappings ADD COLUMN IF NOT EXISTS active BOOLEAN DEFAULT TRUE")
    cur.execute("ALTER TABLE forecasting_sales_mix_mappings ADD COLUMN IF NOT EXISTS notes TEXT")
    cur.execute("ALTER TABLE forecasting_sales_mix_mappings ADD COLUMN IF NOT EXISTS updated_by TEXT")
    cur.execute("ALTER TABLE forecasting_sales_mix_mappings ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    cur.execute("ALTER TABLE forecasting_sales_mix_mappings ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    cur.execute(
        """
        UPDATE forecasting_sales_mix_mappings
        SET multiplier = 1
        WHERE multiplier IS NULL OR multiplier <= 0
        """
    )
    cur.execute(
        """
        UPDATE forecasting_sales_mix_mappings
        SET active = TRUE
        WHERE active IS NULL
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_forecasting_sales_mix_mappings_lookup
            ON forecasting_sales_mix_mappings (
                venue_id,
                LOWER(pos_item_name),
                LOWER(COALESCE(pos_menu, '')),
                LOWER(COALESCE(pos_menu_group, ''))
            )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_forecasting_sales_mix_mappings_menu_item
            ON forecasting_sales_mix_mappings (menu_item_id)
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


def parse_sales_mix_filename_dates(filename):
    base_name = os.path.basename(filename or '')
    match = re.search(r'(\d{4}-\d{2}-\d{2})[_-](\d{4}-\d{2}-\d{2})', base_name)
    if not match:
        return None, None
    try:
        return (
            datetime.strptime(match.group(1), '%Y-%m-%d').date(),
            datetime.strptime(match.group(2), '%Y-%m-%d').date(),
        )
    except ValueError:
        return None, None


def _clean_csv_value(row, key):
    return (row.get(key) or '').strip()


def _find_toast_product_mix_csv(zip_file):
    for name in zip_file.namelist():
        if name.lower().strip('/') == TOAST_PRODUCT_MIX_ALL_LEVELS_FILE:
            return name
    return ''


def parse_toast_product_mix_zip(file_stream, filename=''):
    file_stream.seek(0)
    try:
        upload_bytes = file_stream.read()
        with zipfile.ZipFile(io.BytesIO(upload_bytes)) as zip_file:
            csv_name = _find_toast_product_mix_csv(zip_file)
            if not csv_name:
                return None, 'Upload a Toast Product Mix ZIP that includes All levels.csv.'
            with zip_file.open(csv_name) as csv_file:
                text_stream = io.TextIOWrapper(csv_file, encoding='utf-8-sig', newline='')
                reader = csv.DictReader(text_stream)
                rows = []
                for row in reader:
                    if _clean_csv_value(row, 'Type') != 'menuItem':
                        continue
                    item_name = _clean_csv_value(row, 'Item, open item')
                    if not item_name:
                        continue
                    qty_sold = to_float(row.get('Qty sold'))
                    net_sales = to_float(row.get('Net sales'))
                    rows.append(
                        {
                            'source_type': _clean_csv_value(row, 'Type'),
                            'menu': _clean_csv_value(row, 'Menu'),
                            'menu_group': _clean_csv_value(row, 'Menu group'),
                            'subgroup': _clean_csv_value(row, 'Subgroup'),
                            'item_name': item_name,
                            'size_modifier': _clean_csv_value(row, 'Size modifier'),
                            'modifier_text': _clean_csv_value(row, 'Modifiers, special requests'),
                            'item_tags': _clean_csv_value(row, 'Item tags'),
                            'qty_sold': qty_sold,
                            'avg_price': to_float(row.get('Avg. price')),
                            'gross_sales': to_float(row.get('Gross sales')),
                            'discount_amount': to_float(row.get('Discount amount')),
                            'refund_amount': to_float(row.get('Refund amount')),
                            'void_amount': to_float(row.get('Void amount')),
                            'net_sales': net_sales,
                            'tax': to_float(row.get('Tax')),
                        }
                    )
    except zipfile.BadZipFile:
        return None, 'Upload a valid ZIP export.'
    except UnicodeDecodeError:
        return None, 'Could not read All levels.csv as UTF-8.'

    if not rows:
        return None, 'No menu item rows were found in All levels.csv.'

    sales_start, sales_end = parse_sales_mix_filename_dates(filename)
    food_rows = [row for row in rows if (row.get('menu') or '').upper() == 'FOOD']
    parsed = {
        'sales_start': sales_start,
        'sales_end': sales_end,
        'rows': rows,
        'item_count': len(rows),
        'food_item_count': len(food_rows),
        'total_qty': sum(row.get('qty_sold') or 0 for row in rows),
        'total_net_sales': sum(row.get('net_sales') or 0 for row in rows),
    }
    return parsed, None


def import_forecasting_sales_mix(cur, venue_id, upload, imported_by=''):
    if upload is None or not upload.filename:
        return None, 'Choose a Product Mix ZIP export.'

    source_filename = os.path.basename(upload.filename or 'product_mix.zip')
    parsed, error = parse_toast_product_mix_zip(upload.stream, source_filename)
    if error:
        return None, error

    import_id = generate_id('smx_')
    cur.execute(
        """
        INSERT INTO forecasting_sales_mix_imports (
            id,
            venue_id,
            source_filename,
            sales_start,
            sales_end,
            source_format,
            item_count,
            food_item_count,
            total_qty,
            total_net_sales,
            imported_by
        )
        VALUES (%s, %s, %s, %s, %s, 'toast_product_mix', %s, %s, %s, %s, %s)
        """,
        (
            import_id,
            venue_id or None,
            source_filename,
            parsed.get('sales_start'),
            parsed.get('sales_end'),
            parsed.get('item_count'),
            parsed.get('food_item_count'),
            parsed.get('total_qty'),
            parsed.get('total_net_sales'),
            (imported_by or '').strip() or None,
        ),
    )

    for row in parsed.get('rows') or []:
        cur.execute(
            """
            INSERT INTO forecasting_sales_mix_items (
                import_id,
                source_type,
                menu,
                menu_group,
                subgroup,
                item_name,
                size_modifier,
                modifier_text,
                item_tags,
                qty_sold,
                avg_price,
                gross_sales,
                discount_amount,
                refund_amount,
                void_amount,
                net_sales,
                tax
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                import_id,
                row.get('source_type'),
                row.get('menu'),
                row.get('menu_group'),
                row.get('subgroup'),
                row.get('item_name'),
                row.get('size_modifier'),
                row.get('modifier_text'),
                row.get('item_tags'),
                row.get('qty_sold'),
                row.get('avg_price'),
                row.get('gross_sales'),
                row.get('discount_amount'),
                row.get('refund_amount'),
                row.get('void_amount'),
                row.get('net_sales'),
                row.get('tax'),
            ),
        )

    return get_forecasting_sales_mix_import(cur, import_id), None


def get_forecasting_sales_mix_import(cur, import_id):
    if not import_id:
        return None
    cur.execute(
        """
        SELECT
            smi.id,
            smi.venue_id,
            v.name AS venue_name,
            smi.source_filename,
            smi.sales_start,
            smi.sales_end,
            smi.source_format,
            COALESCE(smi.item_count, 0) AS item_count,
            COALESCE(smi.food_item_count, 0) AS food_item_count,
            COALESCE(smi.total_qty, 0) AS total_qty,
            COALESCE(smi.total_net_sales, 0) AS total_net_sales,
            smi.imported_by,
            smi.created_at
        FROM forecasting_sales_mix_imports smi
        LEFT JOIN venues v ON v.id = smi.venue_id
        WHERE smi.id = %s
        LIMIT 1
        """,
        (import_id,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def list_forecasting_sales_mix_imports(cur, venue_id='', limit=20):
    capped_limit = min(max(int(limit or 20), 1), 100)
    params = []
    venue_filter_sql = ''
    if venue_id:
        venue_filter_sql = 'WHERE smi.venue_id = %s'
        params.append(venue_id)
    params.append(capped_limit)
    cur.execute(
        f"""
        SELECT
            smi.id,
            smi.venue_id,
            v.name AS venue_name,
            smi.source_filename,
            smi.sales_start,
            smi.sales_end,
            smi.source_format,
            COALESCE(smi.item_count, 0) AS item_count,
            COALESCE(smi.food_item_count, 0) AS food_item_count,
            COALESCE(smi.total_qty, 0) AS total_qty,
            COALESCE(smi.total_net_sales, 0) AS total_net_sales,
            smi.imported_by,
            smi.created_at
        FROM forecasting_sales_mix_imports smi
        LEFT JOIN venues v ON v.id = smi.venue_id
        {venue_filter_sql}
        ORDER BY
            smi.sales_start DESC NULLS LAST,
            smi.created_at DESC,
            smi.id DESC
        LIMIT %s
        """,
        params,
    )
    return [dict(row) for row in cur.fetchall()]


def list_forecasting_sales_mix_items(cur, import_id, food_only=False, limit=250):
    if not import_id:
        return []
    capped_limit = min(max(int(limit or 250), 1), 1000)
    food_filter_sql = "AND UPPER(COALESCE(menu, '')) = 'FOOD'" if food_only else ''
    cur.execute(
        f"""
        SELECT
            id,
            import_id,
            menu,
            menu_group,
            subgroup,
            item_name,
            qty_sold,
            avg_price,
            gross_sales,
            discount_amount,
            refund_amount,
            void_amount,
            net_sales,
            tax,
            item_tags
        FROM forecasting_sales_mix_items
        WHERE import_id = %s
          {food_filter_sql}
        ORDER BY qty_sold DESC NULLS LAST, LOWER(item_name)
        LIMIT %s
        """,
        (import_id, capped_limit),
    )
    return [dict(row) for row in cur.fetchall()]


def _sales_mix_mapping_key(menu='', menu_group='', item_name=''):
    return (
        normalize_match_key(menu),
        normalize_match_key(menu_group),
        normalize_match_key(item_name),
    )


def _sales_mix_mapping_lookup_sql():
    return """
        venue_id = %s
        AND LOWER(COALESCE(pos_menu, '')) = LOWER(%s)
        AND LOWER(COALESCE(pos_menu_group, '')) = LOWER(%s)
        AND LOWER(pos_item_name) = LOWER(%s)
    """


def list_forecasting_sales_mix_mappings(cur, venue_id=''):
    if not venue_id:
        return []
    cur.execute(
        """
        SELECT
            smm.id,
            smm.venue_id,
            smm.pos_menu,
            smm.pos_menu_group,
            smm.pos_item_name,
            smm.menu_item_id,
            COALESCE(smm.multiplier, 1) AS multiplier,
            COALESCE(smm.active, TRUE) AS active,
            smm.notes,
            smm.updated_by,
            smm.created_at,
            smm.updated_at
        FROM forecasting_sales_mix_mappings smm
        WHERE smm.venue_id = %s
        ORDER BY
            COALESCE(smm.active, TRUE) DESC,
            LOWER(smm.pos_item_name),
            smm.updated_at DESC NULLS LAST
        """,
        (venue_id,),
    )
    return [dict(row) for row in cur.fetchall()]


def get_forecasting_sales_mix_mapping(cur, venue_id, pos_menu, pos_menu_group, pos_item_name):
    if not venue_id or not pos_item_name:
        return None
    cur.execute(
        f"""
        SELECT
            id,
            venue_id,
            pos_menu,
            pos_menu_group,
            pos_item_name,
            menu_item_id,
            COALESCE(multiplier, 1) AS multiplier,
            COALESCE(active, TRUE) AS active,
            notes,
            updated_by,
            created_at,
            updated_at
        FROM forecasting_sales_mix_mappings
        WHERE {_sales_mix_mapping_lookup_sql()}
        ORDER BY
            COALESCE(active, TRUE) DESC,
            updated_at DESC NULLS LAST,
            id DESC
        LIMIT 1
        """,
        (
            venue_id,
            pos_menu or '',
            pos_menu_group or '',
            pos_item_name,
        ),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _forecast_menu_item_map(items):
    by_id = {}
    by_name = defaultdict(list)
    for item in items or []:
        item_id = item.get('id')
        if not item_id:
            continue
        by_id[item_id] = item
        name_key = normalize_match_key(item.get('name'))
        if name_key:
            by_name[name_key].append(item)
    return by_id, by_name


def _suggest_sales_mix_menu_item(pos_item_name, active_items_by_name):
    item_key = normalize_match_key(pos_item_name)
    if not item_key:
        return None

    exact = active_items_by_name.get(item_key) or []
    if len(exact) == 1:
        return {**exact[0], 'suggestion_type': 'Exact name'}

    contains_matches = []
    for forecast_key, items in active_items_by_name.items():
        if item_key in forecast_key or forecast_key in item_key:
            contains_matches.extend(items)
    unique = {item.get('id'): item for item in contains_matches if item.get('id')}
    if len(unique) == 1:
        item = next(iter(unique.values()))
        return {**item, 'suggestion_type': 'Similar name'}
    return None


def _aggregate_sales_mix_items(items):
    aggregated = {}
    for item in items or []:
        key = (
            item.get('menu') or '',
            item.get('menu_group') or '',
            item.get('item_name') or '',
        )
        if key not in aggregated:
            aggregated[key] = {
                'menu': item.get('menu') or '',
                'menu_group': item.get('menu_group') or '',
                'item_name': item.get('item_name') or '',
                'subgroups': set(),
                'item_tags': set(),
                'source_row_count': 0,
                'qty_sold': 0,
                'gross_sales': 0,
                'discount_amount': 0,
                'refund_amount': 0,
                'void_amount': 0,
                'net_sales': 0,
                'tax': 0,
            }
        target = aggregated[key]
        if item.get('subgroup'):
            target['subgroups'].add(item.get('subgroup'))
        if item.get('item_tags'):
            target['item_tags'].add(item.get('item_tags'))
        target['source_row_count'] += 1
        for field in (
            'qty_sold',
            'gross_sales',
            'discount_amount',
            'refund_amount',
            'void_amount',
            'net_sales',
            'tax',
        ):
            target[field] += to_float(item.get(field))

    rows = []
    for row in aggregated.values():
        row['subgroups'] = ', '.join(sorted(row['subgroups']))
        row['item_tags'] = ', '.join(sorted(row['item_tags']))
        rows.append(row)
    rows.sort(key=lambda row: (-to_float(row.get('qty_sold')), normalize_match_key(row.get('item_name'))))
    return rows


def build_forecasting_sales_mix_par_preview(cur, import_id, venue_id, food_only=True, limit=1000):
    if not import_id or not venue_id:
        return {
            'pos_rows': [],
            'weekly_par_rows': [],
            'summary': {
                'pos_row_count': 0,
                'matched_pos_count': 0,
                'unmatched_pos_count': 0,
                'suggested_pos_count': 0,
                'weekly_par_count': 0,
                'missing_recipe_count': 0,
                'total_source_qty': 0,
                'matched_source_qty': 0,
                'total_par_qty': 0,
                'match_coverage_pct': 0,
            },
        }

    source_rows = list_forecasting_sales_mix_items(cur, import_id, food_only=food_only, limit=limit)
    pos_rows = _aggregate_sales_mix_items(source_rows)
    forecast_items = [
        item
        for item in list_forecasting_menu_items(cur, venue_id)
        if bool(item.get('active'))
    ]
    forecast_by_id, forecast_by_name = _forecast_menu_item_map(forecast_items)
    mappings = list_forecasting_sales_mix_mappings(cur, venue_id)
    mapping_by_key = {}
    for mapping in mappings:
        key = _sales_mix_mapping_key(
            mapping.get('pos_menu'),
            mapping.get('pos_menu_group'),
            mapping.get('pos_item_name'),
        )
        if key not in mapping_by_key:
            mapping_by_key[key] = mapping

    weekly_by_menu_item = {}
    matched_pos_count = 0
    suggested_pos_count = 0
    matched_source_qty = 0
    total_source_qty = 0

    for row in pos_rows:
        total_source_qty += to_float(row.get('qty_sold'))
        key = _sales_mix_mapping_key(row.get('menu'), row.get('menu_group'), row.get('item_name'))
        mapping = mapping_by_key.get(key) or {}
        mapped_item = None
        is_mapped = False
        if mapping.get('active') and mapping.get('menu_item_id'):
            mapped_item = forecast_by_id.get(mapping.get('menu_item_id'))
            is_mapped = bool(mapped_item)

        suggestion = None
        if not is_mapped:
            suggestion = _suggest_sales_mix_menu_item(row.get('item_name'), forecast_by_name)
            if suggestion:
                suggested_pos_count += 1

        multiplier = to_float(mapping.get('multiplier')) if is_mapped else 1
        if multiplier <= 0:
            multiplier = 1
        par_qty = to_float(row.get('qty_sold')) * multiplier if is_mapped else 0

        row['mapping_id'] = mapping.get('id')
        row['menu_item_id'] = mapped_item.get('id') if mapped_item else ''
        row['mapped_item_name'] = mapped_item.get('name') if mapped_item else ''
        row['mapped_item_category'] = mapped_item.get('category') if mapped_item else ''
        row['mapped_recipe_id'] = mapped_item.get('recipe_id') if mapped_item else ''
        row['mapped_recipe_name'] = mapped_item.get('recipe_name') if mapped_item else ''
        row['mapped_yield_unit'] = mapped_item.get('yield_unit') if mapped_item else ''
        row['mapping_active'] = is_mapped
        row['mapping_invalid'] = bool(mapping.get('active') and mapping.get('menu_item_id') and not mapped_item)
        row['multiplier'] = multiplier
        row['notes'] = mapping.get('notes') or ''
        row['suggested_menu_item'] = suggestion or {}
        row['par_qty'] = par_qty
        row['recipe_ready'] = bool(row.get('mapped_recipe_id'))

        if not is_mapped:
            continue

        matched_pos_count += 1
        matched_source_qty += to_float(row.get('qty_sold'))
        weekly = weekly_by_menu_item.setdefault(
            mapped_item.get('id'),
            {
                'menu_item_id': mapped_item.get('id'),
                'item_name': mapped_item.get('name') or 'Forecast item',
                'category': mapped_item.get('category') or 'Uncategorized',
                'recipe_id': mapped_item.get('recipe_id'),
                'recipe_name': mapped_item.get('recipe_name'),
                'yield_qty': mapped_item.get('yield_qty'),
                'yield_unit': mapped_item.get('yield_unit') or 'each',
                'source_item_count': 0,
                'source_qty': 0,
                'par_qty': 0,
                'net_sales': 0,
                'pos_items': [],
            },
        )
        weekly['source_item_count'] += 1
        weekly['source_qty'] += to_float(row.get('qty_sold'))
        weekly['par_qty'] += par_qty
        weekly['net_sales'] += to_float(row.get('net_sales'))
        weekly['pos_items'].append(row.get('item_name') or 'POS item')

    weekly_par_rows = list(weekly_by_menu_item.values())
    weekly_par_rows.sort(key=lambda row: (normalize_match_key(row.get('category')), normalize_match_key(row.get('item_name'))))
    total_par_qty = sum(to_float(row.get('par_qty')) for row in weekly_par_rows)
    missing_recipe_count = sum(1 for row in weekly_par_rows if not row.get('recipe_id'))
    match_coverage_pct = round((matched_source_qty / total_source_qty) * 100) if total_source_qty else 0

    return {
        'pos_rows': pos_rows,
        'weekly_par_rows': weekly_par_rows,
        'summary': {
            'pos_row_count': len(pos_rows),
            'matched_pos_count': matched_pos_count,
            'unmatched_pos_count': len(pos_rows) - matched_pos_count,
            'suggested_pos_count': suggested_pos_count,
            'weekly_par_count': len(weekly_par_rows),
            'missing_recipe_count': missing_recipe_count,
            'total_source_qty': total_source_qty,
            'matched_source_qty': matched_source_qty,
            'total_par_qty': total_par_qty,
            'match_coverage_pct': match_coverage_pct,
        },
    }


def save_forecasting_sales_mix_mapping(cur, venue_id, row, updated_by=''):
    pos_menu = (row.get('pos_menu') or '').strip()
    pos_menu_group = (row.get('pos_menu_group') or '').strip()
    pos_item_name = (row.get('pos_item_name') or '').strip()
    menu_item_id = (row.get('menu_item_id') or '').strip()
    notes = (row.get('notes') or '').strip()
    multiplier = to_float(row.get('multiplier'))
    if multiplier <= 0:
        multiplier = 1
    updater = (updated_by or '').strip() or None

    if not venue_id or not pos_item_name:
        return 'skipped'

    existing = get_forecasting_sales_mix_mapping(cur, venue_id, pos_menu, pos_menu_group, pos_item_name)

    if not menu_item_id:
        if existing:
            cur.execute(
                """
                UPDATE forecasting_sales_mix_mappings
                SET
                    active = FALSE,
                    notes = %s,
                    updated_by = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (notes or None, updater, existing.get('id')),
            )
            return 'cleared'
        return 'skipped'

    cur.execute(
        """
        SELECT id
        FROM forecasting_menu_items
        WHERE id = %s
          AND venue_id = %s
        LIMIT 1
        """,
        (menu_item_id, venue_id),
    )
    if not cur.fetchone():
        return 'invalid'

    if existing:
        cur.execute(
            """
            UPDATE forecasting_sales_mix_mappings
            SET
                menu_item_id = %s,
                multiplier = %s,
                active = TRUE,
                notes = %s,
                updated_by = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (
                menu_item_id,
                multiplier,
                notes or None,
                updater,
                existing.get('id'),
            ),
        )
        return 'updated'

    cur.execute(
        """
        INSERT INTO forecasting_sales_mix_mappings (
            venue_id,
            pos_menu,
            pos_menu_group,
            pos_item_name,
            menu_item_id,
            multiplier,
            active,
            notes,
            updated_by
        )
        VALUES (%s, %s, %s, %s, %s, %s, TRUE, %s, %s)
        """,
        (
            venue_id,
            pos_menu or None,
            pos_menu_group or None,
            pos_item_name,
            menu_item_id,
            multiplier,
            notes or None,
            updater,
        ),
    )
    return 'created'


def save_forecasting_sales_mix_mappings(cur, venue_id, rows, updated_by=''):
    result = {
        'created': 0,
        'updated': 0,
        'cleared': 0,
        'invalid': 0,
        'skipped': 0,
    }
    for row in rows or []:
        status = save_forecasting_sales_mix_mapping(cur, venue_id, row, updated_by=updated_by)
        result[status] = result.get(status, 0) + 1
    result['saved'] = result.get('created', 0) + result.get('updated', 0)
    return result


def create_forecasting_plan_from_sales_mix(cur, import_id, venue_id, week_start, created_by=''):
    if not import_id or not venue_id or not week_start:
        return {
            'ok': False,
            'message': 'Choose a sales mix import, venue, and forecast week.',
        }

    sales_mix_import = get_forecasting_sales_mix_import(cur, import_id)
    if not sales_mix_import or sales_mix_import.get('venue_id') != venue_id:
        return {
            'ok': False,
            'message': 'Sales mix import was not found for this venue.',
        }

    plan = get_or_create_forecasting_plan(cur, venue_id, week_start, created_by=created_by)
    if normalize_forecasting_plan_status(plan.get('status')) == 'submitted':
        return {
            'ok': False,
            'message': 'That forecast week is already submitted. Choose another week or archive the submitted plan first.',
        }

    preview = build_forecasting_sales_mix_par_preview(cur, import_id, venue_id, food_only=True)
    weekly_rows = [
        row
        for row in preview.get('weekly_par_rows') or []
        if to_float(row.get('par_qty')) > 0
    ]
    if not weekly_rows:
        return {
            'ok': False,
            'message': 'Save at least one Product Mix match before creating a forecast draft.',
        }

    source_label = sales_mix_import.get('source_filename') or import_id
    plan_rows = []
    for row in weekly_rows:
        pos_items = row.get('pos_items') or []
        pos_preview = ', '.join(pos_items[:3])
        if len(pos_items) > 3:
            pos_preview = f"{pos_preview} +{len(pos_items) - 3} more"
        notes = f"Weekly par from {source_label}"
        if pos_preview:
            notes = f"{notes}: {pos_preview}"

        plan_row = {
            'menu_item_id': row.get('menu_item_id'),
            'recipe_id': row.get('recipe_id'),
            'item_name': row.get('item_name') or 'Forecast item',
            'category': row.get('category') or 'Uncategorized',
            'unit': row.get('yield_unit') or 'each',
            'notes': notes,
            'mon_qty': to_float(row.get('par_qty')),
            'tue_qty': 0,
            'wed_qty': 0,
            'thu_qty': 0,
            'fri_qty': 0,
            'sat_qty': 0,
            'sun_qty': 0,
        }
        plan_rows.append(plan_row)

    import_window = ''
    if sales_mix_import.get('sales_start') and sales_mix_import.get('sales_end'):
        import_window = f" ({sales_mix_import.get('sales_start')} through {sales_mix_import.get('sales_end')})"
    notes = f"Draft created from Product Mix {source_label}{import_window}. Weekly par quantities are placed on the week-start day."
    save_forecasting_plan_lines(cur, plan['id'], plan_rows, notes=notes)
    missing_recipe_count = sum(1 for row in weekly_rows if not row.get('recipe_id'))

    return {
        'ok': True,
        'plan_id': plan.get('id'),
        'week_start': week_start,
        'line_count': len(plan_rows),
        'missing_recipe_count': missing_recipe_count,
        'total_par_qty': sum(to_float(row.get('par_qty')) for row in weekly_rows),
    }


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
    'build_forecasting_sales_mix_par_preview',
    'build_forecasting_plan_rows',
    'create_forecasting_plan_from_sales_mix',
    'ensure_forecasting_schema',
    'ensure_forecasting_pipeline_schema',
    'ensure_recipe_venue_link',
    'get_forecasting_day_dates',
    'get_forecasting_menu_item',
    'get_forecasting_plan_by_week',
    'get_forecasting_plan_lines',
    'get_forecasting_sales_mix_import',
    'get_forecasting_sales_mix_mapping',
    'get_forecasting_week_window',
    'get_existing_menu_items_by_recipe',
    'get_active_menu_recipe_ids',
    'get_or_create_forecasting_plan',
    'group_forecasting_items_by_category',
    'import_forecasting_sales_mix',
    'list_forecasting_menu_items',
    'list_forecasting_recipes',
    'list_forecasting_sales_mix_imports',
    'list_forecasting_sales_mix_items',
    'list_forecasting_sales_mix_mappings',
    'normalize_forecasting_plan_status',
    'parse_sales_mix_filename_dates',
    'parse_toast_product_mix_zip',
    'save_forecasting_plan_lines',
    'save_forecasting_sales_mix_mapping',
    'save_forecasting_sales_mix_mappings',
    'search_forecasting_recipes',
    'submit_forecasting_plan_to_commissary',
    'sync_forecasting_menu_items_for_recipe',
    'summarize_forecasting_items',
]
