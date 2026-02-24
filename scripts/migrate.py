#!/usr/bin/env python3
import hashlib
import os
import sys

import psycopg2
from dotenv import load_dotenv

load_dotenv()

MIGRATIONS = [
    "ALTER TABLE recipes ADD COLUMN IF NOT EXISTS menu_descriptor TEXT",
    "ALTER TABLE menu_rollouts ADD COLUMN IF NOT EXISTS target_food_cost_percent NUMERIC",
    "ALTER TABLE menu_rollout_items ADD COLUMN IF NOT EXISTS menu_price NUMERIC",
    "ALTER TABLE menu_rollout_items ADD COLUMN IF NOT EXISTS target_food_cost_percent NUMERIC",
    "ALTER TABLE menu_rollout_items ADD COLUMN IF NOT EXISTS popularity_score INTEGER",
    "ALTER TABLE menu_rollout_items ADD COLUMN IF NOT EXISTS menu_section TEXT",
    "ALTER TABLE menu_rollout_items ADD COLUMN IF NOT EXISTS menu_descriptor TEXT",
    "ALTER TABLE events ADD COLUMN IF NOT EXISTS venue_id TEXT",
    "ALTER TABLE event_menu_recipes ADD COLUMN IF NOT EXISTS quantity_unit TEXT",
    "ALTER TABLE event_menu_recipes ADD COLUMN IF NOT EXISTS menu_section TEXT",
    "ALTER TABLE event_menu_recipes ADD COLUMN IF NOT EXISTS menu_descriptor TEXT",
    "ALTER TABLE event_menu_recipes ADD COLUMN IF NOT EXISTS notes TEXT",
    "ALTER TABLE event_menu_recipes ADD COLUMN IF NOT EXISTS sort_order INTEGER DEFAULT 0",
    """
    CREATE TABLE IF NOT EXISTS recipe_weighted_options (
        id TEXT PRIMARY KEY,
        recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
        group_name TEXT NOT NULL,
        item_type TEXT NOT NULL CHECK (item_type IN ('ingredient', 'recipe')),
        item_id TEXT NOT NULL,
        quantity NUMERIC NOT NULL,
        unit TEXT,
        weight_percent NUMERIC NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS venues (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        active BOOLEAN NOT NULL DEFAULT TRUE,
        sort_order INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS recipe_venues (
        recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
        venue_id TEXT NOT NULL REFERENCES venues(id) ON DELETE CASCADE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (recipe_id, venue_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_recipe_venues_venue_id ON recipe_venues (venue_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_event_date ON events (event_date)",
    "CREATE INDEX IF NOT EXISTS idx_event_menu_recipes_event_id ON event_menu_recipes (event_id)",
    """
    CREATE TABLE IF NOT EXISTS banquet_menu_templates (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        venue_id TEXT REFERENCES venues(id) ON DELETE SET NULL,
        source_filename TEXT,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS banquet_menu_template_items (
        id BIGSERIAL PRIMARY KEY,
        template_id TEXT NOT NULL REFERENCES banquet_menu_templates(id) ON DELETE CASCADE,
        menu_section TEXT,
        item_name TEXT NOT NULL,
        menu_descriptor TEXT,
        recipe_id TEXT REFERENCES recipes(id) ON DELETE SET NULL,
        default_quantity NUMERIC DEFAULT 1,
        sort_order INTEGER DEFAULT 0,
        price_text TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_banquet_template_items_template_id ON banquet_menu_template_items (template_id)",
    """
    CREATE TABLE IF NOT EXISTS banquet_menu_items (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        venue_id TEXT REFERENCES venues(id) ON DELETE SET NULL,
        menu_section TEXT,
        menu_descriptor TEXT,
        base_yield_qty NUMERIC DEFAULT 1,
        base_yield_unit TEXT DEFAULT 'each',
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS banquet_menu_item_recipes (
        id BIGSERIAL PRIMARY KEY,
        menu_item_id TEXT NOT NULL REFERENCES banquet_menu_items(id) ON DELETE CASCADE,
        recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
        quantity NUMERIC NOT NULL DEFAULT 1,
        unit TEXT DEFAULT 'batch',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS banquet_menu_item_ingredients (
        id BIGSERIAL PRIMARY KEY,
        menu_item_id TEXT NOT NULL REFERENCES banquet_menu_items(id) ON DELETE CASCADE,
        ingredient_id TEXT NOT NULL REFERENCES ingredients(id) ON DELETE CASCADE,
        quantity NUMERIC NOT NULL,
        unit TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS banquet_events (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        event_date DATE NOT NULL,
        guest_count INTEGER,
        venue_id TEXT REFERENCES venues(id) ON DELETE SET NULL,
        building TEXT,
        room TEXT,
        service_timing TEXT,
        dietary_notes TEXT,
        notes TEXT,
        status TEXT NOT NULL DEFAULT 'planning',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS banquet_event_menu_items (
        id BIGSERIAL PRIMARY KEY,
        event_id TEXT NOT NULL REFERENCES banquet_events(id) ON DELETE CASCADE,
        menu_item_id TEXT NOT NULL REFERENCES banquet_menu_items(id) ON DELETE CASCADE,
        menu_item_name TEXT,
        quantity NUMERIC NOT NULL DEFAULT 1,
        quantity_unit TEXT DEFAULT 'each',
        recipe_id TEXT REFERENCES recipes(id) ON DELETE SET NULL,
        menu_section TEXT,
        menu_descriptor TEXT,
        notes TEXT,
        sort_order INTEGER DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_banquet_menu_items_venue_id ON banquet_menu_items (venue_id)",
    "CREATE INDEX IF NOT EXISTS idx_banquet_menu_item_recipes_item_id ON banquet_menu_item_recipes (menu_item_id)",
    "CREATE INDEX IF NOT EXISTS idx_banquet_menu_item_ingredients_item_id ON banquet_menu_item_ingredients (menu_item_id)",
    "CREATE INDEX IF NOT EXISTS idx_banquet_events_event_date ON banquet_events (event_date)",
    "CREATE INDEX IF NOT EXISTS idx_banquet_events_status ON banquet_events (status)",
    "CREATE INDEX IF NOT EXISTS idx_banquet_event_menu_items_event_id ON banquet_event_menu_items (event_id)",
    "CREATE INDEX IF NOT EXISTS idx_banquet_event_menu_items_menu_item_id ON banquet_event_menu_items (menu_item_id)",
    "ALTER TABLE banquet_menu_item_recipes ADD COLUMN IF NOT EXISTS choice_group TEXT",
    "ALTER TABLE banquet_menu_item_recipes ADD COLUMN IF NOT EXISTS choice_weight_percent NUMERIC",
    """
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
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (venue_id, start_date, end_date, item_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_banquet_shopping_checks_scope ON banquet_shopping_checks (venue_id, start_date, end_date)",
    """
    CREATE TABLE IF NOT EXISTS banquet_event_guest_log (
        id BIGSERIAL PRIMARY KEY,
        event_id TEXT NOT NULL REFERENCES banquet_events(id) ON DELETE CASCADE,
        old_count INTEGER,
        new_count INTEGER,
        changed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_banquet_event_guest_log_event_id ON banquet_event_guest_log (event_id, changed_at DESC)",
    "ALTER TABLE banquet_events ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'planning'",
    "ALTER TABLE banquet_menu_items ADD COLUMN IF NOT EXISTS venue_id TEXT",
    "ALTER TABLE banquet_menu_items ADD COLUMN IF NOT EXISTS menu_section TEXT",
    "ALTER TABLE banquet_menu_items ADD COLUMN IF NOT EXISTS menu_descriptor TEXT",
    "ALTER TABLE banquet_menu_items ADD COLUMN IF NOT EXISTS base_yield_qty NUMERIC DEFAULT 1",
    "ALTER TABLE banquet_menu_items ADD COLUMN IF NOT EXISTS base_yield_unit TEXT DEFAULT 'each'",
    "ALTER TABLE banquet_menu_items ADD COLUMN IF NOT EXISTS notes TEXT",
    "ALTER TABLE banquet_menu_items ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
    "ALTER TABLE banquet_event_menu_items ADD COLUMN IF NOT EXISTS quantity_unit TEXT DEFAULT 'each'",
    "ALTER TABLE banquet_event_menu_items ADD COLUMN IF NOT EXISTS menu_item_name TEXT",
    "ALTER TABLE banquet_event_menu_items ADD COLUMN IF NOT EXISTS recipe_id TEXT",
    "ALTER TABLE banquet_event_menu_items ADD COLUMN IF NOT EXISTS menu_section TEXT",
    "ALTER TABLE banquet_event_menu_items ADD COLUMN IF NOT EXISTS menu_descriptor TEXT",
    "ALTER TABLE banquet_event_menu_items ADD COLUMN IF NOT EXISTS notes TEXT",
    "ALTER TABLE banquet_event_menu_items ADD COLUMN IF NOT EXISTS sort_order INTEGER DEFAULT 0",
    """
    INSERT INTO venues (id, name, sort_order) VALUES
        ('ven_foxtown_brewing', 'Foxtown Brewing', 10),
        ('ven_renards', 'Renard''s', 20),
        ('ven_interurban', 'Interurban', 30),
        ('ven_heritage_meats', 'Heritage Meats', 40),
        ('ven_11s_lounge', '11''s Lounge', 50),
        ('ven_banquets', 'Banquets', 60),
        ('ven_foxtown_landing', 'Foxtown Landing', 70)
    ON CONFLICT (id) DO NOTHING
    """,
    "CREATE INDEX IF NOT EXISTS idx_menu_rollout_items_rollout_id ON menu_rollout_items (rollout_id)",
    "CREATE INDEX IF NOT EXISTS idx_menu_rollouts_listing ON menu_rollouts (is_one_off, year DESC, quarter DESC, venue, name)",
]


def migration_key(index, statement):
    digest = hashlib.sha256(statement.strip().encode("utf-8")).hexdigest()[:12]
    return f"{index:04d}_{digest}"


def main():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1

    conn = psycopg2.connect(database_url)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        migration_key TEXT PRIMARY KEY,
                        applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                cur.execute("SELECT migration_key FROM schema_migrations")
                applied = {row[0] for row in cur.fetchall()}

                applied_now = 0
                skipped = 0
                for idx, statement in enumerate(MIGRATIONS, start=1):
                    key = migration_key(idx, statement)
                    if key in applied:
                        skipped += 1
                        continue
                    cur.execute(statement)
                    cur.execute(
                        "INSERT INTO schema_migrations (migration_key) VALUES (%s)",
                        (key,),
                    )
                    applied_now += 1

        print(
            f"Migrations complete. Applied {applied_now}, skipped {skipped}, total tracked {len(MIGRATIONS)}."
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
