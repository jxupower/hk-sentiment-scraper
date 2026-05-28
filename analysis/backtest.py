"""Walk-forward backtest engine for fundamental screens.

For a given screen + parameter set + date range:
  1. Generate rebalance dates at the chosen frequency.
  2. At each rebalance date, query as-of fundamentals (latest snapshot <=
     rebalance_date - reporting_lag_days, mitigating look-ahead bias from
     earnings restatements).
  3. Derive P/E, P/B, market_cap dynamically from per-share fields + as-of price
     (because akshare historical snapshots store EPS/BPS, not the ratios).
  4. Apply screen predicate; equal-weight the survivors.
  5. Hold until next rebalance; compute realized returns from historical_prices.
  6. Compare to sector-benchmark (equal-weighted portfolio of all viable tickers
     in the same industry at the same rebalance).
  7. Aggregate metrics: total_return, Information Ratio, Sharpe, max_drawdown,
     hit_rate, n_unique_holdings.

Honest limitations documented in plan and dashboard caveat:
  - akshare data is as-restated (not point-in-time); 60-day lag is partial mitigation
  - Survivor bias: delisted tickers may be missing from akshare/yfinance entirely
  - HK trading calendar / holidays ignored (rebalance on month-ends)
  - Equal-weighted only; no transaction costs in MVP
"""
import json
import math
import sqlite3
import statistics
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from analysis.screens import (BUILTIN_SCREENS, ScreenDefinition, ScreenParams,
                                _finite, _load_flagged_tickers)
from utils.logger import get_logger

logger = get_logger(__name__)

REBALANCE_FREQS = {
    "monthly": 1,
    "quarterly": 3,
    "annual": 12,
}

DEFAULT_REPORTING_LAG_DAYS = 60
MIN_HOLDINGS_PER_PERIOD = 2   # need at least N matches to count the period — kept low
                              # so per-industry backtests work even for thin sectors


@dataclass
class BacktestResult:
    run_id: str
    screen_id: str
    industry: Optional[str]
    parameters: ScreenParams
    start_date: str
    end_date: str
    rebalance_freq: str
    n_rebalances: int
    period_returns: list[float]            # portfolio return per holding period
    benchmark_period_returns: list[float]
    total_return: float
    benchmark_return: float
    information_ratio: Optional[float]
    sharpe: Optional[float]
    max_drawdown: Optional[float]
    hit_rate: Optional[float]              # % of periods where portfolio beat benchmark
    n_unique_holdings: int
    holdings_log: list[dict] = field(default_factory=list)


