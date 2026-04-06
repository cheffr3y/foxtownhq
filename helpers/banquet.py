import json
import os
import re
from collections import defaultdict
from datetime import date, datetime, timedelta

from helpers.db_helpers import db_column_exists, db_table_exists
from helpers.formatting import collect_ingredient_usage_from_components, normalize_match_key, split_instruction_steps
from helpers.menu import clean_menu_text
from helpers.recipes import (
    build_component_tree,
    collect_batch_recipe_usage_from_components,
    collect_direct_ingredients_for_prep,
    collect_direct_subrecipes_for_prep,
    collect_ingredients_from_components,
    get_recipe_by_id,
    get_recipe_total_cost,
    menu_line_base_multiplier,
    normalize_recipe_type,
    ratio_from_line_quantity,
)
from helpers.shared import generate_id, parse_float_field, to_float
from helpers.units import convert_cost_per_unit, convert_quantity_between_units, normalize_count_unit, normalize_unit, smart_quantity

def get_banquet_date_window(start_raw, end_raw):
    today = date.today()
    default_start = today
    default_end = default_start + timedelta(days=9)

    start_date = default_start
    end_date = default_end
    try:
        if (start_raw or '').strip():
            start_date = datetime.strptime(start_raw.strip(), '%Y-%m-%d').date()
    except ValueError:
        start_date = default_start
    try:
        if (end_raw or '').strip():
            end_date = datetime.strptime(end_raw.strip(), '%Y-%m-%d').date()
    except ValueError:
        end_date = default_end

    if start_date > end_date:
        start_date, end_date = end_date, start_date
    return start_date, end_date

PREP_FAMILY_ADJECTIVE_STOPWORDS = {
    'rb', 'rm', 'traditional', 'prepared', 'custom', 'house', 'signature',
    'classic', 'style', 'the', 'a', 'an'
}

PREP_FAMILY_FORM_STOPWORDS = {
    'sauce', 'dressing', 'vinaigrette', 'glaze', 'stock', 'broth',
    'base', 'mix', 'seasoning'
}

def derive_prep_family_label(recipe_name):
    raw = clean_menu_text(recipe_name)
    if not raw:
        return 'Other Prep'

    text = raw.lower()
    text = re.sub(r'^(rb|rm)\s+', '', text)
    text = re.sub(r'\([^)]*\)', ' ', text)
    text = text.replace('&', ' and ')
    text = re.sub(r'[^a-z0-9 ]+', ' ', text)
    tokens = [token for token in text.split() if token]
    if not tokens:
        return raw

    base_tokens = [token for token in tokens if token not in PREP_FAMILY_ADJECTIVE_STOPWORDS and not token.isdigit()]
    core_tokens = [token for token in base_tokens if token not in PREP_FAMILY_FORM_STOPWORDS]

    if len(core_tokens) >= 2:
        chosen = core_tokens
    elif len(base_tokens) >= 2:
        chosen = base_tokens
    elif core_tokens:
        chosen = core_tokens
    elif base_tokens:
        chosen = base_tokens
    else:
        chosen = tokens

    label = ' '.join(chosen[:4]).strip()
    return label.title() if label else raw

BANQUET_ACTIVE_STATUSES = ('planning', 'confirmed')

BANQUET_VENUE_ID = 'ven_banquets'

BANQUET_BEO_MAX_BYTES = 25 * 1024 * 1024

BANQUET_BEO_UPLOAD_ROOT = os.path.join(os.path.dirname(__file__), 'uploads', 'banquet_beo')

BANQUET_STATUS_CHOICES = [
    ('planning', 'Planning'),
    ('confirmed', 'Confirmed'),
    ('completed', 'Completed'),
    ('cancelled', 'Cancelled')
]

def resolve_banquet_venue(venues):
    for venue in venues:
        if venue.get('id') == BANQUET_VENUE_ID:
            return venue
    if venues:
        return venues[0]
    return {'id': '', 'name': 'Banquets'}

def banquet_tables_ready(cur):
    required = (
        'public.banquet_menu_items',
        'public.banquet_menu_item_recipes',
        'public.banquet_menu_item_ingredients',
        'public.banquet_events',
        'public.banquet_event_menu_items'
    )
    return all(db_table_exists(cur, table_name) for table_name in required)

def ensure_banquet_menu_item_event_only_column(cur):
    from helpers.db_helpers import db_column_exists
    if not db_column_exists(cur, 'public.banquet_menu_items', 'is_event_only'):
        cur.execute("""
            ALTER TABLE banquet_menu_items
            ADD COLUMN is_event_only BOOLEAN NOT NULL DEFAULT FALSE
        """)

def banquet_shopping_checks_ready(cur):
    return db_table_exists(cur, 'public.banquet_shopping_checks')

def auto_complete_past_banquet_events(cur, venue_id=''):
    if not banquet_tables_ready(cur):
        return 0

    params = [date.today(), list(BANQUET_ACTIVE_STATUSES)]
    venue_filter_sql = ''
    if venue_id:
        venue_filter_sql = ' AND venue_id = %s'
        params.append(venue_id)

    cur.execute(f"""
        UPDATE banquet_events
        SET status = 'completed',
            updated_at = CURRENT_TIMESTAMP
        WHERE event_date < %s
          AND status = ANY(%s)
          {venue_filter_sql}
    """, params)
    updated_count = cur.rowcount or 0
    if updated_count > 0:
        cur.connection.commit()
    return updated_count

def banquet_event_beo_files_ready(cur):
    return db_table_exists(cur, 'public.banquet_event_beo_files')

def ensure_banquet_event_beo_files_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS banquet_event_beo_files (
            id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL REFERENCES banquet_events(id) ON DELETE CASCADE,
            original_filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL,
            mime_type TEXT,
            file_size_bytes BIGINT,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_banquet_event_beo_files_event
        ON banquet_event_beo_files (event_id, uploaded_at DESC)
    """)

def safe_pdf_filename(filename):
    base = os.path.basename((filename or '').strip())
    cleaned = re.sub(r'[^A-Za-z0-9._-]+', '_', base).strip('._')
    if not cleaned:
        cleaned = 'beo.pdf'
    if not cleaned.lower().endswith('.pdf'):
        cleaned = f"{cleaned}.pdf"
    return cleaned

def event_beo_file_path(event_id, stored_filename):
    return os.path.join(BANQUET_BEO_UPLOAD_ROOT, event_id, stored_filename)

def list_banquet_event_beo_files(cur, event_id):
    if not event_id:
        return []
    ensure_banquet_event_beo_files_table(cur)
    cur.execute("""
        SELECT id,
               event_id,
               original_filename,
               stored_filename,
               mime_type,
               file_size_bytes,
               uploaded_at
        FROM banquet_event_beo_files
        WHERE event_id = %s
        ORDER BY uploaded_at DESC, id DESC
    """, (event_id,))
    return cur.fetchall()

def list_banquet_event_beo_files_by_events(cur, event_ids):
    event_ids = [event_id for event_id in (event_ids or []) if event_id]
    if not event_ids:
        return {}
    ensure_banquet_event_beo_files_table(cur)
    cur.execute("""
        SELECT id,
               event_id,
               original_filename,
               stored_filename,
               mime_type,
               file_size_bytes,
               uploaded_at
        FROM banquet_event_beo_files
        WHERE event_id = ANY(%s)
        ORDER BY event_id, uploaded_at DESC, id DESC
    """, (event_ids,))
    grouped = defaultdict(list)
    for row in cur.fetchall():
        grouped[row.get('event_id')].append(row)
    return grouped

def get_banquet_event_beo_file(cur, event_id, file_id):
    ensure_banquet_event_beo_files_table(cur)
    cur.execute("""
        SELECT id,
               event_id,
               original_filename,
               stored_filename,
               mime_type,
               file_size_bytes,
               uploaded_at
        FROM banquet_event_beo_files
        WHERE event_id = %s
          AND id = %s
        LIMIT 1
    """, (event_id, file_id))
    return cur.fetchone()

def store_banquet_event_beo_file(cur, event_id, upload):
    if upload is None or not upload.filename:
        return None, 'Choose a PDF file to upload.'

    original_filename = safe_pdf_filename(upload.filename)
    if not original_filename.lower().endswith('.pdf'):
        return None, 'Only PDF files are allowed.'

    try:
        upload.stream.seek(0, os.SEEK_END)
        file_size = upload.stream.tell()
        upload.stream.seek(0)
    except Exception:
        file_size = 0

    if file_size <= 0:
        return None, 'Uploaded file is empty.'
    if file_size > BANQUET_BEO_MAX_BYTES:
        max_mb = int(BANQUET_BEO_MAX_BYTES / (1024 * 1024))
        return None, f'BEO PDF is too large. Max size is {max_mb} MB.'

    header = upload.stream.read(5)
    upload.stream.seek(0)
    if header != b'%PDF-':
        return None, 'File does not look like a valid PDF.'

    file_id = generate_id('beo_')
    stored_filename = f"{file_id}.pdf"
    event_folder = os.path.join(BANQUET_BEO_UPLOAD_ROOT, event_id)
    os.makedirs(event_folder, exist_ok=True)
    file_path = os.path.join(event_folder, stored_filename)
    upload.save(file_path)

    ensure_banquet_event_beo_files_table(cur)
    cur.execute("""
        INSERT INTO banquet_event_beo_files (
            id, event_id, original_filename, stored_filename, mime_type, file_size_bytes
        ) VALUES (%s, %s, %s, %s, %s, %s)
    """, (
        file_id,
        event_id,
        original_filename,
        stored_filename,
        'application/pdf',
        file_size
    ))

    return {
        'id': file_id,
        'event_id': event_id,
        'original_filename': original_filename,
        'stored_filename': stored_filename,
        'file_size_bytes': file_size
    }, None

def ensure_banquet_shopping_checks_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS banquet_shopping_checks (
            id BIGSERIAL PRIMARY KEY,
            venue_id TEXT NOT NULL,
            start_date DATE NOT NULL,
            end_date DATE NOT NULL,
            item_key TEXT NOT NULL,
            ingredient_id TEXT,
            unit TEXT,
            vendor TEXT,
            checked BOOLEAN NOT NULL DEFAULT FALSE,
            note TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_banquet_shopping_checks_unique
        ON banquet_shopping_checks (venue_id, start_date, end_date, item_key)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_banquet_shopping_checks_scope
        ON banquet_shopping_checks (venue_id, start_date, end_date)
    """)

