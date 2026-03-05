from helpers.db_helpers import db_table_exists
from helpers.units import normalize_count_unit

def get_plated_recipes_for_venue(cur, venue_id='', include_unassigned=True):
    has_recipe_venues = db_table_exists(cur, 'public.recipe_venues') and db_table_exists(cur, 'public.venues')
    if has_recipe_venues:
        if include_unassigned:
            cur.execute("""
                SELECT r.id,
                       r.name,
                       r.recipe_type,
                       r.category,
                       r.yield_qty,
                       r.yield_unit,
                       r.menu_descriptor,
                       COALESCE(string_agg(DISTINCT v.name, ', ' ORDER BY v.name), '') AS venue_names
                FROM recipes r
                LEFT JOIN recipe_venues rv ON rv.recipe_id = r.id
                LEFT JOIN venues v ON v.id = rv.venue_id AND v.active = TRUE
                WHERE r.recipe_type = 'menu'
                  AND (
                        %s = '' OR
                        EXISTS (
                            SELECT 1
                            FROM recipe_venues rvf
                            WHERE rvf.recipe_id = r.id AND rvf.venue_id = %s
                        ) OR
                        NOT EXISTS (
                            SELECT 1
                            FROM recipe_venues rvn
                            WHERE rvn.recipe_id = r.id
                        )
                  )
                GROUP BY r.id
                ORDER BY r.name
            """, (venue_id, venue_id))
        else:
            cur.execute("""
                SELECT r.id,
                       r.name,
                       r.recipe_type,
                       r.category,
                       r.yield_qty,
                       r.yield_unit,
                       r.menu_descriptor,
                       COALESCE(string_agg(DISTINCT v.name, ', ' ORDER BY v.name), '') AS venue_names
                FROM recipes r
                LEFT JOIN recipe_venues rv ON rv.recipe_id = r.id
                LEFT JOIN venues v ON v.id = rv.venue_id AND v.active = TRUE
                WHERE r.recipe_type = 'menu'
                  AND (
                        %s = '' OR
                        EXISTS (
                            SELECT 1
                            FROM recipe_venues rvf
                            WHERE rvf.recipe_id = r.id AND rvf.venue_id = %s
                        )
                  )
                GROUP BY r.id
                ORDER BY r.name
            """, (venue_id, venue_id))
    else:
        cur.execute("""
            SELECT r.id,
                   r.name,
                   r.recipe_type,
                   r.category,
                   r.yield_qty,
                   r.yield_unit,
                   r.menu_descriptor,
                   '' AS venue_names
            FROM recipes r
            WHERE r.recipe_type = 'menu'
            ORDER BY r.name
        """)
    return cur.fetchall()

