"""Multi-factor scoring engine — replaces the value+sentiment composite.

Four factors, each scored as a SUB-SECTOR-relative percentile rank (0-100)
where 100 = best in sub-sector:
  - Value   : E/P, B/P, EV/EBITDA inverse (low ratio = high rank)
  - Quality : ROE, ROA, debt/equity inverse (high profitability = high rank)
  - Growth  : earnings growth, revenue growth (high = high rank)
  - Sentiment: avg article sentiment over window (universe-percentile, not sub-sector)

Strict bucketing: tickers without a `sub_sector` assignment do NOT fall back to
parent yf_sector — they receive None for Value/Quality/Growth percentiles
(sentiment still computes universe-wide). This avoids ranking metadata-NULL
tickers against each other in a meaningless junk bucket.

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
MIN_SECTOR_SIZE = 5                          # need >=N tickers in sub-sector to compute percentiles


# Per-factor ingredient specification. Each entry describes one raw component
# of the factor's composite signal: the underlying fundamentals field, the
# acceptable bounds, the transform that converts the raw value into a
# "higher = better" contribution, and human-readable exclusion reasons used
# by FactorBreakdown when the ingredient is dropped from a ticker's composite.
# Single source of truth — _compute_subsector_factors AND breakdown_for both
# walk this table, so the displayed math always matches the computed math.
_FACTOR_INGREDIENTS = {
    "value": [
        {"name": "Earnings yield (1/PE)", "field": "trailing_pe",
         "bounds": PE_BOUNDS, "transform": (lambda v: 1.0 / v),
         "reason_oob": "trailing P/E outside data-quality bounds",
         "reason_null": "trailing P/E not available"},
        {"name": "Book yield (1/PB)", "field": "price_to_book",
         "bounds": PB_BOUNDS, "transform": (lambda v: 1.0 / v),
         "reason_oob": "P/B outside data-quality bounds",
         "reason_null": "price-to-book not available"},
        {"name": "EBITDA yield (1/EV-EBITDA)", "field": "ev_to_ebitda",
         "bounds": EV_EBITDA_BOUNDS, "transform": (lambda v: 1.0 / v),
         "reason_oob": "EV/EBITDA outside data-quality bounds",
         "reason_null": "EV/EBITDA not available"},
    ],
    "quality": [
        {"name": "Return on equity (ROE)", "field": "return_on_equity",
         "bounds": (-2.0, 2.0), "transform": (lambda v: v),
         "reason_oob": "ROE outside ±200% clip",
         "reason_null": "ROE not available"},
        {"name": "Return on assets (ROA)", "field": "return_on_assets",
         "bounds": (-1.0, 1.0), "transform": (lambda v: v),
         "reason_oob": "ROA outside ±100% clip",
         "reason_null": "ROA not available"},
        {"name": "Debt-to-equity (negated, /100)", "field": "debt_to_equity",
         "bounds": (0.0, 500.0), "transform": (lambda v: -v / 100.0),
         "reason_oob": "D/E outside 0–500% range",
         "reason_null": "D/E not available"},
    ],
    "growth": [
        {"name": "Earnings growth YoY", "field": "earnings_growth",
         "bounds": (-2.0, 5.0), "transform": (lambda v: v),
         "reason_oob": "earnings growth outside -200%/+500% clip",
         "reason_null": "earnings growth not available"},
        {"name": "Revenue growth YoY", "field": "revenue_growth",
         "bounds": (-1.0, 3.0), "transform": (lambda v: v),
         "reason_oob": "revenue growth outside -100%/+300% clip",
         "reason_null": "revenue growth not available"},
    ],
}


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
class IngredientRow:
    """One sub-component of a factor's composite signal for the target ticker."""
    name: str                                 # human label, e.g. "Earnings yield (1/PE)"
    field: str                                # underlying fundamentals key
    target_raw: Optional[float]               # target's raw fundamentals value
    target_contribution: Optional[float]      # what the ingredient contributed to the composite
    included: bool                            # False when dropped from the average
    reason_excluded: str                      # populated when included=False


@dataclass
class PeerSnapshot:
    """One row in the side-by-side V/Q/G comparison table in the breakdown
    drawer. Carries the peer's rank, the composite signal for the factor that
    opened the drawer, and all 8 raw V/Q/G fundamentals so the table can render
    every metric whether the user clicked Value, Quality, or Growth."""
    ticker: str
    name: str
    rank_position: int                        # 1-indexed rank within the bucket
    composite_signal: float                   # signal for the factor in question
    trailing_pe: Optional[float] = None
    price_to_book: Optional[float] = None
    ev_to_ebitda: Optional[float] = None
    return_on_equity: Optional[float] = None
    return_on_assets: Optional[float] = None
    debt_to_equity: Optional[float] = None
    earnings_growth: Optional[float] = None
    revenue_growth: Optional[float] = None


@dataclass
class FactorBreakdown:
    """Per-ticker / per-factor diagnostic — what fed the pctile and where the
    target sits in the sub-sector's composite-signal distribution."""
    ticker: str
    factor: str                               # "value" | "quality" | "growth"
    bucket: str                               # sub-sector name (empty if not bucketed)
    bucket_size: int                          # peers actually ranked (incl. target)
    pctile: Optional[float]                   # the published rank
    target_composite_signal: Optional[float]
    ingredients: list[IngredientRow] = field(default_factory=list)
    peer_signal_min: Optional[float] = None
    peer_signal_p25: Optional[float] = None
    peer_signal_median: Optional[float] = None
    peer_signal_p75: Optional[float] = None
    peer_signal_max: Optional[float] = None
    rank_position: Optional[int] = None       # 1-indexed rank, e.g. 9 of 36
    target_snapshot: Optional[PeerSnapshot] = None    # target's own row for the side-by-side table
    nearest_peers: list[PeerSnapshot] = field(default_factory=list)
    empty_reason: str = ""                    # populated when pctile is None


@dataclass
class EngineDiagnostics:
    fundamentals_count: int = 0
    disqualified_count: int = 0
    scorable_count: int = 0
    sectors_in_play: int = 0
    sectors_skipped_too_small: int = 0
    tickers_without_subsector: int = 0     # unranked because securities.sub_sector IS NULL
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
                as_of_date: Optional[str] = None,
                pre_loaded_rows: Optional[list[dict]] = None,
                market: Optional[str] = None,
                ) -> tuple[list[FactorResult], EngineDiagnostics]:
        weights = self._normalize_weights(weights or DEFAULT_WEIGHTS)
        self._reload_flags()

        # The Backtest engine enriches as-of rows with price-derived P/E,
        # P/B, market_cap before passing them in — the akshare-annual
        # snapshots store only per-share fields, so loading them straight
        # via _load_fundamentals would disqualify almost everything on
        # "no core fundamentals".
        fund_rows = (pre_loaded_rows if pre_loaded_rows is not None
                      else self._load_fundamentals(as_of_date=as_of_date))
        # Market scoping: rows carry `market` post-Phase-1; filter here so
        # the percentile ranking only compares apples to apples (a HK row
        # in the US universe would skew Banks percentiles).
        if market is not None:
            fund_rows = [f for f in fund_rows
                          if (f.get("market") or "HK") == market.upper()]
        sent_by_ticker = self._load_sentiment(sentiment_window_days)

        # Pass 1: disqualify + categorize
        viable, disqualified = [], []
        for f in fund_rows:
            reason = self._viability_check(f)
            if reason:
                disqualified.append((f, reason))
            else:
                viable.append(f)

        # Pass 2: bucket viable tickers by SUB-SECTOR STRICTLY (no fallback to
        # parent sector). Comparing P/E across 301 Technology names
        # (chips + apps + food delivery + solar) is meaningless, so we rank
        # only within sub-sector. Tickers WITHOUT a sub_sector assignment
        # (NULL in securities) are NOT bucketed — they receive None for
        # value/quality/growth percentiles. Sentiment is still universe-wide
        # so they may still get a composite (sentiment-only) row.
        by_subsector: dict[str, list[dict]] = {}
        unranked_tickers: list[dict] = []
        for f in viable:
            sub = f.get("sub_sector")
            if sub:
                by_subsector.setdefault(sub, []).append(f)
            else:
                unranked_tickers.append(f)

        sectors_in_play = 0
        sectors_skipped = 0
        factor_pctiles: dict[str, dict[str, Optional[float]]] = {}  # ticker -> factor -> pctile

        for sub, tickers in by_subsector.items():
            if len(tickers) < MIN_SECTOR_SIZE:
                sectors_skipped += 1
                # Still record None for each factor so they're in the output
                for t in tickers:
                    factor_pctiles[t["ticker"]] = {
                        "value": None, "quality": None, "growth": None,
                    }
                continue
            sectors_in_play += 1
            sub_pctiles = self._compute_subsector_factors(tickers)
            for ticker, pcts in sub_pctiles.items():
                factor_pctiles[ticker] = pcts

        # Unranked (no-sub_sector) tickers — None for all three factors so
        # they appear in the output table with empty V/Q/G cells.
        for t in unranked_tickers:
            factor_pctiles[t["ticker"]] = {
                "value": None, "quality": None, "growth": None,
            }

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
                # Strict sub_sector — matches the bucket used for ranking.
                # Tickers without sub_sector show "—" so the unranked group
                # is visible (their V/Q/G cells will be empty).
                sector=(f.get("sub_sector") or "—"),
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
                sector=(f.get("sub_sector") or "—"),
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
            tickers_without_subsector=len(unranked_tickers),
            tickers_with_sentiment=sum(1 for s in sent_by_ticker.values()
                                       if s["n"] >= min_articles),
            flagged_count=sum(1 for r in results if r.flags),
            note=self._diagnostic_note(len(fund_rows), len(viable), len(disqualified),
                                        sectors_in_play, len(unranked_tickers)),
        )
        return results, diag

    # ---------- private: loading ----------

    def _load_fundamentals(self, *, as_of_date: Optional[str] = None) -> list[dict]:
        # Routes via analysis/data_loader → storage/factory → cloud or sqlite
        # depending on USE_CLOUD_DB. Securities join happens client-side when on
        # cloud (securities stays local). When `as_of_date` is provided the
        # loader returns the latest snapshot per ticker that's <= that date,
        # so the factor engine can score the universe as it looked back then.
        from analysis.data_loader import get_universe_fundamentals
        from storage.database import Database
        return get_universe_fundamentals(Database(self.db_path),
                                          as_of_date=as_of_date)

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

    def _compute_subsector_factors(self, tickers: list[dict]) -> dict[str, dict[str, Optional[float]]]:
        """Return {ticker: {factor: pctile in [0,100]}} for each ticker in this sub-sector.
        Walks the _FACTOR_INGREDIENTS spec via `_factor_signal_breakdown` so the
        scoring math and the click-through breakdown UI stay in sync."""
        _, value_map = self._factor_signal_breakdown("value", None, tickers)
        _, quality_map = self._factor_signal_breakdown("quality", None, tickers)
        _, growth_map = self._factor_signal_breakdown("growth", None, tickers)
        return {
            t["ticker"]: {
                "value": _percentile_rank(t["ticker"], value_map),
                "quality": _percentile_rank(t["ticker"], quality_map),
                "growth": _percentile_rank(t["ticker"], growth_map),
            }
            for t in tickers
        }

    def _factor_signal_breakdown(self, factor: str, target: Optional[dict],
                                   bucket: list[dict],
                                   ) -> tuple[list[IngredientRow], dict[str, float]]:
        """Walk the bucket and compute one factor's composite signal per ticker.

        Composite-signal recipe (post-2026-06 normalization fix):
          1. For each ingredient (E/P, B/P, EV-yield for Value; ROE/ROA/-D/E
             for Quality; earnings/revenue growth for Growth), compute the raw
             "higher = better" contribution.
          2. Percentile-rank that contribution WITHIN the bucket (0-100).
          3. Per ticker, the composite signal is the AVERAGE of its available
             ingredient PERCENTILES — not the average of raw contributions.

        Why this matters: averaging raw contributions made Quality dominated by
        -D/E/100 (magnitude ~1-5) over ROE/ROA (magnitude ~0.05-0.2), so a
        profitable high-debt firm scored worse than an unprofitable low-debt
        firm. Same scale issue on Value (1/PB swamping 1/PE). Percentile-rank
        normalization puts every ingredient on a comparable 0-100 axis.

        Returns (target_ingredients, signal_map):
          - `signal_map[ticker]` is the composite signal (mean of ingredient
            percentiles, 0-100); the outer percentile_rank pass on this map
            produces the published V/Q/G percentile.
          - `target_ingredients` (only populated when target is not None) lists
            each ingredient with target's raw value + the per-ingredient
            percentile (stored in `target_contribution` for backward shape
            compat — see the drawer renderer for the relabel).
        """
        specs = _FACTOR_INGREDIENTS.get(factor)
        if not specs:
            return [], {}
        target_ticker = target["ticker"] if target else None

        # Pass 1: collect per-ingredient raw contributions; track target's raw
        # value + in-bounds status per ingredient for the breakdown table.
        per_ingredient_contribs: list[dict[str, float]] = [{} for _ in specs]
        target_per_ingredient: dict[int, tuple] = {}     # spec_idx -> (raw, included, reason)
        for f in bucket:
            ticker = f["ticker"]
            for i, spec in enumerate(specs):
                raw = _finite(f.get(spec["field"]))
                lo, hi = spec["bounds"]
                in_bounds = raw is not None and lo < raw < hi
                if in_bounds:
                    per_ingredient_contribs[i][ticker] = spec["transform"](raw)
                if ticker == target_ticker:
                    if in_bounds:
                        target_per_ingredient[i] = (raw, True, "")
                    else:
                        reason = (spec["reason_null"] if raw is None
                                   else spec["reason_oob"])
                        target_per_ingredient[i] = (raw, False, reason)

        # Pass 2: percentile-rank each ingredient pool independently. Each
        # ticker's per-ingredient pctile is 0-100 within the subset of bucket
        # tickers for whom that ingredient was in-bounds.
        per_ingredient_pctiles: list[dict[str, Optional[float]]] = [
            {t: _percentile_rank(t, contribs) for t in contribs}
            for contribs in per_ingredient_contribs
        ]

        # Pass 3: per ticker, average available ingredient percentiles.
        signal_map: dict[str, float] = {}
        all_tickers = {f["ticker"] for f in bucket}
        for ticker in all_tickers:
            pctiles = [pmap.get(ticker) for pmap in per_ingredient_pctiles]
            pctiles = [p for p in pctiles if p is not None]
            if pctiles:
                signal_map[ticker] = sum(pctiles) / len(pctiles)

        # Pass 4: target's IngredientRow list. `target_contribution` now
        # carries the per-ingredient PERCENTILE (0-100) rather than the raw
        # transform — the drawer label is updated to match.
        target_rows: list[IngredientRow] = []
        if target_ticker:
            for i, spec in enumerate(specs):
                if i not in target_per_ingredient:
                    continue
                raw, included, reason = target_per_ingredient[i]
                ingredient_pctile = (per_ingredient_pctiles[i].get(target_ticker)
                                       if included else None)
                target_rows.append(IngredientRow(
                    name=spec["name"], field=spec["field"],
                    target_raw=raw, target_contribution=ingredient_pctile,
                    included=included, reason_excluded=reason,
                ))

        # Preserve spec ordering so the breakdown table is stable.
        order = {spec["field"]: i for i, spec in enumerate(specs)}
        target_rows.sort(key=lambda ing: order.get(ing.field, 99))
        return target_rows, signal_map

    def breakdown_for(self, ticker: str, factor: str) -> Optional[FactorBreakdown]:
        """On-demand provenance for one ticker's one factor percentile.

        Re-loads the universe (one extra round-trip per click), reconstructs the
        ticker's strict sub-sector bucket, applies the same viability gate used
        by compute(), then runs the factor's signal computation while capturing
        the target's ingredient detail. Returns a FactorBreakdown with either
        a populated `pctile` and distribution stats, or an `empty_reason`
        explaining why no rank exists for that (ticker, factor) pair.

        `factor` must be one of "value", "quality", "growth". Sentiment isn't
        ranked per sub-sector so it has no breakdown (returns None)."""
        if factor not in {"value", "quality", "growth"}:
            return None
        fund_rows = self._load_fundamentals()
        target = next((f for f in fund_rows if f["ticker"] == ticker), None)
        if not target:
            return None
        sub = target.get("sub_sector")
        if not sub:
            return _empty_breakdown(ticker, factor,
                                     "ticker has no sub_sector assignment — V/Q/G unranked")
        # Apply the same viability gate as compute() so the bucket matches.
        bucket = [f for f in fund_rows
                  if f.get("sub_sector") == sub and not self._viability_check(f)]
        target_in_bucket = any(f["ticker"] == ticker for f in bucket)
        if not target_in_bucket:
            reason = self._viability_check(target) or "ticker excluded from bucket"
            return _empty_breakdown(ticker, factor,
                                     f"ticker disqualified by viability gate: {reason}",
                                     bucket=sub)
        if len(bucket) < MIN_SECTOR_SIZE:
            return _empty_breakdown(ticker, factor,
                                     f"sub-sector has only {len(bucket)} viable "
                                     f"ticker(s) — need ≥{MIN_SECTOR_SIZE}",
                                     bucket=sub, bucket_size=len(bucket))

        ingredients, signal_map = self._factor_signal_breakdown(factor, target, bucket)
        if ticker not in signal_map:
            return _empty_breakdown(ticker, factor,
                                     f"target has no usable {factor} ingredients "
                                     "(all dropped — see ingredient table)",
                                     ingredients=ingredients,
                                     bucket=sub, bucket_size=len(bucket))

        target_signal = signal_map[ticker]
        sorted_signals = sorted(signal_map.values())
        n = len(signal_map)
        below = sum(1 for v in sorted_signals if v < target_signal)
        equal = sum(1 for v in sorted_signals if v == target_signal)
        # 1-indexed rank position via midrank, rounded to nearest int.
        rank_pos = below + (equal + 1) // 2 if equal > 0 else below + 1
        pctile = _percentile_rank(ticker, signal_map)

        # Build target + nearest-peer PeerSnapshots for the side-by-side
        # comparison table. Rank map is 1-indexed by ascending signal. Ties
        # break on ticker string for stable ordering. Nearest 3 by absolute
        # signal distance — surfaces the names actually sitting around the
        # target in the rank.
        ranked = sorted(signal_map.items(), key=lambda kv: (kv[1], kv[0]))
        rank_by_ticker = {t: i + 1 for i, (t, _) in enumerate(ranked)}
        by_ticker_row = {f["ticker"]: f for f in bucket}

        target_snapshot = _build_peer_snapshot(
            ticker, by_ticker_row.get(ticker, target),
            rank_by_ticker[ticker], target_signal,
        )

        candidates = [(t, s) for t, s in signal_map.items() if t != ticker]
        candidates.sort(key=lambda ts: (abs(ts[1] - target_signal), ts[0]))
        nearest_peers = [
            _build_peer_snapshot(t, by_ticker_row.get(t, {}),
                                  rank_by_ticker[t], s)
            for t, s in candidates[:3]
        ]
        # Render peers in rank order (low → high) so the table reads naturally
        # from worst signal to best signal among the nearest neighbours.
        nearest_peers.sort(key=lambda p: p.rank_position)

        return FactorBreakdown(
            ticker=ticker, factor=factor, bucket=sub, bucket_size=n,
            pctile=pctile, target_composite_signal=target_signal,
            ingredients=ingredients,
            peer_signal_min=sorted_signals[0],
            peer_signal_p25=sorted_signals[n // 4],
            peer_signal_median=sorted_signals[n // 2],
            peer_signal_p75=sorted_signals[min(n - 1, 3 * n // 4)],
            peer_signal_max=sorted_signals[-1],
            rank_position=rank_pos,
            target_snapshot=target_snapshot,
            nearest_peers=nearest_peers,
            empty_reason="",
        )

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

    def _diagnostic_note(self, fund: int, viable: int, dq: int, sectors: int,
                          unranked: int = 0) -> str:
        notes = []
        if fund == 0:
            notes.append("No fundamentals data — run 'python main.py fundamentals refresh --tickers ALL'.")
        if dq > 0:
            notes.append(f"{dq} ticker(s) disqualified (microcap / negative book / extreme P/E / unprofitable).")
        if unranked > 0:
            notes.append(f"{unranked} ticker(s) have no sub-sector assignment — "
                         "V/Q/G unranked. Backfill yfinance metadata via "
                         "'fundamentals refresh --tickers ALL' to score them.")
        if sectors < 5:
            notes.append(f"Only {sectors} sub-sector(s) had enough tickers for percentile ranking — "
                         "more universe coverage needed.")
        return "  ".join(notes) if notes else ""


# ============== module-level helpers ==============

def _empty_breakdown(ticker: str, factor: str, reason: str, *,
                      ingredients: Optional[list[IngredientRow]] = None,
                      bucket: str = "", bucket_size: int = 0) -> FactorBreakdown:
    """Build a FactorBreakdown with no pctile, populated only with the
    explanation the drawer should show."""
    return FactorBreakdown(
        ticker=ticker, factor=factor, bucket=bucket, bucket_size=bucket_size,
        pctile=None, target_composite_signal=None,
        ingredients=ingredients or [],
        empty_reason=reason,
    )


def _build_peer_snapshot(ticker: str, row: dict, rank: int,
                          composite_signal: float) -> PeerSnapshot:
    """Construct a PeerSnapshot from a fundamentals row. Used by breakdown_for
    to populate target_snapshot + nearest_peers symmetrically."""
    return PeerSnapshot(
        ticker=ticker,
        name=(row.get("name") or ticker) if row else ticker,
        rank_position=rank,
        composite_signal=composite_signal,
        trailing_pe=_finite(row.get("trailing_pe")) if row else None,
        price_to_book=_finite(row.get("price_to_book")) if row else None,
        ev_to_ebitda=_finite(row.get("ev_to_ebitda")) if row else None,
        return_on_equity=_finite(row.get("return_on_equity")) if row else None,
        return_on_assets=_finite(row.get("return_on_assets")) if row else None,
        debt_to_equity=_finite(row.get("debt_to_equity")) if row else None,
        earnings_growth=_finite(row.get("earnings_growth")) if row else None,
        revenue_growth=_finite(row.get("revenue_growth")) if row else None,
    )


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
