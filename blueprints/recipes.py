from config import RECIPE_Q_FACTOR_PERCENT
from db import get_cursor, get_db
from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for
from flask_login import login_required
from helpers.auth import role_required
from helpers.db_helpers import db_table_exists, ensure_recipe_metadata_columns
from helpers.forecasting import sync_forecasting_menu_items_for_recipe
from helpers.formatting import split_instruction_steps
from helpers.prep_helpers import PREP_NOTE_OPTIONS, clean_prep_note, ensure_prep_schema
from helpers.recipes import (
    build_component_tree,
    clone_recipe,
    get_recipe_by_id,
    get_recipe_components,
    get_recipe_weighted_options,
    infer_recipe_type,
    normalize_recipe_type,
    parse_weighted_options_from_form,
)
from helpers.shared import generate_id, handle_route_error, to_float
from helpers.units import get_unit_system, normalize_unit, summarize_yield_pricing
from helpers.venues import get_active_venues, get_recipe_venue_ids, parse_recipe_venue_ids

bp = Blueprint('recipes', __name__)


def _parse_optional_int(value, label, errors):
    raw = (value or '').strip()
    if not raw:
        return None
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        errors.append(f'{label} must be a whole number.')
        return None
    if parsed < 0:
        errors.append(f'{label} must be zero or greater.')
        return None
    return parsed


def escape_like(value):
    return value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def _weighted_option_source_label(item_type, row):
    name = (row.get('name') or '').strip() or 'Option'
    if item_type == 'ingredient':
        unit = (row.get('unit') or '').strip()
        return f"{name} [Ingredient]{f' ({unit})' if unit else ''}"

    recipe_type = normalize_recipe_type(row.get('recipe_type'))
    if recipe_type == 'menu':
        type_label = 'RM'
    elif recipe_type == 'batch':
        type_label = 'RB'
    else:
        type_label = 'Recipe'
    unit = (row.get('yield_unit') or '').strip()
    return f"{name} [{type_label}]{f' ({unit})' if unit else ''}"


def _recipe_picker_label(row):
    name = (row.get('name') or '').strip() or 'Recipe'
    recipe_type = normalize_recipe_type(row.get('recipe_type'))
    if recipe_type == 'menu':
        suffix = 'RM'
    elif recipe_type == 'batch':
        suffix = 'RB'
    else:
        suffix = 'Recipe'
    return f'{name} ({suffix})'


def _prepare_weighted_option_sources(ingredients_list, weighted_recipes_list):
    sources = []
    label_map = {}

    for ingredient in ingredients_list or []:
        item_id = ingredient.get('id')
        if not item_id:
            continue
        display_label = _weighted_option_source_label('ingredient', ingredient)
        sources.append({
            'item_type': 'ingredient',
            'item_id': item_id,
            'name': ingredient.get('name') or '',
            'display_label': display_label,
            'default_unit': ingredient.get('unit') or ''
        })
        label_map[('ingredient', item_id)] = display_label

    for recipe in weighted_recipes_list or []:
        item_id = recipe.get('id')
        if not item_id:
            continue
        display_label = _weighted_option_source_label('recipe', recipe)
        sources.append({
            'item_type': 'recipe',
            'item_id': item_id,
            'name': recipe.get('name') or '',
            'display_label': display_label,
            'default_unit': recipe.get('yield_unit') or ''
        })
        label_map[('recipe', item_id)] = display_label

    return sources, label_map


def _prepare_option_items_for_form(option_items, label_map):
    prepared = []
    for option in option_items or []:
        item = dict(option)
        item['display_label'] = label_map.get(
            (item.get('item_type') or 'recipe', item.get('item_id')),
            item.get('item_name') or ''
        )
        prepared.append(item)
    return prepared


