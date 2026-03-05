from datetime import datetime, timedelta, timezone

from config import PRICE_REFRESH_DAYS
from flask import url_for

from helpers.banquet import auto_complete_past_banquet_events, build_banquet_datasets
from helpers.formatting import add_amount, amount_label, as_float, as_int, enrich_event


def get_dashboard_counts(cur):
    cur.execute('SELECT COUNT(*) AS count FROM recipes')
    recipe_count = cur.fetchone()['count']

    cur.execute('SELECT COUNT(*) AS count FROM ingredients')
    ingredient_count = cur.fetchone()['count']

    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=PRICE_REFRESH_DAYS)
    cur.execute(
        '''
        SELECT COUNT(*) AS count
        FROM ingredients
        WHERE price_updated_at IS NULL OR price_updated_at < %s
        ''',
        (stale_cutoff,),
    )
    stale_price_count = cur.fetchone()['count']

    recent_cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    cur.execute(
        '''
        SELECT COUNT(*) AS count
        FROM menu_rollouts
        WHERE is_one_off = FALSE
          AND created_at >= %s
        ''',
        (recent_cutoff,),
    )
    recent_rollout_count = cur.fetchone()['count']

    return {
        'recipe_count': recipe_count,
        'ingredient_count': ingredient_count,
        'stale_price_count': stale_price_count,
        'recent_rollout_count': recent_rollout_count,
    }


def build_production_pulse(cur, today, board_window_end, unit_system):
    pulse_datasets = build_banquet_datasets(cur, today, board_window_end, '', unit_system)
    pulse_map = {}
    for prep in pulse_datasets.get('weekly_prep', []) or []:
        root_name = prep.get('recipe_name') or 'Prep Item'
        sub_rows = prep.get('subrecipe_rows') or []

        if not sub_rows and prep.get('recipe_id'):
            fallback_key = f"root:{prep.get('recipe_id')}"
            if fallback_key not in pulse_map:
                pulse_map[fallback_key] = {
                    'recipe_id': prep.get('recipe_id'),
                    'recipe_name': prep.get('recipe_name') or 'Prep Item',
                    'required_batches': 0.0,
                    'required_amounts': {},
                    'used_in_preps': set(),
                }
            pulse_map[fallback_key]['required_batches'] += as_float(prep.get('required_batches'))
            add_amount(
                pulse_map[fallback_key]['required_amounts'],
                prep.get('required_qty'),
                prep.get('required_unit') or prep.get('yield_unit'),
            )
            pulse_map[fallback_key]['used_in_preps'].add(root_name)

        for sub in sub_rows:
            recipe_id = sub.get('recipe_id')
            recipe_name = sub.get('recipe_name') or 'Sub-recipe'
            if recipe_id:
                pulse_key = f"sub:{recipe_id}"
            else:
                pulse_key = f"sub-name:{recipe_name.strip().lower()}"
            if pulse_key not in pulse_map:
                pulse_map[pulse_key] = {
                    'recipe_id': recipe_id,
                    'recipe_name': recipe_name,
                    'required_batches': 0.0,
                    'required_amounts': {},
                    'used_in_preps': set(),
                }
            pulse_map[pulse_key]['required_batches'] += as_float(sub.get('required_batches'))
            add_amount(
                pulse_map[pulse_key]['required_amounts'],
                sub.get('required_qty'),
                sub.get('required_unit') or sub.get('yield_unit'),
            )
            pulse_map[pulse_key]['used_in_preps'].add(root_name)

    production_pulse_items = []
    production_pulse_amounts = {}
    for row in pulse_map.values():
        row_amounts = row.get('required_amounts') or {}
        for amount in row_amounts.values():
            add_amount(production_pulse_amounts, amount.get('qty'), amount.get('unit'))
        production_pulse_items.append({
            'recipe_id': row.get('recipe_id'),
            'recipe_name': row.get('recipe_name'),
            'required_batches': round(as_float(row.get('required_batches')), 2),
            'required_amount_label': amount_label(row_amounts),
            'used_in_preps': sorted(row.get('used_in_preps') or []),
        })
    production_pulse_items.sort(key=lambda item: (-as_float(item.get('required_batches')), (item.get('recipe_name') or '').lower()))
    production_pulse_total_batches = round(sum(as_float(item.get('required_batches')) for item in production_pulse_items), 2)
    production_pulse_total_amount = amount_label(production_pulse_amounts, max_parts=3)

    return {
        'items': production_pulse_items[:8],
        'total_batches': production_pulse_total_batches,
        'total_amount': production_pulse_total_amount,
        'total_subrecipes': len(production_pulse_items),
        'event_count': as_int(pulse_datasets.get('event_count')),
    }


