"""Preset + V/Q/G top-10 walk-forward backtest engine.

Powers the Backtest dashboard tab. Given an investor preset (Buffett /
Graham / Lynch / Greenblatt / Druckenmiller) and a (start, end,
rebalance-frequency) tuple, the engine:

1. Builds a rebalance calendar from the ^HSI trading-day series.
2. At each rebalance t, pulls as-of fundamentals (`snapshot_date <= t - 60d`,
   matching the same reporting-lag guard as `analysis/backtest.py`).
3. Filters the universe through the preset's slider-range overrides
   (see `analysis/preset_filter.py`).
4. Scores V/Q/G via `FactorScoringEngine.compute(as_of_date=...)`, keeps
   the survivors, takes the top 10 by composite percentile.
5. Market-cap weights those top-10 holdings.
6. Computes the period return between t and the next rebalance using
   `adj_close` from `historical_prices`.
7. Compounds returns to produce a normalised (t0 = 100) equity curve;
   compares against ^HSI normalised the same way.

Scoring is memoised by the snapshot_date the FactorScoringEngine ends up
using — consecutive daily rebalances over a quarter share the same
snapshot, so the heavy bucketing+percentile work runs once per unique
snapshot, not once per rebalance.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# Mirror the reporting-lag guard from analysis/backtest.py so the two
# engines treat as-of dates the same way.
DEFAULT_REPORTING_LAG_DAYS = 60
RISK_FREE_RATE_ANNUAL = 0.03   # locked per plan
TARGET_TOP_N = 10
BENCHMARK_TICKER = "^HSI"

# Stride in trading days per rebalance frequency string.
# 5 trading days ≈ 1 calendar week; 21 trading days ≈ 1 calendar month.
REBALANCE_STRIDES = {"1d": 1, "3d": 3, "1w": 5, "1m": 21}

# Annualisation factors (trading days per year, ~252 in HK).
TRADING_DAYS_PER_YEAR = 252
PERIODS_PER_YEAR = {
    "1d": TRADING_DAYS_PER_YEAR,
    "3d": TRADING_DAYS_PER_YEAR / 3,
    "1w": TRADING_DAYS_PER_YEAR / 5,
    "1m": 12,
}


@dataclass
class RebalanceSnapshot:
    """One rebalance event. `holdings` is the post-rebalance portfolio,
    `period_return` is the realised return through the next rebalance
    (None for the final snapshot)."""
    date: str
    holdings: list[tuple[str, float]] = field(default_factory=list)
    period_return: Optional[float] = None


@dataclass
class BacktestMetrics:
    total_return: float
    annualized_return: float
    annualized_vol: float
    sharpe: float                # at rf = 0.03 annual
    max_drawdown: float
    hit_rate: float              # % periods beating benchmark
    n_rebalances: int


@dataclass
class PresetBacktestResult:
    preset_id: str
    preset_label: str
    start_date: str
    end_date: str
    rebalance_freq: str
    rebalance_log: list[RebalanceSnapshot]
    equity_curve: list[tuple[str, float]]     # (date, value, t0=100)
    benchmark_curve: list[tuple[str, float]]
    metrics: BacktestMetrics
    preset_survivors_at_start: list[str]


# ============================================================================
# Helpers
# ============================================================================

def _hsi_trading_days(repo, start_date: str, end_date: str) -> list[str]:
    """Trading days between [start_date, end_date] inclusive, derived from
    ^HSI rows in historical_prices. Honest about HK public holidays without
    having to maintain a calendar table."""
    rows = repo.get_price_series(BENCHMARK_TICKER, start_date, end_date)
    return [r["date"] for r in rows]


def _rebalance_dates(trading_days: list[str], freq: str) -> list[str]:
    """Stride the trading-day calendar to pick rebalance dates. Always
    includes the first and last days of the window."""
    stride = REBALANCE_STRIDES.get(freq)
    if not stride or not trading_days:
        return list(trading_days)
    picked = trading_days[::stride]
    # Ensure the final trading day is in the list — it terminates the last
    # held period and anchors the equity curve's final value.
    if picked[-1] != trading_days[-1]:
        picked.append(trading_days[-1])
    return picked


def _effective_snapshot_date(t: str, snapshot_dates: list[str]) -> Optional[str]:
    """Snapshot_date the factor engine will end up using for an as-of of
    `t` (with the 60-day reporting-lag guard). Used to memoise scoring."""
    cutoff = (date.fromisoformat(t[:10]) -
              timedelta(days=DEFAULT_REPORTING_LAG_DAYS)).isoformat()
    cands = [s for s in snapshot_dates if s <= cutoff]
    return max(cands) if cands else None


# A "major" snapshot is one that covers a meaningful slice of the universe.
# fundamentals_snapshots also accumulates small one-off rows from per-ticker
# refreshes (typically <10 tickers per snapshot_date) — those shouldn't
# force a re-scoring round during a backtest.
MAJOR_SNAPSHOT_THRESHOLD = 100


def _list_snapshot_dates(db_path: str) -> list[str]:
    """Snapshot_dates with at least MAJOR_SNAPSHOT_THRESHOLD tickers,
    ascending. Filtering keeps the memoised scoring rounds bounded
    (typically 1 per year — annual akshare year-end snapshots)."""
    import os
    if os.getenv("USE_CLOUD_DB", "").lower() == "true":
        from storage import cloud_db
        with cloud_db.cursor() as cur:
            cur.execute(
                "SELECT snapshot_date FROM fundamentals_snapshots "
                "GROUP BY snapshot_date HAVING COUNT(*) >= %s "
                "ORDER BY snapshot_date ASC",
                (MAJOR_SNAPSHOT_THRESHOLD,),
            )
            return [str(r[0]) for r in cur.fetchall()]
    else:
        import sqlite3
        with sqlite3.connect(db_path) as conn:
            return [r[0] for r in conn.execute(
                "SELECT snapshot_date FROM fundamentals_snapshots "
                "GROUP BY snapshot_date HAVING COUNT(*) >= ? "
                "ORDER BY snapshot_date ASC",
                (MAJOR_SNAPSHOT_THRESHOLD,),
            ).fetchall()]


def _enrich_as_of(snapshot_row: dict, price: Optional[float]) -> dict:
    """Compute P/E, P/B, market_cap from per-share fields + as-of price
    when the row only has eps_ttm / bps / shares_outstanding. Mirrors
    `analysis/backtest.py:_enrich` so both engines behave identically.
    Returns the row unchanged if `price` is None — caller decides whether
    to drop it."""
    e = dict(snapshot_row)
    if price is None or price <= 0:
        return e

    def _f(v):
        try:
            f = float(v)
            if f != f or f in (float("inf"), float("-inf")):
                return None
            return f
        except (TypeError, ValueError):
            return None

    if e.get("trailing_pe") is None:
        eps = _f(e.get("eps_ttm"))
        if eps and eps > 0:
            e["trailing_pe"] = price / eps
    if e.get("price_to_book") is None:
        bps = _f(e.get("bps"))
        if bps and bps > 0:
            e["price_to_book"] = price / bps
    if e.get("market_cap") is None:
        shares = _f(e.get("shares_outstanding"))
        if shares and shares > 0:
            e["market_cap"] = price * shares
    return e


def _bulk_as_of_prices(repo, tickers: list[str],
                         target_date: str) -> dict[str, float]:
    """{ticker: latest adj_close at or before target_date} via the repo's
    bulk_prices_on_or_before method. One round-trip per snapshot rather
    than one per ticker — turns ~2 minutes of sequential lookups on the
    cloud pool into ~1 second."""
    try:
        return repo.bulk_prices_on_or_before(list(tickers), target_date)
    except AttributeError:
        # Fallback for any future repo that doesn't implement the bulk
        # method yet — degrade to the per-ticker path so the engine still
        # produces results, just slowly.
        out = {}
        for t in tickers:
            try:
                out[t] = repo.get_price_on_or_before(t, target_date)
            except Exception:
                out[t] = None
        return out


def _market_cap_weights(top_holdings) -> list[tuple[str, float]]:
    """Cap-weighted across the supplied FactorResults. Falls back to equal
    weight if no cap data is available."""
    caps = [(r.ticker, r.market_cap or 0.0)
            for r in top_holdings if (r.market_cap or 0) > 0]
    total = sum(c for _, c in caps)
    if total > 0:
        return [(t, c / total) for t, c in caps]
    # No cap data — equal weight across whatever holdings we have
    n = len(top_holdings)
    if n == 0:
        return []
    eq = 1.0 / n
    return [(r.ticker, eq) for r in top_holdings]


def _period_return(holdings: list[tuple[str, float]],
                    t_start: str, t_end: str,
                    price_cache: dict) -> float:
    """Sum(weight × (P_end/P_start − 1)) across the holdings. Holdings
    missing a price at either endpoint are silently dropped from the
    weighted sum (treat as cash; their weight contributes 0 return)."""
    total = 0.0
    for ticker, weight in holdings:
        prices = price_cache.get(ticker)
        if not prices:
            continue
        p_start = _price_on_or_before(prices, t_start)
        p_end = _price_on_or_before(prices, t_end)
        if p_start is None or p_end is None or p_start <= 0:
            continue
        total += weight * (p_end / p_start - 1.0)
    return total


def _price_on_or_before(price_series: list[dict], target_date: str) -> Optional[float]:
    """Linear scan over a per-ticker price series sorted ASC. Could be
    binary-searched, but with ≤ 2,800 rows per ticker and ≤ 1,260
    rebalances over 5y, the linear pass costs sub-ms per call."""
    last = None
    for r in price_series:
        if r["date"] > target_date:
            break
        last = r.get("adj_close")
    return last


def _build_equity_curve(rebalance_log: list[RebalanceSnapshot]) -> list[tuple[str, float]]:
    """Compounding `period_return`s starting from 100."""
    if not rebalance_log:
        return []
    curve = [(rebalance_log[0].date, 100.0)]
    cumulative = 1.0
    for snap in rebalance_log[:-1]:
        cumulative *= (1.0 + (snap.period_return or 0.0))
        # The compounded value at the next rebalance:
        next_date = rebalance_log[rebalance_log.index(snap) + 1].date
        curve.append((next_date, cumulative * 100.0))
    return curve


def _benchmark_curve(repo, start_date: str, end_date: str,
                      rebalance_dates: list[str]) -> list[tuple[str, float]]:
    """^HSI normalized to 100 at start, sampled at the same dates as the
    equity curve so they overlay cleanly on the chart."""
    rows = repo.get_price_series(BENCHMARK_TICKER, start_date, end_date)
    if not rows:
        return []
    base = next((r["adj_close"] for r in rows if r.get("adj_close")), None)
    if not base:
        return []
    by_date = {r["date"]: r.get("adj_close") for r in rows
               if r.get("adj_close") is not None}
    out = []
    for t in rebalance_dates:
        # Use the most recent ^HSI close at-or-before t (handles cases
        # where t isn't a trading day for HSI specifically).
        cands = [d for d in by_date if d <= t]
        if not cands:
            continue
        px = by_date[max(cands)]
        out.append((t, (px / base) * 100.0))
    return out


def _compute_metrics(rebalance_log: list[RebalanceSnapshot],
                      benchmark_curve: list[tuple[str, float]],
                      freq: str) -> BacktestMetrics:
    """Aggregate metrics across the rebalance series. Sharpe is annualised
    at rf=3% per the locked plan; max drawdown is computed off the
    compounded equity curve."""
    n = max(len(rebalance_log) - 1, 0)  # actual periods with a return
    period_returns = [s.period_return or 0.0 for s in rebalance_log[:-1]]

    if not period_returns:
        return BacktestMetrics(0, 0, 0, 0, 0, 0, n)

    # Total return: compounded
    cumulative = 1.0
    equity = [1.0]
    for r in period_returns:
        cumulative *= (1.0 + r)
        equity.append(cumulative)
    total_return = cumulative - 1.0

    # Annualised return + vol
    periods_per_year = PERIODS_PER_YEAR.get(freq, 12)
    if total_return > -1.0:
        ann_return = (1.0 + total_return) ** (periods_per_year / n) - 1.0
    else:
        ann_return = -1.0

    mean_r = sum(period_returns) / n
    if n > 1:
        var = sum((r - mean_r) ** 2 for r in period_returns) / (n - 1)
        sd = math.sqrt(var)
    else:
        sd = 0.0
    ann_vol = sd * math.sqrt(periods_per_year)

    # Sharpe at rf = 3%
    sharpe = (ann_return - RISK_FREE_RATE_ANNUAL) / ann_vol if ann_vol > 0 else 0.0

    # Max drawdown from the equity series
    peak = equity[0]
    max_dd = 0.0
    for v in equity:
        if v > peak:
            peak = v
        dd = (v / peak) - 1.0
        if dd < max_dd:
            max_dd = dd

    # Hit rate vs benchmark — convert benchmark_curve to period returns
    # aligned with the rebalance log.
    bench_returns = []
    if len(benchmark_curve) >= 2:
        for i in range(1, len(benchmark_curve)):
            prev = benchmark_curve[i - 1][1]
            cur = benchmark_curve[i][1]
            if prev > 0:
                bench_returns.append(cur / prev - 1.0)
    matched = min(len(bench_returns), len(period_returns))
    if matched > 0:
        wins = sum(1 for i in range(matched)
                   if period_returns[i] > bench_returns[i])
        hit_rate = wins / matched
    else:
        hit_rate = 0.0

    return BacktestMetrics(
        total_return=total_return,
        annualized_return=ann_return,
        annualized_vol=ann_vol,
        sharpe=sharpe,
        max_drawdown=max_dd,
        hit_rate=hit_rate,
        n_rebalances=n,
    )


# ============================================================================
# Public entry point
# ============================================================================

def run_preset_backtest(preset_id: str,
                          start_date: str,
                          end_date: str,
                          rebalance_freq: str,
                          db_path: str,
                          sector_risk_path: Optional[str] = None,
                          ) -> PresetBacktestResult:
    """Run a preset + V/Q/G top-10 walk-forward backtest. See module
    docstring for the algorithm."""
    from analysis.factor_scores import FactorScoringEngine
    from analysis.preset_filter import apply_preset
    from dashboard.screener_layout import NUMERIC_FILTERS
    from dashboard.screener_presets import INVESTOR_PRESETS
    from storage.database import Database
    from storage.factory import get_prices_repo

    preset = next((p for p in INVESTOR_PRESETS if p["id"] == preset_id), None)
    if preset is None:
        raise ValueError(f"Unknown preset id: {preset_id}")

    db = Database(db_path)
    prices_repo = get_prices_repo(db)

    # --- Build rebalance calendar ---------------------------------------
    trading_days = _hsi_trading_days(prices_repo, start_date, end_date)
    if not trading_days:
        raise RuntimeError(
            f"No ^HSI trading days between {start_date} and {end_date}. "
            "Run a price refresh first."
        )
    rebal_dates = _rebalance_dates(trading_days, rebalance_freq)

    # --- Pre-compute the snapshot-date for each rebalance (memoisation) ---
    snapshot_dates = _list_snapshot_dates(db_path)
    effective_per_rebal = [_effective_snapshot_date(t, snapshot_dates)
                            for t in rebal_dates]

    # Distinct snapshot dates we'll actually need to score
    needed = sorted({s for s in effective_per_rebal if s is not None})
    logger.info("Backtest: %d rebalances span %d unique snapshots — scoring %d times",
                 len(rebal_dates), len(needed), len(needed))

    # --- Score each needed snapshot once ---------------------------------
    # For each snapshot we (a) load as-of fundamentals, (b) bulk-fetch
    # as-of adj_close per ticker, (c) enrich each row with price-derived
    # P/E, P/B, market_cap (akshare-annual stores only per-share fields),
    # then (d) hand the enriched rows to the factor engine via the
    # pre_loaded_rows hatch. Without enrichment ~all tickers would
    # disqualify on "no core fundamentals".
    from analysis.data_loader import get_universe_fundamentals
    engine = FactorScoringEngine(db_path, sector_risk_path)
    score_cache: dict[str, list] = {}
    # Pick the rebalance date that maps to this snapshot for the as-of
    # price lookup. Using the earliest rebalance that maps to the snapshot
    # is correct — that's when the snapshot data first becomes available.
    snap_to_first_rebal = {}
    for i, t in enumerate(rebal_dates):
        snap = effective_per_rebal[i]
        if snap is not None and snap not in snap_to_first_rebal:
            snap_to_first_rebal[snap] = t

    for snap_date in needed:
        raw_rows = get_universe_fundamentals(db, as_of_date=snap_date)
        as_of_t = snap_to_first_rebal.get(snap_date, snap_date)
        price_map = _bulk_as_of_prices(prices_repo,
                                         [r["ticker"] for r in raw_rows],
                                         as_of_t)
        enriched = [_enrich_as_of(r, price_map.get(r["ticker"]))
                     for r in raw_rows]
        results, _diag = engine.compute(as_of_date=snap_date,
                                          pre_loaded_rows=enriched)
        score_cache[snap_date] = results

    # --- Per rebalance: filter → top-10 → cap-weight → next-period return -
    rebalance_log: list[RebalanceSnapshot] = []
    # Universe ticker set across the whole backtest (for the price cache).
    universe_tickers: set[str] = set()
    survivors_at_start: list[str] = []

    for i, t in enumerate(rebal_dates):
        snap = effective_per_rebal[i]
        if snap is None:
            rebalance_log.append(RebalanceSnapshot(date=t, holdings=[]))
            continue
        scored = score_cache[snap]

        # Filter survivors by preset — operate on a row-shaped dict so we
        # can re-use the shared preset_filter helper. Adapt FactorResult →
        # row dict on the fly (small set; cost is negligible).
        as_rows = [{
            "ticker": r.ticker,
            "trailing_pe": r.trailing_pe,
            "price_to_book": r.price_to_book,
            "dividend_yield": r.dividend_yield,
            "return_on_equity": r.return_on_equity,
            "earnings_growth": r.earnings_growth,
            "debt_to_equity": None,    # not on FactorResult; preset rows
                                       # use this. None passes (permissive).
            "market_cap": r.market_cap,
            "ev_to_ebitda": None,      # likewise — None passes for now
            "beta": None,
            "forward_pe": None,
        } for r in scored if not r.disqualified]
        survivors_rows = apply_preset(as_rows, preset, NUMERIC_FILTERS)
        survivor_tickers = {r["ticker"] for r in survivors_rows}
        survivors_full = [r for r in scored if r.ticker in survivor_tickers]

        # Capture start-of-period survivor list once for the Save flow
        if i == 0:
            survivors_at_start = [r.ticker for r in survivors_full]

        # Top N by composite percentile
        ranked = sorted(
            (r for r in survivors_full if r.composite_pctile is not None),
            key=lambda r: -r.composite_pctile,
        )
        top = ranked[:TARGET_TOP_N]
        weights = _market_cap_weights(top)
        rebalance_log.append(RebalanceSnapshot(date=t, holdings=weights))
        universe_tickers.update(t_ for t_, _ in weights)

    # --- Bulk-fetch price series for everything we held ------------------
    price_cache: dict[str, list[dict]] = {}
    for ticker in universe_tickers:
        try:
            price_cache[ticker] = prices_repo.get_price_series(
                ticker, start_date, end_date,
            )
        except Exception as e:  # noqa: BLE001 — degrade per-ticker
            logger.warning("Backtest: failed to fetch prices for %s: %s",
                            ticker, e)
            price_cache[ticker] = []

    # --- Compute period returns ------------------------------------------
    for i in range(len(rebalance_log) - 1):
        t = rebalance_log[i].date
        t_next = rebalance_log[i + 1].date
        rebalance_log[i].period_return = _period_return(
            rebalance_log[i].holdings, t, t_next, price_cache,
        )

    # --- Curves + metrics ------------------------------------------------
    equity_curve = _build_equity_curve(rebalance_log)
    benchmark_curve = _benchmark_curve(prices_repo, start_date, end_date,
                                         [s.date for s in rebalance_log])
    metrics = _compute_metrics(rebalance_log, benchmark_curve, rebalance_freq)

    return PresetBacktestResult(
        preset_id=preset_id,
        preset_label=preset["label"],
        start_date=start_date,
        end_date=end_date,
        rebalance_freq=rebalance_freq,
        rebalance_log=rebalance_log,
        equity_curve=equity_curve,
        benchmark_curve=benchmark_curve,
        metrics=metrics,
        preset_survivors_at_start=survivors_at_start,
    )


def next_backtest_portfolio_name(strategy_label: str, repo) -> str:
    """Pick the next free `<NORMALISED_LABEL>_BACKTEST_N` for the portfolios
    table. Strategy label is normalised to uppercase alphanumeric + `_`
    so the synthetic ticker prefix (`@<NAME>`) stays compatible with the
    name pattern in `analysis/portfolio_synth.py` (`^[A-Z0-9_]{1,32}$`).
    Doesn't fill gaps from deleted runs — lineage stays monotonic."""
    import re
    normalised = re.sub(r"[^A-Z0-9_]+", "_",
                         (strategy_label or "").upper()).strip("_")
    prefix = f"{normalised}_BACKTEST_"
    existing = repo.list_names_starting_with(prefix)
    nums = []
    for name in existing:
        suffix = name[len(prefix):]
        if suffix.isdigit():
            nums.append(int(suffix))
    return f"{prefix}{(max(nums) + 1) if nums else 1}"
