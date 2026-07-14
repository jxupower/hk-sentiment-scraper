"""Single shared cache for index-ticker (^HSI / ^GSPC / …) price fetches.

Before this module, three code paths independently fetched the default
market index:

  1. `main.py` dashboard pre-warm thread (per market, on boot)
  2. `dashboard/market_callbacks.py` on Market-tab first render
  3. `dashboard/risk_callbacks.py` when the user picks an index in Risk Forecast

Each had its own in-process cache (or none). On a cold `/` load with two
market threads in flight and a user landing on Risk within seconds, the
same yfinance/akshare round-trip could fire up to 3× — hundreds of ms to
several seconds of duplicated network work on the critical path.

This module gives all three call sites one **per-ticker lock + TTL cache**:
the first caller does the network fetch, the others wait on the same lock
and hit warm cache. Slot-level locks (not a global lock) so a slow ^HSI
cold-fetch doesn't stall a parallel ^GSPC one.

Cache shape:
  - _PRICES: {ticker: (rows, expires_at)}  — the [{date, adj_close, ...}]
    payload returned by `analysis.data_loader.get_or_fetch_prices`.
  - _OHLC:   {ticker: (rows, expires_at)}  — the full OHLC payload from
    `PricesRepository.get_full_ohlc_series` (a superset of what
    get_or_fetch_prices returns; used by the Market-tab candle chart).

Both share the same 15-min TTL, aligned with PRICE_STALE_DAYS in
analysis/data_loader.py. `invalidate()` clears both — hooked into the
Screener's "Refresh prices now" button through _flush_perf_caches so an
explicit refresh is reflected on the Market/Risk tabs before the TTL
expires.
"""
from __future__ import annotations

import time
from threading import Lock
from typing import Optional

_TTL_SECONDS = 15 * 60

# Per-ticker lock table so parallel misses for different tickers don't
# queue. We create a fresh Lock per ticker on first sight; the tiny memory
# cost (a few dozen locks) is far cheaper than serialising ^HSI + ^GSPC.
_LOCKS: dict[str, Lock] = {}
_LOCKS_META = Lock()

_PRICES: dict[str, tuple[list[dict], float]] = {}
_OHLC: dict[str, tuple[list[dict], float]] = {}


def _ticker_lock(ticker: str) -> Lock:
    lock = _LOCKS.get(ticker)
    if lock is not None:
        return lock
    with _LOCKS_META:
        lock = _LOCKS.get(ticker)
        if lock is None:
            lock = Lock()
            _LOCKS[ticker] = lock
        return lock


def get_index_prices(ticker: str, db, *, period: Optional[str] = None) -> list[dict]:
    """Return the cached price payload for an index ticker (adj_close, etc.)
    Runs the underlying `get_or_fetch_prices` once per (ticker × TTL); all
    other callers hit warm cache. Safe for concurrent use across threads.

    `db` is passed through only on a cache miss.
    """
    now = time.time()
    cached = _PRICES.get(ticker)
    if cached is not None and cached[1] > now:
        return cached[0]
    with _ticker_lock(ticker):
        # Re-check under the lock — another thread may have populated it
        # while we were queued.
        cached = _PRICES.get(ticker)
        now = time.time()
        if cached is not None and cached[1] > now:
            return cached[0]
        from analysis.data_loader import get_or_fetch_prices
        rows = get_or_fetch_prices(ticker, db, period=period) if period \
                 else get_or_fetch_prices(ticker, db)
        rows = rows or []
        _PRICES[ticker] = (rows, now + _TTL_SECONDS)
        return rows


def get_index_ohlc(ticker: str, db) -> list[dict]:
    """Return the full OHLC series (open/high/low/close/adj_close/volume) for
    an index ticker. Primes `historical_prices` if needed via
    `get_index_prices`, then selects the OHLC columns from the repo.

    The two caches share a lock per ticker so a caller wanting OHLC
    doesn't fire a duplicate underlying fetch while a caller wanting
    plain prices is mid-way through the same one.
    """
    now = time.time()
    cached = _OHLC.get(ticker)
    if cached is not None and cached[1] > now:
        return cached[0]
    with _ticker_lock(ticker):
        cached = _OHLC.get(ticker)
        now = time.time()
        if cached is not None and cached[1] > now:
            return cached[0]
        # Prime the historical_prices table if this is the first sight.
        # We reuse get_index_prices so the prices cache warms too — if the
        # user flips from Market tab (OHLC path) to Risk tab (prices path)
        # within the TTL, both hits are warm.
        get_index_prices(ticker, db)
        from storage.factory import get_prices_repo
        rows = get_prices_repo(db).get_full_ohlc_series(ticker) or []
        _OHLC[ticker] = (rows, now + _TTL_SECONDS)
        return rows


def invalidate(ticker: Optional[str] = None) -> None:
    """Clear the cache. When `ticker` is None, wipes everything (used by
    the Screener's manual price-refresh button). Cheap and lock-free —
    a concurrent reader will simply take the miss path and repopulate.
    """
    if ticker is None:
        _PRICES.clear()
        _OHLC.clear()
        return
    _PRICES.pop(ticker, None)
    _OHLC.pop(ticker, None)
