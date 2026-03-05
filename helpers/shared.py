import json
import re
import uuid

from flask import current_app, flash, jsonify, redirect, request, url_for
from pypdf import PdfReader
from werkzeug.exceptions import HTTPException

def to_float(value):
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0

def parse_float_field(value, label, errors, required=False, min_value=None):
    text = (value or '').strip()
    if not text:
        if required:
            errors.append(f"{label} is required.")
        return None
    try:
        number = float(text)
    except ValueError:
        errors.append(f"{label} must be a number.")
        return None
    if min_value is not None and number < min_value:
        errors.append(f"{label} must be at least {min_value}.")
        return None
    return number

def generate_id(prefix):
    return f"{prefix}{uuid.uuid4().hex[:12]}"

INGREDIENT_CATEGORIES = [
    'Produce',
    'Herbs',
    'Dairy',
    'Meat',
    'Poultry',
    'Seafood',
    'Dry Goods',
    'Spices',
    'Baking',
    'Oils & Vinegars',
    'Condiments & Sauces',
    'Bread',
    'Frozen',
    'Beverages',
    'Packaging',
    'Disposables',
    'Cleaning',
    'Other'
]

def inject_helpers():
    # Delayed imports avoid circular references between helper modules.
    from helpers.recipes import RECIPE_CATEGORIES, RECIPE_TYPE_CHOICES
    from helpers.units import get_unit_system, smart_quantity

    return {
        'smart_quantity': smart_quantity,
        'unit_system': get_unit_system(),
        'recipe_type_choices': RECIPE_TYPE_CHOICES,
        'ingredient_categories': INGREDIENT_CATEGORIES,
        'recipe_categories': RECIPE_CATEGORIES,
        'venue_defaults': VENUE_DEFAULTS
    }

def handle_route_error(error, context='route'):
    if isinstance(error, HTTPException):
        raise error
    current_app.logger.exception("Route error in %s", context)
    if request.path.startswith('/api/'):
        return jsonify({'ok': False, 'error': 'Unexpected server error'}), 500
    flash('Something went wrong. Please try again.', 'error')
    return redirect(request.referrer or url_for('dashboard'))

def normalize_return_path(value):
    path = (value or '').strip()
    if not path:
        return None
    if not path.startswith('/') or path.startswith('//'):
        return None
    return path


def _clean_menu_text(value):
    text = re.sub(r'\s+', ' ', (value or '')).strip()
    text = text.replace('  ', ' ')
    return text