def load_banquet_shopping_checklist(cur, venue_id, start_date, end_date):
    ensure_banquet_shopping_checks_table(cur)
    cur.execute("""
        SELECT item_key, checked, note
        FROM banquet_shopping_checks
        WHERE venue_id = %s
          AND start_date = %s
          AND end_date = %s
    """, (venue_id, start_date, end_date))
    return {
        row['item_key']: {
            'checked': bool(row.get('checked')),
            'note': row.get('note') or ''
        }
        for row in cur.fetchall()
    }

def list_banquet_menu_items(cur, venue_id=''):
    if not banquet_tables_ready(cur):
        return []
    has_base_yield = db_column_exists(cur, 'public.banquet_menu_items', 'base_yield_qty') and db_column_exists(cur, 'public.banquet_menu_items', 'base_yield_unit')
    has_event_only_column = db_column_exists(cur, 'public.banquet_menu_items', 'is_event_only')
    base_yield_qty_sql = "mi.base_yield_qty" if has_base_yield else "1::numeric"
    base_yield_unit_sql = "mi.base_yield_unit" if has_base_yield else "'each'"
    event_only_filter_sql = " AND (mi.is_event_only IS NULL OR mi.is_event_only = FALSE)" if has_event_only_column else ""
    if venue_id:
        cur.execute("""
            SELECT mi.id,
                   mi.name,
                   mi.venue_id,
                   COALESCE(v.name, '') AS venue_name,
                   mi.menu_section,
                   mi.menu_descriptor,
                   mi.notes,
                   {base_yield_qty_sql} AS base_yield_qty,
                   {base_yield_unit_sql} AS base_yield_unit,
                   pr.recipe_id AS linked_recipe_id,
                   r.name AS recipe_name,
                   COALESCE(rc.recipe_count, 0) AS recipe_count,
                   COALESCE(extra.extra_count, 0) AS extra_count
            FROM banquet_menu_items mi
            LEFT JOIN venues v ON v.id = mi.venue_id
            LEFT JOIN LATERAL (
                SELECT recipe_id
                FROM banquet_menu_item_recipes
                WHERE menu_item_id = mi.id
                ORDER BY id
                LIMIT 1
            ) pr ON TRUE
            LEFT JOIN recipes r ON r.id = pr.recipe_id
            LEFT JOIN (
                SELECT menu_item_id, COUNT(*) AS recipe_count
                FROM banquet_menu_item_recipes
                GROUP BY menu_item_id
            ) rc ON rc.menu_item_id = mi.id
            LEFT JOIN (
                SELECT menu_item_id, COUNT(*) AS extra_count
                FROM banquet_menu_item_ingredients
                GROUP BY menu_item_id
            ) extra ON extra.menu_item_id = mi.id
            WHERE (mi.venue_id = %s OR mi.venue_id IS NULL){event_only_filter_sql}
            ORDER BY COALESCE(mi.menu_section, 'zzzz'), mi.name
        """.format(
            base_yield_qty_sql=base_yield_qty_sql,
            base_yield_unit_sql=base_yield_unit_sql,
            event_only_filter_sql=event_only_filter_sql
        ), (venue_id,))
    else:
        cur.execute("""
            SELECT mi.id,
                   mi.name,
                   mi.venue_id,
                   COALESCE(v.name, '') AS venue_name,
                   mi.menu_section,
                   mi.menu_descriptor,
                   mi.notes,
                   {base_yield_qty_sql} AS base_yield_qty,
                   {base_yield_unit_sql} AS base_yield_unit,
                   pr.recipe_id AS linked_recipe_id,
                   r.name AS recipe_name,
                   COALESCE(rc.recipe_count, 0) AS recipe_count,
                   COALESCE(extra.extra_count, 0) AS extra_count
            FROM banquet_menu_items mi
            LEFT JOIN venues v ON v.id = mi.venue_id
            LEFT JOIN LATERAL (
                SELECT recipe_id
                FROM banquet_menu_item_recipes
                WHERE menu_item_id = mi.id
                ORDER BY id
                LIMIT 1
            ) pr ON TRUE
            LEFT JOIN recipes r ON r.id = pr.recipe_id
            LEFT JOIN (
                SELECT menu_item_id, COUNT(*) AS recipe_count
                FROM banquet_menu_item_recipes
                GROUP BY menu_item_id
            ) rc ON rc.menu_item_id = mi.id
            LEFT JOIN (
                SELECT menu_item_id, COUNT(*) AS extra_count
                FROM banquet_menu_item_ingredients
                GROUP BY menu_item_id
            ) extra ON extra.menu_item_id = mi.id
            WHERE 1 = 1{event_only_filter_sql}
            ORDER BY COALESCE(mi.menu_section, 'zzzz'), mi.name
        """.format(
            base_yield_qty_sql=base_yield_qty_sql,
            base_yield_unit_sql=base_yield_unit_sql,
            event_only_filter_sql=event_only_filter_sql
        ))
    return cur.fetchall()

def get_banquet_menu_item(cur, menu_item_id):
    if not banquet_tables_ready(cur):
        return None
    has_base_yield = db_column_exists(cur, 'public.banquet_menu_items', 'base_yield_qty') and db_column_exists(cur, 'public.banquet_menu_items', 'base_yield_unit')
    base_yield_qty_sql = "mi.base_yield_qty" if has_base_yield else "1::numeric"
    base_yield_unit_sql = "mi.base_yield_unit" if has_base_yield else "'each'"
    cur.execute("""
        SELECT mi.id,
               mi.name,
               mi.venue_id,
               mi.menu_section,
               mi.menu_descriptor,
               mi.notes,
               {base_yield_qty_sql} AS base_yield_qty,
               {base_yield_unit_sql} AS base_yield_unit,
               pr.recipe_id AS linked_recipe_id,
               pr.quantity AS linked_recipe_qty,
               pr.unit AS linked_recipe_unit
        FROM banquet_menu_items mi
        LEFT JOIN LATERAL (
            SELECT recipe_id, quantity, unit
            FROM banquet_menu_item_recipes
            WHERE menu_item_id = mi.id
            ORDER BY id
            LIMIT 1
        ) pr ON TRUE
        WHERE mi.id = %s
        LIMIT 1
    """.format(base_yield_qty_sql=base_yield_qty_sql, base_yield_unit_sql=base_yield_unit_sql), (menu_item_id,))
    return cur.fetchone()

def get_banquet_menu_item_recipe_components(cur, menu_item_id):
    if not banquet_tables_ready(cur):
        return []
    has_choice_columns = db_column_exists(cur, 'public.banquet_menu_item_recipes', 'choice_group') and db_column_exists(cur, 'public.banquet_menu_item_recipes', 'choice_weight_percent')
    choice_group_sql = "mr.choice_group AS choice_group" if has_choice_columns else "NULL::text AS choice_group"
    choice_weight_sql = "mr.choice_weight_percent AS choice_weight_percent" if has_choice_columns else "NULL::numeric AS choice_weight_percent"
    cur.execute(f"""
        SELECT mr.id,
               mr.menu_item_id,
               mr.recipe_id,
               mr.quantity,
               mr.unit,
               {choice_group_sql},
               {choice_weight_sql},
               r.name AS recipe_name,
               r.yield_qty,
               r.yield_unit,
               r.recipe_type
        FROM banquet_menu_item_recipes mr
        JOIN recipes r ON r.id = mr.recipe_id
        WHERE mr.menu_item_id = %s
        ORDER BY mr.id
    """, (menu_item_id,))
    return cur.fetchall()

def collect_banquet_recipe_components(cur, menu_item_ids):
    if not menu_item_ids:
        return {}
    has_choice_columns = db_column_exists(cur, 'public.banquet_menu_item_recipes', 'choice_group') and db_column_exists(cur, 'public.banquet_menu_item_recipes', 'choice_weight_percent')
    choice_group_sql = "mr.choice_group AS choice_group" if has_choice_columns else "NULL::text AS choice_group"
    choice_weight_sql = "mr.choice_weight_percent AS choice_weight_percent" if has_choice_columns else "NULL::numeric AS choice_weight_percent"
    cur.execute(f"""
        SELECT mr.menu_item_id,
               mr.recipe_id,
               mr.quantity,
               mr.unit,
               {choice_group_sql},
               {choice_weight_sql},
               r.name AS recipe_name,
               r.yield_qty,
               r.yield_unit,
               r.recipe_type
        FROM banquet_menu_item_recipes mr
        JOIN recipes r ON r.id = mr.recipe_id
        WHERE mr.menu_item_id = ANY(%s)
        ORDER BY mr.menu_item_id, mr.id
    """, (menu_item_ids,))
    grouped = defaultdict(list)
    for row in cur.fetchall():
        grouped[row['menu_item_id']].append(row)
    return grouped

def get_banquet_menu_item_component_summaries(cur, menu_item_ids):
    if not menu_item_ids:
        return {}
    summaries = {menu_item_id: {'recipes': [], 'ingredients': []} for menu_item_id in menu_item_ids}

    has_choice_columns = db_column_exists(cur, 'public.banquet_menu_item_recipes', 'choice_group') and db_column_exists(cur, 'public.banquet_menu_item_recipes', 'choice_weight_percent')
    choice_group_sql = "mr.choice_group AS choice_group" if has_choice_columns else "NULL::text AS choice_group"
    choice_weight_sql = "mr.choice_weight_percent AS choice_weight_percent" if has_choice_columns else "NULL::numeric AS choice_weight_percent"
    cur.execute(f"""
        SELECT mr.menu_item_id,
               mr.recipe_id,
               mr.quantity,
               mr.unit,
               {choice_group_sql},
               {choice_weight_sql},
               r.name AS recipe_name
        FROM banquet_menu_item_recipes mr
        JOIN recipes r ON r.id = mr.recipe_id
        WHERE mr.menu_item_id = ANY(%s)
        ORDER BY mr.menu_item_id, mr.id
    """, (menu_item_ids,))
    for row in cur.fetchall():
        summaries.setdefault(row['menu_item_id'], {'recipes': [], 'ingredients': []})
        summaries[row['menu_item_id']]['recipes'].append({
            'id': row.get('recipe_id'),
            'name': row.get('recipe_name'),
            'quantity': to_float(row.get('quantity')),
            'unit': row.get('unit') or '',
            'choice_group': row.get('choice_group'),
            'choice_weight_percent': to_float(row.get('choice_weight_percent'))
        })

    cur.execute("""
        SELECT ai.menu_item_id,
               ai.ingredient_id,
               ai.quantity,
               ai.unit,
               i.name AS ingredient_name
        FROM banquet_menu_item_ingredients ai
        JOIN ingredients i ON i.id = ai.ingredient_id
        WHERE ai.menu_item_id = ANY(%s)
        ORDER BY ai.menu_item_id, ai.id
    """, (menu_item_ids,))
    for row in cur.fetchall():
        summaries.setdefault(row['menu_item_id'], {'recipes': [], 'ingredients': []})
        summaries[row['menu_item_id']]['ingredients'].append({
            'id': row.get('ingredient_id'),
            'name': row.get('ingredient_name'),
            'quantity': to_float(row.get('quantity')),
            'unit': row.get('unit') or ''
        })

    return summaries

