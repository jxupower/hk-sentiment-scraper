"""HK daily OHLCV via akshare (Eastmoney backend) — fast, untainted by
yfinance rate-limiting.

Why two API calls per ticker: akshare's `stock_hk_daily` returns either raw
prices (`adjust=''`) OR forward-adjusted prices (`adjust='qfq'`) but not
both. To keep `close` (raw) and `adj_close` (split + dividend adjusted)
semantically aligned with yfinance-sourced rows already in the DB, we
fetch both and join on date. Total ~2-4s per ticker vs yfinance's
unbounded throttling — acceptable trade-off for schema consistency.

Ticker format: our DB uses '0700.HK' (4-digit + .HK). akshare wants
'00700' (zero-padded 5-digit, no suffix). Conversion is `ticker.split('.')
[0].zfill(5)`.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from typing import Optional

import akshare as ak

from utils.logger import get_logger
from utils.rate_limiter import get_shared_limiter

logger = get_logger(__name__)

# Parallelism (perf P3.17). akshare (Eastmoney backend) is I/O-bound —
# 8 workers give ~8× wall-clock reduction on multi-thousand-ticker
# refreshes. The shared HostRateLimiter still enforces the throttle
# per-host so Eastmoney's rate limits are respected.
_AKSHARE_WORKERS = 8
_AKSHARE_HOST = "em.eastmoney.com"


def _to_ak_symbol(ticker: str) -> str:
    """0700.HK -> 00700. Handles both 4-digit (our format) and already-
    5-digit inputs."""
    base = ticker.split(".")[0]
    return base.zfill(5)


def fetch_one_index(index_code: str) -> list[dict]:
    """Fetch a HK index's full daily history (HSI, HSCEI, HSTECH, etc.).

    The Risk Forecast tab calls this for benchmarks. We use the SINA
    variant of akshare's HK-index endpoint (`stock_hk_index_daily_sina`)
    because the EM variant raised RemoteDisconnected during testing
    while sina returns ~13 years of clean daily data instantly.

    `index_code` is the bare symbol used by sina (e.g. "HSI", "HSCEI",
    "HSTECH") — caller strips any "^" prefix we use internally to
    distinguish indices from equities in our `historical_prices` table.

    Returns the same dict shape as fetch_one so the existing upsert path
    accepts them unchanged. close == adj_close (indices have no
    splits/dividends to adjust for).
    """
    try:
        df = ak.stock_hk_index_daily_sina(symbol=index_code)
    except Exception as e:
        logger.warning("akshare index fetch failed [%s]: %s", index_code, e)
        return []
    if df is None or df.empty:
        return []

    out = []
    for _, row in df.iterrows():
        d = row["date"]
        close = _fnum(row.get("close"))
        out.append({
            "date": d.isoformat() if hasattr(d, "isoformat") else str(d)[:10],
            "open": _fnum(row.get("open")),
            "high": _fnum(row.get("high")),
            "low":  _fnum(row.get("low")),
            "close": close,
            "adj_close": close,  # no corporate actions on an index
            "volume": _inum(row.get("volume")),
        })
    return out


def fetch_one(ticker: str) -> list[dict]:
    """Fetch one ticker's full price history via akshare. Returns rows in
    the same shape as scrapers.historical_price_scraper.fetch_one so the
    repo's upsert_rows accepts them unchanged.

    Returns [] on any failure (delisted, network error, akshare schema
    surprise).
    """
    sym = _to_ak_symbol(ticker)
    try:
        df_raw = ak.stock_hk_daily(symbol=sym, adjust="")
    except Exception as e:
        logger.warning("akshare raw fetch failed [%s]: %s", ticker, e)
        return []
    if df_raw is None or df_raw.empty:
        return []

    try:
        df_adj = ak.stock_hk_daily(symbol=sym, adjust="qfq")
    except Exception as e:
        logger.warning("akshare qfq fetch failed [%s] — using raw close as adj_close: %s",
                        ticker, e)
        df_adj = None

    # Index both by date for the join. akshare's `date` column is
    # datetime.date already.
    adj_by_date = {}
    if df_adj is not None and not df_adj.empty:
        for _, row in df_adj.iterrows():
            adj_by_date[row["date"]] = row.get("close")

    out = []
    for _, row in df_raw.iterrows():
        d = row["date"]
        adj = adj_by_date.get(d, row.get("close"))  # fallback: raw if qfq missing
        out.append({
            "date": d.isoformat() if hasattr(d, "isoformat") else str(d)[:10],
            "open": _fnum(row.get("open")),
            "high": _fnum(row.get("high")),
            "low":  _fnum(row.get("low")),
            "close": _fnum(row.get("close")),
            "adj_close": _fnum(adj),
            "volume": _inum(row.get("volume")),
        })
    return out


def fetch_many(tickers: list[str], prices_repo,
               throttle_seconds: float = 0.5,
               verbose: bool = False,
               delisted_log_path: Optional[Path] = None) -> dict:
    """Fetch + upsert each ticker via akshare. Parallelised with a
    ThreadPoolExecutor + per-host throttle (perf P3.17). Wall-clock for
    a ~2.7 k-ticker HK refresh drops from ~2,300 s (sequential 0.5 s +
    ~0.4 s network per ticker) to ~330 s (8 workers × ~0.4 s network,
    throttle floor 0.5 s per host).

    Preserves the same summary shape + delisted-log behaviour as before
    so the seed script and cron paths don't change. `throttle_seconds`
    is still honoured but as the per-**host** minimum interval, not the
    per-**call** sleep.

    Concurrency-safe against `prices_repo.upsert_rows`: modern SQLite
    with WAL supports single-writer / multi-reader, and the Supabase
    HistoricalPricesRepository uses the pooled psycopg2 connection —
    both are thread-safe for parallel upserts.
    """
    # Refresh the shared limiter's interval to match the caller's ask
    # for this run. Cheap; happens exactly once per fetch_many call.
    limiter = get_shared_limiter()
    limiter._min_interval = float(throttle_seconds)

    counts_lock = Lock()
    attempted = 0
    tickers_with_data = 0
    total_rows = 0
    failed = 0
    newly_delisted: list[str] = []

    def _work(idx: int, ticker: str):
        limiter.wait(_AKSHARE_HOST)
        try:
            rows = fetch_one(ticker)
        except Exception as e:
            logger.warning("akshare fetch_one crashed [%s]: %s", ticker, e)
            return idx, ticker, None, "crashed"

        if not rows:
            if verbose:
                logger.info("  [%d/%d] %s: no data",
                              idx, len(tickers), ticker)
            return idx, ticker, [], "empty"

        try:
            n = prices_repo.upsert_rows(ticker, rows)
            if verbose:
                logger.info("  [%d/%d] %s: %d rows",
                              idx, len(tickers), ticker, n)
            return idx, ticker, rows, ("ok", n)
        except Exception as e:
            logger.warning("price persist failed [%s]: %s", ticker, e)
            return idx, ticker, rows, "persist_failed"

    with ThreadPoolExecutor(max_workers=_AKSHARE_WORKERS,
                              thread_name_prefix="akshare") as pool:
        futures = [pool.submit(_work, i, t)
                    for i, t in enumerate(tickers, start=1)]
        for fut in as_completed(futures):
            _idx, ticker, rows, status = fut.result()
            with counts_lock:
                attempted += 1
                if status == "crashed" or status == "persist_failed":
                    failed += 1
                elif status == "empty":
                    newly_delisted.append(ticker)
                elif isinstance(status, tuple) and status[0] == "ok":
                    tickers_with_data += 1
                    total_rows += status[1]

    # Persist newly-confirmed-empty tickers so future runs skip them.
    if delisted_log_path and newly_delisted:
        delisted_log_path.parent.mkdir(parents=True, exist_ok=True)
        existing = set()
        if delisted_log_path.exists():
            existing = {ln.strip() for ln in delisted_log_path.read_text().splitlines()
                         if ln.strip()}
        new_only = [t for t in newly_delisted if t not in existing]
        if new_only:
            with delisted_log_path.open("a", encoding="utf-8") as f:
                for t in new_only:
                    f.write(f"{t}\n")
            logger.info("recorded %d newly delisted tickers to %s",
                         len(new_only), delisted_log_path)

    summary = {
        "attempted": attempted,
        "tickers_with_data": tickers_with_data,
        "total_rows": total_rows,
        "failed_tickers": failed,
        "newly_delisted": newly_delisted,
    }
    logger.info("akshare price seed complete: %s",
                {k: (len(v) if isinstance(v, list) else v) for k, v in summary.items()})
    return summary


def _fnum(v):
    if v is None:
        return None
    try:
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def _inum(v):
    if v is None:
        return None
    try:
        f = float(v)
        if f != f:
            return None
        return int(f)
    except (TypeError, ValueError):
        return None