class BacktestEngine:
    def __init__(self, db_path: str,
                 reporting_lag_days: int = DEFAULT_REPORTING_LAG_DAYS,
                 sector_risk_path: Optional[str] = None):
        self.db_path = db_path
        self.lag_days = reporting_lag_days
        self.sector_risk_path = sector_risk_path
        self._flagged_cache: Optional[set[str]] = None

    # ---------------- public API ----------------

    def run(self, screen: ScreenDefinition, params: ScreenParams,
            start_date: str, end_date: str,
            rebalance_freq: str = "quarterly",
            industry_filter: Optional[str] = None,
            persist_repo=None, persist_run_id: Optional[str] = None,
            ) -> BacktestResult:
        rebalance_dates = self._generate_rebalance_dates(start_date, end_date, rebalance_freq)
        if len(rebalance_dates) < 2:
            raise ValueError(f"Need at least 2 rebalance dates in [{start_date}, {end_date}] "
                             f"with freq={rebalance_freq}; got {len(rebalance_dates)}")

        flagged = self._get_flagged()

        period_returns: list[float] = []
        benchmark_returns: list[float] = []
        all_holdings: list[dict] = []
        unique_tickers: set[str] = set()

        for i in range(len(rebalance_dates) - 1):
            t0 = rebalance_dates[i]
            t1 = rebalance_dates[i + 1]
            snapshots = self._query_as_of(t0)
            if not snapshots:
                continue

            # Apply industry filter (use yf_sector falling back to watchlist_sector)
            if industry_filter:
                snapshots = [s for s in snapshots
                             if (s.get("yf_sector") or s.get("watchlist_sector")) == industry_filter]

            # Enrich with as-of price and derive missing ratios
            enriched = []
            for s in snapshots:
                price = self._price_on_or_before(s["ticker"], t0)
                if price is None:
                    continue
                e = self._enrich(s, price)
                enriched.append(e)

            # Filter by viability guards (mirrors FactorScoringEngine's principles)
            viable = [e for e in enriched if self._is_viable(e)]
            if not viable:
                continue

            # Exclude flagged if screen says so
            if screen.exclude_flagged:
                candidates = [e for e in viable if e["ticker"] not in flagged]
            else:
                candidates = viable

            # Apply screen predicate
            matches = []
            for cand in candidates:
                crit: list = []
                if screen.predicate(cand, params, crit):
                    matches.append(cand)

            if len(matches) < MIN_HOLDINGS_PER_PERIOD:
                # too few holdings — record as zero-return period to avoid hindsight, skip metric
                continue

            # Equal-weight portfolio return from t0 → t1
            portfolio_rets = []
            for m in matches:
                price_t1 = self._price_on_or_before(m["ticker"], t1)
                if price_t1 is None or m["__price"] is None or m["__price"] == 0:
                    continue
                r = (price_t1 / m["__price"]) - 1
                portfolio_rets.append(r)
                unique_tickers.add(m["ticker"])
                all_holdings.append({
                    "rebalance_date": t0,
                    "ticker": m["ticker"],
                    "weight": 1.0 / max(len(matches), 1),
                    "return_to_next": r,
                    "sector": m.get("yf_sector") or m.get("watchlist_sector"),
                })
            if not portfolio_rets:
                continue
            port_ret = sum(portfolio_rets) / len(portfolio_rets)

            # Benchmark: equal-weighted viable tickers in same industry filter
            # (if industry_filter is set; otherwise across the whole viable universe)
            bench_pool = viable
            bench_rets = []
            for b in bench_pool:
                price_t1 = self._price_on_or_before(b["ticker"], t1)
                if price_t1 is None or b["__price"] is None or b["__price"] == 0:
                    continue
                bench_rets.append((price_t1 / b["__price"]) - 1)
            if not bench_rets:
                continue
            bench_ret = sum(bench_rets) / len(bench_rets)

            period_returns.append(port_ret)
            benchmark_returns.append(bench_ret)

        # Compute aggregate metrics
        result = self._aggregate(
            screen_id=screen.id, industry=industry_filter,
            params=params, start_date=start_date, end_date=end_date,
            rebalance_freq=rebalance_freq,
            period_returns=period_returns,
            benchmark_returns=benchmark_returns,
            n_unique=len(unique_tickers),
            holdings=all_holdings,
            run_id=persist_run_id,
        )

        if persist_repo is not None:
            metrics = {
                "n_rebalances": result.n_rebalances,
                "total_return": result.total_return,
                "benchmark_return": result.benchmark_return,
                "information_ratio": result.information_ratio,
                "sharpe": result.sharpe,
                "max_drawdown": result.max_drawdown,
                "hit_rate": result.hit_rate,
                "n_unique_holdings": result.n_unique_holdings,
            }
            persist_repo.insert_run(result.run_id, result.screen_id, result.industry,
                                    json.dumps(params.to_dict()),
                                    result.start_date, result.end_date,
                                    result.rebalance_freq, metrics)
            persist_repo.insert_holdings(result.run_id, result.holdings_log)

        return result

    # ---------------- internals ----------------

    def _generate_rebalance_dates(self, start: str, end: str, freq: str) -> list[str]:
        step_months = REBALANCE_FREQS[freq]
        d0 = datetime.strptime(start, "%Y-%m-%d").date()
        d_end = datetime.strptime(end, "%Y-%m-%d").date()
        dates = []
        cur = d0
        while cur <= d_end:
            dates.append(cur.strftime("%Y-%m-%d"))
            # Advance by step_months — naive month math
            y, m = cur.year, cur.month + step_months
            while m > 12:
                m -= 12
                y += 1
            try:
                cur = date(y, m, min(cur.day, 28))
            except ValueError:
                cur = date(y, m, 28)
        return dates

    def _query_as_of(self, rebalance_date: str) -> list[dict]:
        """Latest fundamentals snapshot per ticker at-or-before (rebalance - lag)."""
        cutoff = (datetime.strptime(rebalance_date, "%Y-%m-%d").date()
                  - timedelta(days=self.lag_days)).strftime("%Y-%m-%d")
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT f.*, s.name, s.is_watchlist, s.yf_sector, s.watchlist_sector
                FROM fundamentals_snapshots f
                INNER JOIN (
                    SELECT ticker, MAX(snapshot_date) AS max_date
                    FROM fundamentals_snapshots
                    WHERE snapshot_date <= ?
                    GROUP BY ticker
                ) latest ON f.ticker = latest.ticker AND f.snapshot_date = latest.max_date
                INNER JOIN securities s ON f.ticker = s.ticker
                WHERE s.is_active = 1
            """, (cutoff,)).fetchall()
            return [dict(r) for r in rows]

    def _price_on_or_before(self, ticker: str, target_date: str) -> Optional[float]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("""
                SELECT adj_close FROM historical_prices
                WHERE ticker = ? AND date <= ?
                ORDER BY date DESC LIMIT 1
            """, (ticker, target_date)).fetchone()
            return row[0] if row else None

    def _enrich(self, snapshot: dict, price: float) -> dict:
        """Derive trailing_pe, price_to_book, market_cap from per-share + as-of price
        if not already present. Stores price in __price for return calc."""
        e = dict(snapshot)
        e["__price"] = price

        pe = _finite(e.get("trailing_pe"))
        if pe is None:
            eps = _finite(e.get("eps_ttm"))
            if eps and eps > 0:
                e["trailing_pe"] = price / eps

        pb = _finite(e.get("price_to_book"))
        if pb is None:
            bps = _finite(e.get("bps"))
            if bps and bps > 0:
                e["price_to_book"] = price / bps

        mc = _finite(e.get("market_cap"))
        if mc is None:
            shares = _finite(e.get("shares_outstanding"))
            if shares and shares > 0:
                e["market_cap"] = price * shares
        return e

    def _is_viable(self, enriched: dict) -> bool:
        """Mirror FactorScoringEngine viability guards. Hard disqualifiers only."""
        mc = _finite(enriched.get("market_cap"))
        if mc is not None and mc < 200_000_000:
            return False
        pe = _finite(enriched.get("trailing_pe"))
        if pe is not None and (pe < 0.5 or pe > 500):
            return False
        pb = _finite(enriched.get("price_to_book"))
        if pb is not None and pb < 0:
            return False
        pm = _finite(enriched.get("profit_margins"))
        if pm is not None and pm < -0.50:
            return False
        # Need at least one of P/E, P/B, or market cap to be in play
        return any([pe, pb, mc])

    def _get_flagged(self) -> set[str]:
        if self._flagged_cache is None:
            self._flagged_cache = _load_flagged_tickers(self.sector_risk_path)
        return self._flagged_cache

    def _aggregate(self, screen_id: str, industry: Optional[str], params: ScreenParams,
                   start_date: str, end_date: str, rebalance_freq: str,
                   period_returns: list[float], benchmark_returns: list[float],
                   n_unique: int, holdings: list[dict],
                   run_id: Optional[str]) -> BacktestResult:
        run_id = run_id or self._mk_run_id(screen_id, industry, start_date, end_date)
        n_rebalances = len(period_returns)

        if n_rebalances == 0:
            return BacktestResult(
                run_id=run_id, screen_id=screen_id, industry=industry,
                parameters=params, start_date=start_date, end_date=end_date,
                rebalance_freq=rebalance_freq, n_rebalances=0,
                period_returns=[], benchmark_period_returns=[],
                total_return=0.0, benchmark_return=0.0,
                information_ratio=None, sharpe=None, max_drawdown=None,
                hit_rate=None, n_unique_holdings=0, holdings_log=[],
            )

        # Cumulative returns
        cum_port = 1.0
        cum_bench = 1.0
        peak = 1.0
        max_dd = 0.0
        for r_p, r_b in zip(period_returns, benchmark_returns):
            cum_port *= (1 + r_p)
            cum_bench *= (1 + r_b)
            peak = max(peak, cum_port)
            dd = (cum_port - peak) / peak if peak else 0.0
            if dd < max_dd:
                max_dd = dd

        # Information Ratio: (port - bench) excess return mean / std
        excess = [p - b for p, b in zip(period_returns, benchmark_returns)]
        excess_mean = sum(excess) / len(excess)
        try:
            excess_std = statistics.stdev(excess) if len(excess) >= 2 else None
        except statistics.StatisticsError:
            excess_std = None
        ir = (excess_mean / excess_std) if (excess_std and excess_std > 0) else None

        # Sharpe (no risk-free; annualization is approximate)
        try:
            port_std = statistics.stdev(period_returns) if len(period_returns) >= 2 else None
        except statistics.StatisticsError:
            port_std = None
        port_mean = sum(period_returns) / len(period_returns)
        sharpe = (port_mean / port_std) if (port_std and port_std > 0) else None

        hit_rate = sum(1 for e in excess if e > 0) / len(excess)

        return BacktestResult(
            run_id=run_id, screen_id=screen_id, industry=industry,
            parameters=params, start_date=start_date, end_date=end_date,
            rebalance_freq=rebalance_freq, n_rebalances=n_rebalances,
            period_returns=period_returns, benchmark_period_returns=benchmark_returns,
            total_return=cum_port - 1, benchmark_return=cum_bench - 1,
            information_ratio=ir, sharpe=sharpe, max_drawdown=max_dd,
            hit_rate=hit_rate, n_unique_holdings=n_unique, holdings_log=holdings,
        )

    def _mk_run_id(self, screen_id: str, industry: Optional[str],
                   start: str, end: str) -> str:
        ind = (industry or "all").replace(" ", "_").lower()
        return f"{screen_id}_{ind}_{start}_{end}_{uuid.uuid4().hex[:6]}"