def get_banquet_menu_item_ingredients(cur, menu_item_id):
    if not banquet_tables_ready(cur):
        return []
    cur.execute("""
        SELECT ai.id,
               ai.menu_item_id,
               ai.ingredient_id,
               ai.quantity,
               ai.unit,
               i.name AS ingredient_name
        FROM banquet_menu_item_ingredients ai
        JOIN ingredients i ON i.id = ai.ingredient_id
        WHERE ai.menu_item_id = %s
        ORDER BY i.name
    """, (menu_item_id,))
    return cur.fetchall()

def get_banquet_event(cur, event_id):
    if not banquet_tables_ready(cur):
        return None
    cur.execute("""
        SELECT e.id,
               e.name,
               e.event_date,
               e.guest_count AS guests,
               e.guest_count,
               e.venue_id,
               e.building,
               e.room,
               e.service_timing,
               e.dietary_notes,
               e.notes,
               e.status,
               COALESCE(v.name, '') AS venue_name
        FROM banquet_events e
        LEFT JOIN venues v ON v.id = e.venue_id
        WHERE e.id = %s
    """, (event_id,))
    return cur.fetchone()

def get_banquet_event_lines(cur, event_id):
    if not banquet_tables_ready(cur):
        return []
    has_choice_selections_column = db_column_exists(cur, 'public.banquet_event_menu_items', 'choice_selections')
    has_event_only_column = db_column_exists(cur, 'public.banquet_menu_items', 'is_event_only')
    choice_selections_sql = "emi.choice_selections AS line_choice_selections" if has_choice_selections_column else "NULL::text AS line_choice_selections"
    event_only_sql = "mi.is_event_only AS is_event_only" if has_event_only_column else "FALSE AS is_event_only"
    cur.execute(f"""
        SELECT emi.id AS line_id,
               emi.event_id,
               emi.menu_item_id,
               COALESCE(NULLIF(TRIM(emi.menu_item_name), ''), mi.name) AS menu_item_name,
               COALESCE(emi.menu_descriptor, mi.menu_descriptor) AS menu_descriptor,
               COALESCE(emi.menu_section, mi.menu_section) AS menu_section,
               COALESCE(emi.recipe_id, primary_recipe.recipe_id) AS recipe_id,
               emi.quantity,
               emi.quantity_unit,
               emi.notes AS line_notes,
               {choice_selections_sql},
               {event_only_sql},
               r.name AS recipe_name,
               r.yield_qty,
               r.yield_unit,
               r.menu_descriptor AS recipe_descriptor
        FROM banquet_event_menu_items emi
        JOIN banquet_menu_items mi ON mi.id = emi.menu_item_id
        LEFT JOIN LATERAL (
            SELECT recipe_id
            FROM banquet_menu_item_recipes
            WHERE menu_item_id = mi.id
            ORDER BY id
            LIMIT 1
        ) primary_recipe ON TRUE
        LEFT JOIN recipes r ON r.id = COALESCE(emi.recipe_id, primary_recipe.recipe_id)
        WHERE emi.event_id = %s
        ORDER BY COALESCE(emi.sort_order, 0), emi.id
    """, (event_id,))
    rows = cur.fetchall()
    for row in rows:
        row['is_event_only'] = bool(row.get('is_event_only', False))
    return rows

def resolve_or_create_banquet_menu_item(cur, line, venue_id='', is_event_only=False):
    name = clean_menu_text(line.get('menu_item_name'))
    if not name:
        return None

    has_event_only_column = db_column_exists(cur, 'public.banquet_menu_items', 'is_event_only')
    if not is_event_only:
        cur.execute("""
            SELECT id, menu_section, menu_descriptor
            FROM banquet_menu_items
            WHERE LOWER(name) = LOWER(%s)
              AND (venue_id = %s OR venue_id IS NULL)
            ORDER BY CASE WHEN venue_id = %s THEN 0 ELSE 1 END, created_at
            LIMIT 1
        """, (name, venue_id or None, venue_id or None))
        existing = cur.fetchone()
        if existing:
            updates = []
            params = []
            if line.get('menu_section') and not existing.get('menu_section'):
                updates.append('menu_section = %s')
                params.append(line.get('menu_section'))
            if line.get('menu_descriptor') and not existing.get('menu_descriptor'):
                updates.append('menu_descriptor = %s')
                params.append(line.get('menu_descriptor'))
            if updates:
                updates.append('updated_at = CURRENT_TIMESTAMP')
                params.append(existing['id'])
                cur.execute(f"UPDATE banquet_menu_items SET {', '.join(updates)} WHERE id = %s", params)
            return existing['id']

    menu_item_id = generate_id('bmi_')
    if has_event_only_column:
        cur.execute("""
            INSERT INTO banquet_menu_items (id, name, venue_id, menu_section, menu_descriptor, notes, is_event_only)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (
            menu_item_id,
            name,
            venue_id or None,
            line.get('menu_section') or None,
            line.get('menu_descriptor') or None,
            None,
            bool(is_event_only)
        ))
    else:
        cur.execute("""
            INSERT INTO banquet_menu_items (id, name, venue_id, menu_section, menu_descriptor, notes)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            menu_item_id,
            name,
            venue_id or None,
            line.get('menu_section') or None,
            line.get('menu_descriptor') or None,
            None
        ))
    return menu_item_id

def resolve_banquet_recipe_link_unit(cur, recipe_id, preferred_unit=None):
    candidate_unit = normalize_unit(preferred_unit) or normalize_count_unit(preferred_unit) or clean_menu_text(preferred_unit)
    if candidate_unit:
        return candidate_unit

    recipe = get_recipe_by_id(cur, recipe_id) if recipe_id else None
    recipe_unit = normalize_unit((recipe or {}).get('yield_unit')) or normalize_count_unit((recipe or {}).get('yield_unit')) or clean_menu_text((recipe or {}).get('yield_unit'))
    return recipe_unit or 'serving'


def upsert_menu_item_recipe_link(cur, menu_item_id, recipe_id, preferred_unit=None):
    if not menu_item_id or not recipe_id:
        return
    cur.execute("""
        SELECT id
        FROM banquet_menu_item_recipes
        WHERE menu_item_id = %s
          AND recipe_id = %s
        LIMIT 1
    """, (menu_item_id, recipe_id))
    if cur.fetchone():
        return

    link_unit = resolve_banquet_recipe_link_unit(cur, recipe_id, preferred_unit=preferred_unit)
    cur.execute("""
        INSERT INTO banquet_menu_item_recipes (menu_item_id, recipe_id, quantity, unit)
        VALUES (%s, %s, 1, %s)
    """, (menu_item_id, recipe_id, link_unit))

def make_unique_banquet_menu_item_name(cur, base_name, venue_id):
    candidate = clean_menu_text(base_name) or 'Custom Menu Item'
    suffix = 2
    while True:
        cur.execute("""
            SELECT 1
            FROM banquet_menu_items
            WHERE LOWER(name) = LOWER(%s)
              AND (venue_id = %s OR venue_id IS NULL)
            LIMIT 1
        """, (candidate, venue_id or None))
        if not cur.fetchone():
            return candidate
        candidate = f"{base_name} {suffix}"
        suffix += 1

