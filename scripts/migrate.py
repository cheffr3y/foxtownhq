#!/usr/bin/env python3
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
]


def main():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1

    conn = psycopg2.connect(database_url)
    try:
        with conn:
            with conn.cursor() as cur:
                for statement in MIGRATIONS:
                    cur.execute(statement)
        print(f"Applied {len(MIGRATIONS)} migrations.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
