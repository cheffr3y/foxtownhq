import os
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor
from flask import g
from dotenv import load_dotenv

load_dotenv()


def get_db():
    conn = g.get("db_conn")
    if conn is None or getattr(conn, "closed", 1):
        conn = psycopg2.connect(os.getenv("DATABASE_URL"))
        g.db_conn = conn
    return conn


@contextmanager
def get_cursor(dict_cursor=True):
    conn = get_db()
    if dict_cursor:
        cur = conn.cursor(cursor_factory=RealDictCursor)
    else:
        cur = conn.cursor()
    try:
        yield cur
    finally:
        cur.close()


def init_app(app):
    @app.teardown_appcontext
    def close_db(exception=None):
        conn = g.pop("db_conn", None)
        if conn is not None and not getattr(conn, "closed", 1):
            conn.close()