def clone_banquet_menu_item(cur, source_menu_item_id, venue_id, clone_label='Custom'):
    source_item = get_banquet_menu_item(cur, source_menu_item_id)
    if not source_item:
        return None

    base_name = f"{source_item.get('name') or 'Menu Item'} ({clone_label})"
    clone_name = make_unique_banquet_menu_item_name(cur, base_name, venue_id)
    clone_id = generate_id('bmi_')

    has_base_yield = db_column_exists(cur, 'public.banquet_menu_items', 'base_yield_qty') and db_column_exists(cur, 'public.banquet_menu_items', 'base_yield_unit')
    if has_base_yield:
        cur.execute("""
            INSERT INTO banquet_menu_items (
                id, name, venue_id, menu_section, menu_descriptor, base_yield_qty, base_yield_unit, notes
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            clone_id,
            clone_name,
            venue_id or source_item.get('venue_id'),
            source_item.get('menu_section'),
            source_item.get('menu_descriptor'),
            source_item.get('base_yield_qty') or 1,
            source_item.get('base_yield_unit') or 'each',
            source_item.get('notes')
        ))
    else:
        cur.execute("""
            INSERT INTO banquet_menu_items (
                id, name, venue_id, menu_section, menu_descriptor, notes
            ) VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            clone_id,
            clone_name,
            venue_id or source_item.get('venue_id'),
            source_item.get('menu_section'),
            source_item.get('menu_descriptor'),
            source_item.get('notes')
        ))

    has_choice_columns = db_column_exists(cur, 'public.banquet_menu_item_recipes', 'choice_group') and db_column_exists(cur, 'public.banquet_menu_item_recipes', 'choice_weight_percent')
    for row in get_banquet_menu_item_recipe_components(cur, source_menu_item_id):
        if has_choice_columns:
            cur.execute("""
                INSERT INTO banquet_menu_item_recipes (menu_item_id, recipe_id, quantity, unit, choice_group, choice_weight_percent)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (
                clone_id,
                row.get('recipe_id'),
                row.get('quantity') or 1,
                row.get('unit') or 'each',
                row.get('choice_group'),
                row.get('choice_weight_percent')
            ))
        else:
            cur.execute("""
                INSERT INTO banquet_menu_item_recipes (menu_item_id, recipe_id, quantity, unit)
                VALUES (%s, %s, %s, %s)
            """, (
                clone_id,
                row.get('recipe_id'),
                row.get('quantity') or 1,
                row.get('unit') or 'each'
            ))

    for row in get_banquet_menu_item_ingredients(cur, source_menu_item_id):
        cur.execute("""
            INSERT INTO banquet_menu_item_ingredients (menu_item_id, ingredient_id, quantity, unit)
            VALUES (%s, %s, %s, %s)
        """, (
            clone_id,
            row.get('ingredient_id'),
            row.get('quantity') or 0,
            row.get('unit')
        ))

    return {
        'id': clone_id,
        'name': clone_name,
        'menu_section': source_item.get('menu_section'),
        'menu_descriptor': source_item.get('menu_descriptor')
    }

def normalize_event_line_choice_selections(value):
    text = (value or '').strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return None

    if isinstance(payload, dict):
        raw_items = payload.get('items')
    elif isinstance(payload, list):
        raw_items = payload
    else:
        raw_items = []

    items = []
    for raw in raw_items or []:
        if not isinstance(raw, dict):
            continue
        recipe_id = clean_menu_text(raw.get('recipe_id'))
        if not recipe_id:
            continue
        count = to_float(raw.get('count'))
        if count <= 0:
            continue
        items.append({
            'recipe_id': recipe_id,
            'choice_group': clean_menu_text(raw.get('choice_group')) or None,
            'recipe_name': clean_menu_text(raw.get('recipe_name')) or None,
            'count': round(count, 6)
        })

    if not items:
        return None

    return json.dumps({'items': items}, separators=(',', ':'))

def parse_event_line_choice_selections(value):
    normalized = normalize_event_line_choice_selections(value)
    if not normalized:
        return []
    try:
        payload = json.loads(normalized)
    except (TypeError, ValueError):
        return []
    return payload.get('items') or []

def parse_event_recipe_lines(request):
    line_ids = request.form.getlist('line_id[]')
    menu_item_ids = request.form.getlist('line_menu_item_id[]')
    recipe_ids = request.form.getlist('line_recipe_id[]')
    line_is_custom_flags = request.form.getlist('line_is_custom[]')
    names = request.form.getlist('line_menu_item_name[]')
    descriptors = request.form.getlist('line_menu_descriptor[]')
    sections = request.form.getlist('line_menu_section[]')
    quantities = request.form.getlist('line_qty[]')
    quantity_units = request.form.getlist('line_unit[]')
    notes = request.form.getlist('line_notes[]')
    choice_selections = request.form.getlist('line_choice_selections[]')

    max_len = max(
        len(line_ids), len(menu_item_ids), len(recipe_ids), len(names), len(descriptors), len(sections),
        len(quantities), len(quantity_units), len(notes), len(choice_selections), len(line_is_custom_flags), 0
    )
    rows = []
    errors = []
    for idx in range(max_len):
        line_id = (line_ids[idx] if idx < len(line_ids) else '').strip()
        menu_item_id = (menu_item_ids[idx] if idx < len(menu_item_ids) else '').strip()
        recipe_id = (recipe_ids[idx] if idx < len(recipe_ids) else '').strip()
        menu_name = clean_menu_text(names[idx] if idx < len(names) else '')
        descriptor = clean_menu_text(descriptors[idx] if idx < len(descriptors) else '')
        section = clean_menu_text(sections[idx] if idx < len(sections) else '')
        qty_raw = (quantities[idx] if idx < len(quantities) else '').strip()
        qty_unit = clean_menu_text(quantity_units[idx] if idx < len(quantity_units) else '')
        note = clean_menu_text(notes[idx] if idx < len(notes) else '')
        choice_selection_text = normalize_event_line_choice_selections(choice_selections[idx] if idx < len(choice_selections) else '')

        if not any([menu_item_id, recipe_id, menu_name, descriptor, section, qty_raw, qty_unit, note, choice_selection_text]):
            continue
        qty = parse_float_field(qty_raw, 'Menu quantity', errors, required=True, min_value=0.0001)
        if not menu_name and not menu_item_id:
            errors.append('Each banquet line needs a menu item name.')
        rows.append({
            'line_id': line_id or None,
            'menu_item_id': menu_item_id or None,
            'recipe_id': recipe_id or None,
            'menu_item_name': menu_name,
            'menu_descriptor': descriptor or None,
            'menu_section': section or None,
            'quantity': qty,
            'quantity_unit': normalize_unit(qty_unit) or qty_unit or None,
            'notes': note or None,
            'choice_selections': choice_selection_text,
            'is_custom_item': (line_is_custom_flags[idx] if idx < len(line_is_custom_flags) else '') == '1'
        })
    return rows, errors

def parse_menu_item_ingredient_lines(request, valid_ingredient_ids):
    ingredient_ids = request.form.getlist('ingredient_id[]')
    quantities = request.form.getlist('ingredient_qty[]')
    units = request.form.getlist('ingredient_unit[]')

    max_len = max(len(ingredient_ids), len(quantities), len(units), 0)
    rows = []
    errors = []
    for idx in range(max_len):
        ingredient_id = (ingredient_ids[idx] if idx < len(ingredient_ids) else '').strip()
        quantity_raw = (quantities[idx] if idx < len(quantities) else '').strip()
        unit = clean_menu_text(units[idx] if idx < len(units) else '')
        if not any([ingredient_id, quantity_raw, unit]):
            continue
        if ingredient_id not in valid_ingredient_ids:
            errors.append('Additional ingredient row has an invalid ingredient.')
            continue
        quantity = parse_float_field(
            quantity_raw,
            'Additional ingredient quantity',
            errors,
            required=True,
            min_value=0.0001
        )
        if quantity is None:
            continue
        rows.append({
            'ingredient_id': ingredient_id,
            'quantity': quantity,
            'unit': normalize_unit(unit) or unit
        })
    return rows, errors

def parse_menu_item_recipe_lines(request, valid_recipe_ids, valid_source_menu_item_ids=None):
    recipe_ids = request.form.getlist('component_recipe_id[]')
    source_menu_item_ids = request.form.getlist('component_source_menu_item_id[]')
    quantities = request.form.getlist('component_qty[]')
    units = request.form.getlist('component_unit[]')
    choice_groups = request.form.getlist('component_choice_group[]')
    choice_weights = request.form.getlist('component_choice_weight[]')

    max_len = max(len(recipe_ids), len(source_menu_item_ids), len(quantities), len(units), len(choice_groups), len(choice_weights), 0)
    rows = []
    errors = []
    valid_source_ids = set(valid_source_menu_item_ids or set())
    for idx in range(max_len):
        recipe_id = (recipe_ids[idx] if idx < len(recipe_ids) else '').strip()
        source_menu_item_id = (source_menu_item_ids[idx] if idx < len(source_menu_item_ids) else '').strip()
        quantity_raw = (quantities[idx] if idx < len(quantities) else '').strip()
        unit = clean_menu_text(units[idx] if idx < len(units) else '')
        choice_group = clean_menu_text(choice_groups[idx] if idx < len(choice_groups) else '')
        choice_weight_raw = (choice_weights[idx] if idx < len(choice_weights) else '').strip()

        if not any([recipe_id, source_menu_item_id, quantity_raw, unit, choice_group, choice_weight_raw]):
            continue
        if recipe_id not in valid_recipe_ids:
            errors.append('Recipe component row has an invalid recipe.')
            continue
        if source_menu_item_id and source_menu_item_id not in valid_source_ids:
            errors.append('Recipe component row has an invalid menu-item source.')
            continue
        quantity = parse_float_field(
            quantity_raw,
            'Recipe component quantity',
            errors,
            required=True,
            min_value=0.0001
        )
        if quantity is None:
            continue

        choice_weight = None
        if choice_weight_raw:
            choice_weight = parse_float_field(
                choice_weight_raw,
                'Choice weight percent',
                errors,
                required=False,
                min_value=0
            )
            if choice_weight is not None and choice_weight > 100:
                errors.append('Choice weight percent cannot exceed 100.')

        rows.append({
            'recipe_id': recipe_id,
            'source_menu_item_id': source_menu_item_id or None,
            'quantity': quantity,
            'unit': normalize_unit(unit) or unit,
            'choice_group': choice_group or None,
            'choice_weight_percent': choice_weight if choice_group else None
        })

    grouped_choice_rows = defaultdict(list)
    for row in rows:
        group = (row.get('choice_group') or '').strip()
        if group:
            grouped_choice_rows[group].append(row)

    for group_name, group_rows in grouped_choice_rows.items():
        if not group_rows:
            continue
        explicit = [row for row in group_rows if row.get('choice_weight_percent') is not None]
        if not explicit:
            equal = 100.0 / len(group_rows)
            for row in group_rows:
                row['choice_weight_percent'] = equal
            continue

        explicit_total = sum(to_float(row.get('choice_weight_percent')) for row in explicit)
        if explicit_total > 100.0001:
            errors.append(f'Choice group "{group_name}" exceeds 100%.')
            continue

        missing = [row for row in group_rows if row.get('choice_weight_percent') is None]
        if missing:
            remaining = max(0.0, 100.0 - explicit_total)
            each = remaining / len(missing)
            for row in missing:
                row['choice_weight_percent'] = each
        elif 0 < explicit_total < 100:
            scale = 100.0 / explicit_total
            for row in explicit:
                row['choice_weight_percent'] = to_float(row.get('choice_weight_percent')) * scale
    return rows, errors

def collect_banquet_additional_ingredients(cur, menu_item_ids):
    if not menu_item_ids:
        return {}
    cur.execute("""
        SELECT ai.menu_item_id,
               ai.ingredient_id,
               ai.quantity,
               ai.unit,
               i.name AS ingredient_name,
               i.unit AS ingredient_unit,
               i.cost_per_unit,
               i.category,
               i.vendor,
               i.vendor_code,
               i.g_code
        FROM banquet_menu_item_ingredients ai
        JOIN ingredients i ON i.id = ai.ingredient_id
        WHERE ai.menu_item_id = ANY(%s)
    """, (menu_item_ids,))
    grouped = defaultdict(list)
    for row in cur.fetchall():
        grouped[row['menu_item_id']].append(row)
    return grouped

def fetch_banquet_event_lines(cur, start_date, end_date, venue_id=''):
    if not banquet_tables_ready(cur):
        return []
    has_base_yield = db_column_exists(cur, 'public.banquet_menu_items', 'base_yield_qty') and db_column_exists(cur, 'public.banquet_menu_items', 'base_yield_unit')
    has_choice_selections_column = db_column_exists(cur, 'public.banquet_event_menu_items', 'choice_selections')
    has_event_only_column = db_column_exists(cur, 'public.banquet_menu_items', 'is_event_only')
    base_yield_qty_sql = "mi.base_yield_qty" if has_base_yield else "1::numeric"
    base_yield_unit_sql = "mi.base_yield_unit" if has_base_yield else "'each'"
    choice_selections_sql = "emi.choice_selections AS line_choice_selections" if has_choice_selections_column else "NULL::text AS line_choice_selections"
    event_only_sql = "mi.is_event_only AS is_event_only" if has_event_only_column else "FALSE AS is_event_only"
    params = [start_date, end_date, list(BANQUET_ACTIVE_STATUSES)]
    venue_filter_sql = ""
    if venue_id:
        venue_filter_sql = " AND e.venue_id = %s"
        params.append(venue_id)

    cur.execute(f"""
        SELECT e.id AS event_id,
               e.name AS event_name,
               e.event_date,
               e.guest_count AS guests,
               e.venue_id,
               e.status,
               COALESCE(v.name, e.building, 'Banquet') AS venue_name,
               e.building,
               e.room,
               e.service_timing,
               e.dietary_notes,
               e.notes AS event_notes,
               emi.id AS line_id,
               emi.menu_item_id,
               COALESCE(NULLIF(TRIM(emi.menu_item_name), ''), mi.name) AS menu_item_name,
               COALESCE(emi.menu_descriptor, mi.menu_descriptor) AS menu_descriptor,
               COALESCE(emi.menu_section, mi.menu_section) AS menu_section,
               {base_yield_qty_sql} AS base_yield_qty,
               {base_yield_unit_sql} AS base_yield_unit,
               COALESCE(emi.recipe_id, primary_recipe.recipe_id) AS recipe_id,
               emi.quantity,
               emi.quantity_unit,
               emi.notes AS line_notes,
               {choice_selections_sql},
               {event_only_sql},
               r.name AS recipe_name,
               r.category AS recipe_category,
               r.yield_qty,
               r.yield_unit,
               r.menu_descriptor AS recipe_descriptor
        FROM banquet_events e
        LEFT JOIN venues v ON v.id = e.venue_id
        LEFT JOIN banquet_event_menu_items emi ON emi.event_id = e.id
        LEFT JOIN banquet_menu_items mi ON mi.id = emi.menu_item_id
        LEFT JOIN LATERAL (
            SELECT recipe_id
            FROM banquet_menu_item_recipes
            WHERE menu_item_id = mi.id
            ORDER BY id
            LIMIT 1
        ) primary_recipe ON TRUE
        LEFT JOIN recipes r ON r.id = COALESCE(emi.recipe_id, primary_recipe.recipe_id)
        WHERE e.event_date BETWEEN %s AND %s
          AND e.status = ANY(%s)
          {venue_filter_sql}
        ORDER BY e.event_date, e.name, COALESCE(emi.sort_order, 0), emi.id
    """, params)
    rows = cur.fetchall()
    for row in rows:
        row['is_event_only'] = bool(row.get('is_event_only', False))
    return rows

def build_banquet_recipe_detail_card(
    cur,
    recipe_id,
    required_qty,
    required_unit,
    required_batches,
    ingredient_name_map=None,
    unit_system='imperial',
    recipe_cache=None,
    ancestry=None,
    max_depth=4
):
    if not recipe_id:
        return None

    ingredient_name_map = ingredient_name_map or {}
    recipe_cache = recipe_cache or {}
    ancestry = ancestry or set()

    if recipe_id in recipe_cache:
        recipe = recipe_cache.get(recipe_id)
    else:
        recipe = get_recipe_by_id(cur, recipe_id)
        recipe_cache[recipe_id] = recipe
    if not recipe:
        return None

    batches = to_float(required_batches)
    if batches <= 0:
        yield_qty = to_float(recipe.get('yield_qty'))
        if yield_qty > 0 and to_float(required_qty) > 0:
            required_qty_in_yield = to_float(required_qty)
            yield_unit = recipe.get('yield_unit') or required_unit
            if required_unit and yield_unit and required_unit != yield_unit:
                converted = convert_quantity_between_units(required_qty_in_yield, required_unit, yield_unit)
                if converted is not None:
                    required_qty_in_yield = converted
            batches = required_qty_in_yield / yield_qty
        else:
            batches = to_float(required_qty)
    if batches <= 0:
        return None

    components, _, _ = build_component_tree(
        cur,
        recipe_id,
        batches,
        0,
        set(),
        unit_system,
        apply_q_factor=False
    )

    ingredient_totals = {}
    collect_direct_ingredients_for_prep(components, ingredient_totals)
    ingredient_rows = []
    for (ing_id, unit), qty in ingredient_totals.items():
        display = smart_quantity(qty, unit, unit_system)
        ingredient_rows.append({
            'ingredient_id': ing_id,
            'name': ingredient_name_map.get(ing_id, 'Unknown'),
            'quantity': qty,
            'unit': unit,
            'display_quantity': display.get('quantity'),
            'display_unit': display.get('unit')
        })
    ingredient_rows.sort(key=lambda item: (item.get('name') or '').lower())

    subrecipe_totals = {}
    collect_direct_subrecipes_for_prep(components, subrecipe_totals)
    subrecipe_rows = []
    child_cards = []
    if len(ancestry) < max_depth:
        for (child_recipe_id, child_unit), child_qty in subrecipe_totals.items():
            if not child_recipe_id:
                continue
            child_recipe = recipe_cache.get(child_recipe_id)
            if child_recipe is None:
                child_recipe = get_recipe_by_id(cur, child_recipe_id)
                recipe_cache[child_recipe_id] = child_recipe
            if not child_recipe:
                continue

            child_yield_qty = to_float(child_recipe.get('yield_qty'))
            child_yield_unit = child_recipe.get('yield_unit') or child_unit
            child_required_qty_in_yield = child_qty
            if child_unit and child_yield_unit and child_unit != child_yield_unit:
                converted = convert_quantity_between_units(child_qty, child_unit, child_yield_unit)
                if converted is not None:
                    child_required_qty_in_yield = converted
            child_required_batches = (child_required_qty_in_yield / child_yield_qty) if child_yield_qty > 0 else child_required_qty_in_yield
            display = smart_quantity(child_qty, child_unit, unit_system)
            subrecipe_rows.append({
                'recipe_id': child_recipe_id,
                'recipe_name': child_recipe.get('name') or 'Sub-recipe',
                'required_qty': child_qty,
                'required_unit': child_unit,
                'display_required': display,
                'required_batches': child_required_batches,
                'yield_qty': child_yield_qty,
                'yield_unit': child_yield_unit
            })

            if child_recipe_id in ancestry:
                continue
            child_card = build_banquet_recipe_detail_card(
                cur,
                child_recipe_id,
                child_qty,
                child_unit,
                child_required_batches,
                ingredient_name_map=ingredient_name_map,
                unit_system=unit_system,
                recipe_cache=recipe_cache,
                ancestry=ancestry | {recipe_id, child_recipe_id},
                max_depth=max_depth
            )
            if child_card:
                child_cards.append(child_card)
    subrecipe_rows.sort(key=lambda item: (item.get('recipe_name') or '').lower())

    display_required = smart_quantity(required_qty, required_unit, unit_system)
    if (display_required.get('quantity') in ('', '0')) and to_float(required_qty) <= 0:
        yield_qty = to_float(recipe.get('yield_qty'))
        display_required = smart_quantity(
            (yield_qty * batches) if yield_qty > 0 else batches,
            recipe.get('yield_unit') or required_unit,
            unit_system
        )

    return {
        'recipe_id': recipe_id,
        'recipe_name': recipe.get('name') or 'Recipe',
        'recipe_type': normalize_recipe_type(recipe.get('recipe_type')),
        'display_required': display_required,
        'required_batches': batches,
        'ingredient_rows': ingredient_rows,
        'subrecipe_rows': subrecipe_rows,
        'instruction_steps': split_instruction_steps(recipe.get('instructions')),
        'child_cards': child_cards
    }

def build_banquet_datasets(cur, start_date, end_date, venue_id='', unit_system='imperial'):
    rows = fetch_banquet_event_lines(cur, start_date, end_date, venue_id)
    events_map = {}
    ingredient_totals = {}
    ingredient_usage = defaultdict(set)
    batch_usage_qty = {}
    batch_usage_events = defaultdict(set)
    batch_usage_menu_items = defaultdict(set)
    event_daily_ingredients = defaultdict(lambda: defaultdict(float))
    menu_item_execution_totals = {}

    # Keep prep/shopping usable for older event lines that lost menu_item_id
    # by resolving from menu-item name within the same venue.
    unresolved_name_keys = defaultdict(list)
    for row in rows:
        effective_menu_item_id = row.get('menu_item_id')
        row['_effective_menu_item_id'] = effective_menu_item_id
        if effective_menu_item_id:
            continue
        menu_name_key = clean_menu_text(row.get('menu_item_name') or '').lower()
        if not menu_name_key:
            continue
        unresolved_name_keys[(row.get('venue_id') or '', menu_name_key)].append(row)

    if unresolved_name_keys:
        name_keys = sorted({name_key for _, name_key in unresolved_name_keys.keys()})
        cur.execute("""
            SELECT id, venue_id, LOWER(TRIM(name)) AS menu_name_key
            FROM banquet_menu_items
            WHERE LOWER(TRIM(name)) = ANY(%s)
        """, (name_keys,))
        candidates_by_name = defaultdict(list)
        for candidate in cur.fetchall():
            key = candidate.get('menu_name_key') or ''
            candidates_by_name[key].append(candidate)

        for (target_venue_id, menu_name_key), target_rows in unresolved_name_keys.items():
            candidates = candidates_by_name.get(menu_name_key, [])
            resolved = None
            if target_venue_id:
                resolved = next((item for item in candidates if item.get('venue_id') == target_venue_id), None)
            if not resolved:
                resolved = next((item for item in candidates if not item.get('venue_id')), None)
            if not resolved and candidates:
                resolved = candidates[0]
            if resolved:
                resolved_id = resolved.get('id')
                for row in target_rows:
                    row['_effective_menu_item_id'] = resolved_id

    menu_item_ids = list({row.get('_effective_menu_item_id') for row in rows if row.get('_effective_menu_item_id')})
    additional_ingredients = collect_banquet_additional_ingredients(cur, menu_item_ids)
    menu_item_recipe_components = collect_banquet_recipe_components(cur, menu_item_ids)

    for row in rows:
        event_id = row['event_id']
        if event_id not in events_map:
            events_map[event_id] = {
                'id': event_id,
                'name': row.get('event_name'),
                'event_date': row.get('event_date'),
                'guests': row.get('guests'),
                'venue_name': row.get('venue_name'),
                'building': row.get('building'),
                'room': row.get('room'),
                'service_timing': row.get('service_timing'),
                'dietary_notes': row.get('dietary_notes'),
                'notes': row.get('event_notes'),
                'status': row.get('status') or 'planning',
                'lines': [],
                'line_count': 0
            }

        if not row.get('line_id'):
            continue

        recipe = None
        if row.get('recipe_id'):
            recipe = {
                'id': row.get('recipe_id'),
                'name': row.get('recipe_name') or row.get('menu_item_name'),
                'yield_qty': row.get('yield_qty'),
                'yield_unit': row.get('yield_unit')
            }

        effective_menu_item_id = row.get('_effective_menu_item_id') or row.get('menu_item_id')

        line_recipes = []
        for component in menu_item_recipe_components.get(effective_menu_item_id, []):
            component_unit = component.get('unit') or component.get('yield_unit')
            line_quantity_unit = normalize_unit(row.get('quantity_unit')) or normalize_count_unit(row.get('quantity_unit')) or clean_menu_text(row.get('quantity_unit'))
            if (
                row.get('is_event_only')
                and normalize_recipe_type(component.get('recipe_type')) == 'batch'
                and to_float(component.get('quantity')) == 1
                and normalize_count_unit(component_unit) == 'each'
                and line_quantity_unit
                and not normalize_count_unit(line_quantity_unit)
            ):
                # Older event-only links stored batch recipes as "1 serving", which turns
                # output quantities like "4 fl oz" into whole-batch multipliers.
                component_unit = line_quantity_unit
            line_recipes.append({
                'id': component.get('recipe_id'),
                'name': component.get('recipe_name'),
                'yield_qty': component.get('yield_qty'),
                'yield_unit': component.get('yield_unit'),
                'quantity': to_float(component.get('quantity')),
                'unit': component_unit,
                'recipe_type': component.get('recipe_type'),
                'choice_group': component.get('choice_group'),
                'choice_weight_percent': to_float(component.get('choice_weight_percent'))
            })
        if not line_recipes and recipe:
            line_recipes = [dict(recipe)]

        line_choice_selections = parse_event_line_choice_selections(row.get('line_choice_selections'))
        line_choice_count_by_group_recipe = defaultdict(float)
        line_choice_count_by_recipe = defaultdict(float)
        for choice in line_choice_selections:
            recipe_id = clean_menu_text(choice.get('recipe_id'))
            if not recipe_id:
                continue
            count = max(0.0, to_float(choice.get('count')))
            if count <= 0:
                continue
            group_name = clean_menu_text(choice.get('choice_group') or '')
            line_choice_count_by_recipe[recipe_id] += count
            if group_name:
                line_choice_count_by_group_recipe[(group_name, recipe_id)] += count

        choice_group_rows = defaultdict(list)
        for line_recipe in line_recipes:
            group_name = clean_menu_text(line_recipe.get('choice_group') or '')
            if group_name:
                choice_group_rows[group_name].append(line_recipe)

        choice_recipe_occurrences = defaultdict(int)
        for group_rows in choice_group_rows.values():
            for item in group_rows:
                recipe_id = item.get('id')
                if recipe_id:
                    choice_recipe_occurrences[recipe_id] += 1

        choice_multipliers = {}
        for group_name, group_rows in choice_group_rows.items():
            selected_total = 0.0
            has_selected_counts = False
            for item in group_rows:
                recipe_id = item.get('id')
                explicit_count = line_choice_count_by_group_recipe.get((group_name, recipe_id))
                if explicit_count <= 0 and choice_recipe_occurrences.get(recipe_id, 0) == 1:
                    explicit_count = line_choice_count_by_recipe.get(recipe_id)
                if explicit_count > 0:
                    selected_total += explicit_count
                    has_selected_counts = True

            if has_selected_counts and selected_total > 0:
                for item in group_rows:
                    recipe_id = item.get('id')
                    selected_count = line_choice_count_by_group_recipe.get((group_name, recipe_id))
                    if selected_count <= 0 and choice_recipe_occurrences.get(recipe_id, 0) == 1:
                        selected_count = line_choice_count_by_recipe.get(recipe_id)
                    choice_multipliers[id(item)] = max(0.0, selected_count) / selected_total
                continue

            total_weight = sum(max(0.0, to_float(item.get('choice_weight_percent'))) for item in group_rows)
            if total_weight <= 0:
                equal_share = 1.0 / len(group_rows) if group_rows else 0.0
                for item in group_rows:
                    choice_multipliers[id(item)] = equal_share
            else:
                for item in group_rows:
                    choice_multipliers[id(item)] = max(0.0, to_float(item.get('choice_weight_percent'))) / total_weight

        line = {
            'id': row.get('line_id'),
            'menu_item_id': effective_menu_item_id,
            'menu_item_name': row.get('menu_item_name') or row.get('recipe_name') or 'Menu Item',
            'menu_descriptor': row.get('menu_descriptor') or row.get('recipe_descriptor'),
            'menu_section': row.get('menu_section') or 'Uncategorized',
            'quantity': to_float(row.get('quantity')),
            'quantity_unit': row.get('quantity_unit') or row.get('yield_unit') or 'each',
            'base_yield_qty': to_float(row.get('base_yield_qty')) or 1,
            'base_yield_unit': row.get('base_yield_unit') or 'each',
            'is_event_only': bool(row.get('is_event_only')),
            'notes': row.get('line_notes'),
            'choice_selections': line_choice_selections,
            'recipe': line_recipes[0] if line_recipes else recipe,
            'recipes': line_recipes
        }
        line['ratio'] = None
        line['estimated_cost_total'] = None
        line['estimated_cost_per_unit'] = None
        line['components'] = []
        line['recipe_requirements'] = []
        line['additional_ingredients'] = []
        line['additional_ingredient_cost'] = 0

        execution_group_key = (
            effective_menu_item_id or line.get('menu_item_name') or 'menu_item',
            line.get('menu_item_name') or 'Menu Item'
        )
        if execution_group_key not in menu_item_execution_totals:
            menu_item_execution_totals[execution_group_key] = {
                'menu_item_id': effective_menu_item_id,
                'menu_item_name': line.get('menu_item_name') or 'Menu Item',
                'menu_descriptor': line.get('menu_descriptor'),
                'menu_section': line.get('menu_section') or 'Uncategorized',
                'ingredient_totals': {},
                'recipe_totals': {},
                'event_ids': set(),
                'service_rows': [],
                'line_notes': set()
            }
        execution_group = menu_item_execution_totals[execution_group_key]
        if not execution_group.get('menu_descriptor') and line.get('menu_descriptor'):
            execution_group['menu_descriptor'] = line.get('menu_descriptor')
        if (not execution_group.get('menu_section') or execution_group.get('menu_section') == 'Uncategorized') and line.get('menu_section'):
            execution_group['menu_section'] = line.get('menu_section')
        execution_group['event_ids'].add(event_id)
        execution_group['service_rows'].append({
            'event_id': event_id,
            'event_name': row.get('event_name'),
            'event_date': row.get('event_date'),
            'guests': row.get('guests'),
            'service_timing': row.get('service_timing'),
            'quantity': line.get('quantity'),
            'quantity_unit': line.get('quantity_unit'),
            'notes': line.get('notes')
        })
        if line.get('notes'):
            execution_group['line_notes'].add(line.get('notes'))

        line_cost_total = 0
        line_multiplier = menu_line_base_multiplier(
            line.get('quantity'),
            line.get('quantity_unit'),
            line.get('base_yield_qty'),
            line.get('base_yield_unit')
        )
        line['ratio'] = line_multiplier

        for line_recipe in line_recipes:
            ratio_per_menu_unit = ratio_from_line_quantity(
                {
                    'quantity': line_recipe.get('quantity', line.get('base_yield_qty')),
                    'quantity_unit': line_recipe.get('unit', line_recipe.get('yield_unit'))
                },
                line_recipe
            )
            group_multiplier = choice_multipliers.get(id(line_recipe), 1.0)
            total_ratio = ratio_per_menu_unit * line_multiplier * group_multiplier
            line_cost_total += get_recipe_total_cost(cur, line_recipe['id'], unit_system, apply_q_factor=True) * total_ratio

            recipe_yield_qty = to_float(line_recipe.get('yield_qty'))
            required_output_qty = (recipe_yield_qty * total_ratio) if recipe_yield_qty > 0 else total_ratio
            required_output_unit = line_recipe.get('yield_unit') or line_recipe.get('unit') or ''
            display_required = smart_quantity(required_output_qty, required_output_unit, unit_system)
            root_key = (line_recipe.get('id'), required_output_unit)
            batch_usage_qty[root_key] = batch_usage_qty.get(root_key, 0) + required_output_qty
            if event_id:
                batch_usage_events[line_recipe.get('id')].add(event_id)
            if line.get('menu_item_name'):
                batch_usage_menu_items[line_recipe.get('id')].add(line['menu_item_name'])
            line['recipe_requirements'].append({
                'recipe_id': line_recipe.get('id'),
                'recipe_name': line_recipe.get('name') or 'Recipe',
                'recipe_type': normalize_recipe_type(line_recipe.get('recipe_type')),
                'required_qty': required_output_qty,
                'required_unit': required_output_unit,
                'required_batches': total_ratio,
                'display_required': display_required
            })

            execution_recipe_key = (line_recipe.get('id'), required_output_unit or line_recipe.get('yield_unit') or '')
            execution_recipe_row = execution_group['recipe_totals'].get(execution_recipe_key)
            if not execution_recipe_row:
                execution_recipe_row = {
                    'recipe_id': line_recipe.get('id'),
                    'recipe_name': line_recipe.get('name') or 'Recipe',
                    'recipe_type': normalize_recipe_type(line_recipe.get('recipe_type')),
                    'required_qty': 0,
                    'required_unit': required_output_unit,
                    'required_batches': 0,
                    'event_ids': set()
                }
                execution_group['recipe_totals'][execution_recipe_key] = execution_recipe_row
            execution_recipe_row['required_qty'] = to_float(execution_recipe_row.get('required_qty')) + required_output_qty
            execution_recipe_row['required_batches'] = to_float(execution_recipe_row.get('required_batches')) + total_ratio
            execution_recipe_row['event_ids'].add(event_id)

            components, _, _ = build_component_tree(cur, line_recipe['id'], total_ratio, 0, set(), unit_system, apply_q_factor=False)
            if components:
                line['components'].extend(components)
            collect_ingredients_from_components(components, ingredient_totals)
            collect_ingredients_from_components(components, event_daily_ingredients[event_id])
            collect_batch_recipe_usage_from_components(
                components,
                batch_usage_qty,
                event_usage_map=batch_usage_events,
                menu_item_usage_map=batch_usage_menu_items,
                event_id=event_id,
                menu_item_name=line['menu_item_name']
            )

            line_label = line['menu_item_name']
            collect_ingredient_usage_from_components(components, line_label, ingredient_usage)

        for extra in additional_ingredients.get(effective_menu_item_id, []):
            ingredient_id = extra.get('ingredient_id')
            if not ingredient_id:
                continue
            per_unit_qty = to_float(extra.get('quantity'))
            extra_qty = per_unit_qty * line_multiplier
            unit = (extra.get('unit') or extra.get('ingredient_unit') or '').strip()
            key = (ingredient_id, unit)
            ingredient_totals[key] = ingredient_totals.get(key, 0) + extra_qty
            event_daily_ingredients[event_id][key] += extra_qty
            ingredient_usage[ingredient_id].add(line.get('menu_item_name') or 'Menu Item')
            converted_cost = convert_cost_per_unit(
                extra.get('cost_per_unit'),
                extra.get('ingredient_unit'),
                unit
            )
            ext_cost = extra_qty * converted_cost if converted_cost else 0
            line_cost_total += ext_cost
            line['additional_ingredient_cost'] += ext_cost
            line['additional_ingredients'].append({
                'ingredient_id': ingredient_id,
                'name': extra.get('ingredient_name'),
                'quantity': extra_qty,
                'unit': unit,
                'ext_cost': ext_cost
            })
            ingredient_key = (ingredient_id, unit, extra.get('ingredient_name') or 'Unknown')
            ingredient_row = execution_group['ingredient_totals'].get(ingredient_key)
            if not ingredient_row:
                ingredient_row = {
                    'ingredient_id': ingredient_id,
                    'name': extra.get('ingredient_name') or 'Unknown',
                    'quantity': 0,
                    'unit': unit
                }
                execution_group['ingredient_totals'][ingredient_key] = ingredient_row
            ingredient_row['quantity'] = to_float(ingredient_row.get('quantity')) + extra_qty

        line['estimated_cost_total'] = line_cost_total
        line['estimated_cost_per_unit'] = line_cost_total / line['quantity'] if line['quantity'] > 0 else None

        events_map[event_id]['lines'].append(line)
        events_map[event_id]['line_count'] += 1

    events = sorted(events_map.values(), key=lambda item: ((item.get('event_date') or date.today()), (item.get('name') or '').lower()))

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
                SELECT id, name, category, yield_qty, yield_unit, instructions, recipe_type
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
                'required_batches': required_batches,
                'required_qty': required_qty_in_yield,
                'required_unit': yield_unit or qty_unit,
                'display_required': smart_quantity(required_qty_in_yield, yield_unit or qty_unit, unit_system),
                'yield_qty': yield_qty,
                'yield_unit': yield_unit,
                'ingredient_rows': ingredient_rows,
                'subrecipe_rows': subrecipe_rows,
                'instructions': recipe.get('instructions'),
                'used_in_events': sorted(batch_usage_events.get(recipe_id, set())),
                'used_in_menu_items': sorted(batch_usage_menu_items.get(recipe_id, set())),
                'estimated_cost': total_cost
            })
    for prep in weekly_prep:
        prep_family = derive_prep_family_label(prep.get('recipe_name'))
        prep['prep_family'] = prep_family
        prep['prep_family_key'] = normalize_match_key(prep_family)
    weekly_prep.sort(key=lambda item: (item.get('prep_family_key') or '', (item.get('recipe_name') or '').lower()))

    ingredient_name_map = {
        row.get('id'): row.get('name')
        for row in ingredient_master
        if row.get('id')
    }
    recipe_detail_cache = {}
    kitchen_packets = []
    weekly_menu_pulls = []
    for group in menu_item_execution_totals.values():
        ingredient_rows = []
        for row in group['ingredient_totals'].values():
            display = smart_quantity(row.get('quantity'), row.get('unit'), unit_system)
            ingredient_rows.append({
                'ingredient_id': row.get('ingredient_id'),
                'name': row.get('name'),
                'quantity': row.get('quantity'),
                'unit': row.get('unit'),
                'display_quantity': display.get('quantity'),
                'display_unit': display.get('unit')
            })
        ingredient_rows.sort(key=lambda item: (item.get('name') or '').lower())
        if ingredient_rows:
            weekly_menu_pulls.append({
                'menu_item_name': group.get('menu_item_name') or 'Menu Item',
                'ingredient_rows': ingredient_rows,
                'used_in_events': sorted(group.get('event_ids') or set())
            })

        recipe_rows = []
        recipe_cards = []
        for row in group['recipe_totals'].values():
            display_required = smart_quantity(row.get('required_qty'), row.get('required_unit'), unit_system)
            recipe_row = {
                'recipe_id': row.get('recipe_id'),
                'recipe_name': row.get('recipe_name') or 'Recipe',
                'recipe_type': row.get('recipe_type'),
                'required_qty': row.get('required_qty'),
                'required_unit': row.get('required_unit'),
                'required_batches': row.get('required_batches'),
                'display_required': display_required,
                'used_in_events': sorted(row.get('event_ids') or set())
            }
            recipe_rows.append(recipe_row)

            recipe_card = build_banquet_recipe_detail_card(
                cur,
                row.get('recipe_id'),
                row.get('required_qty'),
                row.get('required_unit'),
                row.get('required_batches'),
                ingredient_name_map=ingredient_name_map,
                unit_system=unit_system,
                recipe_cache=recipe_detail_cache,
                ancestry={row.get('recipe_id')} if row.get('recipe_id') else set()
            )
            if recipe_card:
                recipe_cards.append(recipe_card)
        recipe_rows.sort(key=lambda item: (item.get('recipe_name') or '').lower())
        recipe_cards.sort(key=lambda item: (item.get('recipe_name') or '').lower())

        service_rows = sorted(
            group.get('service_rows') or [],
            key=lambda item: (
                item.get('event_date') or date.today(),
                (item.get('event_name') or '').lower(),
                item.get('quantity') or 0
            )
        )
        service_totals_by_unit = defaultdict(float)
        for service_row in service_rows:
            service_unit = service_row.get('quantity_unit') or 'each'
            service_totals_by_unit[service_unit] += to_float(service_row.get('quantity'))
        service_totals = []
        for service_unit, total_qty in service_totals_by_unit.items():
            display = smart_quantity(total_qty, service_unit, unit_system)
            service_totals.append({
                'quantity': total_qty,
                'unit': service_unit,
                'display_quantity': display.get('quantity'),
                'display_unit': display.get('unit')
            })
        service_totals.sort(key=lambda item: (item.get('unit') or '').lower())
        service_total_label = ' · '.join(
            f"{row.get('display_quantity')} {row.get('display_unit')}".strip()
            for row in service_totals
            if row.get('display_quantity') not in (None, '')
        )

        kitchen_packets.append({
            'menu_item_id': group.get('menu_item_id'),
            'menu_item_name': group.get('menu_item_name') or 'Menu Item',
            'menu_descriptor': group.get('menu_descriptor'),
            'menu_section': group.get('menu_section') or 'Uncategorized',
            'ingredient_rows': ingredient_rows,
            'recipe_rows': recipe_rows,
            'recipe_cards': recipe_cards,
            'service_rows': service_rows,
            'service_totals': service_totals,
            'service_total_label': service_total_label or '—',
            'used_in_events': sorted(group.get('event_ids') or set()),
            'line_notes': sorted(group.get('line_notes') or set())
        })
    weekly_menu_pulls.sort(key=lambda item: (item.get('menu_item_name') or '').lower())
    kitchen_packets.sort(key=lambda item: ((item.get('menu_section') or '').lower(), (item.get('menu_item_name') or '').lower()))

    daily_groups = []
    by_day = defaultdict(list)
    for event in events:
        by_day[event.get('event_date')].append(event)
    for day in sorted(by_day.keys()):
        day_events = by_day[day]
        for event in day_events:
            ingredient_rows = []
            for (ing_id, unit), qty in event_daily_ingredients.get(event['id'], {}).items():
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
            event['daily_ingredients'] = ingredient_rows
        daily_groups.append({'date': day, 'events': day_events})

    total_menu_lines = sum(event.get('line_count', 0) for event in events)
    total_estimated_cost = sum(sum(line.get('estimated_cost_total', 0) for line in event.get('lines', [])) for event in events)

    return {
        'events': events,
        'daily_groups': daily_groups,
        'shopping_ingredients': ingredient_master,
        'shopping_total_cost': ingredient_total_cost,
        'weekly_prep': weekly_prep,
        'weekly_menu_pulls': weekly_menu_pulls,
        'kitchen_packets': kitchen_packets,
        'event_count': len(events),
        'menu_line_count': total_menu_lines,
        'total_estimated_cost': total_estimated_cost
    }

def build_banquet_prep_groups(cur, datasets, unit_system='imperial'):
    weekly_prep = datasets.get('weekly_prep') or []
    if not weekly_prep:
        return []

    prep_by_recipe = {}
    for prep in weekly_prep:
        recipe_id = prep.get('recipe_id')
        if recipe_id and recipe_id not in prep_by_recipe:
            prep_by_recipe[recipe_id] = dict(prep)

    root_recipe_sequence = []
    root_recipe_labels = defaultdict(list)
    for event in datasets.get('events', []):
        for line in event.get('lines', []):
            for recipe in line.get('recipes', []) or []:
                recipe_id = recipe.get('id')
                if not recipe_id:
                    continue
                if recipe_id not in root_recipe_sequence:
                    root_recipe_sequence.append(recipe_id)
                line_label = clean_menu_text(line.get('menu_item_name') or recipe.get('name') or '')
                if line_label and line_label not in root_recipe_labels[recipe_id]:
                    root_recipe_labels[recipe_id].append(line_label)

    if not root_recipe_sequence:
        root_recipe_sequence = [prep.get('recipe_id') for prep in weekly_prep if prep.get('recipe_id')]

    stocked_root_recipe_ids = {recipe_id for recipe_id in root_recipe_sequence if recipe_id}
    root_recipe_name_map = {}
    for recipe_id in stocked_root_recipe_ids:
        prep_row = prep_by_recipe.get(recipe_id) or {}
        root_recipe_name_map[recipe_id] = prep_row.get('recipe_name') or 'Main prep card'

    ingredient_name_map = {
        row.get('id'): row.get('name')
        for row in datasets.get('shopping_ingredients', [])
        if row.get('id')
    }
    recipe_cache = {}

    def get_recipe_cached(recipe_id):
        if not recipe_id:
            return None
        if recipe_id not in recipe_cache:
            recipe_cache[recipe_id] = get_recipe_by_id(cur, recipe_id)
        return recipe_cache.get(recipe_id)

    def build_sub_card(recipe_id, required_qty, required_unit, required_batches, ancestry, group_root_recipe_id):
        if not recipe_id:
            return None
        recipe = get_recipe_cached(recipe_id)
        if not recipe:
            return None

        batches = to_float(required_batches)
        if batches <= 0:
            yield_qty = to_float(recipe.get('yield_qty'))
            if yield_qty > 0 and to_float(required_qty) > 0:
                required_qty_in_yield = to_float(required_qty)
                yield_unit = recipe.get('yield_unit') or required_unit
                if required_unit and yield_unit and required_unit != yield_unit:
                    converted = convert_quantity_between_units(required_qty_in_yield, required_unit, yield_unit)
                    if converted is not None:
                        required_qty_in_yield = converted
                batches = required_qty_in_yield / yield_qty
            else:
                batches = to_float(required_qty)
        if batches <= 0:
            return None

        components, _, _ = build_component_tree(
            cur,
            recipe_id,
            batches,
            0,
            set(),
            unit_system,
            apply_q_factor=False
        )

        ingredient_totals = {}
        collect_direct_ingredients_for_prep(components, ingredient_totals)
        ingredient_rows = []
        for (ing_id, unit), qty in ingredient_totals.items():
            display = smart_quantity(qty, unit, unit_system)
            ingredient_rows.append({
                'ingredient_id': ing_id,
                'name': ingredient_name_map.get(ing_id, 'Unknown'),
                'quantity': qty,
                'unit': unit,
                'display_quantity': display.get('quantity'),
                'display_unit': display.get('unit')
            })
        ingredient_rows.sort(key=lambda item: (item.get('name') or '').lower())

        subrecipe_totals = {}
        collect_direct_subrecipes_for_prep(components, subrecipe_totals)
        subrecipe_rows = []
        for (child_recipe_id, child_unit), child_qty in subrecipe_totals.items():
            child_recipe = get_recipe_cached(child_recipe_id)
            if not child_recipe:
                continue
            child_yield_qty = to_float(child_recipe.get('yield_qty'))
            child_yield_unit = child_recipe.get('yield_unit') or child_unit
            child_required_qty_in_yield = child_qty
            if child_unit and child_yield_unit and child_unit != child_yield_unit:
                converted = convert_quantity_between_units(child_qty, child_unit, child_yield_unit)
                if converted is not None:
                    child_required_qty_in_yield = converted
            child_required_batches = (child_required_qty_in_yield / child_yield_qty) if child_yield_qty > 0 else child_required_qty_in_yield
            covered_by_parent_recipe = child_recipe_id in printed_recipe_owner and printed_recipe_owner.get(child_recipe_id) != group_root_recipe_id
            subrecipe_rows.append({
                'recipe_id': child_recipe_id,
                'recipe_name': child_recipe.get('name') or 'Sub-recipe',
                'required_qty': child_qty,
                'required_unit': child_unit,
                'display_required': smart_quantity(child_qty, child_unit, unit_system),
                'required_batches': child_required_batches,
                'covered_by_parent_recipe': covered_by_parent_recipe,
                'covered_by_recipe_name': root_recipe_name_map.get(printed_recipe_owner.get(child_recipe_id)) if covered_by_parent_recipe else None
            })
        subrecipe_rows.sort(key=lambda item: (item.get('recipe_name') or '').lower())

        child_cards = []
        if len(ancestry) < 4:
            for sub_row in subrecipe_rows:
                child_recipe_id = sub_row.get('recipe_id')
                if not child_recipe_id or child_recipe_id in ancestry:
                    continue
                if sub_row.get('covered_by_parent_recipe'):
                    continue
                child_card = build_sub_card(
                    child_recipe_id,
                    sub_row.get('required_qty'),
                    sub_row.get('required_unit'),
                    sub_row.get('required_batches'),
                    ancestry | {child_recipe_id},
                    group_root_recipe_id
                )
                if child_card:
                    child_cards.append(child_card)

        display_required = smart_quantity(required_qty, required_unit, unit_system)
        if (display_required.get('quantity') in ('', '0')) and to_float(required_qty) <= 0:
            yield_qty = to_float(recipe.get('yield_qty'))
            display_required = smart_quantity(yield_qty * batches if yield_qty > 0 else batches, recipe.get('yield_unit') or required_unit, unit_system)

        return {
            'recipe_id': recipe_id,
            'recipe_name': recipe.get('name') or 'Sub-recipe',
            'display_required': display_required,
            'required_batches': batches,
            'ingredient_rows': ingredient_rows,
            'subrecipe_rows': subrecipe_rows,
            'instruction_steps': split_instruction_steps(recipe.get('instructions')),
            'child_cards': child_cards
        }

    def mark_tree_owner(card, owner_recipe_id):
        recipe_id = card.get('recipe_id')
        if recipe_id and recipe_id not in printed_recipe_owner:
            printed_recipe_owner[recipe_id] = owner_recipe_id
        for child in card.get('child_cards') or []:
            mark_tree_owner(child, owner_recipe_id)

    printed_recipe_owner = {}
    groups = []
    for root_recipe_id in root_recipe_sequence:
        root_prep = prep_by_recipe.get(root_recipe_id)
        if not root_prep:
            continue
        if root_recipe_id in printed_recipe_owner:
            continue

        main_labels = (
            root_recipe_labels.get(root_recipe_id)
            or root_prep.get('used_in_menu_items')
            or [root_prep.get('recipe_name')]
        )
        main_labels = [label for label in main_labels if label]
        main_label = main_labels[0] if main_labels else (root_prep.get('recipe_name') or 'Prep Item')

        root_prep['instruction_steps'] = split_instruction_steps(root_prep.get('instructions'))
        root_subrecipe_rows = []
        sub_cards = []

        for sub_row in root_prep.get('subrecipe_rows', []) or []:
            sub_recipe_id = sub_row.get('recipe_id')
            required_batches = to_float(sub_row.get('required_batches'))
            if not sub_recipe_id or required_batches <= 0:
                continue

            covered_by_parent_recipe = sub_recipe_id in printed_recipe_owner and printed_recipe_owner.get(sub_recipe_id) != root_recipe_id
            enriched_row = dict(sub_row)
            enriched_row['covered_by_parent_recipe'] = covered_by_parent_recipe
            enriched_row['covered_by_recipe_name'] = root_recipe_name_map.get(printed_recipe_owner.get(sub_recipe_id)) if covered_by_parent_recipe else None
            root_subrecipe_rows.append(enriched_row)
            if covered_by_parent_recipe:
                continue

            sub_card = build_sub_card(
                sub_recipe_id,
                sub_row.get('required_qty'),
                sub_row.get('required_unit'),
                required_batches,
                {root_recipe_id, sub_recipe_id},
                root_recipe_id
            )
            if not sub_card:
                continue
            if sub_row.get('display_required'):
                sub_card['display_required'] = sub_row.get('display_required')
            sub_cards.append(sub_card)

        root_prep['subrecipe_rows'] = root_subrecipe_rows
        printed_recipe_owner[root_recipe_id] = root_recipe_id
        for card in sub_cards:
            mark_tree_owner(card, root_recipe_id)

        groups.append({
            'main_label': main_label,
            'main_labels': main_labels,
            'root': root_prep,
            'sub_cards': sub_cards
        })

    return groups

__all__ = [
    'get_banquet_date_window',
    'PREP_FAMILY_ADJECTIVE_STOPWORDS',
    'PREP_FAMILY_FORM_STOPWORDS',
    'derive_prep_family_label',
    'BANQUET_ACTIVE_STATUSES',
    'BANQUET_VENUE_ID',
    'BANQUET_BEO_MAX_BYTES',
    'BANQUET_BEO_UPLOAD_ROOT',
    'BANQUET_STATUS_CHOICES',
    'resolve_banquet_venue',
    'banquet_tables_ready',
    'ensure_banquet_menu_item_event_only_column',
    'banquet_shopping_checks_ready',
    'auto_complete_past_banquet_events',
    'banquet_event_beo_files_ready',
    'ensure_banquet_event_beo_files_table',
    'safe_pdf_filename',
    'event_beo_file_path',
    'list_banquet_event_beo_files',
    'list_banquet_event_beo_files_by_events',
    'get_banquet_event_beo_file',
    'store_banquet_event_beo_file',
    'ensure_banquet_shopping_checks_table',
    'load_banquet_shopping_checklist',
    'list_banquet_menu_items',
    'get_banquet_menu_item',
    'get_banquet_menu_item_recipe_components',
    'collect_banquet_recipe_components',
    'get_banquet_menu_item_component_summaries',
    'get_banquet_menu_item_ingredients',
    'get_banquet_event',
    'get_banquet_event_lines',
    'resolve_or_create_banquet_menu_item',
    'upsert_menu_item_recipe_link',
    'make_unique_banquet_menu_item_name',
    'clone_banquet_menu_item',
    'normalize_event_line_choice_selections',
    'parse_event_line_choice_selections',
    'parse_event_recipe_lines',
    'parse_menu_item_ingredient_lines',
    'parse_menu_item_recipe_lines',
    'collect_banquet_additional_ingredients',
    'fetch_banquet_event_lines',
    'build_banquet_datasets',
    'build_banquet_prep_groups',
]
