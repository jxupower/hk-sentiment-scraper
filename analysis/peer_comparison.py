"""Per-ticker peer comparison scorecard.

Plain Bagel's step 3 emphasizes comparing the target to peers in the same
industry on the same metrics ("peer comparison scorecard" in the transcript).
We use yfinance sector classification (already populated in `securities.yf_sector`)
to define the peer group.

For each metric, we compute:
  - target ticker's value
  - peer median
  - peer 25th and 75th percentile
  - target's percentile rank (0-100) within the peer group

Plus a ranked list of top N peers in the same sector by composite score
(reusing FactorScoringEngine).
"""
import math
import sqlite3
import statistics
from dataclasses import dataclass, field
from typing import Optional


METRICS = [
    ("trailing_pe",       "P/E",          False),  # lower better → False = sort ascending
    ("price_to_book",     "P/B",          False),
    ("ev_to_ebitda",      "EV/EBITDA",    False),
    ("dividend_yield",    "Div Y %",      True),   # higher better
    ("return_on_equity",  "ROE",          True),
    ("return_on_assets",  "ROA",          True),
    ("profit_margins",    "Net margin",   True),
    ("earnings_growth",   "Earn growth",  True),
    ("revenue_growth",    "Rev growth",   True),
    ("debt_to_equity",    "D/E",          False),
]


@dataclass
class MetricComparison:
    name: str                  # human label
    field: str                 # column name in DB
    higher_better: bool
    target_value: Optional[float]
    peer_count: int
    peer_median: Optional[float]
    peer_p25: Optional[float]  # 25th percentile
    peer_p75: Optional[float]  # 75th percentile
    target_percentile: Optional[float]  # 0-100, higher = better in higher_better sense


@dataclass
class PeerScorecard:
    target_ticker: str
    target_name: str
    sector: str
    n_peers: int
    metrics: list[MetricComparison] = field(default_factory=list)
    top_peers: list[dict] = field(default_factory=list)  # name, ticker, market_cap


def _finite(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def build_peer_scorecard(ticker: str, db_path: str,
                         top_n_peers: int = 10) -> Optional[PeerScorecard]:
    """Construct a per-metric comparison vs the target's yfinance sector peers."""
    from analysis.data_loader import get_universe_fundamentals
    from storage.database import Database

    # One universe pull, then filter client-side. Avoids two separate
    # cloud round-trips for target+peers.
    universe = get_universe_fundamentals(Database(db_path))
    target_row = next((r for r in universe if r["ticker"] == ticker), None)
    if not target_row:
        return None
    sector = target_row.get("yf_sector") or target_row.get("watchlist_sector")
    if not sector:
        return None
    peer_rows = [r for r in universe
                  if r.get("yf_sector") == sector and r["ticker"] != ticker]

    scorecard = PeerScorecard(
        target_ticker=ticker,
        target_name=target_row.get("name", ticker),
        sector=sector,
        n_peers=len(peer_rows),
    )

    for field_name, label, higher_better in METRICS:
        target_val = _finite(target_row.get(field_name))
        peer_vals = [_finite(r.get(field_name)) for r in peer_rows]
        peer_vals = [v for v in peer_vals if v is not None]
        if not peer_vals:
            scorecard.metrics.append(MetricComparison(
                name=label, field=field_name, higher_better=higher_better,
                target_value=target_val, peer_count=0,
                peer_median=None, peer_p25=None, peer_p75=None,
                target_percentile=None,
            ))
            continue
        med = statistics.median(peer_vals)
        sorted_vals = sorted(peer_vals)
        p25 = sorted_vals[max(0, len(sorted_vals) // 4 - 1)]
        p75 = sorted_vals[min(len(sorted_vals) - 1, 3 * len(sorted_vals) // 4)]
        pctile = None
        if target_val is not None:
            below = sum(1 for v in peer_vals if v < target_val)
            equal = sum(1 for v in peer_vals if v == target_val)
            rank = below + (equal + 1) / 2
            raw_pct = 100 * rank / (len(peer_vals) + 1)
            # If lower is better, invert so high=good is consistent
            pctile = raw_pct if higher_better else (100 - raw_pct)
        scorecard.metrics.append(MetricComparison(
            name=label, field=field_name, higher_better=higher_better,
            target_value=target_val, peer_count=len(peer_vals),
            peer_median=med, peer_p25=p25, peer_p75=p75,
            target_percentile=pctile,
        ))

    # Top N peers by market cap (proxy for prominence)
    peers_by_mc = sorted(
        [(p.get("name", p["ticker"]), p["ticker"], _finite(p.get("market_cap")))
         for p in peer_rows],
        key=lambda x: (x[2] is None, -(x[2] or 0)),
    )[:top_n_peers]
    scorecard.top_peers = [
        {"name": n, "ticker": t, "market_cap": mc}
        for n, t, mc in peers_by_mc
    ]
    return scorecard
