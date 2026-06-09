"""Synthetic-ticker computation for user-saved portfolios.

A saved portfolio gets up to two materialised price series in
`historical_prices`, identified by an `@`-prefixed ticker:

  @NAME        — STATUS-QUO index: `value(t) = Σᵢ sharesᵢ × adj_closeᵢ(t)`,
                  normalised so the first overlapping date = 100.
                  Equivalent to a constant-share buy-and-hold series.
  @NAME$OPT    — OPTIMAL-WEIGHT index: cumulated return series from the
                  weights snapshot the user saved.  `r(t) = Σᵢ wᵢ · rᵢ(t)`,
                  then cumulate `(1+r).cumprod() × 100`.

Both series are inner-joined across constituents — the start date is the
latest IPO among them.

This module is the only place that knows about the @-prefix convention on
the write side.  `analysis/data_loader.py:get_or_fetch_prices` is the only
place that knows about it on the read side; it delegates here on cache
miss / staleness.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd

from storage.database import Database
from storage.factory import get_prices_repo
from utils.logger import get_logger


log = get_logger(__name__)


PORTFOLIO_PREFIX = "@"
OPTIMAL_SUFFIX = "$OPT"
NAME_PATTERN = re.compile(r"^[A-Z0-9_]{1,32}$")


def normalise_name(name: str) -> str:
    """Uppercase, strip whitespace.  Caller should validate via `is_valid_name`
    before persisting — this just canonicalises."""
    return (name or "").strip().upper()


def is_valid_name(name: str) -> bool:
    return bool(NAME_PATTERN.match(name or ""))


def to_status_quo_ticker(name: str) -> str:
    return f"{PORTFOLIO_PREFIX}{normalise_name(name)}"


def to_optimal_ticker(name: str) -> str:
    return f"{PORTFOLIO_PREFIX}{normalise_name(name)}{OPTIMAL_SUFFIX}"


def parse_portfolio_ticker(ticker: str) -> Optional[tuple[str, bool]]:
    """Return `(name, is_optimal)` or None if not a portfolio ticker."""
    if not ticker or not ticker.startswith(PORTFOLIO_PREFIX):
        return None
    bare = ticker[len(PORTFOLIO_PREFIX):]
    if bare.endswith(OPTIMAL_SUFFIX):
        return bare[: -len(OPTIMAL_SUFFIX)], True
    return bare, False


# ============== Computing the price series ==============

def _load_constituent_series(constituent_tickers: list[str],
                              db: Database) -> pd.DataFrame:
    """Load adj-close series for each constituent and inner-join on date.

    Uses the same `get_or_fetch_prices` cache-aside path that the rest of
    the app uses — so if a constituent is missing, it'll attempt a live
    yfinance/akshare fetch.
    """
    from analysis.data_loader import get_or_fetch_prices

    series_by_ticker: dict[str, pd.Series] = {}
    for t in constituent_tickers:
        # Recursion guard — a portfolio's constituents should be real
        # equities/indices, never another portfolio
        if t.startswith(PORTFOLIO_PREFIX):
            raise ValueError(
                f"portfolio constituent {t!r} is itself a portfolio — "
                "nested portfolios are not supported")
        rows = get_or_fetch_prices(t, db)
        if not rows:
            raise ValueError(f"no price data for {t}")
        s = pd.Series(
            [float(r["adj_close"]) for r in rows
             if r.get("adj_close") is not None],
            index=pd.to_datetime([r["date"] for r in rows
                                   if r.get("adj_close") is not None]),
        )
        s = s[~s.index.duplicated(keep="last")].sort_index()
        series_by_ticker[t] = s
    prices = pd.concat(series_by_ticker, axis=1, join="inner")
    prices = prices[constituent_tickers]
    return prices


def compute_status_quo_series(holdings: list[dict],
                               db: Database,
                               base_value: float = 100.0) -> list[dict]:
    """Compute the constant-share buy-and-hold price series.

    `holdings` is a list of `{ticker, shares}`. Rows with `shares <= 0`
    are dropped — only holdings with positive share count contribute to
    the status-quo index.

    Returns rows ready for `historical_prices.upsert_rows`: each is
    `{date, open, high, low, close, adj_close, volume}` with most OHLCV
    fields set to the daily portfolio value (no separate intraday data).
    """
    weighted_rows = [
        {"ticker": h["ticker"], "shares": float(h["shares"])}
        for h in holdings
        if (h.get("shares") or 0) > 0 and h.get("ticker")
    ]
    if len(weighted_rows) < 1:
        raise ValueError(
            "status-quo series needs at least one row with shares > 0")

    tickers = [r["ticker"] for r in weighted_rows]
    shares = [r["shares"] for r in weighted_rows]
    prices = _load_constituent_series(tickers, db)
    if prices.empty:
        raise ValueError(
            "no overlapping price dates across constituents — "
            "is one of them brand new?")

    weighted_value = prices.mul(shares, axis=1).sum(axis=1)
    if weighted_value.empty or float(weighted_value.iloc[0]) == 0.0:
        raise ValueError("initial portfolio value is zero — bad data")

    normalised = weighted_value / float(weighted_value.iloc[0]) * base_value
    return _series_to_price_rows(normalised)


def compute_optimal_series(weights: list[dict],
                            db: Database,
                            base_value: float = 100.0) -> list[dict]:
    """Compute the max-Sharpe optimal-weight index from a saved weight
    snapshot.

    `weights` is a list of `{ticker, weight}` (weights summing to ~1).
    Tickers with weight=0 are dropped to keep the inner-join window wide.

    Math: build the daily returns of each constituent, take the weighted
    sum row-wise, then `value(t) = base × Π(1 + r(s))` for s ≤ t.
    """
    active = [
        {"ticker": w["ticker"], "weight": float(w["weight"])}
        for w in weights
        if abs(float(w.get("weight") or 0)) > 1e-9 and w.get("ticker")
    ]
    if len(active) < 1:
        raise ValueError("optimal series needs at least one non-zero weight")

    tickers = [w["ticker"] for w in active]
    weight_vec = [w["weight"] for w in active]
    prices = _load_constituent_series(tickers, db)
    if prices.empty:
        raise ValueError("no overlapping price dates across constituents")

    returns = prices.pct_change().dropna()
    if returns.empty:
        raise ValueError("not enough return history to build optimal series")

    portfolio_returns = returns.mul(weight_vec, axis=1).sum(axis=1)
    cum = (1.0 + portfolio_returns).cumprod() * base_value
    return _series_to_price_rows(cum)


def _series_to_price_rows(series: pd.Series) -> list[dict]:
    """Adapt a pandas Series of daily portfolio values into the
    `historical_prices` row schema."""
    rows: list[dict] = []
    for ts, value in series.items():
        if value is None or (isinstance(value, float) and pd.isna(value)):
            continue
        v = float(value)
        d = ts.date() if hasattr(ts, "date") else ts
        rows.append({
            "date": d.isoformat() if hasattr(d, "isoformat") else str(d)[:10],
            "open": v, "high": v, "low": v, "close": v,
            "adj_close": v, "volume": None,
        })
    return rows


# ============== Materialise into historical_prices ==============

def rebuild_and_upsert(name: str, portfolio: dict, db: Database) -> dict:
    """Recompute both series for a saved portfolio and upsert them.

    `portfolio` is the row from `CloudPortfoliosRepository.get_portfolio()`
    or a dict with the same shape: `{holdings, optimal_weights, ...}`.

    Returns a dict summary `{status_quo_rows, optimal_rows, errors}`.
    Either side can fail independently — e.g. optimal_weights might not
    be present yet — and the other still upserts.
    """
    norm = normalise_name(name)
    out = {"status_quo_rows": 0, "optimal_rows": 0, "errors": []}
    repo = get_prices_repo(db)

    holdings = portfolio.get("holdings") or []
    try:
        sq_rows = compute_status_quo_series(holdings, db)
        sq_ticker = to_status_quo_ticker(norm)
        repo.upsert_rows(sq_ticker, sq_rows)
        out["status_quo_rows"] = len(sq_rows)
        log.info("Rebuilt %s (%d rows)", sq_ticker, len(sq_rows))
    except Exception as e:
        out["errors"].append(f"status_quo: {type(e).__name__}: {e}")
        log.warning("status-quo rebuild failed for %s: %s", norm, e)

    optimal_weights = portfolio.get("optimal_weights")
    if optimal_weights:
        try:
            opt_rows = compute_optimal_series(optimal_weights, db)
            opt_ticker = to_optimal_ticker(norm)
            repo.upsert_rows(opt_ticker, opt_rows)
            out["optimal_rows"] = len(opt_rows)
            log.info("Rebuilt %s (%d rows)", opt_ticker, len(opt_rows))
        except Exception as e:
            out["errors"].append(f"optimal: {type(e).__name__}: {e}")
            log.warning("optimal rebuild failed for %s: %s", norm, e)
    return out


def delete_synthetic_rows(name: str) -> int:
    """Wipe the two materialised rowsets for a portfolio when it's deleted.

    Returns the number of rows removed across both tickers. Currently
    only meaningful when the cloud DB is configured (the historical_prices
    table for synthetic portfolios lives wherever prices live)."""
    norm = normalise_name(name)
    sq_ticker = to_status_quo_ticker(norm)
    opt_ticker = to_optimal_ticker(norm)
    deleted = 0

    from storage.cloud_db import available, cursor

    if available():
        with cursor() as cur:
            cur.execute(
                "DELETE FROM historical_prices WHERE ticker = ANY(%s)",
                ([sq_ticker, opt_ticker],),
            )
            deleted = cur.rowcount
    else:
        # Local SQLite path (USE_CLOUD_DB=false)
        import sqlite3
        from config import settings
        sqlite_path = settings.SQLITE_DB_PATH if hasattr(settings, "SQLITE_DB_PATH") else "data/sentiment.db"
        try:
            conn = sqlite3.connect(sqlite_path)
            try:
                cur = conn.execute(
                    "DELETE FROM historical_prices WHERE ticker IN (?, ?)",
                    (sq_ticker, opt_ticker),
                )
                deleted = cur.rowcount
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            log.warning("delete synthetic rows fell through to no-op: %s", e)

    log.info("Deleted %d synthetic price rows for portfolio %s", deleted, norm)
    return deleted


# ============== Staleness check (used by data_loader cache-aside) ==============

SYNTHETIC_STALE_DAYS = 1   # bias toward rebuilding: constituent prices may
                            # have refreshed without the synthetic following.


def is_synthetic_stale(latest_date_str: Optional[str]) -> bool:
    if not latest_date_str:
        return True
    try:
        latest = datetime.strptime(latest_date_str[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return True
    return (date.today() - latest).days > SYNTHETIC_STALE_DAYS