def _get_recipe_detail_context(recipe_id):
    unit_system = get_unit_system()
    conn = get_db()
    with get_cursor() as cur:
        ensure_recipe_metadata_columns(cur)
        ensure_prep_schema(cur)
        conn.commit()
        recipe = get_recipe_by_id(cur, recipe_id)

        if not recipe:
            return None

        recipe_venues = []
        if db_table_exists(cur, 'public.recipe_venues') and db_table_exists(cur, 'public.venues'):
            cur.execute("""
                SELECT v.id, v.name
                FROM recipe_venues rv
                JOIN venues v ON v.id = rv.venue_id
                WHERE rv.recipe_id = %s
                ORDER BY v.name
            """, (recipe_id,))
            recipe_venues = cur.fetchall()

        components, total_cost, _ = build_component_tree(cur, recipe_id, 1, 0, set(), unit_system, apply_q_factor=True)
        base_total_cost = None
        q_factor_amount = None
        q_factor_percent = RECIPE_Q_FACTOR_PERCENT
        if total_cost is not None and q_factor_percent and q_factor_percent > 0:
            divisor = 1 + (q_factor_percent / 100)
            base_total_cost = total_cost / divisor
            q_factor_amount = total_cost - base_total_cost
        elif total_cost is not None:
            base_total_cost = total_cost
        yield_pricing = summarize_yield_pricing(
            total_cost,
            recipe.get('yield_qty'),
            recipe.get('yield_unit'),
            unit_system
        )

        return {
            'recipe': recipe,
            'recipe_venues': recipe_venues,
            'components': components,
            'component_count': len(components),
            'total_cost': total_cost,
            'cost_per_yield': yield_pricing['cost_per_yield'],
            'cost_per_yield_unit': yield_pricing['cost_per_yield_unit'],
            'base_total_cost': base_total_cost,
            'q_factor_percent': RECIPE_Q_FACTOR_PERCENT,
            'q_factor_amount': q_factor_amount,
            'instruction_steps': split_instruction_steps(recipe.get('instructions')),
            'critical_steps_list': split_instruction_steps(recipe.get('critical_steps')),
            'storage_lines': split_instruction_steps(recipe.get('storage_instructions')),
        }


@bp.errorhandler(Exception)
def handle_recipes_error(error):
    return handle_route_error(error, 'recipes')

@bp.route('/recipes')
@login_required
def recipes():
    with get_cursor() as cur:
        venues = get_active_venues(cur)
        search_query = (request.args.get('q') or '').strip()
        like_search_query = f"%{escape_like(search_query)}%"
        selected_recipe_type = normalize_recipe_type(request.args.get('recipe_type'))
        selected_category = (request.args.get('category') or '').strip()
        if selected_category.lower() == 'all':
            selected_category = ''

        selected_venue = (request.args.get('venue') or '').strip()
        if selected_venue and venues:
            selected_venue_ids = {row['id'] for row in venues}
            if selected_venue not in selected_venue_ids:
                selected_venue = ''

        cur.execute("""
            SELECT COALESCE(NULLIF(TRIM(category), ''), 'Uncategorized') AS category
            FROM recipes
            GROUP BY 1
            ORDER BY 1
        """)
        category_options = [row['category'] for row in cur.fetchall()]
        if selected_category and selected_category not in category_options:
            selected_category = ''

        has_recipe_venues = db_table_exists(cur, 'public.recipe_venues') and db_table_exists(cur, 'public.venues')
        if has_recipe_venues:
            cur.execute("""
                SELECT r.*,
                       COALESCE(ic.ingredient_count, 0) AS ingredient_count,
                       COALESCE(string_agg(DISTINCT v.name, ', ' ORDER BY v.name), '') AS venue_names,
                       COALESCE(string_agg(DISTINCT rv.venue_id, ',' ORDER BY rv.venue_id), '') AS venue_ids
                FROM recipes r
                LEFT JOIN (
                    SELECT recipe_id, COUNT(*) AS ingredient_count
                    FROM recipe_ingredients
                    GROUP BY recipe_id
                ) ic ON ic.recipe_id = r.id
                LEFT JOIN recipe_venues rv ON rv.recipe_id = r.id
                LEFT JOIN venues v ON v.id = rv.venue_id AND v.active = TRUE
                WHERE (%s = '' OR r.name ILIKE %s ESCAPE '\\' OR COALESCE(r.menu_descriptor, '') ILIKE %s ESCAPE '\\')
                  AND (%s IS NULL OR r.recipe_type = %s)
                  AND (
                        %s = '' OR
                        COALESCE(NULLIF(TRIM(r.category), ''), 'Uncategorized') = %s
                  )
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
                GROUP BY r.id, ic.ingredient_count
                ORDER BY r.name
            """, (
                search_query,
                like_search_query,
                like_search_query,
                selected_recipe_type,
                selected_recipe_type,
                selected_category,
                selected_category,
                selected_venue,
                selected_venue
            ))
        else:
            cur.execute("""
                SELECT r.*, COUNT(ri.id) as ingredient_count
                FROM recipes r
                LEFT JOIN recipe_ingredients ri ON r.id = ri.recipe_id
                WHERE (%s = '' OR r.name ILIKE %s ESCAPE '\\' OR COALESCE(r.menu_descriptor, '') ILIKE %s ESCAPE '\\')
                  AND (%s IS NULL OR r.recipe_type = %s)
                  AND (
                        %s = '' OR
                        COALESCE(NULLIF(TRIM(r.category), ''), 'Uncategorized') = %s
                  )
                GROUP BY r.id
                ORDER BY r.name
            """, (
                search_query,
                like_search_query,
                like_search_query,
                selected_recipe_type,
                selected_recipe_type,
                selected_category,
                selected_category
            ))

        recipes_list = cur.fetchall()
    menu_count = sum(1 for row in recipes_list if row.get('recipe_type') == 'menu')
    batch_count = sum(1 for row in recipes_list if row.get('recipe_type') == 'batch')
    active_filter_count = sum(1 for value in [
        search_query,
        selected_recipe_type,
        selected_category,
        selected_venue
    ] if value)

    def recipe_list_url(unit_value=None):
        params = {}
        if search_query:
            params['q'] = search_query
        if selected_recipe_type:
            params['recipe_type'] = selected_recipe_type
        if selected_category:
            params['category'] = selected_category
        if selected_venue:
            params['venue'] = selected_venue
        if unit_value:
            params['units'] = unit_value
        return url_for('recipes', **params)

    unit_urls = {
        'auto': recipe_list_url('auto'),
        'imperial': recipe_list_url('imperial'),
        'metric': recipe_list_url('metric')
    }

    return render_template(
        'recipes.html',
        recipes=recipes_list,
        venues=venues,
        selected_venue=selected_venue,
        search_query=search_query,
        selected_recipe_type=selected_recipe_type or '',
        selected_category=selected_category,
        category_options=category_options,
        menu_count=menu_count,
        batch_count=batch_count,
        active_filter_count=active_filter_count,
        clear_filters_url=url_for('recipes'),
        unit_urls=unit_urls
    )


