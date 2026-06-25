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
BENCHMARK_TICKER = "^HSI"     # HK default; US runs swap in '^GSPC' via benchmark_for_market
BENCHMARK_TICKER_US = "^GSPC"


def benchmark_for_market(market: str | None) -> str:
    """Default benchmark per market — used by the dashboard Backtest tab so
    a US run is naturally compared to the S&P 500, an HK run to the Hang
    Seng. The user can still override either via the Risk Forecast dropdown
    for ad-hoc comparisons."""
    return BENCHMARK_TICKER_US if (market or "HK").upper() == "US" else BENCHMARK_TICKER
# Cap any single position at this fraction of the portfolio. Cap-weighting
# the top 10 by raw market_cap was producing 40-55% concentrations on a
# single mega-cap; that's not a diversified portfolio, it's a 2-name bet.
# Configurable per-run via the dashboard slider.
DEFAULT_MAX_WEIGHT = 0.20
# Notional capital used to translate weights → share counts in the
# rebalance log. Separate from the equity curve's 100-indexed value so
# the chart stays unitless while the holdings/trade tables show
# human-readable share quantities. 1M HKD at start; share counts at
# subsequent rebalances scale with realised portfolio value.
NOTIONAL_CAPITAL_HKD = 1_000_000

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
class Holding:
    """One post-rebalance position. `shares` is the notional unit count
    derived from `weight × portfolio_value_at_date / price`, where the
    notional capital starts at 100 and compounds with realised returns."""
    ticker: str
    name: str
    price: float          # adj_close at the rebalance date
    weight: float         # 0..1, target portfolio weight
    shares: float         # notional unit count (rounded for display)
    sector: str = "—"     # sub_sector if available, else yf_sector / "—"


@dataclass
class RebalanceSnapshot:
    """One rebalance event. `holdings` is the post-rebalance portfolio,
    `period_return` is the realised return through the next rebalance
    (None for the final snapshot)."""
    date: str
    holdings: list[Holding] = field(default_factory=list)
    period_return: Optional[float] = None


@dataclass
class TradeRecord:
    """A single buy/sell to take the portfolio from one rebalance state to
    the next. Units = absolute delta in shares; price is the as-of close on
    the rebalance date."""
    date: str
    ticker: str
    name: str
    action: str           # "BUY" or "SELL"
    units: float          # |delta shares|, always positive
    price: float


@dataclass
class BacktestMetrics:
    total_return: float
    annualized_return: float
    annualized_vol: float
    sharpe: float                # at rf = 0.03 annual
    max_drawdown: float
    hit_rate: float              # % periods beating benchmark
    n_rebalances: int
    benchmark_total_return: float = 0.0
    excess_return: float = 0.0   # total_return − benchmark_total_return
    avg_turnover_per_rebalance: float = 0.0  # mean Σ|Δw|/2 per rebalance
    annualized_turnover: float = 0.0


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
    # Flattened buy/sell records across all rebalances after the first.
    # The initial holdings table on the UI shows rebalance_log[0]'s
    # post-rebalance positions; trade_log captures every change after that.
    trade_log: list[TradeRecord] = field(default_factory=list)
    # Daily-sampled curves for smoother charts + drawdown visual.
    daily_equity_curve: list[tuple[str, float]] = field(default_factory=list)
    daily_benchmark_curve: list[tuple[str, float]] = field(default_factory=list)
    drawdown_curve: list[tuple[str, float]] = field(default_factory=list)
    # Per-rebalance turnover (Σ|Δw|/2) and sector splits at endpoints.
    turnover_per_rebalance: list[float] = field(default_factory=list)
    sector_breakdown_initial: list[tuple[str, float]] = field(default_factory=list)
    sector_breakdown_final: list[tuple[str, float]] = field(default_factory=list)
    # Applied per-position weight cap and the auto-generated save name.
    weight_cap_used: float = DEFAULT_MAX_WEIGHT
    next_portfolio_name: str = ""
    # Window endpoints actually traded (snapped to ^HSI trading days).
    actual_start: str = ""
    actual_end: str = ""


# ============================================================================
# Helpers
# ============================================================================

