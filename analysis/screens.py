"""Rule-based fundamental screens — pass/fail filters using absolute thresholds.

Unlike the percentile-rank ScoreEngine (relative ranking within sectors), screens
here use *absolute* thresholds informed by how disciplined value/quality investors
actually think. A stock either passes the screen or it doesn't — no scoring, no
sorting beyond market-cap order.

Each screen is a dataclass describing:
  - the criteria (predicate function on a fundamentals row)
  - human-readable description of what it filters for
  - which sector_risk flags to auto-exclude (or include for "distress" screens)

Adding a screen = add an entry to `BUILTIN_SCREENS`. The UI loops over this list.
"""
import math
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import yaml


@dataclass
class ScreenCriterion:
    label: str            # short human label e.g. "P/E in [5, 20]"
    passed: bool


@dataclass
class ScreenResult:
    ticker: str
    name: str
    sector: str
    market_cap: Optional[float]
    is_watchlist: bool
    # Why this ticker matched (or didn't): list of (criterion label, pass/fail)
    criteria: list[ScreenCriterion]
    # Convenience: raw values for the display table
    trailing_pe: Optional[float]
    price_to_book: Optional[float]
    dividend_yield: Optional[float]
    return_on_equity: Optional[float]
    debt_to_equity: Optional[float]
    earnings_growth: Optional[float]
    revenue_growth: Optional[float]
    flagged: bool


@dataclass
class ScreenDefinition:
    id: str
    name: str               # short user-visible name
    description: str        # 1-line summary of intent
    long_description: str   # multi-line explanation of methodology
    predicate: Callable     # (row, criteria_collector) -> bool
    exclude_flagged: bool = True
    is_distress_screen: bool = False  # if True, runs against DISQUALIFIED tickers too


# ============== Builtin screens ==============

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


def _value_screen(row, criteria):
    """Classic value: P/E 5-20, P/B 0.5-3, ROE > 10%, market cap > HK$2B, not flagged."""
    pe  = _finite(row.get("trailing_pe"))
    pb  = _finite(row.get("price_to_book"))
    roe = _finite(row.get("return_on_equity"))
    mc  = _finite(row.get("market_cap"))
    eg  = _finite(row.get("earnings_growth"))

    c_pe   = pe is not None and 5 <= pe <= 20
    c_pb   = pb is not None and 0.5 <= pb <= 3.0
    c_roe  = roe is not None and roe >= 0.10
    c_mc   = mc is not None and mc >= 2_000_000_000
    c_grow = eg is None or eg >= -0.10  # tolerate small earnings dip; reject big declines

    criteria.append(ScreenCriterion("P/E ∈ [5, 20]", bool(c_pe)))
    criteria.append(ScreenCriterion("P/B ∈ [0.5, 3]", bool(c_pb)))
    criteria.append(ScreenCriterion("ROE ≥ 10%", bool(c_roe)))
    criteria.append(ScreenCriterion("Market cap ≥ HK$2B", bool(c_mc)))
    criteria.append(ScreenCriterion("Earnings growth > -10%", bool(c_grow)))
    return all([c_pe, c_pb, c_roe, c_mc, c_grow])


def _quality_compounder_screen(row, criteria):
    """ROE ≥ 15%, D/E < 100%, earnings growth ≥ 0, mkt cap ≥ HK$10B."""
    roe = _finite(row.get("return_on_equity"))
    de  = _finite(row.get("debt_to_equity"))
    eg  = _finite(row.get("earnings_growth"))
    mc  = _finite(row.get("market_cap"))

    c_roe  = roe is not None and roe >= 0.15
    c_de   = de is None or de < 100   # banks have None D/E; tolerate
    c_grow = eg is not None and eg >= 0
    c_mc   = mc is not None and mc >= 10_000_000_000

    criteria.append(ScreenCriterion("ROE ≥ 15%", bool(c_roe)))
    criteria.append(ScreenCriterion("D/E < 100%", bool(c_de)))
    criteria.append(ScreenCriterion("Earnings growth ≥ 0", bool(c_grow)))
    criteria.append(ScreenCriterion("Market cap ≥ HK$10B", bool(c_mc)))
    return all([c_roe, c_de, c_grow, c_mc])


def _income_screen(row, criteria):
    """Dividend yield ≥ 4%, mkt cap ≥ HK$5B, earnings growth ≥ -5% (stable)."""
    dy  = _finite(row.get("dividend_yield"))
    mc  = _finite(row.get("market_cap"))
    eg  = _finite(row.get("earnings_growth"))

    c_dy   = dy is not None and dy >= 4.0    # yfinance returns yield in percent already
    c_mc   = mc is not None and mc >= 5_000_000_000
    c_grow = eg is None or eg >= -0.05

    criteria.append(ScreenCriterion("Dividend yield ≥ 4%", bool(c_dy)))
    criteria.append(ScreenCriterion("Market cap ≥ HK$5B", bool(c_mc)))
    criteria.append(ScreenCriterion("Earnings growth ≥ -5%", bool(c_grow)))
    return all([c_dy, c_mc, c_grow])


