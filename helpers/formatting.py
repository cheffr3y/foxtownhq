import re

from helpers.shared import to_float

def format_display_value(value, precision=2):
    if value is None:
        return ''
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.{precision}f}".rstrip('0').rstrip('.')

def flatten_components_for_pdf(items, level=0):
    rows = []
    for item in items:
        name = item.get('item_name') or ''
        prefix = '  ' * level
        label = ''
        if item.get('type') == 'recipe':
            label = 'Sub-recipe'
        elif item.get('category'):
            label = item.get('category')

        quantity = item.get('display_quantity')
        if quantity is None:
            quantity = item.get('scaled_quantity_display') or item.get('scaled_quantity') or ''
        qty_text = format_display_value(quantity, precision=2)

        unit = item.get('display_unit') or item.get('unit') or ''
        rows.append({
            'name': f"{prefix}{name}",
            'qty': qty_text,
            'unit': unit,
            'notes': label
        })
        if item.get('children'):
            rows.extend(flatten_components_for_pdf(item.get('children'), level + 1))
    return rows

def flatten_components_for_packet(items, ingredient_map, level=0, explode_subrecipes=True, preserve_base_units=False):
    rows = []
    for item in items:
        item_type = item.get('type') or 'component'
        name = item.get('item_name') or 'Unknown'
        if preserve_base_units:
            quantity = item.get('scaled_quantity_display') or item.get('scaled_quantity') or ''
            unit = item.get('unit') or item.get('display_unit') or ''
        else:
            quantity = item.get('display_quantity')
            if quantity in (None, ''):
                quantity = item.get('scaled_quantity_display') or item.get('scaled_quantity') or ''
            unit = item.get('display_unit') or item.get('unit') or ''
        ext_cost = to_float(item.get('cost_total'))

        g_code = ''
        vendor = ''
        vendor_code = ''
        category = item.get('category') or ''
        if item_type == 'ingredient':
            ing = ingredient_map.get(item.get('item_id')) or {}
            g_code = ing.get('g_code') or ''
            vendor = ing.get('vendor') or ''
            vendor_code = ing.get('vendor_code') or ''
            category = ing.get('category') or category

        rows.append({
            'type': item_type,
            'name': f"{'  ' * level}{name}",
            'quantity': quantity,
            'unit': unit,
            'ext_cost': ext_cost,
            'g_code': g_code,
            'vendor': vendor,
            'vendor_code': vendor_code,
            'category': category,
            'notes': item.get('scale_note') or ''
        })
        has_children = bool(item.get('children'))
        if has_children and not explode_subrecipes and item_type == 'recipe':
            has_children = False
        if has_children:
            rows.extend(
                flatten_components_for_packet(
                    item['children'],
                    ingredient_map,
                    level + 1,
                    explode_subrecipes=explode_subrecipes,
                    preserve_base_units=preserve_base_units
                )
            )
    return rows

def collect_ingredient_usage_from_components(components, menu_label, usage_map):
    for item in components:
        if item.get('type') == 'ingredient' and item.get('item_id'):
            key = item.get('item_id')
            usage_map.setdefault(key, set()).add(menu_label)
        if item.get('children'):
            collect_ingredient_usage_from_components(item['children'], menu_label, usage_map)

def sanitize_sheet_title(name, existing):
    title = (name or 'Vendor').strip()
    for ch in ['[', ']', ':', '*', '?', '/', '\\']:
        title = title.replace(ch, ' ')
    title = ' '.join(title.split())
    if not title:
        title = 'Vendor'
    title = title[:31]
    base = title
    counter = 1
    while title in existing:
        suffix = f" {counter}"
        max_len = 31 - len(suffix)
        title = f"{base[:max_len]}{suffix}"
        counter += 1
    existing.add(title)
    return title

def make_safe_filename(text):
    cleaned = ''.join(ch for ch in (text or '') if ch.isalnum() or ch in (' ', '-', '_')).strip()
    if not cleaned:
        return 'order_guide'
    return cleaned.replace(' ', '_').lower()

