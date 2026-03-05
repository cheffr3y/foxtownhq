import re

from flask import request, session

from helpers.shared import to_float

UNIT_DEFS = {
    # Weight (base: g)
    'g': {'type': 'weight', 'to_base': 1.0, 'display': 'g', 'system': 'metric'},
    'kg': {'type': 'weight', 'to_base': 1000.0, 'display': 'kg', 'system': 'metric'},
    'oz': {'type': 'weight', 'to_base': 28.349523125, 'display': 'oz', 'system': 'imperial'},
    'lb': {'type': 'weight', 'to_base': 453.59237, 'display': 'lb', 'system': 'imperial'},
    # Volume (base: ml)
    'ml': {'type': 'volume', 'to_base': 1.0, 'display': 'ml', 'system': 'metric'},
    'l': {'type': 'volume', 'to_base': 1000.0, 'display': 'L', 'system': 'metric'},
    'tsp': {'type': 'volume', 'to_base': 4.92892159375, 'display': 'tsp', 'system': 'imperial'},
    'tbsp': {'type': 'volume', 'to_base': 14.78676478125, 'display': 'tbsp', 'system': 'imperial'},
    'fl oz': {'type': 'volume', 'to_base': 29.5735295625, 'display': 'fl oz', 'system': 'imperial'},
    'cup': {'type': 'volume', 'to_base': 236.5882365, 'display': 'cup', 'system': 'imperial'},
    'pt': {'type': 'volume', 'to_base': 473.176473, 'display': 'pt', 'system': 'imperial'},
    'qt': {'type': 'volume', 'to_base': 946.352946, 'display': 'qt', 'system': 'imperial'},
    'gal': {'type': 'volume', 'to_base': 3785.411784, 'display': 'gal', 'system': 'imperial'},
}

UNIT_ALIASES = {
    # Weight
    'g': 'g', 'gram': 'g', 'grams': 'g',
    'kg': 'kg', 'kilogram': 'kg', 'kilograms': 'kg',
    'oz': 'oz', 'ounce': 'oz', 'ounces': 'oz',
    'lb': 'lb', 'lbs': 'lb', 'pound': 'lb', 'pounds': 'lb',
    # Volume
    'ml': 'ml', 'milliliter': 'ml', 'milliliters': 'ml', 'millilitre': 'ml', 'millilitres': 'ml',
    'l': 'l', 'liter': 'l', 'liters': 'l', 'litre': 'l', 'litres': 'l',
    'tsp': 'tsp', 'teaspoon': 'tsp', 'teaspoons': 'tsp',
    'tbsp': 'tbsp', 'tablespoon': 'tbsp', 'tablespoons': 'tbsp',
    'floz': 'fl oz', 'fluidounce': 'fl oz', 'fluidounces': 'fl oz',
    'cup': 'cup', 'cups': 'cup',
    'pt': 'pt', 'pint': 'pt', 'pints': 'pt',
    'qt': 'qt', 'quart': 'qt', 'quarts': 'qt',
    'gal': 'gal', 'gallon': 'gal', 'gallons': 'gal',
}

SYSTEM_UNITS = {
    'metric': {
        'weight': ['kg', 'g'],
        'volume': ['l', 'ml'],
    },
    'imperial': {
        'weight': ['lb', 'oz'],
        'volume': ['gal', 'qt', 'pt', 'cup', 'fl oz'],
    },
    # Kitchen hybrid: metric weights, imperial volume.
    'hybrid': {
        'weight': ['kg', 'g'],
        'volume': ['gal', 'qt', 'pt', 'cup', 'fl oz'],
    }
}

def normalize_unit(unit):
    if not unit:
        return None
    key = ''.join(ch for ch in unit.lower() if ch.isalnum())
    return UNIT_ALIASES.get(key)

def get_unit_system():
    raw_system = (request.args.get('units') or '').strip().lower()
    aliases = {
        'kitchen': 'hybrid',
        'metric_weights': 'hybrid',
        'metric-weights': 'hybrid',
    }
    system = aliases.get(raw_system, raw_system)
    if system in ('auto', 'metric', 'imperial', 'hybrid'):
        session['unit_system'] = system
    selected = session.get('unit_system', 'auto')
    if selected not in ('auto', 'metric', 'imperial', 'hybrid'):
        return 'auto'
    return selected

def format_number(value, decimals=2):
    if value is None:
        return ''
    rounded = round(value, decimals)
    if abs(rounded - round(rounded)) < (10 ** -decimals):
        return str(int(round(rounded)))
    return f"{rounded:.{decimals}f}".rstrip('0').rstrip('.')