def _avoid_distress_screen(row, criteria):
    """Educational view — surfaces names that LOOK cheap but show actual distress.

    Tuned to be strict: low P/B alone is structural in some industries (banks,
    insurers, property) and is NOT distress. We require extreme cheapness AND
    at least 2 concrete distress signals.
    """
    pe  = _finite(row.get("trailing_pe"))
    pb  = _finite(row.get("price_to_book"))
    pm  = _finite(row.get("profit_margins"))
    eg  = _finite(row.get("earnings_growth"))
    rg  = _finite(row.get("revenue_growth"))
    de  = _finite(row.get("debt_to_equity"))

    # Extreme cheapness (much tighter than naive value)
    looks_cheap = (pe is not None and 0 < pe < 3) or (pb is not None and 0 < pb < 0.25)
    if not looks_cheap:
        return False

    # Distress red flags (tightened)
    red_flags = []
    if pm is not None and pm < -0.10:
        red_flags.append(ScreenCriterion("Profit margins < -10%", True))
    if eg is not None and eg < -0.30:
        red_flags.append(ScreenCriterion("Earnings growth < -30%", True))
    if rg is not None and rg < -0.15:
        red_flags.append(ScreenCriterion("Revenue growth < -15%", True))
    if de is not None and de > 300:
        red_flags.append(ScreenCriterion("D/E > 300%", True))

    # Need at least 2 red flags to call it actual distress
    if len(red_flags) < 2:
        return False

    criteria.append(ScreenCriterion("P/E < 3 OR P/B < 0.25 (extreme cheapness)", True))
    criteria.extend(red_flags)
    return True


BUILTIN_SCREENS = [
    ScreenDefinition(
        id="value", name="Value",
        description="Cheap by P/E and P/B, decent ROE, not too small.",
        long_description=(
            "Classic value screen. Looking for: P/E 5-20×, P/B 0.5-3×, ROE ≥ 10%, "
            "market cap ≥ HK$2B, and earnings haven't fallen more than 10% YoY. "
            "Excludes anything in the flagged sectors (China for-profit edu, "
            "property developers in workout). This is the screen for 'cheap-but-not-a-trap'."
        ),
        predicate=_value_screen, exclude_flagged=True,
    ),
    ScreenDefinition(
        id="quality_compounder", name="Quality Compounder",
        description="High ROE, low debt, growing earnings, sizeable mkt cap.",
        long_description=(
            "Buffett-style 'wonderful business at a fair price'. ROE ≥ 15%, "
            "D/E < 100%, positive YoY earnings growth, market cap ≥ HK$10B. "
            "Doesn't gate on valuation — you may still overpay — but the screen "
            "ensures the underlying business is profitable and growing."
        ),
        predicate=_quality_compounder_screen, exclude_flagged=True,
    ),
    ScreenDefinition(
        id="income", name="Income",
        description="Dividend yield ≥ 4%, mid-large cap, stable earnings.",
        long_description=(
            "For income-oriented portfolios. Dividend yield ≥ 4%, market cap ≥ HK$5B, "
            "earnings haven't fallen more than 5% YoY. Excludes flagged names whose "
            "high yields may reflect price collapse rather than payout strength."
        ),
        predicate=_income_screen, exclude_flagged=True,
    ),
    ScreenDefinition(
        id="avoid_distress", name="Avoid Distress (educational)",
        description="Looks cheap, IS broken. Why your value screen would miss these.",
        long_description=(
            "Educational view. Shows tickers that look 'cheap' (P/E < 5 or P/B < 0.5) "
            "BUT have at least one red flag: negative profit margins, earnings down >20% YoY, "
            "D/E > 200%, or negative book value. This is what a naive value screen would "
            "flag as a top buy — and why a quality / growth overlay matters."
        ),
        predicate=_avoid_distress_screen, exclude_flagged=False,
    ),
]


# ============== Runner ==============

def _load_flagged_tickers(sector_risk_path: Optional[str]) -> set[str]:
    if not sector_risk_path or not Path(sector_risk_path).exists():
        return set()
    with open(sector_risk_path) as fp:
        data = yaml.safe_load(fp) or {}
    flagged = set()
    for f in data.get("flags", []) or []:
        for t in f.get("tickers", []) or []:
            flagged.add(t)
    return flagged


def run_screen(db_path: str, screen: ScreenDefinition,
               sector_risk_path: Optional[str] = None) -> list[ScreenResult]:
    flagged = _load_flagged_tickers(sector_risk_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT f.*, s.name, s.is_watchlist, s.yf_sector, s.watchlist_sector
            FROM fundamentals_snapshots f
            INNER JOIN (
                SELECT ticker, MAX(snapshot_date) AS max_date
                FROM fundamentals_snapshots GROUP BY ticker
            ) latest ON f.ticker = latest.ticker AND f.snapshot_date = latest.max_date
            INNER JOIN securities s ON f.ticker = s.ticker
            WHERE s.is_active = 1
        """).fetchall()
        rows = [dict(r) for r in rows]

    results: list[ScreenResult] = []
    for row in rows:
        is_flagged = row["ticker"] in flagged
        if screen.exclude_flagged and is_flagged:
            continue

        criteria: list[ScreenCriterion] = []
        passed = screen.predicate(row, criteria)
        if not passed:
            continue

        results.append(ScreenResult(
            ticker=row["ticker"],
            name=row.get("name") or row["ticker"],
            sector=row.get("yf_sector") or row.get("watchlist_sector") or "—",
            market_cap=_finite(row.get("market_cap")),
            is_watchlist=bool(row.get("is_watchlist")),
            criteria=criteria,
            trailing_pe=_finite(row.get("trailing_pe")),
            price_to_book=_finite(row.get("price_to_book")),
            dividend_yield=_finite(row.get("dividend_yield")),
            return_on_equity=_finite(row.get("return_on_equity")),
            debt_to_equity=_finite(row.get("debt_to_equity")),
            earnings_growth=_finite(row.get("earnings_growth")),
            revenue_growth=_finite(row.get("revenue_growth")),
            flagged=is_flagged,
        ))

    # Sort by market cap descending
    results.sort(key=lambda r: (r.market_cap is None, -(r.market_cap or 0)))
    return results
