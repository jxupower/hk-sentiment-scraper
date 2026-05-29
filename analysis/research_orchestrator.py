"""Top-level orchestrator — builds a ResearchReport for a single ticker by
composing every relevant existing primitive plus the new Plain-Bagel-aligned
helpers (cagr, forensic, dcf, peer_comparison).

The orchestrator is intentionally a "thin composer": each section pulls data
from existing repositories or new analysis modules. No business logic lives
here — only ordering and packaging.

Reuses:
  - analysis.factor_scores.FactorScoringEngine (single-ticker FactorResult extract)
  - analysis.screens.run_screen (each BUILTIN_SCREENS, check pass/fail)
  - storage.repository.{FundamentalsRepository, HistoricalPricesRepository,
                         SentimentRepository, SecuritiesRepository,
                         ResearchNotesRepository, BacktestRepository}
  - analysis.cagr.multi_horizon_cagr
  - analysis.forensic.detect_red_flags
  - analysis.dcf.{default_inputs_from_snapshot, compute_dcf}
  - analysis.peer_comparison.build_peer_scorecard
"""
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from analysis.cagr import multi_horizon_cagr, yoy_growth_series
from analysis.dcf import DCFInputs, DCFResult, compute_dcf, default_inputs_from_snapshot
from analysis.factor_scores import FactorScoringEngine, FactorResult, Flag
from analysis.forensic import RedFlag, detect_red_flags
from analysis.peer_comparison import PeerScorecard, build_peer_scorecard
from analysis.screens import BUILTIN_SCREENS, run_screen


@dataclass
class ScreenPassFail:
    screen_id: str
    name: str
    passed: bool
    criteria: list  # list[ScreenCriterion]


@dataclass
class HistoryPoint:
    date: str
    eps_ttm: Optional[float]
    bps: Optional[float]
    shares_outstanding: Optional[float]
    return_on_equity: Optional[float]
    return_on_assets: Optional[float]
    profit_margins: Optional[float]
    debt_to_equity: Optional[float]
    earnings_growth: Optional[float]
    revenue_growth: Optional[float]


@dataclass
class ResearchReport:
    # Header / identity
    ticker: str
    name: str
    sector: str
    is_watchlist: bool
    market_cap: Optional[float]
    current_price: Optional[float]

    # Section 1 — Idea
    factor_result: Optional[FactorResult]
    screen_pass_fail: list[ScreenPassFail]
    risk_flags: list[Flag]               # from sector_risk.yaml via FactorScoringEngine
    red_flags: list[RedFlag]             # from forensic.py

    # Section 2 — Business
    recent_articles: list[dict]          # for SWOT auto-population and feed
    saved_notes: Optional[dict]          # from ResearchNotesRepository.get()

    # Section 3 — Financials
    history: list[HistoryPoint]
    cagr_revenue: dict[int, Optional[float]]
    cagr_earnings: dict[int, Optional[float]]
    cagr_bps: dict[int, Optional[float]]
    peer_scorecard: Optional[PeerScorecard]

    # Section 4 — Strategy (mostly composed from history above; no new fields)

    # Section 5 — Valuation
    default_dcf: Optional[DCFResult]
    dcf_inputs_default: Optional[DCFInputs]

    # Section 6 — Notes & Review (handled via saved_notes)


