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
               verbose: bool = False) -> dict:
    """Bulk-download price history for many tickers in chunks, write to repo.

    Returns summary dict: {attempted, tickers_with_data, total_rows, failed_tickers}.

    verbose=True logs each ticker as it's persisted (used by long-running
    interactive seed scripts so the user can see progress within a batch).
    Off by default to keep scheduler/cron logs quiet.
    """
    attempted = 0
    tickers_with_data = 0
    total_rows = 0
    failed = 0

    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        attempted += len(batch)
        try:
            # group_by="ticker" makes the result easy to slice per-ticker
            df = yf.download(tickers=batch, period=period, group_by="ticker",
                             auto_adjust=False, threads=False, progress=False)
        except Exception as e:
            logger.warning("yf.download batch failed (%d tickers): %s", len(batch), e)
            failed += len(batch)
            time.sleep(throttle_seconds)
            continue

        if df is None or df.empty:
            failed += len(batch)
            time.sleep(throttle_seconds)
            continue

        # When batch has >1 ticker, columns are MultiIndex (ticker, price_field).
        # When batch has 1 ticker, columns are flat.
        for idx, ticker in enumerate(batch, start=1):
            try:
                if len(batch) == 1:
                    ticker_df = df
                else:
                    ticker_df = df[ticker] if ticker in df.columns.get_level_values(0) else None
                if ticker_df is None or ticker_df.empty:
                    if verbose:
                        logger.info("  [%d/%d] %s: no data", idx, len(batch), ticker)
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
                    tickers_with_data += 1
                    total_rows += n
                    if verbose:
                        logger.info("  [%d/%d] %s: %d rows", idx, len(batch), ticker, n)
                elif verbose:
                    logger.info("  [%d/%d] %s: empty after dropna", idx, len(batch), ticker)
            except Exception as e:
                logger.warning("price persist failed [%s]: %s", ticker, e)
                failed += 1

        logger.info("price-history progress: %d/%d batches (rows=%d, ok=%d, failed=%d)",
                    (i // batch_size) + 1, (len(tickers) + batch_size - 1) // batch_size,
                    total_rows, tickers_with_data, failed)
        time.sleep(throttle_seconds)

    summary = {
        "attempted": attempted,
        "tickers_with_data": tickers_with_data,
        "total_rows": total_rows,
        "failed_tickers": failed,
    }
    logger.info("Historical price seed complete: %s", summary)
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
