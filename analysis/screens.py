"""Rule-based fundamental screens — pass/fail filters using absolute thresholds.

Unlike the percentile-rank FactorScoringEngine (relative ranking within sectors),
screens here use *absolute* thresholds informed by how disciplined value/quality
investors think. A stock either passes the screen or it doesn't — no scoring.

Each screen is parameterized via `ScreenParams` so the same predicate can be
re-evaluated with different thresholds. The per-industry optimizer
(`analysis/optimization.py`) sweeps the param grid to find the per-industry
thresholds that historically produced the best Information Ratio. The dashboard
falls back to `BUILTIN_SCREENS[i].default_params` when no optimized set is
available for an industry.
"""
import math
import sqlite3
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Callable, Optional

import yaml


@dataclass
class ScreenParams:
    """All possible thresholds across all screens. Each screen uses a subset.
    Default values are "permissive" (pass anything) so unused fields don't
    accidentally filter."""
    pe_min: float = -math.inf
    pe_max: float = math.inf
    pb_min: float = -math.inf
    pb_max: float = math.inf
    roe_min: float = -math.inf
    roe_max: float = math.inf
    de_max: float = math.inf
    earnings_growth_min: float = -math.inf
    revenue_growth_min: float = -math.inf
    profit_margins_min: float = -math.inf
    profit_margins_max: float = math.inf
    dividend_yield_min: float = -math.inf
    market_cap_min: float = 0.0

    def to_dict(self) -> dict:
        """Serialize to dict, replacing inf with None for JSON friendliness."""
        out = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if isinstance(v, float) and (math.isinf(v) or math.isnan(v)):
                out[f.name] = None
            else:
                out[f.name] = v
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "ScreenParams":
        """Reconstruct from to_dict() — None means 'permissive default'."""
        defaults = {f.name: f.default for f in fields(cls)}
        merged = {}
        for f in fields(cls):
            v = d.get(f.name, defaults[f.name])
            if v is None:
                v = defaults[f.name]
            merged[f.name] = v
        return cls(**merged)


@dataclass
class ScreenCriterion:
    label: str
    passed: bool


@dataclass
class ScreenResult:
    ticker: str
    name: str
    sector: str
    market_cap: Optional[float]
    is_watchlist: bool
    criteria: list[ScreenCriterion]
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
    name: str
    description: str
    long_description: str
    predicate: Callable               # (row, params, criteria_collector) -> bool
    default_params: ScreenParams
    exclude_flagged: bool = True


# ============== Predicates (now parameterized) ==============

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


def _value_screen(row, params: ScreenParams, criteria):
    pe  = _finite(row.get("trailing_pe"))
    pb  = _finite(row.get("price_to_book"))
    roe = _finite(row.get("return_on_equity"))
    mc  = _finite(row.get("market_cap"))
    eg  = _finite(row.get("earnings_growth"))

    c_pe   = pe is not None and params.pe_min <= pe <= params.pe_max
    c_pb   = pb is not None and params.pb_min <= pb <= params.pb_max
    c_roe  = roe is not None and roe >= params.roe_min
    c_mc   = mc is not None and mc >= params.market_cap_min
    # Earnings-growth tolerance: missing data passes (we don't have it for all tickers)
    c_grow = eg is None or eg >= params.earnings_growth_min

    criteria.append(ScreenCriterion(
        f"P/E in [{_fmt(params.pe_min)}, {_fmt(params.pe_max)}]", bool(c_pe)))
    criteria.append(ScreenCriterion(
        f"P/B in [{_fmt(params.pb_min)}, {_fmt(params.pb_max)}]", bool(c_pb)))
    criteria.append(ScreenCriterion(
        f"ROE >= {_fmt_pct(params.roe_min)}", bool(c_roe)))
    criteria.append(ScreenCriterion(
        f"Market cap >= HK${_fmt_money(params.market_cap_min)}", bool(c_mc)))
    criteria.append(ScreenCriterion(
        f"Earnings growth >= {_fmt_pct(params.earnings_growth_min)}", bool(c_grow)))
    return all([c_pe, c_pb, c_roe, c_mc, c_grow])


def _quality_compounder_screen(row, params: ScreenParams, criteria):
    roe = _finite(row.get("return_on_equity"))
    de  = _finite(row.get("debt_to_equity"))
    eg  = _finite(row.get("earnings_growth"))
    mc  = _finite(row.get("market_cap"))

    c_roe  = roe is not None and roe >= params.roe_min
    # D/E missing is tolerated (banks have None D/E in yfinance)
    c_de   = de is None or de < params.de_max
    c_grow = eg is not None and eg >= params.earnings_growth_min
    c_mc   = mc is not None and mc >= params.market_cap_min

    criteria.append(ScreenCriterion(f"ROE >= {_fmt_pct(params.roe_min)}", bool(c_roe)))
    criteria.append(ScreenCriterion(f"D/E < {_fmt(params.de_max)}%", bool(c_de)))
    criteria.append(ScreenCriterion(
        f"Earnings growth >= {_fmt_pct(params.earnings_growth_min)}", bool(c_grow)))
    criteria.append(ScreenCriterion(
        f"Market cap >= HK${_fmt_money(params.market_cap_min)}", bool(c_mc)))
    return all([c_roe, c_de, c_grow, c_mc])


