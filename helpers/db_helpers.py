def db_table_exists(cur, table_name):
    cur.execute("SELECT to_regclass(%s) AS table_ref", (table_name,))
    row = cur.fetchone() or {}
    return bool(row.get('table_ref'))

def db_column_exists(cur, table_name, column_name):
    if '.' in table_name:
        schema_name, plain_table = table_name.split('.', 1)
    else:
        schema_name, plain_table = 'public', table_name
    cur.execute("""
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = %s
          AND column_name = %s
        LIMIT 1
    """, (schema_name, plain_table, column_name))
    return cur.fetchone() is not None

def ensure_banquet_menu_item_base_yield_columns(cur):
    """Backfill banquet menu item sold-as fields on older databases before save."""
    if not db_table_exists(cur, 'public.banquet_menu_items'):
        return False

    cur.execute("""
        ALTER TABLE banquet_menu_items
        ADD COLUMN IF NOT EXISTS base_yield_qty NUMERIC DEFAULT 1
    """)
    cur.execute("""
        ALTER TABLE banquet_menu_items
        ADD COLUMN IF NOT EXISTS base_yield_unit TEXT DEFAULT 'each'
    """)
    cur.execute("""
        UPDATE banquet_menu_items
        SET base_yield_qty = 1
        WHERE base_yield_qty IS NULL
    """)
    cur.execute("""
        UPDATE banquet_menu_items
        SET base_yield_unit = 'each'
        WHERE base_yield_unit IS NULL OR TRIM(base_yield_unit) = ''
    """)
    return True

__all__ = [
    'db_table_exists',
    'db_column_exists',
    'ensure_banquet_menu_item_base_yield_columns',
]