def build_research_report(ticker: str, db_path: str,
                          sector_risk_path: Optional[str] = None,
                          user_dcf_inputs: Optional[dict] = None,
                          ) -> Optional[ResearchReport]:
    """Compose the full report for one ticker. Returns None if the ticker has
    no data at all in our DB."""
    # Pull the latest snapshot and basic header info
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        latest = conn.execute("""
            SELECT f.*, s.name, s.is_watchlist, s.yf_sector, s.watchlist_sector
            FROM fundamentals_snapshots f
            INNER JOIN (
                SELECT ticker, MAX(snapshot_date) AS max_date
                FROM fundamentals_snapshots GROUP BY ticker
            ) lt ON f.ticker = lt.ticker AND f.snapshot_date = lt.max_date
            INNER JOIN securities s ON f.ticker = s.ticker
            WHERE f.ticker = ?
        """, (ticker,)).fetchone()
        if not latest:
            return None
        latest = dict(latest)

        # Multi-year history (annual snapshots, sorted oldest first)
        hist_rows = conn.execute("""
            SELECT snapshot_date, eps_ttm, bps, shares_outstanding,
                   return_on_equity, return_on_assets, profit_margins,
                   debt_to_equity, earnings_growth, revenue_growth
            FROM fundamentals_snapshots
            WHERE ticker = ?
            ORDER BY snapshot_date ASC
        """, (ticker,)).fetchall()
        history = [HistoryPoint(
            date=r["snapshot_date"], eps_ttm=r["eps_ttm"], bps=r["bps"],
            shares_outstanding=r["shares_outstanding"],
            return_on_equity=r["return_on_equity"], return_on_assets=r["return_on_assets"],
            profit_margins=r["profit_margins"], debt_to_equity=r["debt_to_equity"],
            earnings_growth=r["earnings_growth"], revenue_growth=r["revenue_growth"],
        ) for r in hist_rows]

        # Recent articles for this ticker (30 days)
        articles = conn.execute("""
            SELECT s.final_score, s.label, s.scored_at,
                   a.source, a.title, a.url, a.published_at
            FROM sentiment_scores s
            JOIN articles a ON s.article_id = a.id
            WHERE s.ticker = ? AND s.scored_at >= datetime('now', '-30 days')
            ORDER BY s.scored_at DESC LIMIT 30
        """, (ticker,)).fetchall()
        articles = [dict(a) for a in articles]

        # Current price (latest historical price, or last_price from snapshot)
        px_row = conn.execute("""
            SELECT adj_close FROM historical_prices
            WHERE ticker = ? ORDER BY date DESC LIMIT 1
        """, (ticker,)).fetchone()
        current_price = px_row[0] if px_row else latest.get("last_price")

        # Saved notes (if any)
        notes_row = conn.execute(
            "SELECT * FROM research_notes WHERE ticker = ?", (ticker,)
        ).fetchone()
        saved_notes = dict(notes_row) if notes_row else None

    # Factor result (extract this ticker from the full engine run)
    engine = FactorScoringEngine(db_path, sector_risk_path)
    all_results, _ = engine.compute()
    factor_result = next((r for r in all_results if r.ticker == ticker), None)
    risk_flags = factor_result.flags if factor_result else []

    # Screen pass/fail
    screen_pass_fail: list[ScreenPassFail] = []
    for screen in BUILTIN_SCREENS:
        # Lightweight check: see if our ticker is in run_screen results
        results = run_screen(db_path, screen, sector_risk_path)
        passed = any(r.ticker == ticker for r in results)
        # Find criteria for this specific ticker (if it ran the predicate)
        matched = next((r for r in results if r.ticker == ticker), None)
        criteria = matched.criteria if matched else []
        screen_pass_fail.append(ScreenPassFail(
            screen_id=screen.id, name=screen.name,
            passed=passed, criteria=criteria,
        ))

    # Forensic red flags
    red_flags = detect_red_flags(ticker, db_path)

    # Multi-horizon CAGRs (use HistoryPoint list, sorted oldest first)
    cagr_revenue = multi_horizon_cagr([
        (h.date, None) for h in history  # No raw revenue field stored — use revenue_growth chain
    ])
    # For revenue and earnings CAGR, akshare gives YoY growth not absolute series.
    # We use eps_ttm and bps as proxies for earnings and book trajectory.
    cagr_earnings = multi_horizon_cagr([(h.date, h.eps_ttm) for h in history])
    cagr_bps = multi_horizon_cagr([(h.date, h.bps) for h in history])
    # Revenue CAGR: derive from cumulative product of (1 + revenue_growth)
    cagr_revenue = _cagr_from_growth_series(history)

    # Peer scorecard
    peer_scorecard = build_peer_scorecard(ticker, db_path)

    # Default DCF — use the most recent snapshot that HAS per-share data.
    # Today's yfinance snapshots often have eps_ttm=None because per-share
    # fields are pulled less reliably than ratios; the akshare historical rows
    # always have them. Fall back through history newest→oldest until we find
    # a snapshot with usable per-share data.
    snap_for_dcf = None
    for h in reversed(history):
        if h.eps_ttm is not None and h.shares_outstanding is not None:
            # Dampen the default growth assumption using median of last 3-5y
            # earnings_growth, not just the most recent year (which can be wildly
            # negative or one-off positive). User can override via UI sliders.
            recent_growths = [hh.earnings_growth for hh in history[-5:]
                              if hh.earnings_growth is not None]
            if recent_growths:
                sorted_g = sorted(recent_growths)
                median_g = sorted_g[len(sorted_g) // 2]
            else:
                median_g = 0.08
            # Clip to a reasonable band: perpetual growth above 25% is unrealistic;
            # perpetual decline below -10% would compute an absurdly low intrinsic
            default_growth = max(-0.05, min(0.20, median_g))
            snap_for_dcf = {
                "eps_ttm": h.eps_ttm,
                "shares_outstanding": h.shares_outstanding,
                "earnings_growth": default_growth,
                "last_price": current_price,
            }
            break
    dcf_inputs_default = default_inputs_from_snapshot(snap_for_dcf) if snap_for_dcf else None
    if user_dcf_inputs and dcf_inputs_default:
        # Apply user overrides on top of defaults
        d = dcf_inputs_default.__dict__.copy()
        d.update(user_dcf_inputs)
        dcf_inputs_default = DCFInputs(**d)
    default_dcf = compute_dcf(dcf_inputs_default) if dcf_inputs_default else None

    return ResearchReport(
        ticker=ticker,
        name=latest.get("name") or ticker,
        sector=latest.get("yf_sector") or latest.get("watchlist_sector") or "—",
        is_watchlist=bool(latest.get("is_watchlist")),
        market_cap=latest.get("market_cap"),
        current_price=current_price,
        factor_result=factor_result,
        screen_pass_fail=screen_pass_fail,
        risk_flags=risk_flags,
        red_flags=red_flags,
        recent_articles=articles,
        saved_notes=saved_notes,
        history=history,
        cagr_revenue=cagr_revenue,
        cagr_earnings=cagr_earnings,
        cagr_bps=cagr_bps,
        peer_scorecard=peer_scorecard,
        default_dcf=default_dcf,
        dcf_inputs_default=dcf_inputs_default,
    )


def _cagr_from_growth_series(history: list[HistoryPoint]) -> dict[int, Optional[float]]:
    """Reconstruct revenue CAGR from YoY revenue_growth chain.
    Since akshare doesn't give us raw revenue, we synthesize a series starting
    at index 100 and applying each YoY growth. The CAGR of that synthetic
    series matches what the actual revenue CAGR would be."""
    if not history:
        return {5: None, 10: None, 15: None}
    synthetic = [(history[0].date, 100.0)]
    cur = 100.0
    for i in range(1, len(history)):
        g = history[i].revenue_growth
        if g is None:
            cur = None
            synthetic.append((history[i].date, None))
            continue
        if cur is None:
            cur = 100.0
        cur = cur * (1 + g)
        synthetic.append((history[i].date, cur))
    return multi_horizon_cagr(synthetic)
