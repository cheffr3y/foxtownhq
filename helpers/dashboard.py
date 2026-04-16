from datetime import date, datetime, timedelta

from flask import url_for

from config import DEFAULT_TARGET_FOOD_COST_PERCENT, PRICE_REFRESH_DAYS
from helpers.db_helpers import db_column_exists, db_table_exists
from helpers.forecasting import ensure_forecasting_schema
from helpers.recipes import get_recipe_total_cost

BREWPUB_VENUE_ID = 'ven_foxtown_brewing'
BREWPUB_VENUE_NAME = 'Foxtown Brewing'
DEFAULT_PIPELINE_STATUS = 'requested'
DEFAULT_TAP_STATUS = 'releasing_soon'
MARGIN_GOOD_THRESHOLD = 28
MARGIN_WARNING_THRESHOLD = 25
MARGIN_DANGER_THRESHOLD = 20
MENU_REVIEW_AGE_DAYS = 7
MENU_NEW_WINDOW_DAYS = 21
TAP_ALERT_WINDOW_DAYS = 14


def _format_date_short(value):
    if not value:
        return 'TBD'
    return value.strftime('%b %d').replace(' 0', ' ')


def _format_date_long(value):
    if not value:
        return 'Not scheduled'
    return value.strftime('%a, %b %d').replace(' 0', ' ')


def _format_service_range(start_date, end_date):
    return f'{_format_date_short(start_date)} - {_format_date_short(end_date)}'


def _format_quantity(value):
    if value in (None, ''):
        return '0'
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        return str(int(number))
    return f'{number:.2f}'.rstrip('0').rstrip('.')


def _normalize_pipeline_status(value):
    normalized = (value or '').strip().lower()
    if normalized in {'requested', 'confirmed', 'received'}:
        return normalized
    return DEFAULT_PIPELINE_STATUS


def _normalize_tap_status(value):
    normalized = (value or '').strip().lower().replace(' ', '_')
    if normalized in {'active', 'releasing_soon', 'dish_needed'}:
        return normalized
    return DEFAULT_TAP_STATUS


def _margin_tone(margin_pct):
    if margin_pct is None:
        return 'muted'
    if margin_pct < MARGIN_DANGER_THRESHOLD:
        return 'danger'
    if margin_pct < MARGIN_GOOD_THRESHOLD:
        return 'warning'
    return 'good'


def _status_tone(status):
    if status == 'Review':
        return 'warning'
    if status == 'New':
        return 'muted'
    return 'good'


def _pipeline_tone(status):
    if status == 'received':
        return 'good'
    if status == 'confirmed':
        return 'warning'
    return 'amber'


