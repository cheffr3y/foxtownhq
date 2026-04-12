from helpers.db_helpers import db_table_exists


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
    return True


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


__all__ = [
    'ensure_forecasting_schema',
    'ensure_recipe_venue_link',
    'get_forecasting_menu_item',
    'get_existing_menu_items_by_recipe',
    'get_active_menu_recipe_ids',
    'group_forecasting_items_by_category',
    'list_forecasting_menu_items',
    'list_forecasting_recipes',
    'search_forecasting_recipes',
    'sync_forecasting_menu_items_for_recipe',
    'summarize_forecasting_items',
]