def get_component_recipes_for_venue(cur, venue_id=''):
    has_recipe_venues = db_table_exists(cur, 'public.recipe_venues') and db_table_exists(cur, 'public.venues')
    if has_recipe_venues:
        cur.execute("""
            SELECT r.id,
                   r.name,
                   r.recipe_type,
                   r.category,
                   r.yield_qty,
                   r.yield_unit,
                   r.menu_descriptor,
                   COALESCE(string_agg(DISTINCT v.name, ', ' ORDER BY v.name), '') AS venue_names
            FROM recipes r
            LEFT JOIN recipe_venues rv ON rv.recipe_id = r.id
            LEFT JOIN venues v ON v.id = rv.venue_id AND v.active = TRUE
            WHERE (
                %s = '' OR
                EXISTS (
                    SELECT 1
                    FROM recipe_venues rvf
                    WHERE rvf.recipe_id = r.id AND rvf.venue_id = %s
                ) OR
                NOT EXISTS (
                    SELECT 1
                    FROM recipe_venues rvn
                    WHERE rvn.recipe_id = r.id
                )
            )
            GROUP BY r.id
            ORDER BY CASE WHEN r.recipe_type = 'menu' THEN 0 WHEN r.recipe_type = 'batch' THEN 1 ELSE 2 END, r.name
        """, (venue_id, venue_id))
    else:
        cur.execute("""
            SELECT r.id,
                   r.name,
                   r.recipe_type,
                   r.category,
                   r.yield_qty,
                   r.yield_unit,
                   r.menu_descriptor,
                   '' AS venue_names
            FROM recipes r
            ORDER BY CASE WHEN r.recipe_type = 'menu' THEN 0 WHEN r.recipe_type = 'batch' THEN 1 ELSE 2 END, r.name
        """)
    options = [dict(row) for row in cur.fetchall()]
    for option in options:
        option['option_source'] = 'recipe'
        option['menu_item_name'] = None
        option['menu_item_base_yield_unit'] = None
        option['source_menu_item_id'] = None
        option['menu_item_base_yield_qty'] = None
        option['default_component_unit'] = (option.get('yield_unit') or 'each')

    has_banquet_menu_tables = db_table_exists(cur, 'public.banquet_menu_items') and db_table_exists(cur, 'public.banquet_menu_item_recipes')
    if has_banquet_menu_tables:
        cur.execute("""
            SELECT r.id,
                   r.name,
                   r.recipe_type,
                   r.category,
                   r.yield_qty,
                   r.yield_unit,
                   r.menu_descriptor,
                   '' AS venue_names,
                   mi.id AS source_menu_item_id,
                   mi.name AS menu_item_name,
                   mi.base_yield_qty AS menu_item_base_yield_qty,
                   mi.base_yield_unit AS menu_item_base_yield_unit
            FROM banquet_menu_items mi
            JOIN LATERAL (
                SELECT recipe_id
                FROM banquet_menu_item_recipes
                WHERE menu_item_id = mi.id
                ORDER BY id
                LIMIT 1
            ) pr ON TRUE
            JOIN recipes r ON r.id = pr.recipe_id
            WHERE (%s = '' OR mi.venue_id = %s OR mi.venue_id IS NULL)
            ORDER BY mi.name, r.name
        """, (venue_id, venue_id))
        for row in cur.fetchall():
            option = dict(row)
            option['option_source'] = 'menu_item'
            option['default_component_unit'] = normalize_count_unit(option.get('menu_item_base_yield_unit')) or 'each'
            options.append(option)

    return options

def get_active_venues(cur):
    if not db_table_exists(cur, 'public.venues'):
        return []
    cur.execute("""
        SELECT id, name, active, sort_order
        FROM venues
        WHERE active = TRUE
        ORDER BY sort_order NULLS LAST, name
    """)
    return cur.fetchall()

def get_recipe_venue_ids(cur, recipe_id):
    if not db_table_exists(cur, 'public.recipe_venues'):
        return []
    cur.execute("""
        SELECT venue_id
        FROM recipe_venues
        WHERE recipe_id = %s
        ORDER BY venue_id
    """, (recipe_id,))
    return [row['venue_id'] for row in cur.fetchall()]

def parse_recipe_venue_ids(request, cur, errors):
    raw_ids = request.form.getlist('recipe_venue_ids[]')
    candidate_ids = sorted({(value or '').strip() for value in raw_ids if (value or '').strip()})
    if not candidate_ids:
        return []
    if not db_table_exists(cur, 'public.venues'):
        return []
    cur.execute("SELECT id FROM venues WHERE id = ANY(%s)", (candidate_ids,))
    valid_ids = {row['id'] for row in cur.fetchall()}
    invalid_ids = [value for value in candidate_ids if value not in valid_ids]
    if invalid_ids:
        errors.append('One or more selected venues were invalid.')
    return [value for value in candidate_ids if value in valid_ids]

__all__ = [
    'get_plated_recipes_for_venue',
    'get_component_recipes_for_venue',
    'get_active_venues',
    'get_recipe_venue_ids',
    'parse_recipe_venue_ids',
]
