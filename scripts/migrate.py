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