def convert_cost_per_unit(cost_per_unit, from_unit, to_unit):
    cost = to_float(cost_per_unit)
    from_canonical = normalize_unit(from_unit)
    to_canonical = normalize_unit(to_unit)
    if not from_canonical or not to_canonical:
        return cost
    if from_canonical == to_canonical:
        return cost
    from_def = UNIT_DEFS.get(from_canonical)
    to_def = UNIT_DEFS.get(to_canonical)
    if not from_def or not to_def:
        return cost
    if from_def['type'] != to_def['type']:
        return cost
    factor = to_def['to_base'] / from_def['to_base']
    return cost * factor

def convert_quantity(quantity, unit, system='auto'):
    quantity = to_float(quantity)
    canonical = normalize_unit(unit)
    if quantity is None or canonical not in UNIT_DEFS:
        return {
            'quantity': quantity,
            'unit': unit,
            'factor': 1.0,
            'converted': False,
            'type': None
        }

    unit_def = UNIT_DEFS[canonical]
    unit_type = unit_def['type']
    base_qty = quantity * unit_def['to_base']

    if system == 'auto':
        system = unit_def['system']

    candidates = SYSTEM_UNITS.get(system, {}).get(unit_type, [])
    if not candidates:
        return {
            'quantity': quantity,
            'unit': unit_def['display'],
            'factor': 1.0,
            'converted': False,
            'type': unit_type
        }

    chosen = candidates[-1]
    for candidate in candidates:
        candidate_qty = base_qty / UNIT_DEFS[candidate]['to_base']
        if candidate_qty >= 1:
            chosen = candidate
            break

    display_qty = base_qty / UNIT_DEFS[chosen]['to_base']
    factor = unit_def['to_base'] / UNIT_DEFS[chosen]['to_base']

    return {
        'quantity': display_qty,
        'unit': UNIT_DEFS[chosen]['display'],
        'factor': factor,
        'converted': chosen != canonical,
        'type': unit_type
    }

def smart_quantity(quantity, unit, system=None):
    unit_system = system or get_unit_system()
    result = convert_quantity(quantity, unit, unit_system)
    return {
        'quantity': format_number(result['quantity']),
        'unit': result['unit'] or unit or '',
        'converted': result['converted'],
        'factor': result['factor'],
        'type': result['type']
    }

def convert_quantity_between_units(quantity, from_unit, to_unit):
    amount = to_float(quantity)
    from_canonical = normalize_unit(from_unit)
    to_canonical = normalize_unit(to_unit)
    if not from_canonical or not to_canonical:
        return amount if (from_unit or '').strip().lower() == (to_unit or '').strip().lower() else None
    if from_canonical == to_canonical:
        return amount
    from_def = UNIT_DEFS.get(from_canonical)
    to_def = UNIT_DEFS.get(to_canonical)
    if not from_def or not to_def or from_def['type'] != to_def['type']:
        return None
    base_value = amount * from_def['to_base']
    return base_value / to_def['to_base']

def normalize_count_unit(unit):
    token = re.sub(r'\s+', ' ', (unit or '')).strip().lower().replace('.', '')
    if not token:
        return ''
    aliases = {
        'ea': 'each',
        'each': 'each',
        'serving': 'each',
        'servings': 'each',
        'plate': 'each',
        'plates': 'each',
        'person': 'each',
        'people': 'each',
        'guest': 'each',
        'guests': 'each',
        'dozen': 'dozen',
        'doz': 'dozen'
    }
    if token in aliases:
        return aliases[token]
    canonical = normalize_unit(token)
    if canonical in ('each', 'dozen'):
        return canonical
    return ''

def convert_count_units(quantity, from_unit, to_unit):
    qty = to_float(quantity)
    from_norm = normalize_count_unit(from_unit)
    to_norm = normalize_count_unit(to_unit)
    if qty <= 0:
        return 0
    if not from_norm or not to_norm:
        return None
    if from_norm == to_norm:
        return qty
    if from_norm == 'dozen' and to_norm == 'each':
        return qty * 12
    if from_norm == 'each' and to_norm == 'dozen':
        return qty / 12
    return None

__all__ = [
    'UNIT_DEFS',
    'UNIT_ALIASES',
    'SYSTEM_UNITS',
    'normalize_unit',
    'get_unit_system',
    'format_number',
    'convert_cost_per_unit',
    'convert_quantity',
    'smart_quantity',
    'convert_quantity_between_units',
    'normalize_count_unit',
    'convert_count_units',
]
