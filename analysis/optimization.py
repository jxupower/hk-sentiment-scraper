"""Walk-forward per-industry parameter optimization for screens.

For each industry, sweep a small grid of screen-parameter combinations across
overlapping walk-forward windows (train_window_months train + test_window_months
test, sliding by step_months). The score for a (params, industry) combo is the
average Information Ratio across windows. Persist the winning params per
(screen, industry) to optimized_parameters.

Note: this is deliberately a *coarse* grid (~30-200 combos per screen) given:
  - akshare gives us 9 annual data points per ticker (HK companies don't report
    quarterly), so walk-forward windows are constrained.
  - Survivor / look-ahead bias (documented) makes hyper-tuning risky anyway.
  - Stage-5 dashboard lets the user manually tune from the optimum.

Per-screen grids are defined as dicts of {param_name: [candidate values]}. The
optimizer expands the Cartesian product, runs a backtest for each, ranks by
average IR. Only ScreenParams fields actually used by the predicate matter.
"""
import itertools
import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from analysis.backtest import BacktestEngine, MIN_HOLDINGS_PER_PERIOD
from analysis.screens import BUILTIN_SCREENS, ScreenDefinition, ScreenParams
from utils.logger import get_logger

logger = get_logger(__name__)


# ============== Parameter grids per screen ==============
# Coarse — 3-4 values per param. Total combos = product across params.
# We intentionally keep it small so optimization runs in minutes, not hours.

PARAM_GRIDS = {
    "value": {
        "pe_max":              [10, 15, 20, 30],
        "pb_max":              [1.5, 3.0, 5.0],
        "roe_min":             [0.05, 0.10, 0.15],
        "earnings_growth_min": [-0.20, -0.10, 0.0],
        "market_cap_min":      [2_000_000_000, 10_000_000_000],
        # pe_min and pb_min kept at defaults (5, 0.5)
    },
    "quality_compounder": {
        "roe_min":             [0.10, 0.15, 0.20],
        "de_max":              [50, 100, 200],
        "earnings_growth_min": [0.0, 0.05, 0.10],
        "market_cap_min":      [5_000_000_000, 10_000_000_000, 50_000_000_000],
    },
    "income": {
        "dividend_yield_min":  [3.0, 4.0, 5.0, 6.0],
        "market_cap_min":      [2_000_000_000, 5_000_000_000, 20_000_000_000],
        "earnings_growth_min": [-0.10, -0.05, 0.0],
    },
    "avoid_distress": {
        # Distress screen is educational; optimizing it on historical IR doesn't
        # make conceptual sense (we WANT it to underperform). Skip optimization.
    },
}


@dataclass
class WindowResult:
    train_start: str
    test_start: str
    test_end: str
    information_ratio: Optional[float]
    n_rebalances: int


@dataclass
class IndustryOptimizationResult:
    screen_id: str
    industry: str
    best_params: ScreenParams
    avg_information_ratio: float
    per_window: list[WindowResult]
    n_combos_evaluated: int


