"""Per-process cache for the Portfolio Rebalancer tab. Mirrors the shape of
analysis/_garch_cache.py.

Why caching is worth it: the efficient-frontier sweep is ~30 SLSQP solves
and the walk-forward backtest is one estimate_mu_sigma + max_sharpe per
rebalance date. Combined ~1-2s. Re-rendering on UI tweaks shouldn't recost.

Cache key: (tickers_tuple, lookback, rebalance, weight_cap, rf, last_date_iso).
The last_date component invalidates cleanly the next day after the seed runs.

This module is also where the three-Sharpe comparison (status quo,
current-only optimum, full-universe optimum) is orchestrated — the math
primitives live in portfolio_optimizer.py, and this module composes them
plus the leave-one-out marginal values into one cached bundle.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import Lock
from typing import Optional

import numpy as np
import pandas as pd

from analysis.portfolio_optimizer import (
    BacktestStrategy,
    FrontierPoint,
    MuSigma,
    candidate_marginal_value,
    compute_returns_matrix,
    efficient_frontier,
    estimate_mu_sigma,
    max_sharpe_portfolio,
    portfolio_metrics,
    status_quo_weights,
    walk_forward_backtest,
)


_MAX_ENTRIES = 16
_DEFAULT_TTL_SECONDS = 900


@dataclass
class PortfolioBundle:
    """Everything the UI needs for one (holdings, lookback, rebalance,
    cap, rf) render."""
    tickers: list[str]                          # full universe (current + candidates)
    current_tickers: list[str]                  # subset with shares > 0
    candidate_tickers: list[str]                # subset with shares == 0
    holdings: list[dict]                        # raw input echoed back
    latest_prices: dict[str, float]
    returns: pd.DataFrame                       # T x N
    mu_sigma: MuSigma

    # Three-Sharpe comparison
    w_status_quo: np.ndarray                    # current shares / market value
    w_current_optimal: np.ndarray               # full-N vector; non-current tickers = 0
    w_full_optimal: np.ndarray                  # full-N

    m_status_quo: dict                          # {return, vol, sharpe}
    m_current_optimal: dict
    m_full_optimal: dict

    frontier: list[FrontierPoint]
    candidate_marginal: dict[str, float]        # ticker -> Sharpe drop if removed
    backtest: dict[str, BacktestStrategy]       # strategy_name -> BacktestStrategy

    # Cache metadata
    built_at: datetime
    last_price_date: str
    key: tuple


_lock = Lock()
_entries: "dict[tuple, PortfolioBundle]" = {}
_order: list[tuple] = []


def get_or_build(holdings: list[dict], *,
                  lookback_days: int, rebalance_days: int,
                  weight_cap: float, rf: float,
                  db, ttl_seconds: int = _DEFAULT_TTL_SECONDS,
                  ) -> PortfolioBundle:
    """Build (or hit cache for) the full portfolio analysis bundle.

    `holdings` is the editable-table data: a list of dicts with keys
    `ticker` and `shares`. Tickers must be non-empty; rows with empty
    ticker are dropped. Duplicates are collapsed (shares summed).

    Raises ValueError if fewer than 2 unique tickers remain (a "portfolio"
    of 1 is just a single asset) or no price data is available.
    """
    cleaned = _clean_holdings(holdings)
    tickers = [h["ticker"] for h in cleaned]
    if len(tickers) < 2:
        raise ValueError("Need at least 2 tickers in the holdings table.")

    # The key includes a snapshot of the holdings so that adding/removing
    # a candidate invalidates cleanly.
    holdings_key = tuple((h["ticker"], float(h.get("shares") or 0))
                          for h in cleaned)
    cache_key_prefix = (tuple(tickers), holdings_key, lookback_days,
                         rebalance_days, round(weight_cap, 4), round(rf, 4))

    # last_price_date is the freshest among the constituent tickers — we
    # resolve it before computing returns so a single function call can
    # short-circuit on cache hit. Computed cheaply by checking just the
    # first ticker; if it's stale, we rebuild anyway.
    from analysis.data_loader import get_or_fetch_prices
    probe_prices = get_or_fetch_prices(tickers[0], db)
    if not probe_prices:
        raise ValueError(f"no price data for {tickers[0]}")
    last_date = str(probe_prices[-1]["date"])[:10]
    key = cache_key_prefix + (last_date,)

    with _lock:
        cached = _entries.get(key)
        if cached and (datetime.now() - cached.built_at) < timedelta(seconds=ttl_seconds):
            return cached

    # Build outside the lock — math can take 1-2s and we don't want to
    # serialize concurrent users on different tickers.
    bundle = _build(cleaned, tickers, lookback_days, rebalance_days,
                     weight_cap, rf, db, last_date, key)

    with _lock:
        _entries[key] = bundle
        _order.append(key)
        while len(_order) > _MAX_ENTRIES:
            evict = _order.pop(0)
            _entries.pop(evict, None)

    return bundle


def invalidate() -> None:
    with _lock:
        _entries.clear()
        _order.clear()


# ============== Build the bundle ==============

def _clean_holdings(holdings: list[dict]) -> list[dict]:
    """Drop rows with empty/None ticker; coerce shares to non-negative float;
    collapse duplicates (sum shares); preserve first-occurrence order."""
    seen: dict[str, float] = {}
    order: list[str] = []
    for h in holdings:
        t = (h.get("ticker") or "").strip().upper()
        if not t:
            continue
        try:
            s = max(0.0, float(h.get("shares") or 0))
        except (TypeError, ValueError):
            s = 0.0
        if t not in seen:
            order.append(t)
            seen[t] = s
        else:
            seen[t] += s
    return [{"ticker": t, "shares": seen[t]} for t in order]


def _build(holdings: list[dict], tickers: list[str], lookback: int,
            rebalance: int, weight_cap: float, rf: float, db,
            last_date: str, key: tuple) -> PortfolioBundle:
    # Returns matrix
    returns = compute_returns_matrix(tickers, lookback_days=lookback, db=db)

    # Estimate mu, sigma
    ms = estimate_mu_sigma(returns)

    # Latest prices for status-quo weighting
    from analysis.data_loader import get_or_fetch_prices
    latest_prices: dict[str, float] = {}
    for t in tickers:
        rows = get_or_fetch_prices(t, db)
        if rows:
            latest_prices[t] = float(rows[-1]["adj_close"])

    # Status quo weights
    w_status_quo = status_quo_weights(holdings, latest_prices, tickers)
    m_status_quo = (portfolio_metrics(w_status_quo, ms.mu, ms.sigma, rf=rf)
                     if w_status_quo.sum() > 0
                     else {"return": 0.0, "vol": 0.0, "sharpe": 0.0})

    # Current-only universe (shares > 0)
    current_tickers = [h["ticker"] for h in holdings if h.get("shares", 0) > 0]
    candidate_tickers = [h["ticker"] for h in holdings if not (h.get("shares") or 0) > 0]

    # Current-only optimum: solve max-Sharpe over the current subset,
    # then expand back to full-N vector (zeros for non-current).
    if len(current_tickers) >= 2:
        idx_current = [tickers.index(t) for t in current_tickers]
        mu_c = ms.mu[idx_current]
        sigma_c = ms.sigma[np.ix_(idx_current, idx_current)]
        # If cap is so tight that current alone can't sum to 1, lift the cap
        # for this subset specifically (still a sensible result for a "best
        # rebalance given my holdings" view)
        local_cap = max(weight_cap, 1.0 / len(current_tickers))
        try:
            w_c_local = max_sharpe_portfolio(mu_c, sigma_c, rf=rf,
                                              weight_cap=local_cap)
        except Exception:
            w_c_local = np.full(len(current_tickers), 1.0 / len(current_tickers))
        w_current_optimal = np.zeros(len(tickers))
        for i, idx in enumerate(idx_current):
            w_current_optimal[idx] = w_c_local[i]
        m_current_optimal = portfolio_metrics(w_current_optimal, ms.mu, ms.sigma, rf=rf)
    elif len(current_tickers) == 1:
        idx = tickers.index(current_tickers[0])
        w_current_optimal = np.zeros(len(tickers))
        w_current_optimal[idx] = 1.0
        m_current_optimal = portfolio_metrics(w_current_optimal, ms.mu, ms.sigma, rf=rf)
    else:
        w_current_optimal = np.zeros(len(tickers))
        m_current_optimal = {"return": 0.0, "vol": 0.0, "sharpe": 0.0}

    # Full-universe optimum
    try:
        w_full_optimal = max_sharpe_portfolio(ms.mu, ms.sigma, rf=rf,
                                                weight_cap=weight_cap)
        m_full_optimal = portfolio_metrics(w_full_optimal, ms.mu, ms.sigma, rf=rf)
    except Exception:
        w_full_optimal = np.full(len(tickers), 1.0 / len(tickers))
        m_full_optimal = portfolio_metrics(w_full_optimal, ms.mu, ms.sigma, rf=rf)

    # Frontier
    frontier = efficient_frontier(ms.mu, ms.sigma, n_points=30,
                                    weight_cap=weight_cap, rf=rf)

    # Candidate marginal values (only for tickers flagged as candidates)
    cand_mv = candidate_marginal_value(ms.mu, ms.sigma, tickers,
                                         candidate_tickers, rf=rf,
                                         weight_cap=weight_cap)

    # Walk-forward backtest
    backtest = walk_forward_backtest(returns, lookback=min(lookback, len(returns) // 2),
                                       rebalance_freq=rebalance, rf=rf,
                                       weight_cap=weight_cap)

    return PortfolioBundle(
        tickers=tickers,
        current_tickers=current_tickers,
        candidate_tickers=candidate_tickers,
        holdings=holdings,
        latest_prices=latest_prices,
        returns=returns,
        mu_sigma=ms,
        w_status_quo=w_status_quo,
        w_current_optimal=w_current_optimal,
        w_full_optimal=w_full_optimal,
        m_status_quo=m_status_quo,
        m_current_optimal=m_current_optimal,
        m_full_optimal=m_full_optimal,
        frontier=frontier,
        candidate_marginal=cand_mv,
        backtest=backtest,
        built_at=datetime.now(),
        last_price_date=last_date,
        key=key,
    )
