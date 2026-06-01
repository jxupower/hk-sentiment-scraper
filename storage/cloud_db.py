"""Supabase Postgres connection helper.

Used by `storage/cloud_repository.py` for historical_prices + fundamentals_snapshots.
All other tables stay in local SQLite.

Connection pooling: ThreadedConnectionPool with maxconn=10 to stay well below
Supabase free-tier's ~60-direct-connection cap.

Usage:
    from storage.cloud_db import cursor
    with cursor() as cur:
        cur.execute("SELECT 1")
        print(cur.fetchone())
"""
from contextlib import contextmanager
from typing import Optional

import logging

from config import settings

_pool = None
_pool_init_error: Optional[Exception] = None

log = logging.getLogger(__name__)


def _init_pool():
    """Lazy init — only import psycopg2 + dial the DB when something actually
    needs cloud DB. Lets the rest of the app run without psycopg2 installed."""
    global _pool, _pool_init_error
    if _pool is not None or _pool_init_error is not None:
        return
    try:
        from psycopg2.pool import ThreadedConnectionPool
    except ImportError as e:
        _pool_init_error = e
        log.warning("psycopg2 not installed; cloud DB disabled. Run: pip install psycopg2-binary")
        return
    if not settings.SUPABASE_DB_URL:
        _pool_init_error = RuntimeError("SUPABASE_DB_URL not set in .env")
        return
    try:
        _pool = ThreadedConnectionPool(
            minconn=1, maxconn=10, dsn=settings.SUPABASE_DB_URL,
            connect_timeout=10,
        )
        log.info("Supabase connection pool initialized")
    except Exception as e:
        _pool_init_error = e
        log.error("Failed to init Supabase pool: %s", e)


def available() -> bool:
    """True if the cloud DB is reachable. Cheap — uses the cached pool state."""
    if not settings.cloud_db_configured():
        return False
    _init_pool()
    return _pool is not None


@contextmanager
def connection():
    """Yield a pooled Postgres connection. Returns it to the pool on exit."""
    _init_pool()
    if _pool is None:
        raise RuntimeError(
            f"Cloud DB unavailable: {_pool_init_error}. "
            "Set USE_CLOUD_DB=true and SUPABASE_DB_URL in .env."
        )
    conn = _pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


@contextmanager
def cursor(dict_rows: bool = False):
    """Yield a cursor with auto-commit on success. Pass dict_rows=True to get
    DictCursor (rows accessible by column name like sqlite3.Row)."""
    import psycopg2.extras
    with connection() as conn:
        cursor_factory = psycopg2.extras.RealDictCursor if dict_rows else None
        with conn.cursor(cursor_factory=cursor_factory) as cur:
            yield cur


def ping() -> bool:
    """Round-trip a SELECT 1 to verify connectivity. Returns True on success."""
    try:
        with cursor() as cur:
            cur.execute("SELECT 1")
            return cur.fetchone()[0] == 1
    except Exception as e:
        log.error("Cloud DB ping failed: %s", e)
        return False


def close():
    """Close all pooled connections. Call at process shutdown."""
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None