@bp.route('/recipes/new', methods=['GET', 'POST'])
@login_required
@role_required('chef')
def recipe_new():
    conn = get_db()
    option_items_input = []
    selected_venue_ids = []
    with get_cursor() as cur:
        ensure_recipe_metadata_columns(cur)
        ensure_prep_schema(cur)
        conn.commit()
        if request.method == 'POST':
            errors = []
            name = (request.form.get('name') or '').strip()
            category = (request.form.get('category') or '').strip()
            yield_qty = (request.form.get('yield_qty') or '').strip()
            yield_unit = (request.form.get('yield_unit') or '').strip()
            yield_unit = normalize_unit(yield_unit) or yield_unit
            instructions = (request.form.get('instructions') or '').strip()
            menu_descriptor = (request.form.get('menu_descriptor') or '').strip()
            source_venue = (request.form.get('source_venue') or '').strip()
            equipment = (request.form.get('equipment') or '').strip()
            station = (request.form.get('station') or '').strip()
            critical_steps = (request.form.get('critical_steps') or '').strip()
            storage_instructions = (request.form.get('storage_instructions') or '').strip()
            recipe_type = infer_recipe_type(name, request.form.get('recipe_type'))
            prep_time_minutes = _parse_optional_int(request.form.get('prep_time_minutes'), 'Prep time', errors)
            shelf_life_days = _parse_optional_int(request.form.get('shelf_life_days'), 'Shelf life', errors)
            if recipe_type == 'menu':
                yield_qty = '1'
                yield_unit = 'serving'

            if not name:
                errors.append('Recipe name is required.')

            selected_venue_ids = parse_recipe_venue_ids(request, cur, errors)

            ingredient_ids = request.form.getlist('ingredient_id[]')
            ingredient_names = request.form.getlist('ingredient_name[]')
            ingredient_qtys = request.form.getlist('ingredient_qty[]')
            ingredient_units = request.form.getlist('ingredient_unit[]')
            ingredient_prep_notes = request.form.getlist('prep_note[]')

            for idx, ing_id in enumerate(ingredient_ids):
                ing_id = (ing_id or '').strip()
                ing_name = (ingredient_names[idx] if idx < len(ingredient_names) else '').strip()
                if not ing_id and ing_name:
                    errors.append(f'Ingredient \"{ing_name}\" was not found. Select it from the list or create it first.')

            sub_ids = request.form.getlist('sub_recipe_id[]')
            sub_names = request.form.getlist('sub_recipe_name[]')
            for idx, sub_id in enumerate(sub_ids):
                sub_id = (sub_id or '').strip()
                sub_name = (sub_names[idx] if idx < len(sub_names) else '').strip()
                if not sub_id and sub_name:
                    errors.append(f'Sub-recipe \"{sub_name}\" was not found. Select it from the list.')

            weighted_option_rows = parse_weighted_options_from_form(request, recipe_type, cur, errors)
            option_group_names = request.form.getlist('option_group_name[]')
            option_item_types = request.form.getlist('option_item_type[]')
            option_item_ids = request.form.getlist('option_item_id[]')
            option_item_names = request.form.getlist('option_item_name[]')
            option_qtys = request.form.getlist('option_qty[]')
            option_units = request.form.getlist('option_unit[]')
            option_weights = request.form.getlist('option_weight[]')
            max_options = max(
                len(option_group_names),
                len(option_item_types),
                len(option_item_ids),
                len(option_item_names),
                len(option_qtys),
                len(option_units),
                len(option_weights),
                0
            )
            for idx in range(max_options):
                group_name = (option_group_names[idx] if idx < len(option_group_names) else '').strip()
                item_type = (option_item_types[idx] if idx < len(option_item_types) else '').strip()
                item_id = (option_item_ids[idx] if idx < len(option_item_ids) else '').strip()
                item_name = (option_item_names[idx] if idx < len(option_item_names) else '').strip()
                qty = (option_qtys[idx] if idx < len(option_qtys) else '').strip()
                unit = (option_units[idx] if idx < len(option_units) else '').strip()
                weight = (option_weights[idx] if idx < len(option_weights) else '').strip()
                if not any([group_name, item_id, item_name, qty, unit, weight]):
                    continue
                option_items_input.append({
                    'group_name': group_name,
                    'item_type': item_type or 'recipe',
                    'item_id': item_id,
                    'item_name': item_name,
                    'quantity': qty,
                    'unit': unit,
                    'weight_percent': weight
                })

            if errors:
                flash(' '.join(sorted(set(errors))), 'error')
            else:
                recipe_id = generate_id('rec_')
                try:
                    cur.execute("""
                        INSERT INTO recipes (
                            id,
                            name,
                            category,
                            yield_qty,
                            yield_unit,
                            instructions,
                            source_venue,
                            equipment,
                            station,
                            critical_steps,
                            storage_instructions,
                            shelf_life_days,
                            prep_time_minutes,
                            recipe_type,
                            menu_descriptor
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        recipe_id,
                        name,
                        category or None,
                        yield_qty or None,
                        yield_unit or None,
                        instructions or None,
                        source_venue or None,
                        equipment or None,
                        station or None,
                        critical_steps or None,
                        storage_instructions or None,
                        shelf_life_days,
                        prep_time_minutes,
                        recipe_type,
                        menu_descriptor or None
                    ))

                    for venue_id in selected_venue_ids:
                        cur.execute("""
                            INSERT INTO recipe_venues (recipe_id, venue_id)
                            VALUES (%s, %s)
                            ON CONFLICT (recipe_id, venue_id) DO NOTHING
                        """, (recipe_id, venue_id))

                    # Ingredients
                    for idx, ing_id in enumerate(ingredient_ids):
                        ing_id = (ing_id or '').strip()
                        if not ing_id:
                            continue
                        qty = (ingredient_qtys[idx] if idx < len(ingredient_qtys) else '').strip()
                        unit = (ingredient_units[idx] if idx < len(ingredient_units) else '').strip()
                        prep_note = clean_prep_note(ingredient_prep_notes[idx] if idx < len(ingredient_prep_notes) else '')
                        unit = normalize_unit(unit) or unit
                        if qty == '':
                            continue
                        cur.execute("""
                            INSERT INTO recipe_ingredients (id, recipe_id, type, item_id, quantity, unit, prep_note)
                            VALUES (%s, %s, 'ingredient', %s, %s, %s, %s)
                        """, (
                            generate_id('ri_'),
                            recipe_id,
                            ing_id,
                            qty,
                            unit or None,
                            prep_note
                        ))

                    # Sub-recipes
                    sub_qtys = request.form.getlist('sub_recipe_qty[]')
                    sub_units = request.form.getlist('sub_recipe_unit[]')

                    for idx, sub_id in enumerate(sub_ids):
                        sub_id = (sub_id or '').strip()
                        if not sub_id:
                            continue
                        qty = (sub_qtys[idx] if idx < len(sub_qtys) else '').strip()
                        unit = (sub_units[idx] if idx < len(sub_units) else '').strip()
                        unit = normalize_unit(unit) or unit
                        if qty == '':
                            continue
                        cur.execute("""
                            INSERT INTO recipe_ingredients (id, recipe_id, type, item_id, quantity, unit)
                            VALUES (%s, %s, 'recipe', %s, %s, %s)
                        """, (
                            generate_id('ri_'),
                            recipe_id,
                            sub_id,
                            qty,
                            unit or None
                        ))

                    for option in weighted_option_rows:
                        cur.execute("""
                            INSERT INTO recipe_weighted_options (id, recipe_id, group_name, item_type, item_id, quantity, unit, weight_percent)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """, (
                            generate_id('rwo_'),
                            recipe_id,
                            option['group_name'],
                            option['item_type'],
                            option['item_id'],
                            option['quantity'],
                            option['unit'] or None,
                            option['weight_percent']
                        ))

                    conn.commit()
                    flash('Recipe created', 'success')
                    return redirect(url_for('recipe_detail', recipe_id=recipe_id))
                except Exception:
                    conn.rollback()
                    flash('Error creating recipe', 'error')

        # GET or validation error fallback
        cur.execute("SELECT id, name, unit, category FROM ingredients ORDER BY name")
        ingredients_list = cur.fetchall()
        venues = get_active_venues(cur)

        cur.execute("""
            SELECT id, name, yield_qty, yield_unit, recipe_type
            FROM recipes
            ORDER BY name
        """)
        recipes_list = cur.fetchall()

        cur.execute("""
            SELECT id, name, yield_qty, yield_unit, recipe_type
            FROM recipes
            ORDER BY name
        """)
        weighted_recipes_list = cur.fetchall()

    weighted_option_sources, option_label_map = _prepare_weighted_option_sources(ingredients_list, weighted_recipes_list)
    return render_template(
        'recipe_form.html',
        mode='new',
        recipe={
            'name': (request.form.get('name') if request.method == 'POST' else '') or '',
            'category': (request.form.get('category') if request.method == 'POST' else '') or '',
            'yield_qty': (request.form.get('yield_qty') if request.method == 'POST' else '') or '',
            'yield_unit': (request.form.get('yield_unit') if request.method == 'POST' else '') or '',
            'instructions': (request.form.get('instructions') if request.method == 'POST' else '') or '',
            'recipe_type': (request.form.get('recipe_type') if request.method == 'POST' else '') or '',
            'menu_descriptor': (request.form.get('menu_descriptor') if request.method == 'POST' else '') or '',
            'source_venue': (request.form.get('source_venue') if request.method == 'POST' else '') or '',
            'equipment': (request.form.get('equipment') if request.method == 'POST' else '') or '',
            'station': (request.form.get('station') if request.method == 'POST' else '') or '',
            'critical_steps': (request.form.get('critical_steps') if request.method == 'POST' else '') or '',
            'storage_instructions': (request.form.get('storage_instructions') if request.method == 'POST' else '') or '',
            'prep_time_minutes': (request.form.get('prep_time_minutes') if request.method == 'POST' else '') or '',
            'shelf_life_days': (request.form.get('shelf_life_days') if request.method == 'POST' else '') or ''
        },
        ingredients=ingredients_list,
        recipes=recipes_list,
        recipe_picker_labels={row['id']: _recipe_picker_label(row) for row in recipes_list},
        weighted_recipes=weighted_recipes_list,
        weighted_option_sources=weighted_option_sources,
        venues=venues,
        selected_venue_ids=selected_venue_ids,
        ingredient_items=[],
        subrecipe_items=[],
        prep_note_options=PREP_NOTE_OPTIONS,
        option_items=_prepare_option_items_for_form(option_items_input, option_label_map)
    )

@bp.route('/recipes/<recipe_id>/edit', methods=['GET', 'POST'])
@login_required
@role_required('chef')
def recipe_edit(recipe_id):
    conn = get_db()
    with get_cursor() as cur:
        ensure_recipe_metadata_columns(cur)
        ensure_prep_schema(cur)
        conn.commit()
        recipe = get_recipe_by_id(cur, recipe_id)
        if not recipe:
            flash('Recipe not found', 'error')
            return redirect(url_for('recipes'))

        selected_venue_ids = get_recipe_venue_ids(cur, recipe_id)

        if request.method == 'POST':
            errors = []
            name = (request.form.get('name') or '').strip()
            category = (request.form.get('category') or '').strip()
            yield_qty = (request.form.get('yield_qty') or '').strip()
            yield_unit = (request.form.get('yield_unit') or '').strip()
            yield_unit = normalize_unit(yield_unit) or yield_unit
            instructions = (request.form.get('instructions') or '').strip()
            menu_descriptor = (request.form.get('menu_descriptor') or '').strip()
            source_venue = (request.form.get('source_venue') or '').strip()
            equipment = (request.form.get('equipment') or '').strip()
            station = (request.form.get('station') or '').strip()
            critical_steps = (request.form.get('critical_steps') or '').strip()
            storage_instructions = (request.form.get('storage_instructions') or '').strip()
            recipe_type = infer_recipe_type(name, request.form.get('recipe_type'))
            prep_time_minutes = _parse_optional_int(request.form.get('prep_time_minutes'), 'Prep time', errors)
            shelf_life_days = _parse_optional_int(request.form.get('shelf_life_days'), 'Shelf life', errors)
            if recipe_type == 'menu':
                yield_qty = '1'
                yield_unit = 'serving'

            recipe = dict(recipe)
            recipe.update({
                'name': name,
                'category': category,
                'yield_qty': yield_qty,
                'yield_unit': yield_unit,
                'instructions': instructions,
                'recipe_type': recipe_type,
                'menu_descriptor': menu_descriptor,
                'source_venue': source_venue,
                'equipment': equipment,
                'station': station,
                'critical_steps': critical_steps,
                'storage_instructions': storage_instructions,
                'prep_time_minutes': prep_time_minutes,
                'shelf_life_days': shelf_life_days,
            })

            if not name:
                errors.append('Recipe name is required.')

            selected_venue_ids = parse_recipe_venue_ids(request, cur, errors)

            ingredient_ids = request.form.getlist('ingredient_id[]')
            ingredient_names = request.form.getlist('ingredient_name[]')
            ingredient_qtys = request.form.getlist('ingredient_qty[]')
            ingredient_units = request.form.getlist('ingredient_unit[]')
            ingredient_prep_notes = request.form.getlist('prep_note[]')

            for idx, ing_id in enumerate(ingredient_ids):
                ing_id = (ing_id or '').strip()
                ing_name = (ingredient_names[idx] if idx < len(ingredient_names) else '').strip()
                if not ing_id and ing_name:
                    errors.append(f'Ingredient \"{ing_name}\" was not found. Select it from the list or create it first.')

            sub_ids = request.form.getlist('sub_recipe_id[]')
            sub_names = request.form.getlist('sub_recipe_name[]')
            for idx, sub_id in enumerate(sub_ids):
                sub_id = (sub_id or '').strip()
                sub_name = (sub_names[idx] if idx < len(sub_names) else '').strip()
                if not sub_id and sub_name:
                    errors.append(f'Sub-recipe \"{sub_name}\" was not found. Select it from the list.')

            weighted_option_rows = parse_weighted_options_from_form(request, recipe_type, cur, errors)
            option_group_names = request.form.getlist('option_group_name[]')
            option_item_types = request.form.getlist('option_item_type[]')
            option_item_ids = request.form.getlist('option_item_id[]')
            option_item_names = request.form.getlist('option_item_name[]')
            option_qtys = request.form.getlist('option_qty[]')
            option_units = request.form.getlist('option_unit[]')
            option_weights = request.form.getlist('option_weight[]')

            if errors:
                flash(' '.join(sorted(set(errors))), 'error')
            else:
                try:
                    cur.execute("""
                        UPDATE recipes
                        SET name = %s,
                            category = %s,
                            yield_qty = %s,
                            yield_unit = %s,
                            instructions = %s,
                            source_venue = %s,
                            equipment = %s,
                            station = %s,
                            critical_steps = %s,
                            storage_instructions = %s,
                            shelf_life_days = %s,
                            prep_time_minutes = %s,
                            recipe_type = %s,
                            menu_descriptor = %s
                        WHERE id = %s
                    """, (
                        name,
                        category or None,
                        yield_qty or None,
                        yield_unit or None,
                        instructions or None,
                        source_venue or None,
                        equipment or None,
                        station or None,
                        critical_steps or None,
                        storage_instructions or None,
                        shelf_life_days,
                        prep_time_minutes,
                        recipe_type,
                        menu_descriptor or None,
                        recipe_id
                    ))

                    sync_forecasting_menu_items_for_recipe(
                        cur,
                        recipe_id,
                        name=name,
                        category=category,
                        description=menu_descriptor,
                    )

                    if db_table_exists(cur, 'public.recipe_venues'):
                        cur.execute("DELETE FROM recipe_venues WHERE recipe_id = %s", (recipe_id,))
                        for venue_id in selected_venue_ids:
                            cur.execute("""
                                INSERT INTO recipe_venues (recipe_id, venue_id)
                                VALUES (%s, %s)
                                ON CONFLICT (recipe_id, venue_id) DO NOTHING
                            """, (recipe_id, venue_id))

                    cur.execute("DELETE FROM recipe_ingredients WHERE recipe_id = %s", (recipe_id,))

                    for idx, ing_id in enumerate(ingredient_ids):
                        ing_id = (ing_id or '').strip()
                        if not ing_id:
                            continue
                        qty = (ingredient_qtys[idx] if idx < len(ingredient_qtys) else '').strip()
                        unit = (ingredient_units[idx] if idx < len(ingredient_units) else '').strip()
                        prep_note = clean_prep_note(ingredient_prep_notes[idx] if idx < len(ingredient_prep_notes) else '')
                        unit = normalize_unit(unit) or unit
                        if qty == '':
                            continue
                        cur.execute("""
                            INSERT INTO recipe_ingredients (id, recipe_id, type, item_id, quantity, unit, prep_note)
                            VALUES (%s, %s, 'ingredient', %s, %s, %s, %s)
                        """, (
                            generate_id('ri_'),
                            recipe_id,
                            ing_id,
                            qty,
                            unit or None,
                            prep_note
                        ))

                    sub_qtys = request.form.getlist('sub_recipe_qty[]')
                    sub_units = request.form.getlist('sub_recipe_unit[]')

                    for idx, sub_id in enumerate(sub_ids):
                        sub_id = (sub_id or '').strip()
                        if not sub_id:
                            continue
                        qty = (sub_qtys[idx] if idx < len(sub_qtys) else '').strip()
                        unit = (sub_units[idx] if idx < len(sub_units) else '').strip()
                        unit = normalize_unit(unit) or unit
                        if qty == '':
                            continue
                        cur.execute("""
                            INSERT INTO recipe_ingredients (id, recipe_id, type, item_id, quantity, unit)
                            VALUES (%s, %s, 'recipe', %s, %s, %s)
                        """, (
                            generate_id('ri_'),
                            recipe_id,
                            sub_id,
                            qty,
                            unit or None
                        ))

                    cur.execute("DELETE FROM recipe_weighted_options WHERE recipe_id = %s", (recipe_id,))
                    for option in weighted_option_rows:
                        cur.execute("""
                            INSERT INTO recipe_weighted_options (id, recipe_id, group_name, item_type, item_id, quantity, unit, weight_percent)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """, (
                            generate_id('rwo_'),
                            recipe_id,
                            option['group_name'],
                            option['item_type'],
                            option['item_id'],
                            option['quantity'],
                            option['unit'] or None,
                            option['weight_percent']
                        ))

                    conn.commit()
                    flash('Recipe updated', 'success')
                    return redirect(url_for('recipe_detail', recipe_id=recipe_id))
                except Exception:
                    conn.rollback()
                    flash('Error updating recipe', 'error')

        # GET or validation error fallback
        components = get_recipe_components(cur, recipe_id)
        ingredient_items = [c for c in components if c.get('type') == 'ingredient']
        subrecipe_items = [c for c in components if c.get('type') == 'recipe']
        if request.method == 'POST':
            option_items = []
            max_options = max(
                len(option_group_names),
                len(option_item_types),
                len(option_item_ids),
                len(option_item_names),
                len(option_qtys),
                len(option_units),
                len(option_weights),
                0
            )
            for idx in range(max_options):
                group_name = (option_group_names[idx] if idx < len(option_group_names) else '').strip()
                item_type = (option_item_types[idx] if idx < len(option_item_types) else '').strip()
                item_id = (option_item_ids[idx] if idx < len(option_item_ids) else '').strip()
                item_name = (option_item_names[idx] if idx < len(option_item_names) else '').strip()
                qty = (option_qtys[idx] if idx < len(option_qtys) else '').strip()
                unit = (option_units[idx] if idx < len(option_units) else '').strip()
                weight = (option_weights[idx] if idx < len(option_weights) else '').strip()
                if not any([group_name, item_id, item_name, qty, unit, weight]):
                    continue
                option_items.append({
                    'group_name': group_name,
                    'item_type': item_type or 'recipe',
                    'item_id': item_id,
                    'item_name': item_name,
                    'quantity': qty,
                    'unit': unit,
                    'weight_percent': weight
                })
        else:
            option_items = get_recipe_weighted_options(cur, recipe_id)

        cur.execute("SELECT id, name, unit, category FROM ingredients ORDER BY name")
        ingredients_list = cur.fetchall()
        venues = get_active_venues(cur)

        cur.execute("""
            SELECT id, name, yield_qty, yield_unit, recipe_type
            FROM recipes
            WHERE id != %s
            ORDER BY name
        """, (recipe_id,))
        recipes_list = cur.fetchall()

        cur.execute("""
            SELECT id, name, yield_qty, yield_unit, recipe_type
            FROM recipes
            WHERE id != %s
            ORDER BY name
        """, (recipe_id,))
        weighted_recipes_list = cur.fetchall()

    weighted_option_sources, option_label_map = _prepare_weighted_option_sources(ingredients_list, weighted_recipes_list)
    return render_template(
        'recipe_form.html',
        mode='edit',
        recipe=recipe,
        ingredients=ingredients_list,
        recipes=recipes_list,
        recipe_picker_labels={row['id']: _recipe_picker_label(row) for row in recipes_list},
        weighted_recipes=weighted_recipes_list,
        weighted_option_sources=weighted_option_sources,
        venues=venues,
        selected_venue_ids=selected_venue_ids,
        ingredient_items=ingredient_items,
        subrecipe_items=subrecipe_items,
        prep_note_options=PREP_NOTE_OPTIONS,
        option_items=_prepare_option_items_for_form(option_items, option_label_map)
    )

@bp.route('/recipe-generator', methods=['GET', 'POST'])
@login_required
@role_required('chef')
def recipe_generator():
    flash('Recipe Generator has been merged into New Recipe.', 'info')
    return redirect(url_for('recipe_new'))

@bp.route('/recipes/<recipe_id>/clone', methods=['POST'])
@login_required
@role_required('chef')
def recipe_clone(recipe_id):
    conn = get_db()
    with get_cursor() as cur:
        ensure_recipe_metadata_columns(cur)
        ensure_prep_schema(cur)
        conn.commit()
        source_recipe = get_recipe_by_id(cur, recipe_id)
        if not source_recipe:
            flash('Recipe not found', 'error')
            return redirect(url_for('recipes'))

        try:
            cloned = clone_recipe(cur, recipe_id)
            if not cloned:
                conn.rollback()
                flash('Unable to clone recipe.', 'error')
                return redirect(url_for('recipe_detail', recipe_id=recipe_id))
            conn.commit()
            flash(f"Cloned {source_recipe.get('name') or 'recipe'} as {cloned['name']}.", 'success')
            return redirect(url_for('recipe_edit', recipe_id=cloned['id']))
        except Exception:
            conn.rollback()
            flash('Error cloning recipe', 'error')
            return redirect(url_for('recipe_detail', recipe_id=recipe_id))

@bp.route('/recipes/<recipe_id>/delete', methods=['POST'])
@login_required
@role_required('admin')
def recipe_delete(recipe_id):
    conn = get_db()
    with get_cursor() as cur:
        cur.execute("SELECT id, name FROM recipes WHERE id = %s", (recipe_id,))
        recipe = cur.fetchone()
        if not recipe:
            flash('Recipe not found', 'error')
            return redirect(url_for('recipes'))

        cur.execute("""
            SELECT COUNT(*) AS count
            FROM recipe_ingredients
            WHERE type = 'recipe' AND item_id = %s
        """, (recipe_id,))
        used_in_recipes = int((cur.fetchone() or {}).get('count') or 0)

        used_in_rollouts = 0
        if db_table_exists(cur, 'public.menu_rollout_items'):
            cur.execute("""
                SELECT COUNT(*) AS count
                FROM menu_rollout_items
                WHERE recipe_id = %s
            """, (recipe_id,))
            used_in_rollouts = int((cur.fetchone() or {}).get('count') or 0)

        used_in_weighted = 0
        if db_table_exists(cur, 'public.recipe_weighted_options'):
            cur.execute("""
                SELECT COUNT(*) AS count
                FROM recipe_weighted_options
                WHERE item_type = 'recipe' AND item_id = %s
            """, (recipe_id,))
            used_in_weighted = int((cur.fetchone() or {}).get('count') or 0)

        blockers = []
        if used_in_recipes:
            blockers.append(f"{used_in_recipes} recipe(s)")
        if used_in_rollouts:
            blockers.append(f"{used_in_rollouts} rollout item(s)")
        if used_in_weighted:
            blockers.append(f"{used_in_weighted} weighted option(s)")

        if blockers:
            flash(f"Can't delete {recipe['name']} — used in {', '.join(blockers)}.", 'error')
            return redirect(url_for('recipe_detail', recipe_id=recipe_id))

        try:
            cur.execute("DELETE FROM recipe_weighted_options WHERE recipe_id = %s", (recipe_id,))
            cur.execute("DELETE FROM recipe_ingredients WHERE recipe_id = %s", (recipe_id,))
            cur.execute("DELETE FROM recipes WHERE id = %s", (recipe_id,))
            conn.commit()
            flash('Recipe deleted', 'success')
            return redirect(url_for('recipes'))
        except Exception:
            conn.rollback()
            flash('Error deleting recipe', 'error')
            return redirect(url_for('recipe_detail', recipe_id=recipe_id))

@bp.route('/recipes/<recipe_id>')
@login_required
def recipe_detail(recipe_id):
    context = _get_recipe_detail_context(recipe_id)
    if not context:
        flash('Recipe not found', 'error')
        return redirect(url_for('recipes'))
    return render_template('recipe_detail.html', auto_print=False, **context)


@bp.route('/recipes/<recipe_id>/print')
@login_required
def recipe_print(recipe_id):
    context = _get_recipe_detail_context(recipe_id)
    if not context:
        flash('Recipe not found', 'error')
        return redirect(url_for('recipes'))
    return render_template('recipe_detail.html', auto_print=True, **context)


@bp.route('/api/recipes/search')
@login_required
def api_recipes_search():
    query = (request.args.get('q') or '').strip()
    recipe_type = normalize_recipe_type(request.args.get('type'))
    try:
        limit = int(request.args.get('limit', 20))
    except (TypeError, ValueError):
        limit = 20
    limit = min(max(limit, 1), 100)

    sql = """
        SELECT id, name, recipe_type, yield_unit
        FROM recipes
        WHERE (%s = '' OR name ILIKE %s ESCAPE '\\')
    """
    like_query = f"%{escape_like(query)}%"
    params = [query, like_query]
    if recipe_type:
        sql += " AND recipe_type = %s"
        params.append(recipe_type)
    sql += " ORDER BY name LIMIT %s"
    params.append(limit)

    with get_cursor() as cur:
        cur.execute(sql, tuple(params))
        results = cur.fetchall()

    return jsonify({'results': results})
