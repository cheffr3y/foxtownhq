import os


def _to_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


PRICE_REFRESH_DAYS = int(os.getenv('PRICE_REFRESH_DAYS', '56'))
RECIPE_Q_FACTOR_PERCENT = _to_float(os.getenv('RECIPE_Q_FACTOR_PERCENT'), 5)
DEFAULT_Q_FACTOR_PERCENT = _to_float(os.getenv('DEFAULT_Q_FACTOR_PERCENT'), RECIPE_Q_FACTOR_PERCENT)
DEFAULT_TARGET_FOOD_COST_PERCENT = _to_float(os.getenv('DEFAULT_TARGET_FOOD_COST_PERCENT'), 20)
