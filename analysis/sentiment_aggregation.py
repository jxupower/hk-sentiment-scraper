"""Mcap-weighted aggregation of per-ticker sentiment + price-momentum into
per-sub-sector + per-ticker signals.

Replaces the per-ticker `yahoo.fetch_price_history(period='1mo')` loop in
`scheduler/job_runner.py` (which was watchlist-bounded — ~50 tickers per
market per cycle). The new path covers every active sub-sector-tagged
ticker (~6,668 across both markets) with one Supabase bulk price pull
per market, then a pure-Python aggregation.

Design choices:
- **Mcap-weight**: bigger names in a sub-sector dominate the card sentiment
  reading. The natural way to summarise "what's the market saying about
  Banks today" without giving a $50M micro-cap regional bank equal voice
  to JPMorgan. Falls back to equal-weight when no mcap is available.
- **5-day momentum**: matches the pre-redesign metric. Computed from the
  closing-price series we already pull from `historical_prices` cache,
  not via per-ticker yfinance API calls.
- **Sub-sector confidence**: blend of article volume (more articles ⇒
  more confidence) and signal magnitude (stronger reading ⇒ more
  confidence). Same shape as the existing per-ticker `signal_gen`
  outputs so the dashboard renders without changes.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from utils.logger import get_logger

logger = get_logger(__name__)


def _safe_pct_change(series: list[float], lookback: int = 5) -> Optional[float]:
    """Trailing-N-bar pct change. Returns None when the series is too short."""
    if not series or len(series) < lookback + 1:
        return None
    if series[-1 - lookback] in (0, None):
        return None
    return (series[-1] - series[-1 - lookback]) / series[-1 - lookback] * 100.0


def compute_ticker_momentum(prices_repo, tickers: list[str],
                              lookback_days: int = 5,
                              window_days: int = 35) -> dict[str, Optional[float]]:
    """{ticker: 5-day pct change} in one Supabase round-trip via
    `bulk_get_price_series`. Returns None for tickers with no series.

    `window_days` (default 35 to allow for weekends/holidays around a
    5-trading-day lookback) is the date range we pull — covers ~25
    trading days, plenty of slack."""
    if not tickers:
        return {}
    end = datetime.utcnow().date().isoformat()
    start = (datetime.utcnow().date() - timedelta(days=window_days)).isoformat()
    try:
        series_by_ticker = prices_repo.bulk_get_price_series(
            tickers, start, end)
    except Exception as e:
        logger.warning("bulk_get_price_series failed: %s", e)
        return {t: None for t in tickers}

    out: dict[str, Optional[float]] = {}
    for t in tickers:
        rows = series_by_ticker.get(t) or []
        closes = [float(r["adj_close"]) for r in rows
                   if r.get("adj_close") is not None]
        out[t] = _safe_pct_change(closes, lookback=lookback_days)
    return out


def _mcap_weighted_avg(values_by_ticker: dict[str, Optional[float]],
                         mcap_by_ticker: dict[str, Optional[float]]) -> Optional[float]:
    """Weighted average of `values` by `mcap`. Tickers with None for
    either are skipped. Falls back to a plain mean when no mcap weights
    survive."""
    weighted_sum = 0.0
    total_w = 0.0
    n_unweighted = 0
    plain_sum = 0.0
    for t, v in values_by_ticker.items():
        if v is None:
            continue
        n_unweighted += 1
        plain_sum += v
        w = mcap_by_ticker.get(t)
        if w is None or w <= 0:
            continue
        weighted_sum += v * w
        total_w += w
    if total_w > 0:
        return weighted_sum / total_w
    if n_unweighted > 0:
        return plain_sum / n_unweighted
    return None


def aggregate_subsector_signals(
    subsector_to_tickers: dict[str, list[str]],
    sentiment_by_ticker: dict[str, Optional[float]],
    article_count_by_ticker: dict[str, int],
    momentum_by_ticker: dict[str, Optional[float]],
    mcap_by_ticker: dict[str, Optional[float]],
) -> list[dict]:
    """Compute one row per sub-sector: mcap-weighted sentiment + momentum,
    article count sum, plus a derived direction / confidence pair so the
    existing `SectorSignalRepository.upsert_signal` shape is honoured.

    Returns `[{sector, avg_sentiment_24h, article_count_24h,
    avg_price_momentum, direction, confidence}, ...]`.
    """
    out: list[dict] = []
    for sub, tickers in subsector_to_tickers.items():
        if not tickers:
            continue
        sent_avg = _mcap_weighted_avg(
            {t: sentiment_by_ticker.get(t) for t in tickers},
            mcap_by_ticker,
        )
        mom_avg = _mcap_weighted_avg(
            {t: momentum_by_ticker.get(t) for t in tickers},
            mcap_by_ticker,
        )
        article_sum = sum(article_count_by_ticker.get(t, 0) for t in tickers)

        # Direction + confidence mimic analysis/signals.py:compute_sector_signal
        # so dashboard rendering stays unchanged. Threshold values match.
        direction = "NEUTRAL"
        if sent_avg is not None:
            if sent_avg > 0.15 and (mom_avg or 0) > 0:
                direction = "UP"
            elif sent_avg < -0.15 and (mom_avg or 0) < 0:
                direction = "DOWN"
            elif abs(sent_avg) > 0.15:
                direction = "MIXED"

        confidence = 0.0
        if sent_avg is not None:
            mag_score = min(abs(sent_avg) / 0.3, 1.0)
            vol_score = min(article_sum / 20.0, 1.0)
            confidence = round((mag_score + vol_score) / 2.0, 2)

        out.append({
            "sector":             sub,
            "avg_sentiment_24h":  round(sent_avg, 4) if sent_avg is not None else None,
            "avg_sentiment_7d":   None,  # 7d not yet computed (caller passes 24h only)
            "article_count_24h":  article_sum,
            "avg_price_momentum": round(mom_avg, 4) if mom_avg is not None else None,
            "direction":          direction,
            "confidence":         confidence,
        })
    return out
