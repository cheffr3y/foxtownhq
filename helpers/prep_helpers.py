from helpers.db_helpers import db_column_exists, db_table_exists
from helpers.shared import to_float

PREP_NOTE_OPTIONS = [
    'Diced',
    'Sliced ¼"',
    'Sliced thin',
    'Shredded',
    'Rough chop',
    'Minced',
    'Julienne',
    'Whole',
]


def ensure_prep_schema(cur):
    if not db_table_exists(cur, 'public.recipe_ingredients'):
        return False
    if db_column_exists(cur, 'public.recipe_ingredients', 'prep_note'):
        return False
    cur.execute("ALTER TABLE recipe_ingredients ADD COLUMN IF NOT EXISTS prep_note TEXT")
    return True


def clean_prep_note(value):
    text = (value or '').strip()
    return text or None


def get_prep_aggregation(cur):
    if not db_table_exists(cur, 'public.recipe_ingredients'):
        return {}

    cur.execute(
        """
        SELECT
            i.id AS ingredient_id,
            i.name AS ingredient_name,
            ri.prep_note,
            ri.unit,
            SUM(ri.quantity) AS total_quantity
        FROM recipe_ingredients ri
        JOIN ingredients i ON i.id = ri.item_id
        JOIN recipes r ON r.id = ri.recipe_id
        WHERE ri.type = 'ingredient'
          AND NULLIF(TRIM(ri.prep_note), '') IS NOT NULL
          AND r.recipe_type = 'menu'
        GROUP BY i.id, i.name, ri.prep_note, ri.unit
        ORDER BY LOWER(i.name), LOWER(ri.prep_note), ri.unit
        """
    )

    grouped = {}
    for row in cur.fetchall():
        ingredient_name = row.get('ingredient_name') or 'Unnamed Ingredient'
        grouped.setdefault(ingredient_name, []).append({
            'prep_note': row.get('prep_note') or '',
            'unit': row.get('unit') or '',
            'total_quantity': to_float(row.get('total_quantity')),
        })
    return grouped


__all__ = [
    'PREP_NOTE_OPTIONS',
    'ensure_prep_schema',
    'clean_prep_note',
    'get_prep_aggregation',
]
