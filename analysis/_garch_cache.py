"""Per-process cache for GARCH fits + simulated paths used by the
Risk Forecast tab.

Why: fitting is fast (~25ms on HSI's 3k returns) but Monte Carlo at 5,000
paths over a 21-day horizon plus risk-metric extraction adds another ~10ms.
A user that re-renders the same ticker (e.g. flipping between charts or
adjusting cosmetic UI bits) shouldn't refit — only history-window or ticker
changes should trigger a refit.

Cache key: (ticker, history_window_days, horizon, last_price_date_iso).
The last_price_date component means new daily seed runs auto-invalidate
the next morning; slider tweaks on the same data hit warm cache.

TTL: 15 minutes (matches the convention used by analysis/_research_cache.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Lock
from typing import Optional

import numpy as np
import pandas as pd

from analysis.risk_garch import (
    GARCHFit, RiskMetrics, VolForecast,
    compute_log_returns, fit_garch, forecast_volatility,
    risk_metrics, simulate_paths,
)


# How many distinct (ticker, window, horizon, date) tuples to keep before
# evicting in FIFO order. Each entry is ~1 MB (5000x21 float64 paths +
# max-drawdown vector). 32 entries = ~32 MB; well within budget for a
# dashboard process.
_MAX_ENTRIES = 32
_DEFAULT_TTL_SECONDS = 900


@dataclass
class RiskBundle:
    """Everything the UI needs for one (ticker, window, horizon) render."""
    ticker: str
    prices: pd.Series              # full price series used to fit (post-window slicing)
    returns_pct: pd.Series         # log returns × 100
    fit: GARCHFit
    forecast: VolForecast
    paths: np.ndarray              # (n_paths, horizon) cumulative log returns (fraction)
    metrics: RiskMetrics
    built_at: datetime
    last_price_date: str           # ISO date of the latest price used; for cache key


_lock = Lock()
_entries: "dict[tuple, RiskBundle]" = {}
_order: list[tuple] = []  # FIFO order of keys for eviction


def get_or_build(ticker: str, prices: list[dict], *,
                  history_window_days: int,
                  horizon: int,
                  n_paths: int = 5000,
                  ttl_seconds: int = _DEFAULT_TTL_SECONDS,
                  seed: Optional[int] = 42,
                  ) -> RiskBundle:
    """Return a cached or freshly-built RiskBundle for this ticker.

    `prices` is the raw list-of-dicts from data_loader.get_or_fetch_prices —
    we slice to history_window_days inside the cache so the slice is part
    of the cached state and a window change forces a refit.

    Concurrent calls for the same key coalesce: thread A's build is shared
    with thread B that arrives mid-build.

    Raises ValueError if prices contain too few rows after windowing
    (< 250 returns — the same floor enforced by fit_garch).
    """
    if not prices:
        raise ValueError(f"no price data for {ticker}")

    last_price_date = str(prices[-1]["date"])[:10]
    key = (ticker, history_window_days, horizon, last_price_date)

    with _lock:
        cached = _entries.get(key)
        if cached and (datetime.now() - cached.built_at) < timedelta(seconds=ttl_seconds):
            return cached

        # Slice to the requested history window (0 = MAX)
        price_series = pd.Series([float(r["adj_close"]) for r in prices
                                    if r.get("adj_close") is not None])
        if history_window_days and history_window_days > 0:
            price_series = price_series.iloc[-history_window_days:]
        if len(price_series) < 251:  # need >=250 returns for fit_garch
            raise ValueError(
                f"{ticker}: only {len(price_series)} prices in window "
                f"({history_window_days}d) — need >=251 for a stable fit"
            )

        returns = compute_log_returns(price_series)
        fit = fit_garch(returns)
        forecast = forecast_volatility(fit, horizon=horizon)
        paths = simulate_paths(fit, horizon=horizon, n_paths=n_paths, seed=seed)
        metrics = risk_metrics(paths, current_price=float(price_series.iloc[-1]))

        bundle = RiskBundle(
            ticker=ticker,
            prices=price_series,
            returns_pct=returns,
            fit=fit,
            forecast=forecast,
            paths=paths,
            metrics=metrics,
            built_at=datetime.now(),
            last_price_date=last_price_date,
        )
        _entries[key] = bundle
        _order.append(key)

        # FIFO eviction
        while len(_order) > _MAX_ENTRIES:
            evict_key = _order.pop(0)
            _entries.pop(evict_key, None)

        return bundle


def invalidate() -> None:
    """Drop every cached entry. Used by tests."""
    with _lock:
        _entries.clear()
        _order.clear()


def stats() -> dict:
    """Diagnostic — how many entries, oldest age."""
    with _lock:
        if not _entries:
            return {"entries": 0, "oldest_age_s": None, "newest_age_s": None}
        now = datetime.now()
        ages = [(now - b.built_at).total_seconds() for b in _entries.values()]
        return {
            "entries": len(_entries),
            "oldest_age_s": max(ages),
            "newest_age_s": min(ages),
        }
