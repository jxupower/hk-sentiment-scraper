"""Multi-year daily OHLCV via yfinance bulk download.

yfinance's `Ticker(t).history()` works per-ticker but is slow for batch use.
`yf.download(tickers=[batch], period='10y')` does many tickers at once and is
~10x faster, but has gotchas:
 - thread-unsafe (issue #2557) — don't multi-thread
 - rate-limited; chunk at ~50 tickers per call with sleep between
 - returns a wide DataFrame: columns are (price_field, ticker) MultiIndex
   when len(tickers) > 1, just plain columns when len(tickers) == 1
"""
import time
from pathlib import Path
from typing import Optional

import pandas as pd
import yfinance as yf

from utils.logger import get_logger

logger = get_logger(__name__)


def fetch_one(ticker: str, period: str = "10y") -> list[dict]:
    """Fetch one ticker's full price history. Returns list of dicts ready for
    HistoricalPricesRepository.upsert_rows."""
    try:
        df = yf.Ticker(ticker).history(period=period, auto_adjust=False)
    except Exception as e:
        logger.warning("yfinance history failed [%s]: %s", ticker, e)
        return []
    if df is None or df.empty:
        return []

    out = []
    for ts, row in df.iterrows():
        out.append({
            "date": ts.strftime("%Y-%m-%d"),
            "open": _fnum(row.get("Open")),
            "high": _fnum(row.get("High")),
            "low":  _fnum(row.get("Low")),
            "close": _fnum(row.get("Close")),
            "adj_close": _fnum(row.get("Adj Close") or row.get("Close")),
            "volume": _inum(row.get("Volume")),
        })
    return out


def fetch_many(tickers: list[str], prices_repo,
               period: str = "10y", batch_size: int = 50,
               throttle_seconds: float = 2.0,
               verbose: bool = False,
               delisted_log_path: Optional[Path] = None) -> dict:
    """Bulk-download price history for many tickers in chunks, write to repo.

    Returns summary dict: {attempted, tickers_with_data, total_rows, failed_tickers,
    newly_delisted}.

    verbose=True logs each ticker as it's persisted (used by long-running
    interactive seed scripts so the user can see progress within a batch).
    Off by default to keep scheduler/cron logs quiet.

    delisted_log_path: when set, tickers that come back empty from a bulk
    download whose OTHER tickers succeeded are appended (one per line) to
    this file as confirmed-no-data. Callers (e.g. resume_historical_seed)
    load this file at start and skip those tickers, avoiding repeat probes
    against yfinance. Tickers from a wholly-failed batch are NOT recorded —
    that's a transient error, not a confirmed delisting.
    """
    # Perf P3.17: parallelise batches. yfinance itself is thread-unsafe
    # (issue #2557 — `threads=False` inside yf.download is REQUIRED), but
    # separate `yf.download` INVOCATIONS are safe as long as we throttle
    # per-host to stay polite. Conservative worker count (4, not 8) for
    # yfinance because each batch is heavier than a single akshare call.
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from threading import Lock
    from utils.rate_limiter import get_shared_limiter

    limiter = get_shared_limiter()
    limiter._min_interval = float(throttle_seconds)
    _WORKERS = 4
    _HOST = "query1.finance.yahoo.com"

    counts_lock = Lock()
    attempted = 0
    tickers_with_data = 0
    total_rows = 0
    failed = 0
    newly_delisted: list[str] = []

    batches = [tickers[i:i + batch_size]
                for i in range(0, len(tickers), batch_size)]

    def _work_batch(batch_idx: int, batch: list[str]):
        limiter.wait(_HOST)
        try:
            df = yf.download(tickers=batch, period=period, group_by="ticker",
                             auto_adjust=False, threads=False, progress=False)
        except Exception as e:
            logger.warning("yf.download batch failed (%d tickers): %s",
                            len(batch), e)
            return batch_idx, batch, None, "download_failed"
        if df is None or df.empty:
            return batch_idx, batch, None, "empty"
        return batch_idx, batch, df, "ok"

    with ThreadPoolExecutor(max_workers=_WORKERS,
                              thread_name_prefix="yf-batch") as pool:
        futures = [pool.submit(_work_batch, i, b)
                    for i, b in enumerate(batches)]
        completed = 0
        for fut in as_completed(futures):
            batch_idx, batch, df, status = fut.result()
            with counts_lock:
                attempted += len(batch)
                completed += 1

            if status != "ok":
                with counts_lock:
                    failed += len(batch)
                continue

            # Per-ticker slice + upsert. Repo upserts are thread-safe
            # (WAL SQLite: single-writer, our upserts are per-ticker;
            # Supabase psycopg2: pooled). Doing them inside the future
            # keeps the pool saturated on I/O.
            local_newly_delisted: list[str] = []
            local_with_data = 0
            local_rows = 0
            local_failed = 0
            for idx, ticker in enumerate(batch, start=1):
                try:
                    if len(batch) == 1:
                        ticker_df = df
                    else:
                        ticker_df = (df[ticker]
                                       if ticker in df.columns.get_level_values(0)
                                       else None)
                    if ticker_df is None or ticker_df.empty:
                        if verbose:
                            logger.info("  [%d/%d] %s: no data",
                                          idx, len(batch), ticker)
                        local_newly_delisted.append(ticker)
                        continue
                    rows = []
                    for ts, row in ticker_df.dropna(how="all").iterrows():
                        rows.append({
                            "date": ts.strftime("%Y-%m-%d"),
                            "open": _fnum(row.get("Open")),
                            "high": _fnum(row.get("High")),
                            "low":  _fnum(row.get("Low")),
                            "close": _fnum(row.get("Close")),
                            "adj_close": _fnum(row.get("Adj Close") or row.get("Close")),
                            "volume": _inum(row.get("Volume")),
                        })
                    if rows:
                        n = prices_repo.upsert_rows(ticker, rows)
                        local_with_data += 1
                        local_rows += n
                        if verbose:
                            logger.info("  [%d/%d] %s: %d rows",
                                          idx, len(batch), ticker, n)
                    else:
                        local_newly_delisted.append(ticker)
                        if verbose:
                            logger.info("  [%d/%d] %s: empty after dropna",
                                          idx, len(batch), ticker)
                except Exception as e:
                    logger.warning("price persist failed [%s]: %s", ticker, e)
                    local_failed += 1

            with counts_lock:
                tickers_with_data += local_with_data
                total_rows += local_rows
                failed += local_failed
                newly_delisted.extend(local_newly_delisted)
                logger.info("price-history progress: %d/%d batches "
                            "(rows=%d, ok=%d, failed=%d)",
                            completed, len(batches), total_rows,
                            tickers_with_data, failed)

    # Persist the confirmed-delisted set so future runs skip them.
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
    logger.info("Historical price seed complete: %s",
                {k: (len(v) if isinstance(v, list) else v) for k, v in summary.items()})
    return summary


def _fnum(v) -> Optional[float]:
    if v is None or pd.isna(v):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _inum(v) -> Optional[int]:
    if v is None or pd.isna(v):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