def split_instruction_steps(instructions):
    raw = (instructions or '').strip()
    if not raw:
        return []
    normalized = raw.replace('\r\n', '\n').replace('\r', '\n')
    numbered_chunks = re.split(r'(?:^|\s+)(?=\d+\.\s+)', normalized.strip())
    parts = []
    if len(numbered_chunks) > 1:
        for chunk in numbered_chunks:
            text = re.sub(r'^\d+\.\s*', '', chunk.strip())
            if text:
                parts.append(text)
        if parts:
            return parts
    for line in normalized.split('\n'):
        text = re.sub(r'^\d+\.\s*', '', line.strip())
        if text:
            parts.append(text)
    if parts:
        return parts
    return [raw]

def normalize_match_key(value):
    text = re.sub(r'\s+', ' ', (value or '')).strip().lower()
    text = text.replace('  ', ' ')
    text = re.sub(r'^(rm|rb)\s+', '', text)
    text = re.sub(r'[^a-z0-9 ]+', ' ', text)
    return ' '.join(text.split())


def as_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def as_float(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def format_quantity(value):
    number = as_float(value)
    if abs(number - round(number)) < 1e-9:
        return f"{int(round(number)):,}"
    return f"{number:,.2f}".rstrip('0').rstrip('.')


def add_amount(accumulator, quantity, unit):
    qty = as_float(quantity)
    if qty <= 0:
        return
    clean_unit = (unit or '').strip() or 'units'
    key = clean_unit.lower()
    if key not in accumulator:
        accumulator[key] = {
            'unit': clean_unit,
            'qty': 0.0,
        }
    accumulator[key]['qty'] += qty


def amount_label(accumulator, max_parts=2):
    if not accumulator:
        return '0 units'
    ordered = sorted(accumulator.values(), key=lambda item: (-as_float(item.get('qty')), (item.get('unit') or '').lower()))
    parts = [f"{format_quantity(item.get('qty'))} {item.get('unit')}" for item in ordered[:max_parts]]
    if len(ordered) > max_parts:
        parts.append(f"+{len(ordered) - max_parts} more")
    return ' · '.join(parts)


def event_status_tone(status):
    key = (status or '').strip().lower()
    if key in ('confirmed', 'completed', 'executed'):
        return 'emerald'
    if key in ('cancelled', 'blocked'):
        return 'rose'
    return 'amber'


def estimate_prep_completion(event):
    status_key = (event.get('status') or '').strip().lower()
    if status_key in ('completed', 'executed'):
        return 100
    if status_key in ('cancelled',):
        return 0

    line_count = as_int(event.get('line_count'))
    guest_count = max(1, as_int(event.get('guests')))
    expected_line_count = max(1, round(guest_count / 26))
    coverage_ratio = min(1.0, line_count / expected_line_count) if expected_line_count else 0.0

    if status_key == 'confirmed':
        return int(max(42, min(97, round(55 + (coverage_ratio * 42)))))
    return int(max(10, min(84, round(18 + (coverage_ratio * 62)))))


def enrich_event(event):
    event_copy = dict(event)
    status_key = (event_copy.get('status') or 'planning').strip().lower()
    event_copy['status'] = status_key
    event_copy['status_tone'] = event_status_tone(status_key)
    event_copy['prep_completion'] = estimate_prep_completion(event_copy)
    event_copy['is_confirmed'] = status_key == 'confirmed'
    return event_copy

__all__ = [
    'format_display_value',
    'flatten_components_for_pdf',
    'flatten_components_for_packet',
    'collect_ingredient_usage_from_components',
    'sanitize_sheet_title',
    'make_safe_filename',
    'split_instruction_steps',
    'normalize_match_key',
    'as_int',
    'as_float',
    'format_quantity',
    'add_amount',
    'amount_label',
    'event_status_tone',
    'estimate_prep_completion',
    'enrich_event',
]