def _ensure_commissary_pipeline_schema(cur):
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
        UPDATE commissary_pipeline
        SET status = 'requested'
        WHERE status IS NULL OR TRIM(status) = ''
        """
    )
    cur.execute(
        """
        UPDATE commissary_pipeline
        SET unit = 'each'
        WHERE unit IS NULL OR TRIM(unit) = ''
        """
    )
    cur.execute(
        """
        UPDATE commissary_pipeline
        SET quantity = 0
        WHERE quantity IS NULL
        """
    )
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


def _ensure_tap_releases_schema(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS tap_releases (
            id BIGSERIAL PRIMARY KEY,
            beer_name TEXT NOT NULL,
            style TEXT,
            abv NUMERIC,
            handle_number INTEGER,
            release_date DATE,
            status TEXT NOT NULL DEFAULT 'releasing_soon',
            paired_dish_id TEXT REFERENCES recipes(id) ON DELETE SET NULL,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute("ALTER TABLE tap_releases ADD COLUMN IF NOT EXISTS beer_name TEXT")
    cur.execute("ALTER TABLE tap_releases ADD COLUMN IF NOT EXISTS style TEXT")
    cur.execute("ALTER TABLE tap_releases ADD COLUMN IF NOT EXISTS abv NUMERIC")
    cur.execute("ALTER TABLE tap_releases ADD COLUMN IF NOT EXISTS handle_number INTEGER")
    cur.execute("ALTER TABLE tap_releases ADD COLUMN IF NOT EXISTS release_date DATE")
    cur.execute("ALTER TABLE tap_releases ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'releasing_soon'")
    cur.execute("ALTER TABLE tap_releases ADD COLUMN IF NOT EXISTS paired_dish_id TEXT")
    cur.execute("ALTER TABLE tap_releases ADD COLUMN IF NOT EXISTS notes TEXT")
    cur.execute("ALTER TABLE tap_releases ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    cur.execute("ALTER TABLE tap_releases ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    cur.execute(
        """
        UPDATE tap_releases
        SET status = 'releasing_soon'
        WHERE status IS NULL OR TRIM(status) = ''
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_tap_releases_release_date
            ON tap_releases (release_date)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_tap_releases_status
            ON tap_releases (status)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_tap_releases_paired_dish_id
            ON tap_releases (paired_dish_id)
        """
    )


def ensure_dashboard_schema(cur):
    ensure_forecasting_schema(cur)
    _ensure_commissary_pipeline_schema(cur)
    _ensure_tap_releases_schema(cur)
    return True


def _get_rd_queue_where_clause(cur):
    if db_column_exists(cur, 'public.recipes', 'status'):
        return "LOWER(COALESCE(status, '')) = 'rd_hold'"
    if db_column_exists(cur, 'public.recipes', 'station'):
        return (
            "LOWER(COALESCE(station, '')) IN "
            "('rd_hold', 'rd', 'r&d', 'rnd', 'r_and_d', 'research', 'research_and_development')"
        )
    return 'FALSE'


def get_dashboard_counts(cur):
    cur.execute('SELECT COUNT(*) AS count FROM recipes')
    recipe_count = cur.fetchone()['count']

    cur.execute('SELECT COUNT(*) AS count FROM ingredients')
    ingredient_count = cur.fetchone()['count']

    stale_cutoff = datetime.utcnow() - timedelta(days=PRICE_REFRESH_DAYS)
    cur.execute(
        """
        SELECT COUNT(*) AS count
        FROM ingredients
        WHERE price_updated_at IS NULL OR price_updated_at < %s
        """,
        (stale_cutoff,),
    )
    stale_price_count = cur.fetchone()['count']

    if db_table_exists(cur, 'public.buffet_events'):
        cur.execute('SELECT COUNT(*) AS count FROM buffet_events')
        buffet_event_count = cur.fetchone()['count']
    else:
        buffet_event_count = 0

    if db_table_exists(cur, 'public.forecasting_menu_items'):
        cur.execute(
            """
            SELECT COUNT(*) AS count
            FROM forecasting_menu_items
            WHERE COALESCE(active, TRUE) = TRUE
            """
        )
        forecasting_active_count = cur.fetchone()['count']
    else:
        forecasting_active_count = 0

    return {
        'recipe_count': recipe_count,
        'ingredient_count': ingredient_count,
        'stale_price_count': stale_price_count,
        'buffet_event_count': buffet_event_count,
        'forecasting_active_count': forecasting_active_count,
    }


def list_rd_queue_items(cur):
    where_clause = _get_rd_queue_where_clause(cur)
    cur.execute(
        f"""
        SELECT
            id,
            name,
            COALESCE(NULLIF(TRIM(category), ''), 'Uncategorized') AS category,
            menu_descriptor,
            created_at,
            recipe_type
        FROM recipes
        WHERE {where_clause}
        ORDER BY created_at DESC NULLS LAST, name
        """
    )
    return [dict(row) for row in cur.fetchall()]


def _query_brewpub_window_count(cur, table_name, start_date, end_date):
    if not db_table_exists(cur, f'public.{table_name}'):
        return 0
    cur.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM {table_name}
        WHERE event_date BETWEEN %s AND %s
        """,
        (start_date, end_date),
    )
    return cur.fetchone()['count']


