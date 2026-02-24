import os
import time
from contextlib import contextmanager

import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from flask import g
from dotenv import load_dotenv

load_dotenv()

_POOL = None
_POOL_MIN = int(os.getenv("DB_POOL_MIN", "1"))
_POOL_MAX = int(os.getenv("DB_POOL_MAX", "10"))
_CONNECT_RETRIES = int(os.getenv("DB_CONNECT_RETRIES", "3"))
_RETRY_DELAY_SECONDS = float(os.getenv("DB_RETRY_DELAY_SECONDS", "0.2"))


class PooledConnectionProxy:
    """Wrap a pooled psycopg2 connection so .close() returns it to the pool."""

    def __init__(self, raw_conn):
        self._raw_conn = raw_conn
        self._released = False

    @property
    def closed(self):
        if self._released:
            return 1
        return getattr(self._raw_conn, "closed", 1)

    def close(self):
        if self._released:
            return
        try:
            if not getattr(self._raw_conn, "closed", 1):
                _get_pool().putconn(self._raw_conn)
        finally:
            self._released = True

    def __getattr__(self, name):
        if self._released:
            raise psycopg2.InterfaceError("Connection already returned to pool")
        return getattr(self._raw_conn, name)


def _get_pool():
    global _POOL
    if _POOL is None:
        _POOL = pool.ThreadedConnectionPool(
            _POOL_MIN,
            _POOL_MAX,
            dsn=os.getenv("DATABASE_URL"),
        )
    return _POOL


def _acquire_conn_with_retry():
    last_error = None
    for attempt in range(max(1, _CONNECT_RETRIES)):
        try:
            return _get_pool().getconn()
        except psycopg2.OperationalError as exc:
            last_error = exc
            if attempt < (_CONNECT_RETRIES - 1):
                time.sleep(_RETRY_DELAY_SECONDS * (attempt + 1))
    if last_error:
        raise last_error
    raise psycopg2.OperationalError("Unable to acquire a database connection")


def get_db():
    conn = g.get("db_conn")
    if conn is None or getattr(conn, "closed", 1):
        raw_conn = _acquire_conn_with_retry()
        conn = PooledConnectionProxy(raw_conn)
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
        if conn is None:
            return
        if exception is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass
