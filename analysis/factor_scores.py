"""Multi-factor scoring engine — replaces the value+sentiment composite.

Four factors, each scored as a sector-relative percentile rank (0-100) where
100 = best in sector:
  - Value   : E/P, B/P, EV/EBITDA inverse (low ratio = high rank)
  - Quality : ROE, ROA, debt/equity inverse (high profitability = high rank)
  - Growth  : earnings growth, revenue growth (high = high rank)
  - Sentiment: avg article sentiment over window (universe-percentile, not sector)

Composite = weighted average of available factor percentiles, also expressed
as a 0-100 percentile. No BUY/SELL classification — too prescriptive given
the data quality. The dashboard renders percentile bars instead.

Viability guards (hard disqualifiers — ticker not scored at all):
  - Market cap < HK$200M (low liquidity)
  - Negative book value (broken balance sheet)
  - P/E < 0.5 OR P/E > 500 (data quality red flag)
  - Profit margins < -50% (deeply unprofitable; not a screening target)

Sector-risk flags (informational only — ticker still scored, warning badge shown):
  Loaded from config/sector_risk.yaml at every compute() call.
"""
import math
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean, median, stdev
from typing import Optional

import yaml


# ============== Tunable thresholds ==============
MARKET_CAP_FLOOR_HKD = 200_000_000          # excludes micro-caps
PE_BOUNDS = (0.5, 500.0)                    # excludes near-zero (data) and astronomical
PB_BOUNDS = (0.01, 50.0)
EV_EBITDA_BOUNDS = (0.5, 200.0)
PROFIT_MARGIN_FLOOR = -0.50                 # -50% margins → disqualified
DEFAULT_WEIGHTS = {"value": 0.30, "quality": 0.30, "growth": 0.20, "sentiment": 0.20}
DEFAULT_SENTIMENT_WINDOW_DAYS = 7
DEFAULT_MIN_ARTICLES = 3
MIN_SECTOR_SIZE = 5                          # need >=N tickers in sector to compute percentiles


@dataclass
class Flag:
    id: str
    label: str
    severity: str    # "high" | "medium" | "low"


@dataclass
class FactorResult:
    ticker: str
    name: str
    sector: str
    is_watchlist: bool

    # Per-factor percentile (0-100, higher = better). None if factor unavailable.
    value_pctile: Optional[float]
    quality_pctile: Optional[float]
    growth_pctile: Optional[float]
    sentiment_pctile: Optional[float]

    composite_pctile: Optional[float]
    article_count: int

    # Underlying raw values (for display)
    trailing_pe: Optional[float]
    price_to_book: Optional[float]
    dividend_yield: Optional[float]
    market_cap: Optional[float]
    return_on_equity: Optional[float]
    earnings_growth: Optional[float]
    revenue_growth: Optional[float]

    # Status
    disqualified: bool
    disqualification_reason: str
    flags: list[Flag] = field(default_factory=list)


@dataclass
class EngineDiagnostics:
    fundamentals_count: int = 0
    disqualified_count: int = 0
    scorable_count: int = 0
    sectors_in_play: int = 0
    sectors_skipped_too_small: int = 0
    tickers_with_sentiment: int = 0
    flagged_count: int = 0
    note: str = ""


