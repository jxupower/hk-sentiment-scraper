"""Supabase Postgres connection helper.

Used by `storage/cloud_repository.py` for historical_prices + fundamentals_snapshots.
All other tables stay in local SQLite.

Connection pooling: ThreadedConnectionPool with maxconn=20 by default (Supabase
free tier allows ~60 direct connections; Pro allows ~200). Sizes are
env-configurable so a future scheduler-container split (see the perf
redesign plan, P2.9) can tune independently:

    SUPABASE_POOL_MIN=2  SUPABASE_POOL_MAX=20  SUPABASE_POOL_TIMEOUT_S=3.0

`connection()` waits up to SUPABASE_POOL_TIMEOUT_S when the pool is
exhausted, then raises a clear `PoolError`. This replaces the previous
maxconn=10 setup, which under a burst (multi-user click, scrape thread
holding a slow chart connection) would either fail-fast with a naked
`PoolError` traceback or, worse, appear as a UI hang on the caller side.

Usage:
    from storage.cloud_db import cursor
    with cursor() as cur:
        cur.execute("SELECT 1")
        print(cur.fetchone())
"""
from contextlib import contextmanager
from typing import Optional
import os
import time

import logging

from config import settings

_pool = None
_pool_init_error: Optional[Exception] = None

log = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


# Pool sizing + acquire-timeout. Read once at module import; changing env at
# runtime requires a process restart (matches how the rest of settings.py works).
_POOL_MIN = _env_int("SUPABASE_POOL_MIN", 2)
_POOL_MAX = _env_int("SUPABASE_POOL_MAX", 20)
_ACQUIRE_TIMEOUT_S = _env_float("SUPABASE_POOL_TIMEOUT_S", 3.0)


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
            minconn=_POOL_MIN, maxconn=_POOL_MAX, dsn=settings.SUPABASE_DB_URL,
            connect_timeout=10,
        )
        log.info("Supabase connection pool initialized (min=%d, max=%d, "
                 "acquire_timeout=%.1fs)",
                 _POOL_MIN, _POOL_MAX, _ACQUIRE_TIMEOUT_S)
    except Exception as e:
        _pool_init_error = e
        log.error("Failed to init Supabase pool: %s", e)


def _getconn_with_timeout():
    """Wait up to `_ACQUIRE_TIMEOUT_S` for a free pool connection.

    psycopg2's `ThreadedConnectionPool.getconn()` fails-fast with `PoolError`
    when the pool is exhausted — it does NOT block. That produces UX
    surprises under bursty load: a click that would succeed 100 ms later
    returns a traceback immediately. This wrapper polls at 50 ms intervals
    until the deadline; if still exhausted we raise the same `PoolError`
    the caller would have seen before (predictable error surface).
    """
    from psycopg2.pool import PoolError

    deadline = time.monotonic() + _ACQUIRE_TIMEOUT_S
    while True:
        try:
            return _pool.getconn()
        except PoolError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


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
    conn = _getconn_with_timeout()
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