def build_pinned_and_recent_recipes(cur, datasets):
    recipe_usage = {}
    for event in datasets.get('events', []) or []:
        for line in event.get('lines', []) or []:
            line_recipes = line.get('recipes') or ([line.get('recipe')] if line.get('recipe') else [])
            for line_recipe in line_recipes:
                recipe_id = line_recipe.get('id')
                if not recipe_id:
                    continue
                if recipe_id not in recipe_usage:
                    recipe_usage[recipe_id] = {
                        'id': recipe_id,
                        'name': line_recipe.get('name') or line.get('menu_item_name') or 'Recipe',
                        'hit_count': 0,
                        'source': 'Pinned',
                    }
                recipe_usage[recipe_id]['hit_count'] += 1

    pinned_recipes = sorted(
        recipe_usage.values(),
        key=lambda row: (-as_int(row.get('hit_count')), (row.get('name') or '').lower())
    )[:8]
    pinned_recipe_ids = {row.get('id') for row in pinned_recipes if row.get('id')}

    cur.execute("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'recipes'
          AND column_name = ANY(%s)
    """, (['updated_at', 'created_at'],))
    recipe_columns = {row.get('column_name') for row in cur.fetchall()}
    if 'updated_at' in recipe_columns and 'created_at' in recipe_columns:
        recent_order_expr = 'COALESCE(updated_at, created_at)'
    elif 'updated_at' in recipe_columns:
        recent_order_expr = 'updated_at'
    elif 'created_at' in recipe_columns:
        recent_order_expr = 'created_at'
    else:
        recent_order_expr = 'id'

    cur.execute(f"""
        SELECT id, name, recipe_type, category
        FROM recipes
        ORDER BY {recent_order_expr} DESC NULLS LAST, name
        LIMIT 16
    """)
    recent_recipes = []
    for row in cur.fetchall():
        if row.get('id') in pinned_recipe_ids:
            continue
        recent_recipes.append({
            'id': row.get('id'),
            'name': row.get('name') or 'Recipe',
            'recipe_type': row.get('recipe_type') or 'other',
            'category': row.get('category') or '',
            'source': 'Recent',
        })
        if len(recent_recipes) >= 8:
            break

    return pinned_recipes, recent_recipes


def build_system_health(production_board_live, production_board_active_tasks, events_with_missing_menus, stale_price_count):
    system_health_items = []
    if production_board_live:
        system_health_items.append({
            'label': 'Production Board Live',
            'count_text': str(production_board_active_tasks),
            'detail': 'active prep tasks',
            'tone': 'emerald',
            'href': url_for('production_board'),
        })
    if events_with_missing_menus:
        first_event = events_with_missing_menus[0]
        system_health_items.append({
            'label': 'Events Missing Menu Items',
            'count_text': str(len(events_with_missing_menus)),
            'detail': 'Events within 72 hours have no menu lines attached.',
            'href': url_for('banquet_event_edit', event_id=first_event.get('id')),
            'tone': 'amber',
        })
    if stale_price_count:
        system_health_items.append({
            'label': 'Ingredient Price Refresh Needed',
            'count_text': str(stale_price_count),
            'detail': f'Ingredients are older than {PRICE_REFRESH_DAYS or 56} days.',
            'href': url_for('ingredients', needs_update='1'),
            'tone': 'rose',
        })
    if not system_health_items:
        system_health_items.append({
            'label': 'System Health',
            'count_text': 'All Clear',
            'detail': 'No urgent pricing or coverage risks in the next 72 hours.',
            'href': url_for('dashboard'),
            'tone': 'emerald',
        })
    return system_health_items


def build_dashboard_view_model(cur, today, week_end, selected_venue, unit_system, stale_price_count):
    auto_complete_past_banquet_events(cur, selected_venue)
    datasets = build_banquet_datasets(cur, today, week_end, selected_venue, unit_system)

    today_events = [enrich_event(event) for event in datasets.get('events', []) if event.get('event_date') == today]
    upcoming_events = [
        enrich_event(event) for event in datasets.get('events', [])
        if event.get('event_date') and today < event.get('event_date') <= week_end
    ]

    events_with_missing_menus = [
        event for event in today_events + upcoming_events
        if event.get('event_date')
        and event.get('event_date') <= (today + timedelta(days=3))
        and int(event.get('line_count') or 0) == 0
    ]

    board_window_end = today + timedelta(days=2)
    board_datasets_events = [
        event
        for event in datasets.get('events', [])
        if event.get('event_date') and event['event_date'] <= board_window_end
    ]
    production_board_active_tasks = sum(
        as_int(event.get('line_count'))
        for event in board_datasets_events
        if (event.get('status') or '').strip().lower() not in ('completed', 'executed', 'cancelled')
    )
    production_board_live = production_board_active_tasks > 0

    production_pulse = build_production_pulse(cur, today, board_window_end, unit_system)
    pinned_recipes, recent_recipes = build_pinned_and_recent_recipes(cur, datasets)
    system_health_items = build_system_health(
        production_board_live,
        production_board_active_tasks,
        events_with_missing_menus,
        stale_price_count,
    )

    return {
        'today_events': today_events,
        'upcoming_events': upcoming_events,
        'production_board_live': production_board_live,
        'production_board_active_tasks': production_board_active_tasks,
        'production_pulse_items': production_pulse['items'],
        'production_pulse_total_batches': production_pulse['total_batches'],
        'production_pulse_total_amount': production_pulse['total_amount'],
        'production_pulse_total_subrecipes': production_pulse['total_subrecipes'],
        'production_pulse_event_count': production_pulse['event_count'],
        'pinned_recipes': pinned_recipes,
        'recent_recipes': recent_recipes,
        'system_health_items': system_health_items,
    }


__all__ = [
    'get_dashboard_counts',
    'build_production_pulse',
    'build_pinned_and_recent_recipes',
    'build_system_health',
    'build_dashboard_view_model',
]