def parse_catering_pdf_to_template_items(file_stream):
    reader = PdfReader(file_stream)
    parsed = []
    current_major_section = 'Imported Menu'
    current_subsection = 'Items'
    current_package = ''
    current_item = None

    ignored_prefixes = (
        'Pricing is subject',
        'Receptions Serving',
        'Buffets Serving',
        'Listed prices are per person',
        'All are priced',
        'Price listed',
        'Place cards',
        'ALL DINNERS INCLUDE',
        'Consumption bar'
    )
    ignored_exact = {
        'Catering Menu',
        'THE START OF SOMETHING DELICIOUS'
    }

    def flush_current():
        nonlocal current_item
        if not current_item:
            return
        current_item['item_name'] = _clean_menu_text(current_item.get('item_name'))
        current_item['menu_descriptor'] = _clean_menu_text(current_item.get('menu_descriptor'))
        if current_item['item_name']:
            parsed.append(current_item)
        current_item = None

    for page in reader.pages:
        text = page.extract_text() or ''
        for raw_line in text.splitlines():
            line = _clean_menu_text(raw_line)
            if not line:
                continue
            if line in ignored_exact or line.startswith(ignored_prefixes):
                continue

            upper = line.upper()
            if upper in (
                'BREAKFAST ENHANCEMENTS', 'REFRESH AND RENEW: BREAKOUT PACKAGES',
                'SNACK ATTACK', 'MORNING BEVERAGES', 'STATIONS - DAYTIME',
                'RECEPTIONS DISPLAYS', 'RECEPTIONS STATIONS', 'SMALL BITES',
                "HORS D'OEUVRES", 'BUFFET PACKAGES', 'CUSTOMIZE YOUR DINNER BUFFET',
                'PLATED 3-COURSE DINNER', 'LATE NIGHT SNACKS',
                'FOXTOWN PREMIUM BEVERAGE PACKAGE', 'FOXTOWN STANDARD BEVERAGE PACKAGE',
                'BEER, WINE & SODA PACKAGES', 'PACKAGE ENHANCEMENTS'
            ):
                flush_current()
                current_major_section = line.title()
                current_subsection = 'Items'
                current_package = ''
                continue

            if upper in BANQUET_PACKAGE_SUBSECTIONS:
                flush_current()
                current_subsection = line.title()
                continue

            if upper.startswith('CHOICE OF ') or upper.startswith('CHOOSE ') or upper.endswith(' OPTIONS'):
                continue

            package_match = re.match(r'^([A-Z0-9 &\'’\-\(\)\.]+?)\s+\$\s*([0-9]+(?:\.[0-9]+)?|Market Price)$', line)
            if package_match and len(package_match.group(1).split()) >= 2:
                flush_current()
                current_package = _clean_menu_text(package_match.group(1).title())
                current_subsection = 'Items'
                continue

            item_with_price = re.match(r'^(.*?)\s+\$\s*([0-9]+(?:\.[0-9]+)?|Market Price)$', line)
            if item_with_price:
                flush_current()
                section_parts = [current_major_section]
                if current_package:
                    section_parts.append(current_package)
                if current_subsection and current_subsection != 'Items':
                    section_parts.append(current_subsection)
                section = ' · '.join(section_parts)
                current_item = {
                    'menu_section': section,
                    'item_name': _clean_menu_text(item_with_price.group(1)),
                    'menu_descriptor': '',
                    'price_text': item_with_price.group(2)
                }
                continue

            if '|' in line:
                flush_current()
                left, right = line.split('|', 1)
                section_parts = [current_major_section]
                if current_package:
                    section_parts.append(current_package)
                if current_subsection and current_subsection != 'Items':
                    section_parts.append(current_subsection)
                section = ' · '.join(section_parts)
                current_item = {
                    'menu_section': section,
                    'item_name': _clean_menu_text(left),
                    'menu_descriptor': _clean_menu_text(right),
                    'price_text': ''
                }
                continue

            is_heading_like = upper == line and len(line.split()) <= 8 and any(ch.isalpha() for ch in line)
            if is_heading_like and len(line) < 50:
                flush_current()
                current_subsection = line.title()
                continue

            if not current_item:
                section_parts = [current_major_section]
                if current_package:
                    section_parts.append(current_package)
                if current_subsection and current_subsection != 'Items':
                    section_parts.append(current_subsection)
                section = ' · '.join(section_parts)
                current_item = {
                    'menu_section': section,
                    'item_name': line,
                    'menu_descriptor': '',
                    'price_text': ''
                }
            else:
                if current_item.get('menu_descriptor'):
                    current_item['menu_descriptor'] = f"{current_item['menu_descriptor']} {line}".strip()
                else:
                    current_item['menu_descriptor'] = line

    flush_current()

    deduped = []
    seen = set()
    for item in parsed:
        key = (item.get('menu_section', '').lower(), item.get('item_name', '').lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped

def find_or_create_ingredient(cur, name, unit_value):
    cur.execute("SELECT id FROM ingredients WHERE LOWER(name) = LOWER(%s) LIMIT 1", (name,))
    existing = cur.fetchone()
    if existing:
        return existing['id'], False

    ingredient_id = generate_id('ing_')
    cur.execute("""
        INSERT INTO ingredients (id, name, unit)
        VALUES (%s, %s, %s)
    """, (
        ingredient_id,
        name,
        unit_value or None
    ))
    return ingredient_id, True

VENUE_DEFAULTS = [
    'Foxtown Brewing',
    "Renard's",
    'Interurban',
    'Heritage Meats',
    "11's Lounge",
    'Banquets',
    'Foxtown Landing'
]

BANQUET_MENU_SECTION_OPTIONS = [
    'Breakfast',
    'Breakouts',
    'Snacks',
    'Daytime Stations',
    'Reception Displays',
    'Reception Stations',
    'Small Bites',
    "Hors D'Oeuvres",
    'Buffet Packages',
    'Custom Buffet',
    'Plated Dinner',
    'Late Night Snacks',
    'Beverages',
    'Enhancements',
    'Other'
]

BANQUET_PACKAGE_SUBSECTIONS = {
    'STARTERS', 'ENTREES', 'SIDES', 'SWEETS', 'DESSERT', 'DESSERTS', 'COLD', 'WARM',
    'COLD APPETIZERS', 'WARM APPETIZERS'
}

def normalize_quarter(value):
    if not value:
        return None
    value = value.strip().upper()
    if value in ('1', 'Q1'):
        return 'Q1'
    if value in ('2', 'Q2'):
        return 'Q2'
    if value in ('3', 'Q3'):
        return 'Q3'
    if value in ('4', 'Q4'):
        return 'Q4'
    return value

__all__ = [
    'to_float',
    'parse_float_field',
    'generate_id',
    'INGREDIENT_CATEGORIES',
    'inject_helpers',
    'handle_route_error',
    'normalize_return_path',
    'parse_catering_pdf_to_template_items',
    'find_or_create_ingredient',
    'VENUE_DEFAULTS',
    'BANQUET_MENU_SECTION_OPTIONS',
    'BANQUET_PACKAGE_SUBSECTIONS',
    'normalize_quarter',
]