def _hsi_trading_days(repo, start_date: str, end_date: str,
                       benchmark: str = BENCHMARK_TICKER) -> list[str]:
    """Trading days between [start_date, end_date] inclusive, derived from
    benchmark rows in historical_prices. Honest about market holidays
    without maintaining a calendar table. Default benchmark is ^HSI for
    backwards compatibility; pass `benchmark='^GSPC'` for US."""
    rows = repo.get_price_series(benchmark, start_date, end_date)
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
    to drop it.

    Also normalises `debt_to_equity` to percent units. akshare-annual
    stores D/E as a fraction (0.41 = 41%) while yfinance stores it as a
    percent (33.5 = 33.5%); the preset slider's threshold space (e.g.
    Buffett's [0, 50]) is calibrated for the percent form. Without this
    fix, every Buffett backtest against historical akshare rows would
    silently disable the D/E cap."""
    e = dict(snapshot_row)
    if price is None or price <= 0:
        return _normalise_de_units(e)

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
    return _normalise_de_units(e)


def _normalise_de_units(row: dict) -> dict:
    """Treat D/E < 5 as a fraction-form value and rescale to percent.
    Conservative threshold: a true fraction-form D/E sits in 0–2 for
    almost every real company; a percent-form value of 5 means 5% which
    is so low it's effectively no debt — either reading still passes
    Buffett's '<=50' cap. Mutates a copy of `row` (caller passed in a
    fresh dict already)."""
    raw = row.get("debt_to_equity")
    if raw is None:
        return row
    try:
        f = float(raw)
    except (TypeError, ValueError):
        return row
    if f < 5.0:
        row["debt_to_equity"] = f * 100.0
    return row


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


def _apply_weight_cap(weights: list[tuple[str, float]],
                        cap: float) -> list[tuple[str, float]]:
    """Iterative waterfall: cap any position above `cap`, redistribute the
    excess to uncapped names pro-rata by their current weight. Repeats
    until no position exceeds cap (or 20 iterations as a safety stop).
    No-op when `cap >= 1.0` or weights are already compliant.

    Worked example: cap=0.20, raw [(A, 0.50), (B, 0.30), (C, 0.20)]
      iter 1: A → 0.20, excess 0.30; B,C uncapped (sum 0.50)
              B += 0.30 × 0.30/0.50 = 0.18 → 0.48; C += 0.12 → 0.32
      iter 2: B → 0.20, excess 0.28; A,C — A capped, C uncapped (sum 0.32)
              C += 0.28 → 0.60 → capped → 0.20, excess 0.40
              all 3 capped at 0.20 → sums to 0.60, not 1.0
      Edge: when ALL positions cap out, we leave them at cap and accept
      the under-allocation (caller can detect Σweights < 1 and pad with
      cash, but here we just normalise back to 1.0)."""
    if cap >= 1.0 or not weights:
        return weights
    weights = [(t, w) for t, w in weights]
    for _ in range(20):
        max_w = max(w for _, w in weights)
        if max_w <= cap + 1e-9:
            break
        capped_idx = {i for i, (_, w) in enumerate(weights) if w > cap}
        excess = sum(weights[i][1] - cap for i in capped_idx)
        non_capped_idx = [i for i in range(len(weights)) if i not in capped_idx]
        non_capped_total = sum(weights[i][1] for i in non_capped_idx)
        if non_capped_total <= 0:
            break
        new_weights = []
        for i, (t, w) in enumerate(weights):
            if i in capped_idx:
                new_weights.append((t, cap))
            else:
                new_weights.append((t, w + excess * (w / non_capped_total)))
        weights = new_weights
    # If everything capped out and Σ < 1, renormalise so weights still
    # sum to 1.0 — equivalent to scaling all positions up proportionally
    # while keeping their relative magnitudes pinned to the cap.
    total = sum(w for _, w in weights)
    if total > 0 and abs(total - 1.0) > 1e-6:
        weights = [(t, w / total) for t, w in weights]
    return weights


def _cap_weight_holdings(top_holdings, name_map: dict,
                           price_map: dict, sector_map: dict,
                           weight_cap: float = DEFAULT_MAX_WEIGHT,
                           ) -> list[Holding]:
    """Build cap-weighted Holding records for the top-N selections.
    Each Holding carries the as-of price + display name + sector; shares
    are 0.0 here and back-filled later once portfolio value is known.

    Pipeline:
      1. Cap-weight by raw market_cap (or equal-weight if cap data
         entirely missing).
      2. Fill missing-cap survivors with equal-weight slots so the top-N
         intent is preserved (no silent truncation).
      3. Apply per-position weight cap via `_apply_weight_cap`.
    """
    if not top_holdings:
        return []

    caps = [(r.ticker, r.market_cap)
            for r in top_holdings if (r.market_cap or 0) > 0]
    total = sum(c for _, c in caps)
    missing = [r.ticker for r in top_holdings
                if not (r.market_cap and r.market_cap > 0)]

    weighted: list[tuple[str, float]] = []
    if total > 0 and not missing:
        weighted = [(t, c / total) for t, c in caps]
    elif total > 0 and missing:
        n_total = len(top_holdings)
        avg_w = 1.0 / n_total
        bearing_frac = len(caps) / n_total
        weighted.extend((t, (c / total) * bearing_frac) for t, c in caps)
        weighted.extend((t, avg_w) for t in missing)
    else:
        eq = 1.0 / len(top_holdings)
        weighted = [(r.ticker, eq) for r in top_holdings]

    # Concentration cap before materialising Holdings.
    weighted = _apply_weight_cap(weighted, weight_cap)

    out = []
    for ticker, weight in weighted:
        price = price_map.get(ticker) or 0.0
        out.append(Holding(
            ticker=ticker,
            name=name_map.get(ticker, ticker),
            price=price,
            weight=weight,
            shares=0.0,
            sector=sector_map.get(ticker, "—"),
        ))
    return out


def _period_return(holdings: list[Holding],
                    t_start: str, t_end: str,
                    price_cache: dict) -> float:
    """Sum(weight × (P_end/P_start − 1)) across the holdings. Holdings
    missing a price at either endpoint are silently dropped from the
    weighted sum (treat as cash; their weight contributes 0 return)."""
    total = 0.0
    for h in holdings:
        prices = price_cache.get(h.ticker)
        if not prices:
            continue
        p_start = _price_on_or_before(prices, t_start)
        p_end = _price_on_or_before(prices, t_end)
        if p_start is None or p_end is None or p_start <= 0:
            continue
        total += h.weight * (p_end / p_start - 1.0)
    return total


def _fill_shares_and_trades(rebalance_log: list[RebalanceSnapshot],
                              equity_curve: list[tuple[str, float]],
                              price_cache: dict,
                              ) -> list[TradeRecord]:
    """Walk the rebalance log, back-fill the shares field on each Holding,
    and emit one TradeRecord per BUY or SELL relative to the previous
    rebalance. Notional capital starts at equity_curve[0] (100); shares at
    rebalance i are derived from `weight × equity_value(i) / price`.

    A ticker that joins the top-10 (or appears for the first time) is a
    full BUY; a ticker that drops out is a full SELL; a ticker that stays
    but with a different target weight gets the delta in either direction.

    Trades happen at the new rebalance's as-of price — for dropped tickers
    (no longer in `snap.holdings`) we look up the price at `snap.date` in
    the supplied `price_cache` rather than reusing the stale price from
    the previous rebalance (which was off by ~4-5% on real backtests).
    """
    trades: list[TradeRecord] = []
    if not rebalance_log:
        return trades

    # equity_curve is indexed from 100; scale to the notional HKD so the
    # holdings/trade tables show readable share quantities.
    equity_by_date = {d: v * (NOTIONAL_CAPITAL_HKD / 100.0)
                       for d, v in equity_curve}
    prev_shares: dict[str, float] = {}
    prev_holding: dict[str, Holding] = {}

    for snap in rebalance_log:
        pv = equity_by_date.get(snap.date, float(NOTIONAL_CAPITAL_HKD))
        new_shares: dict[str, float] = {}
        for h in snap.holdings:
            if h.price and h.price > 0:
                h.shares = round((h.weight * pv) / h.price, 2)
            else:
                h.shares = 0.0
            new_shares[h.ticker] = h.shares

        # Trades for everything except the very first rebalance (whose
        # holdings ARE the initial portfolio, shown in its own card).
        if prev_shares:
            all_tickers = set(prev_shares) | set(new_shares)
            for ticker in sorted(all_tickers):
                old = prev_shares.get(ticker, 0.0)
                new = new_shares.get(ticker, 0.0)
                delta = round(new - old, 2)
                if abs(delta) < 0.01:
                    continue
                if ticker in new_shares:
                    # Kept-or-added: use the new snapshot's price (already
                    # the as-of price at snap.date).
                    h_meta = next(h for h in snap.holdings if h.ticker == ticker)
                    name = h_meta.name
                    price_at_sell = h_meta.price
                else:
                    # Dropped: look up the actual as-of price on the SELL
                    # date, not the stale price from the previous rebalance.
                    prev = prev_holding[ticker]
                    name = prev.name
                    series = price_cache.get(ticker) or []
                    fresh = _price_on_or_before(series, snap.date)
                    price_at_sell = fresh if fresh is not None else prev.price
                trades.append(TradeRecord(
                    date=snap.date,
                    ticker=ticker,
                    name=name,
                    action="BUY" if delta > 0 else "SELL",
                    units=abs(delta),
                    price=price_at_sell,
                ))

        prev_shares = new_shares
        prev_holding = {h.ticker: h for h in snap.holdings}

    return trades


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


def _index_price_cache(price_cache: dict) -> dict:
    """Flatten {ticker: [{date, adj_close}, ...]} → {ticker: {date: price}}.
    Used by the daily-equity-curve builder so per-day lookups are O(1)
    instead of a linear scan per (date, ticker) pair (the prior O(n*m)
    walk made a 5y daily curve impractically slow)."""
    out = {}
    for ticker, series in price_cache.items():
        out[ticker] = {r["date"]: r.get("adj_close") for r in series
                        if r.get("adj_close") is not None}
    return out


def _build_daily_equity_curve(rebalance_log: list[RebalanceSnapshot],
                                price_cache: dict,
                                trading_days: list[str],
                                ) -> list[tuple[str, float]]:
    """Daily-sampled equity curve. For each trading day in [t_i, t_{i+1}),
    portfolio value = pv_at(t_i) × Σ weight_j × (P_j(d) / P_j(t_i)) — the
    static-portfolio mark-to-market between rebalances. At each rebalance
    we 'lock in' the new portfolio value and move on. Trading days outside
    the rebalance window are skipped.

    Smoother visual than the rebalance-only sample (13 vs 250 points for
    1y monthly) and exposes intra-period drawdowns. Cost: ~O(n_days *
    n_holdings) price-index lookups, all O(1) after _index_price_cache."""
    if not rebalance_log:
        return []
    price_idx = _index_price_cache(price_cache)

    daily: list[tuple[str, float]] = []
    pv = 100.0
    # Iterate (snap, next_snap) pairs; each yields a held-period [snap, next_snap]
    for i, snap in enumerate(rebalance_log[:-1]):
        next_snap = rebalance_log[i + 1]
        period_days = [d for d in trading_days
                       if snap.date <= d < next_snap.date]
        # Pre-resolve start prices once per holding (constant in this period).
        start_prices = {}
        for h in snap.holdings:
            series = price_idx.get(h.ticker, {})
            # First date in period with a price for this ticker
            p_start = None
            for d in period_days:
                if d in series:
                    p_start = series[d]; break
            if p_start is None or p_start <= 0:
                # Fall back to the most recent price at/before snap.date.
                # _price_on_or_before is fine here since this is one call,
                # not in the inner loop.
                p_start = _price_on_or_before(price_cache.get(h.ticker, []),
                                                snap.date)
            start_prices[h.ticker] = p_start

        for d in period_days:
            value_fraction = 0.0
            for h in snap.holdings:
                p_start = start_prices.get(h.ticker)
                p_now = price_idx.get(h.ticker, {}).get(d)
                if p_start and p_now and p_start > 0:
                    value_fraction += h.weight * (p_now / p_start)
                else:
                    # Treat as cash (no return contribution)
                    value_fraction += h.weight * 1.0
            daily.append((d, pv * value_fraction))
        # Lock in: end-of-period pv = pv × (1 + period_return)
        pv *= (1.0 + (snap.period_return or 0.0))

    # Final point at last rebalance date
    daily.append((rebalance_log[-1].date, pv))
    return daily


def _drawdown_series(daily_equity: list[tuple[str, float]]
                      ) -> list[tuple[str, float]]:
    """Running drawdown from peak — negative pct values, 0 at fresh peak."""
    if not daily_equity:
        return []
    peak = float("-inf")
    out = []
    for d, v in daily_equity:
        if v > peak:
            peak = v
        dd = (v / peak - 1.0) if peak > 0 else 0.0
        out.append((d, dd))
    return out


def _turnover_per_rebalance(rebalance_log: list[RebalanceSnapshot]
                              ) -> list[float]:
    """Per-rebalance turnover as Σ|Δweight|/2. Sum-halved because each
    trade affects two positions (one buyer side, one seller side); the
    convention matches industry fund-turnover reports."""
    out = []
    for i in range(1, len(rebalance_log)):
        prev = {h.ticker: h.weight for h in rebalance_log[i - 1].holdings}
        cur = {h.ticker: h.weight for h in rebalance_log[i].holdings}
        tickers = set(prev) | set(cur)
        turn = sum(abs(cur.get(t, 0.0) - prev.get(t, 0.0))
                   for t in tickers) / 2.0
        out.append(turn)
    return out


def _sector_breakdown(holdings: list[Holding]) -> list[tuple[str, float]]:
    """Aggregate portfolio weight by sector. Returns sorted (desc by weight)
    [(sector, weight)] list — fed to the donut chart on the dashboard."""
    if not holdings:
        return []
    by_sector: dict[str, float] = {}
    for h in holdings:
        key = h.sector or "—"
        by_sector[key] = by_sector.get(key, 0.0) + h.weight
    return sorted(by_sector.items(), key=lambda kv: -kv[1])


def _build_daily_benchmark_curve(repo, start_date: str, end_date: str,
                                    base_date: str,
                                    benchmark: str = BENCHMARK_TICKER) -> list[tuple[str, float]]:
    """Daily benchmark normalised to 100 at the strategy's first rebalance
    date (`base_date`), so the two daily curves overlay cleanly on the chart."""
    rows = repo.get_price_series(benchmark, start_date, end_date)
    if not rows:
        return []
    by_date = {r["date"]: r.get("adj_close") for r in rows
               if r.get("adj_close") is not None}
    cands = [d for d in by_date if d <= base_date]
    if not cands:
        return []
    base = by_date[max(cands)]
    if not base or base <= 0:
        return []
    out = []
    for r in rows:
        if r.get("adj_close") is None or r["date"] < base_date:
            continue
        out.append((r["date"], (r["adj_close"] / base) * 100.0))
    return out


def _build_equity_curve(rebalance_log: list[RebalanceSnapshot]) -> list[tuple[str, float]]:
    """Compounding `period_return`s starting from 100. Indexed by enumerate
    rather than `list.index(snap)` — the latter was O(n^2) and compared
    full dataclass instances, both of which compounded badly on 5y daily
    backtests."""
    if not rebalance_log:
        return []
    curve = [(rebalance_log[0].date, 100.0)]
    cumulative = 1.0
    for i, snap in enumerate(rebalance_log[:-1]):
        cumulative *= (1.0 + (snap.period_return or 0.0))
        next_date = rebalance_log[i + 1].date
        curve.append((next_date, cumulative * 100.0))
    return curve


def _benchmark_curve(repo, start_date: str, end_date: str,
                      rebalance_dates: list[str],
                      benchmark: str = BENCHMARK_TICKER) -> list[tuple[str, float]]:
    """Benchmark normalized to 100 at start, sampled at the same dates as
    the equity curve so they overlay cleanly on the chart."""
    rows = repo.get_price_series(benchmark, start_date, end_date)
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
                          weight_cap: float = DEFAULT_MAX_WEIGHT,
                          market: str = "HK",
                          ) -> PresetBacktestResult:
    """Run a preset + V/Q/G top-10 walk-forward backtest. See module
    docstring for the algorithm. `market` scopes the universe (HK / US)
    and flips the benchmark + trading-day calendar to the market default."""
    from analysis.factor_scores import FactorScoringEngine
    from analysis.preset_filter import apply_preset
    from dashboard.screener_layout import NUMERIC_FILTERS
    from dashboard.screener_presets import INVESTOR_PRESETS
    from storage.database import Database
    from storage.factory import get_prices_repo

    preset = next((p for p in INVESTOR_PRESETS if p["id"] == preset_id), None)
    if preset is None:
        raise ValueError(f"Unknown preset id: {preset_id}")

    market = (market or "HK").upper()
    benchmark = benchmark_for_market(market)

    db = Database(db_path)
    prices_repo = get_prices_repo(db)

    # --- Build rebalance calendar ---------------------------------------
    trading_days = _hsi_trading_days(prices_repo, start_date, end_date,
                                       benchmark=benchmark)
    if not trading_days:
        raise RuntimeError(
            f"No {benchmark} trading days between {start_date} and {end_date}. "
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

    # Global ticker → display name + sector lookups, accumulated across
    # the snapshots the engine scores. Used by Holding and TradeRecord so
    # the UI can show human-readable names + sector breakdowns without
    # re-querying securities for each rebalance.
    name_map: dict[str, str] = {}
    sector_map: dict[str, str] = {}
    # Per-snapshot enriched rows keyed by ticker. We hand these to the
    # factor engine (pre_loaded_rows hatch) AND retain them for the
    # preset filter so it can see fields the FactorResult doesn't carry
    # (D/E, EV/EBITDA, profit_margins, forward_pe, beta) — those used
    # to be hardcoded None in the synthetic as_rows and silently passed
    # every preset, defeating Buffett's D/E cap entirely.
    enriched_by_snap: dict[str, dict[str, dict]] = {}

    for snap_date in needed:
        raw_rows = get_universe_fundamentals(db, as_of_date=snap_date)
        for r in raw_rows:
            t_ = r.get("ticker")
            if not t_:
                continue
            if t_ not in name_map and r.get("name"):
                name_map[t_] = r["name"]
            if t_ not in sector_map:
                sector_map[t_] = (r.get("sub_sector")
                                   or r.get("effective_sector")
                                   or r.get("yf_sector") or "—")
        as_of_t = snap_to_first_rebal.get(snap_date, snap_date)
        price_map = _bulk_as_of_prices(prices_repo,
                                         [r["ticker"] for r in raw_rows],
                                         as_of_t)
        enriched = [_enrich_as_of(r, price_map.get(r["ticker"]))
                     for r in raw_rows]
        enriched_by_snap[snap_date] = {r["ticker"]: r for r in enriched}
        results, _diag = engine.compute(as_of_date=snap_date,
                                          pre_loaded_rows=enriched,
                                          market=market)
        score_cache[snap_date] = results

    # --- Per rebalance: filter → top-10 → cap-weight → next-period return -
    rebalance_log: list[RebalanceSnapshot] = []
    # Universe ticker set across the whole backtest (for the price cache).
    universe_tickers: set[str] = set()
    survivors_at_start: list[str] = []
    # Per-rebalance as-of price map so each Holding gets the price actually
    # observed at its rebalance date (not the snapshot's first-rebalance
    # price, which only drives factor enrichment).
    per_rebal_price_map: dict[str, dict] = {}

    for i, t in enumerate(rebal_dates):
        snap = effective_per_rebal[i]
        if snap is None:
            rebalance_log.append(RebalanceSnapshot(date=t, holdings=[]))
            continue
        scored = score_cache[snap]
        snap_rows = enriched_by_snap.get(snap, {})

        # Filter survivors by preset — feed the enriched snapshot row
        # directly so D/E, EV/EBITDA, forward_pe, beta etc. are visible
        # to the filter (the FactorResult shape only covers what's needed
        # for ranking, not for full preset criteria). When a scored
        # ticker has no enriched row (universe row dropped at load time)
        # we synthesise from the FactorResult so it can still be filtered
        # on the fields it does carry.
        rows_for_filter = []
        for r in scored:
            if r.disqualified:
                continue
            snap_row = snap_rows.get(r.ticker)
            if snap_row is not None:
                rows_for_filter.append(snap_row)
            else:
                rows_for_filter.append({
                    "ticker": r.ticker,
                    "trailing_pe": r.trailing_pe,
                    "price_to_book": r.price_to_book,
                    "dividend_yield": r.dividend_yield,
                    "return_on_equity": r.return_on_equity,
                    "earnings_growth": r.earnings_growth,
                    "market_cap": r.market_cap,
                })
        survivors_rows = apply_preset(rows_for_filter, preset, NUMERIC_FILTERS)
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
        # Per-rebalance prices for the top selections only — bulk SQL with a
        # ~10-element ticker list is cheap.
        rebal_prices = _bulk_as_of_prices(
            prices_repo, [r.ticker for r in top], t,
        )
        per_rebal_price_map[t] = rebal_prices
        holdings = _cap_weight_holdings(top, name_map, rebal_prices,
                                          sector_map, weight_cap)
        rebalance_log.append(RebalanceSnapshot(date=t, holdings=holdings))
        universe_tickers.update(h.ticker for h in holdings)

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
                                         [s.date for s in rebalance_log],
                                         benchmark=benchmark)
    metrics = _compute_metrics(rebalance_log, benchmark_curve, rebalance_freq)

    # --- Shares + trade log (needs equity_curve to know notional value) --
    # Pass price_cache so dropped-ticker SELL trades use the as-of price
    # at the SELL date instead of a stale price from the previous rebalance.
    trade_log = _fill_shares_and_trades(rebalance_log, equity_curve,
                                          price_cache)

    # --- Daily curves + drawdown + turnover + sector breakdowns ---------
    daily_equity = _build_daily_equity_curve(rebalance_log, price_cache,
                                               trading_days)
    daily_benchmark = _build_daily_benchmark_curve(
        prices_repo, start_date, end_date,
        rebalance_log[0].date if rebalance_log else start_date,
        benchmark=benchmark,
    )
    drawdown = _drawdown_series(daily_equity)
    turnover = _turnover_per_rebalance(rebalance_log)
    sec_initial = _sector_breakdown(rebalance_log[0].holdings) if rebalance_log else []
    sec_final = _sector_breakdown(rebalance_log[-1].holdings) if rebalance_log else []

    # Extend metrics with benchmark/turnover-derived numbers.
    bench_total = (benchmark_curve[-1][1] / 100.0 - 1.0
                    if benchmark_curve else 0.0)
    metrics.benchmark_total_return = bench_total
    metrics.excess_return = metrics.total_return - bench_total
    if turnover:
        metrics.avg_turnover_per_rebalance = sum(turnover) / len(turnover)
        ppy = PERIODS_PER_YEAR.get(rebalance_freq, 12)
        metrics.annualized_turnover = metrics.avg_turnover_per_rebalance * ppy

    # --- Save-name preview (uses portfolio repo if cloud is enabled) ----
    next_name = ""
    try:
        from storage.cloud_repository import CloudPortfoliosRepository
        repo = CloudPortfoliosRepository()
        next_name = next_backtest_portfolio_name(preset["label"], repo)
    except Exception:
        # Cloud unavailable or table missing — leave preview blank, the
        # save callback will retry and surface any real error there.
        next_name = ""

    actual_start = rebalance_log[0].date if rebalance_log else start_date
    actual_end = rebalance_log[-1].date if rebalance_log else end_date

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
        trade_log=trade_log,
        daily_equity_curve=daily_equity,
        daily_benchmark_curve=daily_benchmark,
        drawdown_curve=drawdown,
        turnover_per_rebalance=turnover,
        sector_breakdown_initial=sec_initial,
        sector_breakdown_final=sec_final,
        weight_cap_used=weight_cap,
        next_portfolio_name=next_name,
        actual_start=actual_start,
        actual_end=actual_end,
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


# ============================================================================
# Factor-verification long/short backtest
# ----------------------------------------------------------------------------
# Verifies that the V/Q/G composite percentile is a real signal: at each
# rebalance, build dollar-neutral long-top-decile / short-bottom-decile
# baskets (sub-sector-neutral, equal-weight within each leg), then track
# the spread. A real factor should produce a positive cumulative spread
# with positive Sharpe + Information Coefficient + decile monotonicity.
#
# Distinct from `run_preset_backtest` in three ways:
#   - No preset filter — the test must isolate the V/Q/G signal alone
#   - Long AND short legs (signed weights), not just long-only top-10
#   - Equal-weight within each leg (mcap-weighting would conflate with the
#     size factor and contaminate the test)
# ============================================================================

DEFAULT_DECILE_FRACTION = 0.10
DEFAULT_MIN_NAMES_PER_SUBSECTOR = 10
N_DECILES = 10


@dataclass
class FactorVerificationResult:
    """Output bundle for a long/short factor-verification backtest.

    All curves start at 100. `spread_curve[i]` is the cumulative value of
    a $1-long / $1-short market-neutral basket (200% gross exposure)."""
    market: str
    start_date: str
    end_date: str
    rebalance_freq: str
    # Three normalised equity curves (date, value)
    long_curve: list[tuple[str, float]] = field(default_factory=list)
    short_curve: list[tuple[str, float]] = field(default_factory=list)
    spread_curve: list[tuple[str, float]] = field(default_factory=list)
    # Mean forward return per decile bucket (1 = bottom, 10 = top), pooled
    # across all rebalance periods. The signature monotonicity chart.
    decile_returns: dict[int, float] = field(default_factory=dict)
    decile_counts: dict[int, int] = field(default_factory=dict)
    # Per-rebalance Information Coefficient (Spearman ρ between composite
    # pctile and forward return). NaNs / Nones filtered out before stats.
    ic_series: list[tuple[str, float]] = field(default_factory=list)
    # Per-rebalance long + short composition (latest snapshot is what the
    # dashboard shows in its holdings tables).
    rebalance_log: list[dict] = field(default_factory=list)
    # Headline metrics — all decimal (0.10 = 10%).
    metrics: dict = field(default_factory=dict)
    # Diagnostics
    n_subsectors_used: int = 0
    avg_long_size: float = 0.0
    avg_short_size: float = 0.0
    actual_start: str = ""
    actual_end: str = ""


def _signed_period_return(holdings_signed: list[tuple[str, float]],
                            t_start: str, t_end: str,
                            price_cache: dict,
                            ) -> tuple[float, float]:
    """Per-period long-leg + short-leg returns given a list of
    `(ticker, signed_weight)` pairs. Returns `(r_long, r_short)`:

      r_long  = Σ   w_i  × (P_end/P_start - 1)  for positive-weight names
      r_short = Σ  |w_j| × (P_end/P_start - 1)  for negative-weight names

    Note `r_short` is the return of being LONG the short basket — the L/S
    spread is `r_long − r_short`. Tickers missing a price at either
    endpoint are skipped (treated as cash, zero contribution).
    """
    r_long = 0.0
    r_short = 0.0
    for ticker, w in holdings_signed:
        series = price_cache.get(ticker)
        if not series:
            continue
        p0 = _price_on_or_before(series, t_start)
        p1 = _price_on_or_before(series, t_end)
        if p0 is None or p1 is None or p0 <= 0:
            continue
        ret = p1 / p0 - 1.0
        if w >= 0:
            r_long += w * ret
        else:
            r_short += abs(w) * ret
    return r_long, r_short


def _decile_buckets_per_subsector(results, n_buckets: int = N_DECILES,
                                     min_names: int = DEFAULT_MIN_NAMES_PER_SUBSECTOR
                                     ) -> dict[str, list[list]]:
    """Group scored results by sub_sector (carried on FactorResult.sector
    after the engine's strict bucketing — see factor_scores.py:308) and
    split each sub-sector into N equal-sized buckets sorted by composite
    percentile ASCENDING (so bucket 0 = lowest composite, bucket N-1 =
    highest composite).

    Sub-sectors with fewer than `min_names` ranked tickers are skipped
    entirely — a decile split on 3 tickers is meaningless. Returns
    `{sub_sector: [bucket_0_results, ..., bucket_{N-1}_results]}`."""
    by_sub: dict[str, list] = {}
    for r in results:
        if r.disqualified or r.composite_pctile is None:
            continue
        sub = (r.sector or "—")
        if sub == "—":
            continue
        by_sub.setdefault(sub, []).append(r)

    out: dict[str, list[list]] = {}
    for sub, rs in by_sub.items():
        if len(rs) < min_names:
            continue
        rs_sorted = sorted(rs, key=lambda r: r.composite_pctile)
        n = len(rs_sorted)
        buckets: list[list] = [[] for _ in range(n_buckets)]
        # Equal-sized buckets by index. Last bucket absorbs the remainder
        # when n is not divisible by n_buckets (the convention for
        # decile-based factor analysis).
        for i, r in enumerate(rs_sorted):
            b = min(int(i * n_buckets / n), n_buckets - 1)
            buckets[b].append(r)
        out[sub] = buckets
    return out


def _spearman_safe(x: list[float], y: list[float]) -> Optional[float]:
    """Spearman rank correlation, returning None if either series is too
    short, fully constant, or NaN-bearing. Wraps scipy with the right
    nan_policy."""
    if not x or len(x) != len(y) or len(x) < 5:
        return None
    try:
        from scipy.stats import spearmanr
        rho, _p = spearmanr(x, y, nan_policy="omit")
        if rho is None or math.isnan(rho):
            return None
        return float(rho)
    except Exception:
        return None


def run_factor_verification_backtest(
    start_date: str,
    end_date: str,
    rebalance_freq: str,
    db_path: str,
    sector_risk_path: Optional[str] = None,
    market: str = "HK",
    min_names_per_subsector: int = DEFAULT_MIN_NAMES_PER_SUBSECTOR,
) -> FactorVerificationResult:
    """V/Q/G factor-verification long/short backtest.

    At each rebalance, score the universe (no preset filter) and within
    each sub-sector pick the top decile (LONG) + bottom decile (SHORT)
    by composite V/Q/G percentile. Pool across sub-sectors, equal-weight
    within each leg, hold until the next rebalance.

    Outputs three normalised equity curves (long / short / spread), per-
    decile mean forward returns (the monotonicity test), per-rebalance
    Information Coefficient, and headline annualised metrics.
    """
    from analysis.factor_scores import FactorScoringEngine
    from analysis.data_loader import get_universe_fundamentals
    from storage.database import Database
    from storage.factory import get_prices_repo

    market = (market or "HK").upper()
    benchmark = benchmark_for_market(market)

    db = Database(db_path)
    prices_repo = get_prices_repo(db)

    # --- Calendar + memoised snapshot list (same pattern as preset BT) ---
    trading_days = _hsi_trading_days(prices_repo, start_date, end_date,
                                       benchmark=benchmark)
    if not trading_days:
        raise RuntimeError(
            f"No {benchmark} trading days between {start_date} and "
            f"{end_date}. Run a price refresh first."
        )
    rebal_dates = _rebalance_dates(trading_days, rebalance_freq)
    snapshot_dates = _list_snapshot_dates(db_path)
    effective_per_rebal = [_effective_snapshot_date(t, snapshot_dates)
                            for t in rebal_dates]
    needed = sorted({s for s in effective_per_rebal if s is not None})
    logger.info("FactorVerify: %d rebalances span %d unique snapshots — "
                 "scoring %d times", len(rebal_dates), len(needed), len(needed))

    # --- Score each needed snapshot once ---------------------------------
    engine = FactorScoringEngine(db_path, sector_risk_path)
    score_cache: dict[str, list] = {}
    snap_to_first_rebal: dict[str, str] = {}
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
                                          pre_loaded_rows=enriched,
                                          market=market)
        score_cache[snap_date] = results

    # --- Per rebalance: build long / short / decile buckets --------------
    # `verify_log[i]` keeps the per-rebalance decision so we can render the
    # latest holdings + compute IC + decile returns after price lookups.
    verify_log: list[dict] = []
    universe_tickers: set[str] = set()
    subsectors_seen: set[str] = set()
    long_sizes: list[int] = []
    short_sizes: list[int] = []

    for i, t in enumerate(rebal_dates):
        snap = effective_per_rebal[i]
        if snap is None:
            verify_log.append({"date": t, "long": [], "short": [],
                                  "deciles": {}})
            continue
        scored = score_cache[snap]
        buckets_by_sub = _decile_buckets_per_subsector(
            scored, n_buckets=N_DECILES,
            min_names=min_names_per_subsector,
        )
        subsectors_seen.update(buckets_by_sub.keys())

        # Pool top + bottom decile across all qualifying sub-sectors.
        longs: list = []
        shorts: list = []
        deciles_pool: dict[int, list] = {b: [] for b in range(N_DECILES)}
        for sub, buckets in buckets_by_sub.items():
            shorts.extend(buckets[0])               # bucket 0 = lowest composite
            longs.extend(buckets[N_DECILES - 1])    # bucket N-1 = highest
            for b, rs in enumerate(buckets):
                deciles_pool[b].extend(rs)

        long_sizes.append(len(longs))
        short_sizes.append(len(shorts))
        universe_tickers.update(r.ticker for r in longs)
        universe_tickers.update(r.ticker for r in shorts)
        for rs in deciles_pool.values():
            universe_tickers.update(r.ticker for r in rs)

        # Signed equal-weight: |Σw_long| = 1.0, |Σw_short| = 1.0
        w_long = (1.0 / len(longs)) if longs else 0.0
        w_short = (-1.0 / len(shorts)) if shorts else 0.0
        holdings_signed = ([(r.ticker, w_long) for r in longs] +
                            [(r.ticker, w_short) for r in shorts])

        verify_log.append({
            "date": t,
            "long": longs,
            "short": shorts,
            "deciles": deciles_pool,
            "holdings_signed": holdings_signed,
        })

    # --- Bulk-fetch price series ----------------------------------------
    price_cache: dict[str, list[dict]] = {}
    for ticker in universe_tickers:
        try:
            price_cache[ticker] = prices_repo.get_price_series(
                ticker, start_date, end_date,
            )
        except Exception as e:
            logger.warning("FactorVerify: price fetch failed for %s: %s",
                            ticker, e)
            price_cache[ticker] = []

    # --- Per-period returns: long, short, spread, deciles, IC -----------
    long_curve: list[tuple[str, float]] = []
    short_curve: list[tuple[str, float]] = []
    spread_curve: list[tuple[str, float]] = []
    period_long_rets: list[float] = []
    period_short_rets: list[float] = []
    period_spread_rets: list[float] = []
    decile_period_rets: dict[int, list[float]] = {b: [] for b in range(N_DECILES)}
    ic_series: list[tuple[str, float]] = []

    long_val = 100.0
    short_val = 100.0
    spread_val = 100.0
    # Anchor curves at the first rebalance date.
    if verify_log:
        long_curve.append((verify_log[0]["date"], long_val))
        short_curve.append((verify_log[0]["date"], short_val))
        spread_curve.append((verify_log[0]["date"], spread_val))

    for i in range(len(verify_log) - 1):
        snap = verify_log[i]
        t = snap["date"]
        t_next = verify_log[i + 1]["date"]
        if not snap.get("holdings_signed"):
            long_curve.append((t_next, long_val))
            short_curve.append((t_next, short_val))
            spread_curve.append((t_next, spread_val))
            continue

        r_long, r_short = _signed_period_return(
            snap["holdings_signed"], t, t_next, price_cache,
        )
        r_spread = r_long - r_short
        period_long_rets.append(r_long)
        period_short_rets.append(r_short)
        period_spread_rets.append(r_spread)

        # Decile bucket returns (equal-weight within each decile).
        for b, rs in snap.get("deciles", {}).items():
            if not rs:
                continue
            w = 1.0 / len(rs)
            r_b = 0.0
            n_priced = 0
            for r in rs:
                series = price_cache.get(r.ticker)
                if not series:
                    continue
                p0 = _price_on_or_before(series, t)
                p1 = _price_on_or_before(series, t_next)
                if p0 is None or p1 is None or p0 <= 0:
                    continue
                r_b += w * (p1 / p0 - 1.0)
                n_priced += 1
            if n_priced > 0:
                decile_period_rets[b].append(r_b)

        # Information Coefficient — Spearman ρ of composite_pctile vs
        # this-period forward return, across every ranked + priced ticker
        # (pooled across all sub-sectors).
        pcts, fwds = [], []
        for sub_buckets in snap.get("deciles", {}).values():
            for r in sub_buckets:
                if r.composite_pctile is None:
                    continue
                series = price_cache.get(r.ticker)
                if not series:
                    continue
                p0 = _price_on_or_before(series, t)
                p1 = _price_on_or_before(series, t_next)
                if p0 is None or p1 is None or p0 <= 0:
                    continue
                pcts.append(float(r.composite_pctile))
                fwds.append(p1 / p0 - 1.0)
        ic = _spearman_safe(pcts, fwds)
        if ic is not None:
            ic_series.append((t_next, ic))

        long_val *= (1.0 + r_long)
        short_val *= (1.0 + r_short)
        spread_val *= (1.0 + r_spread)
        long_curve.append((t_next, long_val))
        short_curve.append((t_next, short_val))
        spread_curve.append((t_next, spread_val))

    # --- Headline metrics -----------------------------------------------
    ppy = PERIODS_PER_YEAR.get(rebalance_freq, 12)
    n_periods = len(period_spread_rets)

    def _ann_ret(curve: list[tuple[str, float]]) -> float:
        if len(curve) < 2 or curve[0][1] <= 0:
            return 0.0
        total = curve[-1][1] / curve[0][1] - 1.0
        years = n_periods / ppy if ppy else 0
        if years <= 0:
            return total
        return (1.0 + total) ** (1.0 / years) - 1.0

    def _ann_vol(rets: list[float]) -> float:
        if len(rets) < 2:
            return 0.0
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return math.sqrt(var * ppy)

    def _max_dd(curve: list[tuple[str, float]]) -> float:
        if not curve:
            return 0.0
        peak = curve[0][1]
        worst = 0.0
        for _, v in curve:
            if v > peak:
                peak = v
            dd = (v - peak) / peak if peak > 0 else 0.0
            if dd < worst:
                worst = dd
        return worst

    ann_long = _ann_ret(long_curve)
    ann_short = _ann_ret(short_curve)
    ann_spread = _ann_ret(spread_curve)
    vol_spread = _ann_vol(period_spread_rets)
    sharpe_spread = ((ann_spread - RISK_FREE_RATE_ANNUAL) / vol_spread
                       if vol_spread > 0 else 0.0)
    dd_spread = _max_dd(spread_curve)
    hit_rate = (sum(1 for r in period_spread_rets if r > 0) / n_periods
                  if n_periods else 0.0)
    ic_vals = [v for _, v in ic_series]
    mean_ic = sum(ic_vals) / len(ic_vals) if ic_vals else 0.0
    if len(ic_vals) > 1:
        ic_mean = mean_ic
        ic_var = sum((v - ic_mean) ** 2 for v in ic_vals) / (len(ic_vals) - 1)
        ic_std = math.sqrt(ic_var)
        ic_tstat = (ic_mean / (ic_std / math.sqrt(len(ic_vals)))
                      if ic_std > 0 else 0.0)
    else:
        ic_tstat = 0.0

    decile_means: dict[int, float] = {}
    decile_counts: dict[int, int] = {}
    for b, rs in decile_period_rets.items():
        decile_counts[b + 1] = len(rs)        # 1-indexed for display
        decile_means[b + 1] = sum(rs) / len(rs) if rs else 0.0

    metrics = {
        "ann_return_long":   ann_long,
        "ann_return_short":  ann_short,
        "ann_return_spread": ann_spread,
        "ann_vol_spread":    vol_spread,
        "spread_sharpe":     sharpe_spread,
        "spread_max_dd":     dd_spread,
        "hit_rate":          hit_rate,
        "mean_ic":           mean_ic,
        "ic_tstat":          ic_tstat,
        "n_periods":         n_periods,
        "n_ic_observations": len(ic_vals),
    }

    actual_start = verify_log[0]["date"] if verify_log else start_date
    actual_end = verify_log[-1]["date"] if verify_log else end_date

    return FactorVerificationResult(
        market=market,
        start_date=start_date,
        end_date=end_date,
        rebalance_freq=rebalance_freq,
        long_curve=long_curve,
        short_curve=short_curve,
        spread_curve=spread_curve,
        decile_returns=decile_means,
        decile_counts=decile_counts,
        ic_series=ic_series,
        rebalance_log=verify_log,
        metrics=metrics,
        n_subsectors_used=len(subsectors_seen),
        avg_long_size=(sum(long_sizes) / len(long_sizes)
                         if long_sizes else 0.0),
        avg_short_size=(sum(short_sizes) / len(short_sizes)
                          if short_sizes else 0.0),
        actual_start=actual_start,
        actual_end=actual_end,
    )
