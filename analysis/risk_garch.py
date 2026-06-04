"""GJR-GARCH(1,1) with Student-t innovations — fit, multi-step variance
forecast, Monte Carlo simulation of return paths, and risk metrics.

All math is framework-agnostic so the Dash layer is a thin wrapper.

Model spec (from `arch.arch_model(returns, mean='Constant', vol='GARCH',
p=1, o=1, q=1, dist='t')`):

    r_t       = mu + eps_t                                # mean equation
    sigma2_t  = omega + alpha*eps_{t-1}^2
                       + gamma*eps_{t-1}^2*1[eps_{t-1}<0]  # leverage
                       + beta*sigma2_{t-1}                # variance eq.
    eps_t / sigma_t ~ Student-t(nu)                       # fat-tailed innovations

Returns are passed in PERCENT (i.e. multiplied by 100) — this is what arch
expects for numerical stability of the optimizer. risk_metrics() converts
back to fractions for display.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd
from arch import arch_model
from scipy.stats import t as student_t

# Trading days per year for annualization. HKEX has ~248-252 trading days/yr;
# 252 is the conventional finance choice.
TRADING_DAYS_PER_YEAR = 252


@dataclass
class GARCHFit:
    """Parameters + diagnostics from a fitted GJR-GARCH(1,1)-t model.
    Keeps the heavyweight arch_result so we can call .forecast() later."""
    arch_result: object  # arch.univariate.base.ARCHModelResult; not type-hinted
                         # to avoid making arch import a hard dep at type-check time
    mu: float
    omega: float
    alpha: float
    gamma: float
    beta: float
    nu: float
    persistence: float          # alpha + gamma/2 + beta — should be < 1 for stationarity
    unconditional_vol_pct: float  # long-run annualized vol implied by params (%)
    half_life_days: Optional[float]  # log(0.5) / log(persistence); None if persistence>=1
    aic: float
    bic: float
    n_obs: int


@dataclass
class VolForecast:
    """Multi-step variance forecast. Each entry is one day ahead."""
    daily_variance: np.ndarray   # shape (horizon,) in percent^2
    daily_vol_pct: np.ndarray    # shape (horizon,) in percent
    annualised_vol_pct: np.ndarray  # shape (horizon,) — daily_vol_pct * sqrt(252)


@dataclass
class RiskMetrics:
    """All numbers are FRACTIONS of current price (-0.05 = 5% loss)."""
    var_95_1d: float
    var_99_1d: float
    cvar_95_1d: float
    cvar_99_1d: float
    var_95_5d: float
    var_99_5d: float
    cvar_95_5d: float
    cvar_99_5d: float
    var_95_horizon: float        # at end of full horizon (whatever was simulated)
    var_99_horizon: float
    cvar_95_horizon: float
    cvar_99_horizon: float
    p_loss_10: float             # P(min over horizon < -10%)
    p_loss_20: float             # P(min over horizon < -20%)
    max_drawdowns: np.ndarray    # per-path max drawdown (fraction), shape (n_paths,)
    horizon_days: int


# ============== Returns + fit ==============

def compute_log_returns(prices: pd.Series) -> pd.Series:
    """Daily log returns in PERCENT (x100). arch's optimizer is numerically
    stable when |returns| is order O(1) rather than O(0.01)."""
    return (np.log(prices / prices.shift(1)).dropna() * 100.0)


def fit_garch(returns: pd.Series) -> GARCHFit:
    """Fit GJR-GARCH(1,1) with Student-t innovations. `returns` must be in
    percent. Raises ValueError if the model fails to converge."""
    if len(returns) < 250:
        raise ValueError(f"need >=250 returns for a stable fit; got {len(returns)}")

    am = arch_model(returns, mean="Constant", vol="GARCH",
                     p=1, o=1, q=1, dist="t")
    res = am.fit(disp="off", show_warning=False)
    p = res.params

    alpha = float(p.get("alpha[1]", 0.0))
    gamma = float(p.get("gamma[1]", 0.0))
    beta = float(p.get("beta[1]", 0.0))
    omega = float(p.get("omega", 0.0))
    persistence = alpha + gamma / 2.0 + beta

    # Long-run unconditional variance (only defined when persistence < 1)
    if persistence < 1.0 and persistence > 0.0:
        uncond_var_daily = omega / (1.0 - persistence)
        uncond_vol_annual_pct = float(np.sqrt(uncond_var_daily * TRADING_DAYS_PER_YEAR))
        half_life = float(np.log(0.5) / np.log(persistence))
    else:
        uncond_vol_annual_pct = float("nan")
        half_life = None

    return GARCHFit(
        arch_result=res,
        mu=float(p.get("mu", 0.0)),
        omega=omega,
        alpha=alpha,
        gamma=gamma,
        beta=beta,
        nu=float(p.get("nu", float("nan"))),
        persistence=persistence,
        unconditional_vol_pct=uncond_vol_annual_pct,
        half_life_days=half_life,
        aic=float(res.aic),
        bic=float(res.bic),
        n_obs=int(res.nobs),
    )


# ============== Forecast ==============

def forecast_volatility(fit: GARCHFit, horizon: int) -> VolForecast:
    """Analytic multi-step variance forecast. Returns daily and annualized vol."""
    fc = fit.arch_result.forecast(horizon=horizon, reindex=False)
    # fc.variance is a DataFrame of shape (1, horizon); take the row.
    daily_var = np.asarray(fc.variance.values[0], dtype=float)
    daily_vol = np.sqrt(daily_var)
    annual_vol = daily_vol * np.sqrt(TRADING_DAYS_PER_YEAR)
    return VolForecast(
        daily_variance=daily_var,
        daily_vol_pct=daily_vol,
        annualised_vol_pct=annual_vol,
    )


# ============== Monte Carlo ==============

def simulate_paths(fit: GARCHFit, horizon: int, n_paths: int = 5000,
                    seed: Optional[int] = None) -> np.ndarray:
    """Simulate `n_paths` future return paths under the fitted GJR-GARCH-t
    model. Returns array of shape (n_paths, horizon) of CUMULATIVE log
    returns in FRACTIONS (not percent), so e.g. 0.05 = 5% cumulative gain
    from start.

    Uses the recursive variance update from the fitted parameters:
        sigma2_t = omega + (alpha + gamma*1[eps<0])*eps_{t-1}^2 + beta*sigma2_{t-1}
        eps_t    = sigma_t * Z_t,    Z_t ~ Student-t(nu) / sqrt(nu/(nu-2))
        r_t      = mu + eps_t
    The Student-t draws are scaled so unit variance (the standard arch
    parameterization).
    """
    rng = np.random.default_rng(seed)

    mu = fit.mu
    omega = fit.omega
    alpha = fit.alpha
    gamma = fit.gamma
    beta = fit.beta
    nu = fit.nu

    # Initial conditions: take last residual + variance from the fit.
    last_resid = float(fit.arch_result.resid.iloc[-1])
    last_var = float(fit.arch_result.conditional_volatility.iloc[-1] ** 2)

    # Allocate output: returns in PERCENT, we'll convert at the end.
    returns_pct = np.empty((n_paths, horizon), dtype=float)

    # Pre-draw all innovations: shape (n_paths, horizon)
    # student_t.rvs gives variance nu/(nu-2); divide by sqrt of that for unit variance.
    raw = student_t.rvs(df=nu, size=(n_paths, horizon), random_state=rng)
    scale = np.sqrt((nu - 2.0) / nu) if nu > 2.0 else 1.0
    z = raw * scale  # now ~ unit-variance Student-t

    # Run the recursion vectorized over paths
    sigma2 = np.full(n_paths, last_var, dtype=float)
    eps_prev = np.full(n_paths, last_resid, dtype=float)

    for h in range(horizon):
        # Variance update uses the PREVIOUS innovation
        leverage_term = np.where(eps_prev < 0, gamma, 0.0)
        sigma2 = omega + (alpha + leverage_term) * (eps_prev ** 2) + beta * sigma2
        sigma = np.sqrt(sigma2)
        # Draw innovation for step h
        eps = sigma * z[:, h]
        returns_pct[:, h] = mu + eps
        eps_prev = eps  # for next step's leverage check

    # Cumulative log returns, converted percent -> fraction
    cum_pct = np.cumsum(returns_pct, axis=1)
    return cum_pct / 100.0


# ============== Risk metrics ==============

def risk_metrics(paths: np.ndarray, current_price: float) -> RiskMetrics:
    """Compute VaR, CVaR, drawdown distribution, and exceedance probs from
    Monte Carlo paths. `paths` is shape (n_paths, horizon) of cumulative
    log-return fractions; we convert to simple return fractions for VaR/CVaR
    reporting since that's what users intuitively understand."""
    if paths.ndim != 2:
        raise ValueError(f"paths must be 2-D; got shape {paths.shape}")
    n_paths, horizon = paths.shape

    # Cumulative log-return -> simple return fraction
    # simple = exp(log_ret) - 1
    simple_paths = np.exp(paths) - 1.0

    # 1-day, 5-day, full-horizon distributions (terminal values at those steps)
    def _var_cvar(col_returns: np.ndarray, alpha: float) -> tuple[float, float]:
        """alpha=0.05 for VaR95; returns NEGATIVE numbers (losses)."""
        var_q = float(np.quantile(col_returns, alpha))
        # CVaR = mean of returns at or below the VaR quantile
        cvar_q = float(col_returns[col_returns <= var_q].mean()) if (col_returns <= var_q).any() else var_q
        return var_q, cvar_q

    one_d = simple_paths[:, 0]
    five_d = simple_paths[:, min(4, horizon - 1)]
    full_d = simple_paths[:, -1]

    v95_1, c95_1 = _var_cvar(one_d, 0.05)
    v99_1, c99_1 = _var_cvar(one_d, 0.01)
    v95_5, c95_5 = _var_cvar(five_d, 0.05)
    v99_5, c99_5 = _var_cvar(five_d, 0.01)
    v95_h, c95_h = _var_cvar(full_d, 0.05)
    v99_h, c99_h = _var_cvar(full_d, 0.01)

    # Per-path max drawdown: lowest simple return reached during the path,
    # measured from the path's running peak. For pure forward simulation
    # starting at 0, this is min over the path RELATIVE to running max.
    running_max = np.maximum.accumulate(simple_paths, axis=1)
    drawdowns = simple_paths - running_max  # always <= 0
    max_drawdowns = drawdowns.min(axis=1)   # most negative = worst drawdown

    # Exceedance probs: did the path AT ANY POINT lose >X% (peak-to-trough OR
    # cumulative from start)? Use cumulative-from-start since that's what
    # users mean by "lost 10%"
    min_in_path = simple_paths.min(axis=1)
    p_loss_10 = float((min_in_path < -0.10).mean())
    p_loss_20 = float((min_in_path < -0.20).mean())

    return RiskMetrics(
        var_95_1d=v95_1, var_99_1d=v99_1, cvar_95_1d=c95_1, cvar_99_1d=c99_1,
        var_95_5d=v95_5, var_99_5d=v99_5, cvar_95_5d=c95_5, cvar_99_5d=c99_5,
        var_95_horizon=v95_h, var_99_horizon=v99_h,
        cvar_95_horizon=c95_h, cvar_99_horizon=c99_h,
        p_loss_10=p_loss_10, p_loss_20=p_loss_20,
        max_drawdowns=max_drawdowns,
        horizon_days=horizon,
    )


# ============== Rolling realized vol (for the vol cone) ==============

def rolling_realized_vol_pct(returns_pct: pd.Series, window: int) -> pd.Series:
    """Rolling annualized realized vol in percent. `returns_pct` is the same
    percent-scaled log-return series we feed to fit_garch."""
    daily_std = returns_pct.rolling(window).std()
    return daily_std * np.sqrt(TRADING_DAYS_PER_YEAR)