def _income_screen(row, params: ScreenParams, criteria):
    dy = _finite(row.get("dividend_yield"))
    mc = _finite(row.get("market_cap"))
    eg = _finite(row.get("earnings_growth"))

    c_dy   = dy is not None and dy >= params.dividend_yield_min
    c_mc   = mc is not None and mc >= params.market_cap_min
    c_grow = eg is None or eg >= params.earnings_growth_min

    criteria.append(ScreenCriterion(
        f"Dividend yield >= {_fmt(params.dividend_yield_min)}%", bool(c_dy)))
    criteria.append(ScreenCriterion(
        f"Market cap >= HK${_fmt_money(params.market_cap_min)}", bool(c_mc)))
    criteria.append(ScreenCriterion(
        f"Earnings growth >= {_fmt_pct(params.earnings_growth_min)}", bool(c_grow)))
    return all([c_dy, c_mc, c_grow])


def _fmt(v):
    if v is None or (isinstance(v, float) and (math.isinf(v) or math.isnan(v))):
        return "∞" if v == math.inf else ("-∞" if v == -math.inf else "?")
    if isinstance(v, float) and v == int(v):
        return f"{int(v)}"
    return f"{v}"

def _fmt_pct(v):
    if v is None or (isinstance(v, float) and (math.isinf(v) or math.isnan(v))):
        return "any"
    return f"{v * 100:.0f}%"

def _fmt_money(v):
    if v is None or v == 0:
        return "0"
    abbr = [(1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")]
    for size, suffix in abbr:
        if v >= size:
            return f"{v/size:.1f}{suffix}"
    return f"{v:.0f}"


# ============== Builtin screens with default params (preserve old behavior) ==============

BUILTIN_SCREENS = [
    ScreenDefinition(
        id="value", name="Value",
        description="Cheap by P/E and P/B, decent ROE, not too small.",
        long_description=(
            "Classic value screen. Default thresholds: P/E in [5, 20], P/B in [0.5, 3], "
            "ROE >= 10%, market cap >= HK$2B, earnings growth tolerated down to -10%. "
            "Excludes anything in the flagged sectors. "
            "Backtest can override these per-industry from optimized_parameters."
        ),
        predicate=_value_screen, exclude_flagged=True,
        default_params=ScreenParams(
            pe_min=5, pe_max=20,
            pb_min=0.5, pb_max=3,
            roe_min=0.10,
            market_cap_min=2_000_000_000,
            earnings_growth_min=-0.10,
        ),
    ),
    ScreenDefinition(
        id="quality_compounder", name="Quality Compounder",
        description="High ROE, low debt, growing earnings, sizeable mkt cap.",
        long_description=(
            "Buffett-style 'wonderful business at a fair price'. Default thresholds: "
            "ROE >= 15%, D/E < 100%, earnings growth >= 0, market cap >= HK$10B. "
            "Doesn't gate on valuation — you may still overpay — but the screen "
            "ensures the underlying business is profitable and growing."
        ),
        predicate=_quality_compounder_screen, exclude_flagged=True,
        default_params=ScreenParams(
            roe_min=0.15,
            de_max=100,
            earnings_growth_min=0.0,
            market_cap_min=10_000_000_000,
        ),
    ),
    ScreenDefinition(
        id="income", name="Income",
        description="Dividend yield >= 4%, mid-large cap, stable earnings.",
        long_description=(
            "For income-oriented portfolios. Default thresholds: dividend yield >= 4%, "
            "market cap >= HK$5B, earnings growth >= -5%. Excludes flagged names whose "
            "high yields may reflect price collapse rather than payout strength."
        ),
        predicate=_income_screen, exclude_flagged=True,
        default_params=ScreenParams(
            dividend_yield_min=4.0,
            market_cap_min=5_000_000_000,
            earnings_growth_min=-0.05,
        ),
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
               sector_risk_path: Optional[str] = None,
               params: Optional[ScreenParams] = None) -> list[ScreenResult]:
    """Apply the screen to the latest fundamentals snapshot of every active ticker.

    `params` defaults to `screen.default_params` for backward compatibility with
    the dashboard. The optimizer / backtest engine pass custom params."""
    use_params = params if params is not None else screen.default_params
    flagged = _load_flagged_tickers(sector_risk_path)
    from analysis.data_loader import get_universe_fundamentals
    from storage.database import Database
    rows = get_universe_fundamentals(Database(db_path))

    results: list[ScreenResult] = []
    for row in rows:
        is_flagged = row["ticker"] in flagged
        if screen.exclude_flagged and is_flagged:
            continue

        criteria: list[ScreenCriterion] = []
        passed = screen.predicate(row, use_params, criteria)
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

    results.sort(key=lambda r: (r.market_cap is None, -(r.market_cap or 0)))
    return results