class WalkForwardOptimizer:
    def __init__(self, db_path: str, sector_risk_path: Optional[str] = None):
        self.db_path = db_path
        self.sector_risk_path = sector_risk_path
        self.engine = BacktestEngine(db_path, sector_risk_path=sector_risk_path)

    def optimize(self, screen: ScreenDefinition, industry: str,
                 start_date: str, end_date: str,
                 train_window_months: int = 36,
                 test_window_months: int = 12,
                 step_months: int = 12,
                 rebalance_freq: str = "quarterly",
                 persist_repo=None,
                 ) -> IndustryOptimizationResult:
        """Run walk-forward CV across the parameter grid for one (screen, industry).
        Returns the params with best avg IR across windows. Persists to
        optimized_parameters if persist_repo given."""
        grid = PARAM_GRIDS.get(screen.id, {})
        if not grid:
            logger.info("No parameter grid for screen %s — skipping optimization", screen.id)
            return None

        param_combos = list(self._expand_grid(grid))
        windows = list(self._walk_forward_windows(
            start_date, end_date, train_window_months, test_window_months, step_months))
        if not windows:
            raise ValueError(f"No walk-forward windows fit in [{start_date}, {end_date}] "
                             f"with train={train_window_months}mo + test={test_window_months}mo")

        logger.info("Optimizing %s for industry=%r: %d param combos × %d windows = %d evals",
                    screen.id, industry, len(param_combos), len(windows),
                    len(param_combos) * len(windows))

        best_score = -math.inf
        best_params = None
        best_window_results: list[WindowResult] = []
        for combo_idx, combo_kwargs in enumerate(param_combos, start=1):
            params = self._build_params(screen, combo_kwargs)
            per_window: list[WindowResult] = []
            irs: list[float] = []
            for win in windows:
                try:
                    res = self.engine.run(
                        screen, params, win["test_start"], win["test_end"],
                        rebalance_freq=rebalance_freq, industry_filter=industry,
                        persist_repo=None,  # don't persist during sweep
                    )
                except Exception as e:
                    logger.debug("backtest crashed combo=%s window=%s: %s",
                                 combo_kwargs, win, e)
                    continue
                per_window.append(WindowResult(
                    train_start=win["train_start"], test_start=win["test_start"],
                    test_end=win["test_end"],
                    information_ratio=res.information_ratio,
                    n_rebalances=res.n_rebalances,
                ))
                if res.information_ratio is not None:
                    irs.append(res.information_ratio)
            if not irs:
                continue
            avg_ir = sum(irs) / len(irs)
            if avg_ir > best_score:
                best_score = avg_ir
                best_params = params
                best_window_results = per_window

            if combo_idx % 20 == 0:
                logger.info("  combo %d/%d: best_avg_IR so far = %.3f",
                            combo_idx, len(param_combos), best_score)

        if best_params is None:
            logger.warning("No viable param combo for %s × %s — no rebalances ever produced.",
                           screen.id, industry)
            return None

        result = IndustryOptimizationResult(
            screen_id=screen.id, industry=industry,
            best_params=best_params, avg_information_ratio=best_score,
            per_window=best_window_results,
            n_combos_evaluated=len(param_combos),
        )

        if persist_repo is not None:
            persist_repo.upsert(
                screen_id=screen.id, industry=industry,
                parameters_json=json.dumps(best_params.to_dict()),
                information_ratio=best_score,
                n_windows=len(best_window_results),
                train_months=train_window_months,
                test_months=test_window_months,
            )

        return result

    def optimize_all_industries(self, screen: ScreenDefinition,
                                  industries: list[str],
                                  start_date: str, end_date: str,
                                  persist_repo=None,
                                  **kwargs) -> list[IndustryOptimizationResult]:
        out = []
        for industry in industries:
            try:
                res = self.optimize(screen, industry, start_date, end_date,
                                    persist_repo=persist_repo, **kwargs)
                if res:
                    out.append(res)
                    logger.info("  %s × %s: best avg IR = %.3f",
                                screen.id, industry, res.avg_information_ratio)
            except Exception as e:
                logger.error("Optimization failed for %s × %s: %s", screen.id, industry, e)
        return out

    # ---------------- helpers ----------------

    @staticmethod
    def _expand_grid(grid: dict) -> list[dict]:
        keys = list(grid.keys())
        for values in itertools.product(*[grid[k] for k in keys]):
            yield dict(zip(keys, values))

    @staticmethod
    def _build_params(screen: ScreenDefinition, overrides: dict) -> ScreenParams:
        """Start from default_params, override with grid kwargs."""
        d = screen.default_params.to_dict()
        d.update(overrides)
        return ScreenParams.from_dict(d)

    @staticmethod
    def _walk_forward_windows(start: str, end: str,
                              train_months: int, test_months: int,
                              step_months: int) -> list[dict]:
        """Sliding (train_window, test_window) pairs from start → end."""
        d0 = datetime.strptime(start, "%Y-%m-%d").date()
        d_end = datetime.strptime(end, "%Y-%m-%d").date()
        windows = []
        train_start = d0
        while True:
            test_start = _add_months(train_start, train_months)
            test_end = _add_months(test_start, test_months)
            if test_end > d_end:
                break
            windows.append({
                "train_start": train_start.strftime("%Y-%m-%d"),
                "test_start": test_start.strftime("%Y-%m-%d"),
                "test_end": test_end.strftime("%Y-%m-%d"),
            })
            train_start = _add_months(train_start, step_months)
        return windows


def _add_months(d: date, n: int) -> date:
    y, m = d.year, d.month + n
    while m > 12:
        m -= 12
        y += 1
    while m < 1:
        m += 12
        y -= 1
    try:
        return date(y, m, d.day)
    except ValueError:
        return date(y, m, 28)
