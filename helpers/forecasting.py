import csv
import io
import os
import re
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta

from helpers.db_helpers import db_table_exists
from helpers.formatting import normalize_match_key
from helpers.shared import generate_id, to_float


FORECASTING_DAY_FIELDS = (
    ('mon', 'Mon', 0),
    ('tue', 'Tue', 1),
    ('wed', 'Wed', 2),
    ('thu', 'Thu', 3),
    ('fri', 'Fri', 4),
    ('sat', 'Sat', 5),
    ('sun', 'Sun', 6),
)

FORECASTING_PLAN_STATUSES = {'draft', 'submitted', 'archived'}
TOAST_PRODUCT_MIX_ALL_LEVELS_FILE = 'all levels.csv'


def escape_like(value):
    return value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')