def _query_latest_brewpub_rollout(cur):
    if not (db_table_exists(cur, 'public.menu_rollouts') and db_table_exists(cur, 'public.menu_rollout_items')):
        return None
    cur.execute(
        """
        SELECT
            id,
            name,
            venue,
            year,
            quarter,
            target_food_cost_percent,
            created_at
        FROM menu_rollouts
        WHERE COALESCE(is_one_off, FALSE) = FALSE
          AND LOWER(COALESCE(venue, '')) = LOWER(%s)
        ORDER BY year DESC NULLS LAST, quarter DESC NULLS LAST, created_at DESC NULLS LAST, name
        LIMIT 1
        """,
        (BREWPUB_VENUE_NAME,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _query_rollout_menu_rows(cur, rollout_id):
    item_created_expr = (
        'mri.created_at'
        if db_column_exists(cur, 'public.menu_rollout_items', 'created_at')
        else 'mr.created_at'
    )
    cur.execute(
        f"""
        SELECT
            mri.recipe_id,
            r.name,
            COALESCE(NULLIF(TRIM(mri.menu_section), ''), NULLIF(TRIM(r.category), ''), 'Menu') AS category,
            COALESCE(NULLIF(TRIM(mri.menu_descriptor), ''), NULLIF(TRIM(r.menu_descriptor), ''), '') AS notes,
            mri.menu_price,
            COALESCE(mri.target_food_cost_percent, %s) AS target_food_cost_percent,
            {item_created_expr} AS item_created_at,
            mr.created_at AS rollout_created_at
        FROM menu_rollout_items mri
        JOIN menu_rollouts mr ON mr.id = mri.rollout_id
        LEFT JOIN recipes r ON r.id = mri.recipe_id
        WHERE mri.rollout_id = %s
        ORDER BY LOWER(COALESCE(r.name, '')) ASC
        """,
        (DEFAULT_TARGET_FOOD_COST_PERCENT, rollout_id),
    )
    return [dict(row) for row in cur.fetchall()]


def _query_fallback_menu_rows(cur):
    created_at_expr = 'created_at' if db_column_exists(cur, 'public.recipes', 'created_at') else 'NULL::timestamp'
    cur.execute(
        f"""
        SELECT
            id AS recipe_id,
            name,
            COALESCE(NULLIF(TRIM(category), ''), 'Menu') AS category,
            COALESCE(NULLIF(TRIM(menu_descriptor), ''), '') AS notes,
            NULL::numeric AS menu_price,
            %s::numeric AS target_food_cost_percent,
            {created_at_expr} AS item_created_at,
            {created_at_expr} AS rollout_created_at
        FROM recipes
        WHERE LOWER(COALESCE(recipe_type, '')) = 'menu'
        ORDER BY {created_at_expr} DESC NULLS LAST, LOWER(name) ASC
        LIMIT 8
        """,
        (DEFAULT_TARGET_FOOD_COST_PERCENT,),
    )
    rows = [dict(row) for row in cur.fetchall()]
    if rows:
        return rows

    cur.execute(
        f"""
        SELECT
            id AS recipe_id,
            name,
            COALESCE(NULLIF(TRIM(category), ''), 'Recipe') AS category,
            COALESCE(NULLIF(TRIM(menu_descriptor), ''), '') AS notes,
            NULL::numeric AS menu_price,
            %s::numeric AS target_food_cost_percent,
            {created_at_expr} AS item_created_at,
            {created_at_expr} AS rollout_created_at
        FROM recipes
        ORDER BY {created_at_expr} DESC NULLS LAST, LOWER(name) ASC
        LIMIT 8
        """,
        (DEFAULT_TARGET_FOOD_COST_PERCENT,),
    )
    return [dict(row) for row in cur.fetchall()]


def _timestamp_to_date(value):
    if isinstance(value, datetime):
        return value.date()
    return value


def _recipe_needs_cost_refresh(cur, recipe_id, stale_cutoff, cache):
    if not recipe_id or not db_table_exists(cur, 'public.recipe_ingredients'):
        return False
    if recipe_id in cache:
        return cache[recipe_id]

    cur.execute(
        """
        SELECT type, item_id
        FROM recipe_ingredients
        WHERE recipe_id = %s
        """,
        (recipe_id,),
    )
    component_rows = [dict(row) for row in cur.fetchall()]
    ingredient_ids = [row['item_id'] for row in component_rows if row.get('type') == 'ingredient' and row.get('item_id')]
    child_recipe_ids = [row['item_id'] for row in component_rows if row.get('type') == 'recipe' and row.get('item_id')]

    needs_refresh = False
    if ingredient_ids:
        cur.execute(
            """
            SELECT id, price_updated_at
            FROM ingredients
            WHERE id = ANY(%s)
            """,
            (ingredient_ids,),
        )
        ingredient_rows = {row['id']: dict(row) for row in cur.fetchall()}
        if len(ingredient_rows) != len(set(ingredient_ids)):
            needs_refresh = True
        for ingredient in ingredient_rows.values():
            updated_at = ingredient.get('price_updated_at')
            if updated_at is None or updated_at < stale_cutoff:
                needs_refresh = True
                break

    if not needs_refresh:
        for child_recipe_id in child_recipe_ids:
            if _recipe_needs_cost_refresh(cur, child_recipe_id, stale_cutoff, cache):
                needs_refresh = True
                break

    cache[recipe_id] = needs_refresh
    return needs_refresh


def _tap_sort_key(release, today):
    release_date = release.get('release_date')
    status = _normalize_tap_status(release.get('status'))
    if status == 'active':
        return (0, abs((release_date - today).days) if release_date else 0)
    if release_date and release_date >= today:
        return (1, (release_date - today).days)
    return (2, abs((today - release_date).days) if release_date else 9999)


def _query_tap_releases(cur, today):
    if not db_table_exists(cur, 'public.tap_releases'):
        return []
    cur.execute(
        """
        SELECT
            tr.id,
            tr.beer_name,
            tr.style,
            tr.abv,
            tr.handle_number,
            tr.release_date,
            tr.status,
            tr.paired_dish_id,
            tr.notes,
            tr.created_at,
            tr.updated_at,
            r.name AS paired_dish_name
        FROM tap_releases tr
        LEFT JOIN recipes r ON r.id = tr.paired_dish_id
        ORDER BY tr.release_date ASC NULLS LAST, tr.id ASC
        """
    )
    releases = [dict(row) for row in cur.fetchall()]
    return sorted(releases, key=lambda release: _tap_sort_key(release, today))


def _build_tap_program_cards(tap_releases, today):
    cards = []
    for release in tap_releases[:2]:
        status = _normalize_tap_status(release.get('status'))
        release_date = release.get('release_date')
        paired_dish_name = release.get('paired_dish_name')
        if not paired_dish_name and release_date and release_date <= (today + timedelta(days=TAP_ALERT_WINDOW_DAYS)):
            status_label = 'Dish needed'
            status_tone = 'warning'
        elif status == 'active':
            status_label = 'Active'
            status_tone = 'good'
        elif status == 'dish_needed':
            status_label = 'Dish needed'
            status_tone = 'warning'
        else:
            status_label = 'Releasing soon'
            status_tone = 'muted'

        cards.append(
            {
                'beer_name': release.get('beer_name') or 'Tap slot open',
                'style': release.get('style') or 'Style pending',
                'handle_number': release.get('handle_number'),
                'abv': release.get('abv'),
                'release_date': release_date,
                'status_label': status_label,
                'status_tone': status_tone,
                'paired_dish_name': paired_dish_name,
                'paired_dish_id': release.get('paired_dish_id'),
                'notes': release.get('notes') or '',
            }
        )

    while len(cards) < 2:
        cards.append(
            {
                'beer_name': 'Tap slot open',
                'style': 'Release placeholder',
                'handle_number': None,
                'abv': None,
                'release_date': None,
                'status_label': 'Dish needed',
                'status_tone': 'warning',
                'paired_dish_name': None,
                'paired_dish_id': None,
                'notes': '',
            }
        )

    return cards


def _build_tap_pair_map(tap_releases):
    pair_map = {}
    for release in tap_releases:
        paired_dish_id = release.get('paired_dish_id')
        if paired_dish_id and paired_dish_id not in pair_map:
            pair_map[paired_dish_id] = release
    return pair_map


def _build_menu_board_items(cur, menu_rows, tap_pair_map, today, stale_cutoff, unit_system):
    refresh_cache = {}
    menu_items = []
    for row in menu_rows:
        recipe_id = row.get('recipe_id')
        recipe_cost = get_recipe_total_cost(cur, recipe_id, unit_system) if recipe_id else None
        menu_price = row.get('menu_price')
        margin_pct = None
        if recipe_cost is not None and menu_price not in (None, ''):
            try:
                price_value = float(menu_price)
            except (TypeError, ValueError):
                price_value = 0
            if price_value > 0:
                margin_pct = round(((price_value - recipe_cost) / price_value) * 100, 1)

        cost_refresh_needed = _recipe_needs_cost_refresh(cur, recipe_id, stale_cutoff, refresh_cache)
        row_date = _timestamp_to_date(row.get('rollout_created_at') or row.get('item_created_at'))
        is_new = bool(row_date and row_date >= (today - timedelta(days=MENU_NEW_WINDOW_DAYS)))
        needs_review = bool(cost_refresh_needed or menu_price in (None, '') or (margin_pct is not None and margin_pct < MARGIN_WARNING_THRESHOLD))

        if needs_review:
            status_label = 'Review'
        elif is_new:
            status_label = 'New'
        else:
            status_label = 'Active'

        tap_release = tap_pair_map.get(recipe_id)
        tap_pair_name = tap_release.get('beer_name') if tap_release else None
        review_age_days = (today - row_date).days if status_label == 'Review' and row_date else 0
        menu_items.append(
            {
                'recipe_id': recipe_id,
                'name': row.get('name') or 'Untitled dish',
                'category': row.get('category') or 'Menu',
                'notes': row.get('notes') or '',
                'menu_price': menu_price,
                'recipe_cost': round(recipe_cost, 2) if recipe_cost is not None else None,
                'margin_pct': margin_pct,
                'margin_display': f'{margin_pct:.1f}%' if margin_pct is not None else '—',
                'margin_tone': _margin_tone(margin_pct),
                'status_label': status_label,
                'status_tone': _status_tone(status_label),
                'tap_pair_name': tap_pair_name,
                'tap_pair_release_id': tap_release.get('id') if tap_release else None,
                'tap_pair_display': tap_pair_name or '— unpaired',
                'cost_refresh_needed': cost_refresh_needed,
                'needs_costing': menu_price in (None, ''),
                'below_margin_threshold': bool(margin_pct is not None and margin_pct < MARGIN_WARNING_THRESHOLD),
                'review_age_days': review_age_days,
            }
        )
    return menu_items


def _build_pipeline_items(cur):
    if not db_table_exists(cur, 'public.commissary_pipeline'):
        return []
    cur.execute(
        """
        SELECT
            id,
            item_name,
            quantity,
            unit,
            status,
            due_date,
            requested_by,
            notes,
            created_at,
            updated_at
        FROM commissary_pipeline
        ORDER BY
            CASE LOWER(COALESCE(status, 'requested'))
                WHEN 'requested' THEN 0
                WHEN 'confirmed' THEN 1
                ELSE 2
            END,
            due_date ASC NULLS LAST,
            updated_at DESC NULLS LAST,
            id DESC
        LIMIT 8
        """
    )
    pipeline_items = []
    for row in cur.fetchall():
        entry = dict(row)
        status = _normalize_pipeline_status(entry.get('status'))
        pipeline_items.append(
            {
                'id': entry.get('id'),
                'item_name': entry.get('item_name') or 'Request item',
                'quantity_label': f"{_format_quantity(entry.get('quantity'))} {(entry.get('unit') or 'each').strip()}".strip(),
                'status': status,
                'status_label': status.title(),
                'status_tone': _pipeline_tone(status),
                'due_date': entry.get('due_date'),
                'requested_by': entry.get('requested_by') or '',
                'notes': entry.get('notes') or '',
            }
        )
    return pipeline_items


def _build_pipeline_summary(cur, today, week_start, week_end):
    if not db_table_exists(cur, 'public.commissary_pipeline'):
        return {
            'weekly_total': 0,
            'weekly_confirmed': 0,
            'weekly_pending': 0,
            'overdue_requested': 0,
            'created_this_week': 0,
        }
    cur.execute(
        """
        SELECT
            COUNT(*) FILTER (WHERE due_date BETWEEN %s AND %s) AS weekly_total,
            COUNT(*) FILTER (
                WHERE due_date BETWEEN %s AND %s
                  AND LOWER(COALESCE(status, 'requested')) IN ('confirmed', 'received')
            ) AS weekly_confirmed,
            COUNT(*) FILTER (
                WHERE due_date BETWEEN %s AND %s
                  AND LOWER(COALESCE(status, 'requested')) = 'requested'
            ) AS weekly_pending,
            COUNT(*) FILTER (
                WHERE due_date < %s
                  AND LOWER(COALESCE(status, 'requested')) = 'requested'
            ) AS overdue_requested,
            COUNT(*) FILTER (
                WHERE created_at::date BETWEEN %s AND %s
            ) AS created_this_week
        FROM commissary_pipeline
        """,
        (
            week_start,
            week_end,
            week_start,
            week_end,
            week_start,
            week_end,
            today,
            week_start,
            week_end,
        ),
    )
    row = cur.fetchone() or {}
    return {
        'weekly_total': row.get('weekly_total') or 0,
        'weekly_confirmed': row.get('weekly_confirmed') or 0,
        'weekly_pending': row.get('weekly_pending') or 0,
        'overdue_requested': row.get('overdue_requested') or 0,
        'created_this_week': row.get('created_this_week') or 0,
    }


def _build_fast_access_items(counts, menu_flags_count, below_margin_count, forecast_due, rd_queue_count, today):
    forecasting_sub = 'Due' if forecast_due else 'Sent'
    return [
        {
            'title': 'Menu costing',
            'href': url_for('menu.menu_costing'),
            'meta': f'{below_margin_count} flagged' if below_margin_count else f'{menu_flags_count} to review' if menu_flags_count else 'Clear',
        },
        {
            'title': 'Ingredient DB',
            'href': url_for('ingredients.ingredients'),
            'meta': f"{counts['ingredient_count']} total",
        },
        {
            'title': 'Forecasting',
            'href': url_for('forecasting.forecasting_dashboard', venue_id=BREWPUB_VENUE_ID),
            'meta': forecasting_sub,
        },
        {
            'title': 'Recipe DB',
            'href': url_for('recipes.recipes'),
            'meta': f"{counts['recipe_count']} recipes",
        },
        {
            'title': 'R&D queue',
            'href': url_for('rd_queue'),
            'meta': f'{rd_queue_count} open',
        },
        {
            'title': 'Daily prep',
            'href': url_for('prep.prep_index'),
            'meta': _format_date_short(today),
        },
    ]


def _build_command_alerts(menu_items, pipeline_summary, stale_price_count, tap_releases, today):
    alerts = []

    tap_unpaired = [
        release for release in tap_releases
        if release.get('release_date')
        and release['release_date'] <= (today + timedelta(days=TAP_ALERT_WINDOW_DAYS))
        and not release.get('paired_dish_id')
    ]
    if tap_unpaired:
        next_release = tap_unpaired[0]
        alerts.append(
            {
                'priority': 1,
                'tone': 'warning',
                'text': f"{next_release.get('beer_name') or 'Upcoming tap release'} needs a paired dish",
                'action': f"Release {_format_date_short(next_release.get('release_date'))} · open Tap Program",
                'href': f"{url_for('dashboard')}#tap-program",
            }
        )

    below_margin_items = [item for item in menu_items if item.get('below_margin_threshold')]
    if below_margin_items:
        alerts.append(
            {
                'priority': 2,
                'tone': 'warning',
                'text': f"{len(below_margin_items)} menu item{'s' if len(below_margin_items) != 1 else ''} below {MARGIN_WARNING_THRESHOLD}% margin",
                'action': 'Review costing and refresh pricing',
                'href': url_for('menu.menu_costing'),
            }
        )

    if pipeline_summary['overdue_requested']:
        alerts.append(
            {
                'priority': 3,
                'tone': 'amber',
                'text': f"{pipeline_summary['overdue_requested']} commissary request{'s' if pipeline_summary['overdue_requested'] != 1 else ''} past due",
                'action': 'Open commissary pipeline',
                'href': f"{url_for('dashboard')}#commissary-pipeline",
            }
        )

    if stale_price_count:
        alerts.append(
            {
                'priority': 4,
                'tone': 'warning',
                'text': f"{stale_price_count} ingredient price{'s' if stale_price_count != 1 else ''} older than {PRICE_REFRESH_DAYS} days",
                'action': 'Refresh ingredient costs',
                'href': url_for('ingredients.ingredients', needs_update='1'),
            }
        )

    aged_review_items = [
        item for item in menu_items
        if item.get('status_label') == 'Review' and item.get('review_age_days', 0) >= MENU_REVIEW_AGE_DAYS
    ]
    if aged_review_items:
        alerts.append(
            {
                'priority': 5,
                'tone': 'amber',
                'text': f"{len(aged_review_items)} menu item{'s' if len(aged_review_items) != 1 else ''} stuck in Review for 7+ days",
                'action': 'Open live menu board',
                'href': f"{url_for('dashboard')}#live-menu",
            }
        )

    alerts.sort(key=lambda item: item['priority'])
    return alerts[:5]


def _build_sidebar_navigation(menu_flags_count, upcoming_release_count, overdue_requested, below_margin_count, banquet_count, buffet_count):
    primary_nav = [
        {
            'label': 'Dashboard',
            'sub': 'Command overview',
            'href': url_for('dashboard'),
            'badge_text': None,
            'badge_tone': 'muted',
            'active': True,
        },
        {
            'label': 'Menu Board',
            'sub': 'Active brewpub menu',
            'href': f"{url_for('dashboard')}#live-menu",
            'badge_text': str(menu_flags_count) if menu_flags_count else None,
            'badge_tone': 'warning' if menu_flags_count else 'muted',
            'active': False,
        },
        {
            'label': 'Tap Program',
            'sub': 'Beer calendar and pairings',
            'href': f"{url_for('dashboard')}#tap-program",
            'badge_text': str(upcoming_release_count) if upcoming_release_count else None,
            'badge_tone': 'warning' if upcoming_release_count else 'muted',
            'active': False,
        },
        {
            'label': 'Forecasting',
            'sub': 'Commissary feed / outbound pull requests',
            'href': url_for('forecasting.forecasting_dashboard', venue_id=BREWPUB_VENUE_ID),
            'badge_text': '!' if overdue_requested else None,
            'badge_tone': 'amber' if overdue_requested else 'muted',
            'active': False,
        },
        {
            'label': 'Recipes',
            'sub': 'Master sheets',
            'href': url_for('recipes.recipes'),
            'badge_text': None,
            'badge_tone': 'muted',
            'active': False,
        },
        {
            'label': 'Ingredients',
            'sub': 'Inventory file',
            'href': url_for('ingredients.ingredients'),
            'badge_text': None,
            'badge_tone': 'muted',
            'active': False,
        },
        {
            'label': 'Costing',
            'sub': 'Margin checks',
            'href': url_for('menu.menu_costing'),
            'badge_text': str(below_margin_count) if below_margin_count else None,
            'badge_tone': 'warning' if below_margin_count else 'muted',
            'active': False,
        },
    ]
    support_nav = [
        {
            'label': 'Banquets',
            'sub': 'Support view',
            'href': url_for('banquet.banquet_planner'),
            'badge_text': str(banquet_count) if banquet_count else None,
            'badge_tone': 'muted',
        },
        {
            'label': 'Commissary',
            'sub': 'Pipeline status',
            'href': url_for('commissary.commissary_planner', day=date.today().isoformat()),
            'badge_text': None,
            'badge_tone': 'muted',
        },
        {
            'label': 'Buffets',
            'sub': 'Support queue',
            'href': url_for('buffet.buffet_planner'),
            'badge_text': str(buffet_count) if buffet_count else None,
            'badge_tone': 'muted',
        },
    ]
    return primary_nav, support_nav


def build_dashboard_view_model(cur, today, week_start, week_end, unit_system, counts):
    ensure_dashboard_schema(cur)

    stale_cutoff = datetime.utcnow() - timedelta(days=PRICE_REFRESH_DAYS)
    tap_releases = _query_tap_releases(cur, today)
    tap_pair_map = _build_tap_pair_map(tap_releases)

    latest_rollout = _query_latest_brewpub_rollout(cur)
    if latest_rollout:
        menu_rows = _query_rollout_menu_rows(cur, latest_rollout['id'])
    else:
        menu_rows = _query_fallback_menu_rows(cur)

    menu_items = _build_menu_board_items(cur, menu_rows, tap_pair_map, today, stale_cutoff, unit_system)
    pipeline_items = _build_pipeline_items(cur)
    pipeline_summary = _build_pipeline_summary(cur, today, week_start, week_end)

    next_tap_release = next((release for release in tap_releases if release.get('release_date') and release['release_date'] >= today), None)
    upcoming_release_count = sum(1 for release in tap_releases if release.get('release_date') and release['release_date'] >= today)
    menu_flags_count = sum(1 for item in menu_items if item.get('status_label') == 'Review' or item.get('needs_costing'))
    below_margin_count = sum(1 for item in menu_items if item.get('below_margin_threshold'))
    cost_refresh_count = sum(1 for item in menu_items if item.get('cost_refresh_needed'))
    active_specials_count = sum(1 for item in menu_items if item.get('status_label') == 'New')
    banquet_count = _query_brewpub_window_count(cur, 'banquet_events', week_start, week_end)
    buffet_count = _query_brewpub_window_count(cur, 'buffet_events', week_start, week_end)
    rd_queue_items = list_rd_queue_items(cur)
    forecast_due = pipeline_summary['created_this_week'] == 0
    tap_program_cards = _build_tap_program_cards(tap_releases, today)
    command_alerts = _build_command_alerts(menu_items, pipeline_summary, counts['stale_price_count'], tap_releases, today)
    primary_nav, support_nav = _build_sidebar_navigation(
        menu_flags_count,
        upcoming_release_count,
        pipeline_summary['overdue_requested'],
        below_margin_count,
        banquet_count,
        buffet_count,
    )

    next_tap_sub = (next_tap_release or {}).get('paired_dish_name') or 'Paired dish needed'
    next_tap_sub_tone = 'warning' if not (next_tap_release or {}).get('paired_dish_name') else 'muted'
    menu_health_watch = below_margin_count > 0

    stat_cards = [
        {
            'label': 'This Week Service',
            'value': _format_service_range(week_start, week_end),
            'sub': f"{active_specials_count} active special{'s' if active_specials_count != 1 else ''}",
            'sub_tone': 'muted',
        },
        {
            'label': 'Next Tap Release',
            'value': _format_date_long((next_tap_release or {}).get('release_date')),
            'sub': next_tap_sub,
            'sub_tone': next_tap_sub_tone,
        },
        {
            'label': 'Commissary Fulfillment',
            'value': f"{pipeline_summary['weekly_confirmed']} / {pipeline_summary['weekly_total']}",
            'sub': f"{pipeline_summary['weekly_pending']} items pending" if pipeline_summary['weekly_pending'] else 'No requests pending',
            'sub_tone': 'warning' if pipeline_summary['weekly_pending'] else 'muted',
        },
        {
            'label': 'Menu Health',
            'value': 'WATCH' if menu_health_watch else 'GOOD',
            'sub': f"{cost_refresh_count} item{'s' if cost_refresh_count != 1 else ''} need cost refresh",
            'sub_tone': 'warning' if menu_health_watch else 'good',
        },
    ]

    fast_access_items = _build_fast_access_items(
        counts,
        menu_flags_count,
        below_margin_count,
        forecast_due,
        len(rd_queue_items),
        today,
    )

    return {
        'brewpub_venue_name': BREWPUB_VENUE_NAME,
        'primary_nav': primary_nav,
        'support_nav': support_nav,
        'stat_cards': stat_cards,
        'menu_board_items': menu_items,
        'menu_board_action_url': url_for('menu.menu_rollout_new'),
        'menu_board_review_count': menu_flags_count,
        'pipeline_items': pipeline_items,
        'pipeline_action_url': url_for('forecast_send'),
        'pipeline_summary': pipeline_summary,
        'tap_program_cards': tap_program_cards,
        'tap_program_action_url': f"{url_for('dashboard')}#tap-program",
        'fast_access_items': fast_access_items,
        'command_alerts': command_alerts,
        'rd_queue_count': len(rd_queue_items),
        'forecast_due': forecast_due,
        'banquet_window_count': banquet_count,
        'buffet_window_count': buffet_count,
    }


__all__ = [
    'BREWPUB_VENUE_ID',
    'BREWPUB_VENUE_NAME',
    'ensure_dashboard_schema',
    'get_dashboard_counts',
    'list_rd_queue_items',
    'build_dashboard_view_model',
]