class FactorScoringEngine:
    def __init__(self, db_path: str, sector_risk_path: Optional[str] = None):
        self.db_path = db_path
        self.sector_risk_path = sector_risk_path
        self._flag_index: dict[str, list[Flag]] = {}  # ticker -> [Flag,...]

    # ---------- public API ----------

    def compute(self, weights: Optional[dict] = None,
                sentiment_window_days: int = DEFAULT_SENTIMENT_WINDOW_DAYS,
                min_articles: int = DEFAULT_MIN_ARTICLES,
                ) -> tuple[list[FactorResult], EngineDiagnostics]:
        weights = self._normalize_weights(weights or DEFAULT_WEIGHTS)
        self._reload_flags()

        fund_rows = self._load_fundamentals()
        sent_by_ticker = self._load_sentiment(sentiment_window_days)

        # Pass 1: disqualify + categorize
        viable, disqualified = [], []
        for f in fund_rows:
            reason = self._viability_check(f)
            if reason:
                disqualified.append((f, reason))
            else:
                viable.append(f)

        # Pass 2: bucket viable tickers by SUB-SECTOR (when available) and
        # compute per-bucket percentile ranks. Falling back through
        # effective_sector → yf_sector → watchlist_sector → "—" keeps
        # tickers without a sub_sector assignment in the system. The reason
        # to prefer sub_sector: comparing P/E across 301 Technology names
        # (chips + apps + food delivery + solar) is meaningless; per-sub-sector
        # ranking lets a Semiconductor P/E be ranked against Semiconductors.
        by_sector: dict[str, list[dict]] = {}
        for f in viable:
            sec = (f.get("sub_sector") or f.get("effective_sector")
                    or f.get("yf_sector") or f.get("watchlist_sector") or "—")
            by_sector.setdefault(sec, []).append(f)

        sectors_in_play = 0
        sectors_skipped = 0
        factor_pctiles: dict[str, dict[str, Optional[float]]] = {}  # ticker -> factor -> pctile

        for sector, tickers in by_sector.items():
            if len(tickers) < MIN_SECTOR_SIZE:
                sectors_skipped += 1
                # Still record None for each factor so they're in the output
                for t in tickers:
                    factor_pctiles[t["ticker"]] = {
                        "value": None, "quality": None, "growth": None,
                    }
                continue
            sectors_in_play += 1
            sector_pctiles = self._compute_sector_factors(tickers)
            for ticker, pcts in sector_pctiles.items():
                factor_pctiles[ticker] = pcts

        # Pass 3: sentiment percentile (universe-wide, not sector)
        sentiment_pctile = self._compute_sentiment_pctile(sent_by_ticker, min_articles)

        # Pass 4: build FactorResult per viable ticker
        results: list[FactorResult] = []
        for f in viable:
            ticker = f["ticker"]
            pcts = factor_pctiles.get(ticker, {})
            v = pcts.get("value")
            q = pcts.get("quality")
            g = pcts.get("growth")
            s = sentiment_pctile.get(ticker)
            article_count = sent_by_ticker.get(ticker, {}).get("n", 0)

            composite = self._composite(v, q, g, s, weights)

            results.append(FactorResult(
                ticker=ticker,
                name=f.get("name") or ticker,
                # Report the sub_sector when present so downstream UI shows
                # the sharper bucket; fall back through the same chain used
                # for ranking so this label always matches the peer group.
                sector=(f.get("sub_sector") or f.get("effective_sector")
                          or f.get("yf_sector") or f.get("watchlist_sector") or "—"),
                is_watchlist=bool(f.get("is_watchlist")),
                value_pctile=v, quality_pctile=q, growth_pctile=g, sentiment_pctile=s,
                composite_pctile=composite,
                article_count=article_count,
                trailing_pe=f.get("trailing_pe"),
                price_to_book=f.get("price_to_book"),
                dividend_yield=f.get("dividend_yield"),
                market_cap=f.get("market_cap"),
                return_on_equity=f.get("return_on_equity"),
                earnings_growth=f.get("earnings_growth"),
                revenue_growth=f.get("revenue_growth"),
                disqualified=False, disqualification_reason="",
                flags=self._flag_index.get(ticker, []),
            ))

        # Pass 5: append disqualified rows at the end (visible if user filters for them)
        for f, reason in disqualified:
            results.append(FactorResult(
                ticker=f["ticker"], name=f.get("name") or f["ticker"],
                sector=(f.get("sub_sector") or f.get("effective_sector")
                          or f.get("yf_sector") or f.get("watchlist_sector") or "—"),
                is_watchlist=bool(f.get("is_watchlist")),
                value_pctile=None, quality_pctile=None, growth_pctile=None,
                sentiment_pctile=None, composite_pctile=None, article_count=0,
                trailing_pe=f.get("trailing_pe"), price_to_book=f.get("price_to_book"),
                dividend_yield=f.get("dividend_yield"), market_cap=f.get("market_cap"),
                return_on_equity=f.get("return_on_equity"),
                earnings_growth=f.get("earnings_growth"),
                revenue_growth=f.get("revenue_growth"),
                disqualified=True, disqualification_reason=reason,
                flags=self._flag_index.get(f["ticker"], []),
            ))

        # Sort by composite percentile desc, None at end
        results.sort(key=lambda r: (r.composite_pctile is None, -(r.composite_pctile or 0)))

        diag = EngineDiagnostics(
            fundamentals_count=len(fund_rows),
            disqualified_count=len(disqualified),
            scorable_count=len(viable),
            sectors_in_play=sectors_in_play,
            sectors_skipped_too_small=sectors_skipped,
            tickers_with_sentiment=sum(1 for s in sent_by_ticker.values()
                                       if s["n"] >= min_articles),
            flagged_count=sum(1 for r in results if r.flags),
            note=self._diagnostic_note(len(fund_rows), len(viable), len(disqualified),
                                        sectors_in_play),
        )
        return results, diag

    # ---------- private: loading ----------

    def _load_fundamentals(self) -> list[dict]:
        # Routes via analysis/data_loader → storage/factory → cloud or sqlite
        # depending on USE_CLOUD_DB. Securities join happens client-side when on
        # cloud (securities stays local).
        from analysis.data_loader import get_universe_fundamentals
        from storage.database import Database
        return get_universe_fundamentals(Database(self.db_path))

    def _load_sentiment(self, window_days: int) -> dict[str, dict]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(f"""
                SELECT ticker, AVG(final_score) AS avg_sent, COUNT(*) AS n
                FROM sentiment_scores
                WHERE scored_at >= datetime('now', '-{int(window_days)} days')
                GROUP BY ticker
            """).fetchall()
            return {r[0]: {"avg_sent": r[1], "n": r[2]} for r in rows}

    def _reload_flags(self):
        """Re-read sector_risk.yaml on every call so user edits take effect immediately."""
        self._flag_index = {}
        if not self.sector_risk_path:
            return
        p = Path(self.sector_risk_path)
        if not p.exists():
            return
        with open(p) as fp:
            data = yaml.safe_load(fp) or {}
        for raw in data.get("flags", []) or []:
            flag = Flag(
                id=raw.get("id", "unknown"),
                label=raw.get("label", ""),
                severity=raw.get("severity", "medium"),
            )
            for t in raw.get("tickers", []) or []:
                self._flag_index.setdefault(t, []).append(flag)

    # ---------- private: viability ----------

    def _viability_check(self, f: dict) -> str:
        """Return reason string if ticker should be disqualified, else empty string."""
        mc = _finite(f.get("market_cap"))
        if mc is not None and mc < MARKET_CAP_FLOOR_HKD:
            return f"market cap < HK${MARKET_CAP_FLOOR_HKD/1e6:.0f}M"

        pb = _finite(f.get("price_to_book"))
        if pb is not None and pb < 0:
            return "negative book value"

        pe = _finite(f.get("trailing_pe"))
        if pe is not None and pe < PE_BOUNDS[0]:
            return f"P/E < {PE_BOUNDS[0]} (data quality)"
        if pe is not None and pe > PE_BOUNDS[1]:
            return f"P/E > {PE_BOUNDS[1]:.0f} (extreme)"

        pm = _finite(f.get("profit_margins"))
        if pm is not None and pm < PROFIT_MARGIN_FLOOR:
            return f"profit margin < {PROFIT_MARGIN_FLOOR:+.0%}"

        # Need at least P/E OR P/B OR market cap to be rankable
        if not any([pe, pb, mc]):
            return "no core fundamentals"
        return ""

    # ---------- private: factor computation ----------

    def _compute_sector_factors(self, tickers: list[dict]) -> dict[str, dict[str, Optional[float]]]:
        """Return {ticker: {factor: pctile in [0,100]}} for each ticker in this sector."""
        # Pull the raw signal for each factor; sign normalized so HIGHER = BETTER.
        # value: low P/E, low P/B, low EV/EBITDA → high score → use negative ratios
        # quality: high ROE, high ROA, low D/E → use ROE+ROA-D/E_normalized
        # growth: high earnings_growth, high revenue_growth
        value_signals: dict[str, float] = {}
        quality_signals: dict[str, float] = {}
        growth_signals: dict[str, float] = {}

        for f in tickers:
            ticker = f["ticker"]

            # VALUE — use earnings yield (E/P = 1/PE), book yield (B/P = 1/PB) where in bounds
            value_parts = []
            pe = _finite(f.get("trailing_pe"))
            if pe is not None and PE_BOUNDS[0] < pe < PE_BOUNDS[1]:
                value_parts.append(1.0 / pe)              # earnings yield
            pb = _finite(f.get("price_to_book"))
            if pb is not None and PB_BOUNDS[0] < pb < PB_BOUNDS[1]:
                value_parts.append(1.0 / pb)              # book yield
            evx = _finite(f.get("ev_to_ebitda"))
            if evx is not None and EV_EBITDA_BOUNDS[0] < evx < EV_EBITDA_BOUNDS[1]:
                value_parts.append(1.0 / evx)             # EBITDA yield (rough)
            if value_parts:
                value_signals[ticker] = sum(value_parts) / len(value_parts)

            # QUALITY — ROE + ROA + (negative D/E normalized)
            quality_parts = []
            roe = _finite(f.get("return_on_equity"))
            if roe is not None and -2 < roe < 2:           # clip to ±200%
                quality_parts.append(roe)
            roa = _finite(f.get("return_on_assets"))
            if roa is not None and -1 < roa < 1:
                quality_parts.append(roa)
            de = _finite(f.get("debt_to_equity"))
            if de is not None and 0 <= de < 500:           # D/E is in %; 500 = 5x equity in debt
                quality_parts.append(-de / 100.0)          # higher debt = lower quality
            if quality_parts:
                quality_signals[ticker] = sum(quality_parts) / len(quality_parts)

            # GROWTH — earnings + revenue YoY
            growth_parts = []
            eg = _finite(f.get("earnings_growth"))
            if eg is not None and -2 < eg < 5:             # clip ±200% / +500%
                growth_parts.append(eg)
            rg = _finite(f.get("revenue_growth"))
            if rg is not None and -1 < rg < 3:
                growth_parts.append(rg)
            if growth_parts:
                growth_signals[ticker] = sum(growth_parts) / len(growth_parts)

        # Convert raw signals to percentile ranks within sector
        return {
            ticker: {
                "value": _percentile_rank(ticker, value_signals),
                "quality": _percentile_rank(ticker, quality_signals),
                "growth": _percentile_rank(ticker, growth_signals),
            }
            for ticker in {f["ticker"] for f in tickers}
        }

    def _compute_sentiment_pctile(self, sent_by_ticker: dict, min_articles: int) -> dict:
        eligible = {t: s["avg_sent"] for t, s in sent_by_ticker.items()
                    if s["n"] >= min_articles and s["avg_sent"] is not None}
        return {t: _percentile_rank(t, eligible) for t in eligible}

    def _composite(self, v, q, g, s, weights: dict[str, float]) -> Optional[float]:
        """Weighted average of available factor percentiles. Weights re-normalize over
        whichever factors are available — a ticker with no sentiment data still gets a
        composite, just dropping sentiment from the mix."""
        components = [(weights["value"], v), (weights["quality"], q),
                      (weights["growth"], g), (weights["sentiment"], s)]
        active = [(w, val) for w, val in components if val is not None]
        if not active:
            return None
        total_w = sum(w for w, _ in active)
        if total_w == 0:
            return None
        return sum(w * val for w, val in active) / total_w

    def _normalize_weights(self, w: dict) -> dict[str, float]:
        clean = {k: max(0.0, float(w.get(k, 0))) for k in DEFAULT_WEIGHTS}
        s = sum(clean.values())
        if s == 0:
            return DEFAULT_WEIGHTS
        return {k: v / s for k, v in clean.items()}

    def _diagnostic_note(self, fund: int, viable: int, dq: int, sectors: int) -> str:
        notes = []
        if fund == 0:
            notes.append("No fundamentals data — run 'python main.py fundamentals refresh --tickers ALL'.")
        if dq > 0:
            notes.append(f"{dq} ticker(s) disqualified (microcap / negative book / extreme P/E / unprofitable).")
        if sectors < 5:
            notes.append(f"Only {sectors} sectors had enough tickers for percentile ranking — "
                         "more universe coverage needed.")
        return "  ".join(notes) if notes else ""


# ============== module-level helpers ==============

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


def _percentile_rank(ticker: str, signal_map: dict[str, float]) -> Optional[float]:
    """Return ticker's percentile rank in signal_map (0-100, higher signal = higher rank).
    Returns None if ticker not in map (signal unavailable)."""
    if ticker not in signal_map:
        return None
    n = len(signal_map)
    if n <= 1:
        return 50.0  # only one ticker, can't rank meaningfully
    my_value = signal_map[ticker]
    below = sum(1 for v in signal_map.values() if v < my_value)
    equal = sum(1 for v in signal_map.values() if v == my_value)
    # Midrank for ties
    rank = below + (equal + 1) / 2
    return round(100 * rank / (n + 1), 1)
