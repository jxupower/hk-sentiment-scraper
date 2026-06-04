"""Modern Portfolio Theory — max-Sharpe optimization, efficient frontier,
walk-forward backtest. Pure math layer; no Dash imports.

All math is documented in the plan file (Layers 1-6). High-level summary:
  - Returns: daily simple, inner-joined across tickers (common date range)
  - mu: annualised sample mean (noisiest input — documented limitation)
  - sigma: Ledoit-Wolf shrunk covariance, annualised
  - Optimization: scipy.optimize.minimize(SLSQP) over long-only + per-asset cap
  - Frontier: sweep target returns, solve min-variance at each point
  - Backtest: walk-forward refit + apply weights, compute realised metrics

The optimizer is fast (<100ms for N=50). The frontier sweep is the heaviest
piece (~30 QPs ≈ 1-2s). Caching is therefore worthwhile — handled by
analysis/_portfolio_cache.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from sklearn.covariance import LedoitWolf


TRADING_DAYS_PER_YEAR = 252


# ============== Returns ==============

def compute_returns_matrix(tickers: list[str], lookback_days: int,
                            db) -> pd.DataFrame:
    """Pull adj-close prices for each ticker via the cache-aside data loader,
    align on a common date intersection, compute simple daily returns.

    `lookback_days` is in TRADING days; 0 = MAX (no clipping). When some
    tickers have shorter history than others, the intersection is what
    matters. Returns a DataFrame with shape (T, N), date index, columns
    = tickers preserving the input order.

    Raises ValueError when fewer than 50 overlapping trading days remain
    (covariance estimation breaks down below ~100 obs anyway, but 50 is
    the absolute floor).
    """
    from analysis.data_loader import get_or_fetch_prices

    series_by_ticker: dict[str, pd.Series] = {}
    for t in tickers:
        rows = get_or_fetch_prices(t, db)
        if not rows:
            raise ValueError(f"no price data for {t}")
        s = pd.Series(
            [float(r["adj_close"]) for r in rows if r.get("adj_close") is not None],
            index=pd.to_datetime([r["date"] for r in rows
                                   if r.get("adj_close") is not None]),
        )
        s = s[~s.index.duplicated(keep="last")].sort_index()
        series_by_ticker[t] = s

    # Inner-join on date so every ticker contributes only when all do
    prices = pd.concat(series_by_ticker, axis=1, join="inner")
    prices = prices[tickers]  # preserve caller-supplied order

    if lookback_days and lookback_days > 0:
        prices = prices.iloc[-lookback_days:]

    if len(prices) < 51:    # need at least 50 returns
        raise ValueError(
            f"only {len(prices)} overlapping trading days across {tickers} — "
            "covariance estimate would be too noisy")

    returns = prices.pct_change().dropna()
    return returns


# ============== mu, sigma estimation ==============

@dataclass
class MuSigma:
    mu: np.ndarray              # annualised expected returns, shape (N,)
    sigma: np.ndarray           # annualised cov matrix, shape (N, N)
    mu_se: np.ndarray           # standard errors on mu (annualised)
    shrinkage: float            # Ledoit-Wolf alpha actually used
    n_obs: int                  # T
    tickers: list[str]


def estimate_mu_sigma(returns: pd.DataFrame) -> MuSigma:
    """Annualised mean + Ledoit-Wolf shrunk covariance.

    mu_se is the standard error of the sample mean, also annualised
    (= daily_std * sqrt(252) / sqrt(T)). It's purely informational — we
    expose it so the UI can show the user how noisy μ actually is.
    """
    n_obs = len(returns)
    daily_mean = returns.mean().to_numpy()
    daily_std = returns.std().to_numpy()
    mu = daily_mean * TRADING_DAYS_PER_YEAR
    mu_se = daily_std * np.sqrt(TRADING_DAYS_PER_YEAR / n_obs)

    lw = LedoitWolf().fit(returns.to_numpy())
    sigma = lw.covariance_ * TRADING_DAYS_PER_YEAR
    shrinkage = float(lw.shrinkage_)

    return MuSigma(mu=mu, sigma=sigma, mu_se=mu_se,
                    shrinkage=shrinkage, n_obs=n_obs,
                    tickers=list(returns.columns))


# ============== Core metrics ==============

def portfolio_metrics(weights: np.ndarray, mu: np.ndarray, sigma: np.ndarray,
                        rf: float = 0.0) -> dict:
    """Annualised return, vol, Sharpe. `rf` is the annual risk-free rate
    (e.g. 0.0 or 0.04 for HIBOR ~4%)."""
    w = np.asarray(weights, dtype=float)
    ret = float(np.dot(w, mu))
    var = float(np.dot(w, np.dot(sigma, w)))
    vol = float(np.sqrt(max(var, 0.0)))
    sharpe = (ret - rf) / vol if vol > 1e-12 else 0.0
    return {"return": ret, "vol": vol, "sharpe": sharpe}


# ============== Max-Sharpe optimizer ==============

def max_sharpe_portfolio(mu: np.ndarray, sigma: np.ndarray, *,
                          rf: float = 0.0, weight_cap: float = 0.30,
                          initial_weights: Optional[np.ndarray] = None,
                          ) -> np.ndarray:
    """Long-only, fully-invested, per-asset cap, max Sharpe via SLSQP.

    Returns a length-N weight vector summing to 1.0 (within solver
    tolerance), each weight in [0, weight_cap]. Raises ValueError when
    the cap is too small to fit (N × weight_cap < 1)."""
    n = len(mu)
    if n * weight_cap < 1.0 - 1e-9:
        raise ValueError(
            f"weight_cap={weight_cap} too tight for N={n} — "
            f"need cap >= 1/N = {1.0/n:.3f}")

    def neg_sharpe(w):
        m = portfolio_metrics(w, mu, sigma, rf)
        return -m["sharpe"]

    w0 = initial_weights if initial_weights is not None else np.full(n, 1.0 / n)
    bounds = [(0.0, weight_cap)] * n
    constraints = [{"type": "eq", "fun": lambda w: np.sum(w) - 1.0}]

    res = minimize(neg_sharpe, w0, method="SLSQP",
                    bounds=bounds, constraints=constraints,
                    options={"maxiter": 300, "ftol": 1e-9})

    if not res.success:
        # SLSQP can declare failure even when the answer is fine; fall back
        # to equal-weight if the solution is clearly broken (NaN / out of bounds)
        if not np.all(np.isfinite(res.x)) or np.any(res.x < -1e-6):
            return np.full(n, 1.0 / n)
    return _clean_weights(res.x, weight_cap)


def _clean_weights(w: np.ndarray, cap: float) -> np.ndarray:
    """Clip tiny numerical artefacts and renormalize."""
    w = np.clip(w, 0.0, cap)
    s = w.sum()
    return w / s if s > 0 else w


# ============== Minimum-variance at target return (for frontier) ==============

def min_variance_portfolio_at_return(mu: np.ndarray, sigma: np.ndarray,
                                       target_return: float, *,
                                       weight_cap: float = 0.30,
                                       ) -> Optional[np.ndarray]:
    """Find the weight vector minimising variance subject to:
      - Σw = 1, 0 <= w_i <= cap
      - w'μ = target_return (within solver tol)
    Returns None when no feasible solution exists (e.g. target_return
    above what the cap-constrained universe can deliver)."""
    n = len(mu)
    if n * weight_cap < 1.0 - 1e-9:
        return None

    def portfolio_var(w):
        return float(np.dot(w, np.dot(sigma, w)))

    w0 = np.full(n, 1.0 / n)
    bounds = [(0.0, weight_cap)] * n
    constraints = [
        {"type": "eq", "fun": lambda w: np.sum(w) - 1.0},
        {"type": "eq", "fun": lambda w: np.dot(w, mu) - target_return},
    ]
    res = minimize(portfolio_var, w0, method="SLSQP",
                    bounds=bounds, constraints=constraints,
                    options={"maxiter": 300, "ftol": 1e-9})
    if not res.success or not np.all(np.isfinite(res.x)):
        return None
    # Tolerance check: actual portfolio return close to target
    actual = float(np.dot(res.x, mu))
    if abs(actual - target_return) > 1e-3:
        return None
    return _clean_weights(res.x, weight_cap)


# ============== Efficient frontier ==============

@dataclass
class FrontierPoint:
    target_return: float
    realised_return: float
    vol: float
    sharpe: float
    weights: np.ndarray


def efficient_frontier(mu: np.ndarray, sigma: np.ndarray, *,
                        n_points: int = 30, weight_cap: float = 0.30,
                        rf: float = 0.0) -> list[FrontierPoint]:
    """Trace the efficient frontier by sweeping target returns.

    The feasible return range is constrained by the per-asset cap: the
    maximum achievable return is `cap × top-k μ_i` where k = ceil(1/cap)
    (you can put at most `cap` on each of the top names). We sweep
    between the minimum-variance portfolio's return and that upper bound.

    Points that fail to solve (infeasible targets) are dropped silently.
    """
    # Find min-variance portfolio (any return) to anchor the low end
    n = len(mu)
    def variance(w):
        return float(np.dot(w, np.dot(sigma, w)))
    w0 = np.full(n, 1.0 / n)
    bounds = [(0.0, weight_cap)] * n
    constraints = [{"type": "eq", "fun": lambda w: np.sum(w) - 1.0}]
    res = minimize(variance, w0, method="SLSQP",
                    bounds=bounds, constraints=constraints,
                    options={"maxiter": 300, "ftol": 1e-9})
    if not res.success:
        return []
    min_var_ret = float(np.dot(res.x, mu))

    # Upper bound: cap × top names
    top_indices = np.argsort(-mu)
    cum = 0.0
    max_ret = 0.0
    for idx in top_indices:
        take = min(weight_cap, 1.0 - cum)
        max_ret += take * mu[idx]
        cum += take
        if cum >= 1.0 - 1e-9:
            break

    if max_ret <= min_var_ret:
        return []

    targets = np.linspace(min_var_ret, max_ret, n_points)
    points: list[FrontierPoint] = []
    for tr in targets:
        w = min_variance_portfolio_at_return(mu, sigma, tr, weight_cap=weight_cap)
        if w is None:
            continue
        m = portfolio_metrics(w, mu, sigma, rf)
        points.append(FrontierPoint(
            target_return=float(tr),
            realised_return=m["return"],
            vol=m["vol"],
            sharpe=m["sharpe"],
            weights=w,
        ))
    return points


# ============== Status-quo weights from holdings ==============

def status_quo_weights(holdings: list[dict], latest_prices: dict[str, float],
                        tickers_order: list[str]) -> np.ndarray:
    """Convert (ticker, shares) holdings + latest prices into a weight
    vector aligned with `tickers_order` (the same order returned by
    estimate_mu_sigma). Candidates (shares==0) get weight 0."""
    by_ticker = {h["ticker"]: float(h.get("shares") or 0) for h in holdings}
    values = np.array([
        by_ticker.get(t, 0.0) * latest_prices.get(t, 0.0)
        for t in tickers_order
    ])
    total = values.sum()
    if total <= 0:
        # All-zero holdings (everything is a candidate) → no status quo
        return np.zeros(len(tickers_order))
    return values / total


# ============== Leave-one-out marginal value ==============

def candidate_marginal_value(mu: np.ndarray, sigma: np.ndarray,
                                tickers: list[str],
                                candidate_tickers: list[str], *,
                                rf: float = 0.0, weight_cap: float = 0.30,
                                ) -> dict[str, float]:
    """For each candidate ticker, return the drop in `S_full_optimal` if
    that candidate is removed from the universe. Higher value = more
    valuable addition.

    Sharpe drop = S_full - S_without_this_candidate. Positive when the
    candidate is contributing; near-zero when the optimizer wouldn't
    have used it anyway.
    """
    w_full = max_sharpe_portfolio(mu, sigma, rf=rf, weight_cap=weight_cap)
    s_full = portfolio_metrics(w_full, mu, sigma, rf)["sharpe"]

    out: dict[str, float] = {}
    name_to_idx = {t: i for i, t in enumerate(tickers)}
    for c in candidate_tickers:
        idx = name_to_idx.get(c)
        if idx is None:
            continue
        keep = [i for i in range(len(tickers)) if i != idx]
        if len(keep) < 2:
            out[c] = 0.0
            continue
        mu_k = mu[keep]
        sigma_k = sigma[np.ix_(keep, keep)]
        # If the remaining cap budget can't sum to 1, the candidate is
        # genuinely required — give it max marginal value
        if len(keep) * weight_cap < 1.0 - 1e-9:
            out[c] = float(s_full)  # interpretation: removing it breaks feasibility
            continue
        try:
            w_k = max_sharpe_portfolio(mu_k, sigma_k, rf=rf, weight_cap=weight_cap)
            s_k = portfolio_metrics(w_k, mu_k, sigma_k, rf)["sharpe"]
            out[c] = float(s_full - s_k)
        except Exception:
            out[c] = 0.0
    return out


# ============== Walk-forward backtest ==============

@dataclass
class BacktestStrategy:
    name: str
    daily_returns: pd.Series      # daily portfolio returns indexed by date
    total_return: float           # cumulative
    annualised_return: float
    annualised_vol: float
    sharpe: float                 # annualised
    max_drawdown: float
    turnover: float               # average per-rebalance one-way turnover, fraction


def _series_metrics(returns: pd.Series, rf: float = 0.0) -> dict:
    """Annualised return, vol, Sharpe, max drawdown of a daily-return series."""
    if returns.empty:
        return {"total_return": 0.0, "annualised_return": 0.0,
                "annualised_vol": 0.0, "sharpe": 0.0, "max_drawdown": 0.0}
    cum = (1.0 + returns).cumprod()
    total = float(cum.iloc[-1] - 1.0)
    daily_mean = float(returns.mean())
    daily_std = float(returns.std())
    ann_ret = daily_mean * TRADING_DAYS_PER_YEAR
    ann_vol = daily_std * np.sqrt(TRADING_DAYS_PER_YEAR)
    sharpe = (ann_ret - rf) / ann_vol if ann_vol > 1e-12 else 0.0
    # Max drawdown
    peak = cum.cummax()
    dd = (cum - peak) / peak
    max_dd = float(dd.min())
    return {"total_return": total, "annualised_return": ann_ret,
            "annualised_vol": ann_vol, "sharpe": sharpe, "max_drawdown": max_dd}


def walk_forward_backtest(returns: pd.DataFrame, *,
                            lookback: int, rebalance_freq: int,
                            rf: float = 0.0, weight_cap: float = 0.30,
                            ) -> dict[str, BacktestStrategy]:
    """Walk-forward simulation of the max-Sharpe strategy vs three
    baselines (equal-weight, status-quo-frozen, first-asset buy-and-hold
    as a proxy benchmark when no explicit benchmark is provided).

    For each rebalance date t = lookback, lookback+K, ...:
      1. Estimate μ_t, Σ_t from returns[t-lookback : t]
      2. Optimize -> w_t*
      3. Apply w_t* to realised returns[t : t+K]

    `returns` is the full T × N returns DataFrame from compute_returns_matrix.
    Returns four strategies: 'max_sharpe', 'equal_weight', 'first_asset'
    (an implicit benchmark — first column, typically HSI or the first ticker).
    Caller can extend with HSI explicitly by adding ^HSI as the first column.
    """
    T, N = returns.shape
    if T < lookback + rebalance_freq + 1:
        # Not enough data to do even one cycle
        empty = pd.Series(dtype=float)
        nil = BacktestStrategy(name="(empty)", daily_returns=empty,
                                total_return=0.0, annualised_return=0.0,
                                annualised_vol=0.0, sharpe=0.0,
                                max_drawdown=0.0, turnover=0.0)
        return {"max_sharpe": nil, "equal_weight": nil, "first_asset": nil}

    # Build daily return series for each strategy by stepping forward
    ms_daily: list[pd.Series] = []
    ew_daily: list[pd.Series] = []
    fa_daily: list[pd.Series] = []

    prev_ms_w = None
    ms_turnovers: list[float] = []

    rebalance_dates = list(range(lookback, T, rebalance_freq))
    for t in rebalance_dates:
        train = returns.iloc[t - lookback : t]
        end_t = min(t + rebalance_freq, T)
        future = returns.iloc[t : end_t]
        if future.empty:
            break

        try:
            ms = estimate_mu_sigma(train)
            w_ms = max_sharpe_portfolio(ms.mu, ms.sigma, rf=rf,
                                          weight_cap=weight_cap)
        except Exception:
            w_ms = np.full(N, 1.0 / N)

        if prev_ms_w is not None:
            ms_turnovers.append(float(np.abs(w_ms - prev_ms_w).sum() / 2.0))
        prev_ms_w = w_ms

        # Equal-weight benchmark
        w_eq = np.full(N, 1.0 / N)

        # Daily portfolio returns under each weight vector
        ms_daily.append(future.dot(w_ms))
        ew_daily.append(future.dot(w_eq))
        fa_daily.append(future.iloc[:, 0])  # first column

    ms_returns = pd.concat(ms_daily) if ms_daily else pd.Series(dtype=float)
    ew_returns = pd.concat(ew_daily) if ew_daily else pd.Series(dtype=float)
    fa_returns = pd.concat(fa_daily) if fa_daily else pd.Series(dtype=float)

    avg_turnover = float(np.mean(ms_turnovers)) if ms_turnovers else 0.0

    def _to_strategy(name: str, r: pd.Series, turnover: float) -> BacktestStrategy:
        m = _series_metrics(r, rf=rf)
        return BacktestStrategy(
            name=name, daily_returns=r,
            total_return=m["total_return"],
            annualised_return=m["annualised_return"],
            annualised_vol=m["annualised_vol"],
            sharpe=m["sharpe"],
            max_drawdown=m["max_drawdown"],
            turnover=turnover,
        )

    return {
        "max_sharpe": _to_strategy("Max-Sharpe (walk-fwd)", ms_returns, avg_turnover),
        "equal_weight": _to_strategy("Equal-weight (rebal)", ew_returns, 0.0),
        "first_asset": _to_strategy(returns.columns[0], fa_returns, 0.0),
    }
