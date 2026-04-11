from helpers.db_helpers import db_table_exists

FORECASTING_CATEGORY_OPTIONS = [
    'Appetizers',
    'Soups & Salads',
    'Burgers',
    'Sandwiches',
    'Entrees',
    'Sides',
    'Desserts',
    'Kids',
    'Specials',
]


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
            item.id,
            item.name,
            COALESCE(NULLIF(TRIM(item.category), ''), 'Uncategorized') AS category,
            item.venue_id,
            item.description,
            item.recipe_id,
            COALESCE(item.sort_order, 0) AS sort_order,
            item.active,
            item.created_at,
            item.updated_at,
            recipe.name AS recipe_name,
            recipe.recipe_type,
            recipe.category AS recipe_category,
            recipe.yield_qty,
            recipe.yield_unit,
            venue.name AS venue_name
        FROM forecasting_menu_items item
        LEFT JOIN recipes recipe ON recipe.id = item.recipe_id
        LEFT JOIN venues venue ON venue.id = item.venue_id
        WHERE item.id = %s
        LIMIT 1
        """,
        (item_id,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def list_forecasting_menu_items(cur, venue_id=''):
    cur.execute(
        """
        SELECT
            item.id,
            item.name,
            COALESCE(NULLIF(TRIM(item.category), ''), 'Uncategorized') AS category,
            item.venue_id,
            item.description,
            item.recipe_id,
            COALESCE(item.sort_order, 0) AS sort_order,
            item.active,
            item.created_at,
            item.updated_at,
            recipe.name AS recipe_name,
            recipe.recipe_type,
            recipe.yield_qty,
            recipe.yield_unit,
            venue.name AS venue_name
        FROM forecasting_menu_items item
        LEFT JOIN recipes recipe ON recipe.id = item.recipe_id
        LEFT JOIN venues venue ON venue.id = item.venue_id
        WHERE item.active = TRUE
          AND (%s = '' OR item.venue_id = %s)
        ORDER BY
            COALESCE(NULLIF(TRIM(item.category), ''), 'Uncategorized') ASC,
            COALESCE(item.sort_order, 0) ASC,
            LOWER(item.name) ASC
        """,
        (venue_id or '', venue_id or ''),
    )
    return [dict(row) for row in cur.fetchall()]


def summarize_forecasting_menu_items(cur, venue_id=''):
    cur.execute(
        """
        SELECT
            COUNT(*) AS total_items,
            COUNT(*) FILTER (WHERE recipe_id IS NOT NULL) AS items_with_recipe,
            COUNT(*) FILTER (WHERE recipe_id IS NULL) AS items_missing_recipe
        FROM forecasting_menu_items
        WHERE active = TRUE
          AND (%s = '' OR venue_id = %s)
        """,
        (venue_id or '', venue_id or ''),
    )
    row = cur.fetchone() or {}
    total_items = int(row.get('total_items') or 0)
    items_with_recipe = int(row.get('items_with_recipe') or 0)
    items_missing_recipe = int(row.get('items_missing_recipe') or 0)
    completion_pct = int(round((items_with_recipe / total_items) * 100)) if total_items else 0
    return {
        'total_items': total_items,
        'items_with_recipe': items_with_recipe,
        'items_missing_recipe': items_missing_recipe,
        'completion_pct': completion_pct,
    }


def group_forecasting_items_by_category(items):
    grouped = {}
    for item in items or []:
        category = (item.get('category') or 'Uncategorized').strip() or 'Uncategorized'
        grouped.setdefault(category, []).append(item)
    return grouped


def get_forecasting_recipe(cur, recipe_id):
    if not recipe_id:
        return None

    if db_table_exists(cur, 'public.recipe_venues') and db_table_exists(cur, 'public.venues'):
        cur.execute(
            """
            SELECT
                recipe.id,
                recipe.name,
                recipe.recipe_type,
                recipe.category,
                COALESCE(string_agg(DISTINCT venue.name, ', ' ORDER BY venue.name), '') AS venue_names
            FROM recipes recipe
            LEFT JOIN recipe_venues link ON link.recipe_id = recipe.id
            LEFT JOIN venues venue ON venue.id = link.venue_id AND venue.active = TRUE
            WHERE recipe.id = %s
            GROUP BY recipe.id
            LIMIT 1
            """,
            (recipe_id,),
        )
    else:
        cur.execute(
            """
            SELECT
                id,
                name,
                recipe_type,
                category,
                '' AS venue_names
            FROM recipes
            WHERE id = %s
            LIMIT 1
            """,
            (recipe_id,),
        )

    row = cur.fetchone()
    return dict(row) if row else None


def search_forecasting_recipes(cur, query='', venue_id='', limit=20):
    capped_limit = min(max(int(limit or 20), 1), 100)
    like_query = f"%{escape_like(query or '')}%"

    if db_table_exists(cur, 'public.recipe_venues') and db_table_exists(cur, 'public.venues'):
        cur.execute(
            """
            SELECT
                recipe.id,
                recipe.name,
                recipe.recipe_type,
                recipe.category,
                COALESCE(string_agg(DISTINCT venue.name, ', ' ORDER BY venue.name), '') AS venue_names,
                CASE
                    WHEN %s <> ''
                     AND EXISTS (
                        SELECT 1
                        FROM recipe_venues priority_link
                        WHERE priority_link.recipe_id = recipe.id
                          AND priority_link.venue_id = %s
                    ) THEN 0
                    ELSE 1
                END AS venue_priority
            FROM recipes recipe
            LEFT JOIN recipe_venues link ON link.recipe_id = recipe.id
            LEFT JOIN venues venue ON venue.id = link.venue_id AND venue.active = TRUE
            WHERE (%s = '' OR recipe.name ILIKE %s ESCAPE '\\')
            GROUP BY recipe.id
            ORDER BY venue_priority ASC, LOWER(recipe.name) ASC
            LIMIT %s
            """,
            (venue_id or '', venue_id or '', query or '', like_query, capped_limit),
        )
    else:
        cur.execute(
            """
            SELECT
                id,
                name,
                recipe_type,
                category,
                '' AS venue_names,
                1 AS venue_priority
            FROM recipes
            WHERE (%s = '' OR name ILIKE %s ESCAPE '\\')
            ORDER BY LOWER(name) ASC
            LIMIT %s
            """,
            (query or '', like_query, capped_limit),
        )

    return [
        {
            'id': row.get('id'),
            'name': row.get('name'),
            'recipe_type': row.get('recipe_type'),
            'category': row.get('category'),
            'venue_names': row.get('venue_names') or '',
        }
        for row in cur.fetchall()
    ]


__all__ = [
    'FORECASTING_CATEGORY_OPTIONS',
    'ensure_forecasting_schema',
    'ensure_recipe_venue_link',
    'get_forecasting_menu_item',
    'list_forecasting_menu_items',
    'summarize_forecasting_menu_items',
    'group_forecasting_items_by_category',
    'get_forecasting_recipe',
    'search_forecasting_recipes',
]
