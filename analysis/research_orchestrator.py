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
from analysis.dcf import (DCFInputs, DCFResult, GrowthProvenance,
                           compute_dcf, default_inputs_from_snapshot)
from analysis.factor_scores import FactorResult, Flag
from analysis.forensic import RedFlag, detect_red_flags
from analysis.peer_comparison import PeerScorecard, build_peer_scorecard
from analysis.screens import BUILTIN_SCREENS


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
    sub_sector: Optional[str]
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
    dcf_growth_provenance: Optional[GrowthProvenance] = None

    # Section 6 — Notes & Review (handled via saved_notes)

    # Section 3b — Raw financial statements (income / balance / cashflow).
    # Defaulted so old call sites that don't pass it (or fixtures) still work.
    # Newest-first per statement_type, one dict per period with line_items.
    financial_statements: dict = field(default_factory=lambda: {
        "income": [], "balance": [], "cashflow": []
    })


def build_research_report(ticker: str, db_path: str,
                          sector_risk_path: Optional[str] = None,
                          user_dcf_inputs: Optional[dict] = None,
                          *,
                          skip_financial_statements: bool = False,
                          ) -> Optional[ResearchReport]:
    """Compose the full report for one ticker. Returns None if the ticker has
    no data at all in our DB.

    `skip_financial_statements=True` skips the (potentially 3-8s) raw filings
    fetch in Section 3b. The dashboard uses this for the initial render and
    fetches statements lazily when the user clicks "Load Financial Statements".
    """
    from analysis._research_cache import get_or_build as get_universe_cache
    from analysis.data_loader import (
        get_or_fetch_fundamentals_history, get_or_fetch_prices, get_universe_fundamentals
    )
    from storage.database import Database
    db = Database(db_path)

    # Latest fundamentals + securities row for this ticker (via universe pull;
    # cheaper than a single-ticker JOIN against cloud since we need this same
    # data for the factor engine below anyway).
    universe = get_universe_fundamentals(db)
    latest = next((r for r in universe if r["ticker"] == ticker), None)
    if not latest:
        return None

    # Multi-year history via cache-aside (akshare-on-miss)
    hist_rows = get_or_fetch_fundamentals_history(ticker, db)
    history = [HistoryPoint(
        date=h.get("snapshot_date") if isinstance(h.get("snapshot_date"), str)
              else (h["snapshot_date"].isoformat() if h.get("snapshot_date") else None),
        eps_ttm=_to_float(h.get("eps_ttm")), bps=_to_float(h.get("bps")),
        shares_outstanding=_to_float(h.get("shares_outstanding")),
        return_on_equity=_to_float(h.get("return_on_equity")),
        return_on_assets=_to_float(h.get("return_on_assets")),
        profit_margins=_to_float(h.get("profit_margins")),
        debt_to_equity=_to_float(h.get("debt_to_equity")),
        earnings_growth=_to_float(h.get("earnings_growth")),
        revenue_growth=_to_float(h.get("revenue_growth")),
    ) for h in hist_rows]

    # Articles + saved notes stay in local SQLite
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        articles = conn.execute("""
            SELECT s.final_score, s.label, s.scored_at,
                   a.source, a.title, a.url, a.published_at
            FROM sentiment_scores s
            JOIN articles a ON s.article_id = a.id
            WHERE s.ticker = ? AND s.scored_at >= datetime('now', '-30 days')
            ORDER BY s.scored_at DESC LIMIT 30
        """, (ticker,)).fetchall()
        articles = [dict(a) for a in articles]

        # Current price: latest from cache (cache-aside ensures cloud has it)
        price_rows = get_or_fetch_prices(ticker, db, period="1mo")
        current_price = (float(price_rows[-1]["adj_close"])
                          if price_rows and price_rows[-1].get("adj_close") is not None
                          else latest.get("last_price"))

        # Saved notes (if any)
        notes_row = conn.execute(
            "SELECT * FROM research_notes WHERE ticker = ?", (ticker,)
        ).fetchone()
        saved_notes = dict(notes_row) if notes_row else None

    # Factor result + screen pass/fail come from a per-process cache so we
    # don't re-score the whole universe and re-scan all 4 screens on every
    # ticker load. See analysis/_research_cache.py — 15-min TTL.
    cache = get_universe_cache(db_path, sector_risk_path)
    factor_result = cache.factor_results.get(ticker)
    risk_flags = factor_result.flags if factor_result else []

    screen_pass_fail: list[ScreenPassFail] = []
    for screen in BUILTIN_SCREENS:
        results = cache.screen_results_by_id.get(screen.id, [])
        matched = next((r for r in results if r.ticker == ticker), None)
        screen_pass_fail.append(ScreenPassFail(
            screen_id=screen.id, name=screen.name,
            passed=matched is not None,
            criteria=matched.criteria if matched else [],
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

    # Default DCF — feed the 3-tier resolver in analysis/dcf.py.
    # Tier 1 (preferred): median of available 5y CAGRs across earnings,
    # revenue, and book value per share — already computed above for Section 3.
    # Tier 2: forward analyst consensus from yfinance (cache-aside, 7d TTL).
    # Tier 3: trailing earnings_growth from the latest snapshot (yfinance YoY,
    # i.e. the same value we used to feed the OLD default flow).
    # Tier 4: 8% hardcoded floor inside the resolver.
    #
    # We still need a snapshot dict with at least eps_ttm + shares_outstanding
    # to build base_fcf — walk history newest→oldest until we find one.
    snap_for_dcf = None
    for h in reversed(history):
        if h.eps_ttm is not None and h.shares_outstanding is not None:
            snap_for_dcf = {
                "eps_ttm": h.eps_ttm,
                "shares_outstanding": h.shares_outstanding,
                # Trailing YoY for the resolver's tier 3. Median of last few
                # years smooths one-off jumps; the resolver will use it only
                # if neither CAGR nor analyst consensus is available.
                "earnings_growth": _median_trailing_growth(history),
                "last_price": current_price,
            }
            break
    dcf_inputs_default = None
    dcf_growth_provenance: Optional[GrowthProvenance] = None
    if snap_for_dcf:
        try:
            from analysis.data_loader import get_or_fetch_analyst_growth
            analyst_5y = get_or_fetch_analyst_growth(ticker, db)
        except Exception:
            analyst_5y = None
        resolved = default_inputs_from_snapshot(
            snap_for_dcf,
            cagr_earnings_5y=cagr_earnings.get(5),
            cagr_revenue_5y=cagr_revenue.get(5),
            cagr_bps_5y=cagr_bps.get(5),
            analyst_growth_5y=analyst_5y,
        )
        if resolved:
            dcf_inputs_default, dcf_growth_provenance = resolved
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
        sub_sector=latest.get("sub_sector"),
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
        dcf_growth_provenance=dcf_growth_provenance,
        financial_statements=(_load_financial_statements(ticker, db)
                              if not skip_financial_statements
                              else {"income": [], "balance": [], "cashflow": []}),
    )


def _load_financial_statements(ticker: str, db) -> dict:
    """Wrap the cache-aside loader so a fetch failure doesn't crash the whole
    report. Section 3b will render an 'unavailable' state on empty dict."""
    try:
        from analysis.data_loader import get_or_fetch_financial_statements
        return get_or_fetch_financial_statements(ticker, db)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Financial statements fetch failed [%s]: %s",
                                              ticker, e)
        return {"income": [], "balance": [], "cashflow": []}


def _to_float(v):
    """Coerce Postgres Decimal / SQLite REAL / None to plain float (or None)."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _median_trailing_growth(history: list[HistoryPoint]) -> Optional[float]:
    """Median earnings_growth over the last 5 historical rows. Used as the
    'trailing' tier-3 input to the DCF growth resolver — smooths over one-off
    jumps that the most-recent year may show, without going as deep as a
    full 5y CAGR (which is computed separately and feeds tier 1)."""
    recent = [h.earnings_growth for h in history[-5:]
                if h.earnings_growth is not None]
    if not recent:
        return None
    sorted_g = sorted(recent)
    return sorted_g[len(sorted_g) // 2]


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
