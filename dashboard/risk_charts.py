"""Chart factories for the Risk Forecast tab. Pure functions of the data
bundle returned by analysis/_garch_cache.py — no Dash imports required."""
from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from dashboard import theme as T


def _hex_to_rgba(hex_color: str, alpha: float) -> str:
    """Convert #rrggbb to rgba(r,g,b,a) for fill colors with transparency."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def fan_chart(prices: pd.Series, paths: np.ndarray, ticker: str) -> go.Figure:
    """The centerpiece: historical price line plus the 5/25/50/75/95
    percentile bands of simulated future paths, anchored at the last
    historical price.

    `paths` is cumulative log returns (fractions), shape (n_paths, horizon).
    We convert to prices via current_price * exp(cum_log_return).
    """
    current_price = float(prices.iloc[-1])
    # paths: (n_paths, horizon) -> prices: same shape
    sim_prices = current_price * np.exp(paths)

    # Percentile bands across paths, per horizon step
    pcts = [5, 25, 50, 75, 95]
    bands = {p: np.quantile(sim_prices, p / 100.0, axis=0) for p in pcts}

    # X axis: a synthetic index for history + forward (we show last ~250 days
    # of history so the fan is visible without dwarfing the past)
    hist_tail = prices.iloc[-250:].reset_index(drop=True)
    n_hist = len(hist_tail)
    horizon = paths.shape[1]
    x_hist = list(range(-n_hist + 1, 1))            # ..., -2, -1, 0
    x_fwd = list(range(1, horizon + 1))             # 1..horizon

    # Prepend current price so each forward band starts on the chart at day 0
    def _prepend(arr):
        return np.concatenate([[current_price], arr])

    x_fwd_full = [0] + x_fwd

    fig = go.Figure()

    # Historical line
    fig.add_trace(go.Scatter(
        x=x_hist, y=hist_tail.values,
        mode="lines", name="Historical price",
        line=dict(color=T.TEXT_MUTED, width=1.5),
        hovertemplate="day %{x}<br>price %{y:.2f}<extra></extra>",
    ))

    # 5-95% outer band (very faint)
    fig.add_trace(go.Scatter(
        x=x_fwd_full, y=_prepend(bands[95]),
        mode="lines", line=dict(width=0),
        showlegend=False, hoverinfo="skip",
    ))
    fig.add_trace(go.Scatter(
        x=x_fwd_full, y=_prepend(bands[5]),
        mode="lines", line=dict(width=0),
        fill="tonexty", fillcolor=_hex_to_rgba(T.PRIMARY, 0.10),
        name="5–95% range",
        hovertemplate="day +%{x}<br>5–95%% band<extra></extra>",
    ))

    # 25-75% inner band (more visible)
    fig.add_trace(go.Scatter(
        x=x_fwd_full, y=_prepend(bands[75]),
        mode="lines", line=dict(width=0),
        showlegend=False, hoverinfo="skip",
    ))
    fig.add_trace(go.Scatter(
        x=x_fwd_full, y=_prepend(bands[25]),
        mode="lines", line=dict(width=0),
        fill="tonexty", fillcolor=_hex_to_rgba(T.PRIMARY, 0.22),
        name="25–75% range",
        hovertemplate="day +%{x}<br>25–75%% band<extra></extra>",
    ))

    # Median path
    fig.add_trace(go.Scatter(
        x=x_fwd_full, y=_prepend(bands[50]),
        mode="lines", name="Median path",
        line=dict(color=T.PRIMARY, width=2, dash="dot"),
        hovertemplate="day +%{x}<br>median %{y:.2f}<extra></extra>",
    ))

    # Vertical "now" marker
    fig.add_vline(x=0, line=dict(color=T.TEXT_FAINT, width=1, dash="dash"))

    fig.update_layout(**T.chart_layout(
        title=f"{ticker} — historical + {horizon}d Monte Carlo fan",
        height=420,
        xaxis=dict(title="Trading days from today",
                    gridcolor=T.BORDER, linecolor=T.BORDER,
                    tickfont=dict(color=T.TEXT_MUTED)),
        yaxis=dict(title="Price",
                    gridcolor=T.BORDER, linecolor=T.BORDER,
                    tickfont=dict(color=T.TEXT_MUTED)),
        hovermode="x unified",
    ))
    return fig


def vol_cone_chart(returns_pct: pd.Series, forecast_annual_pct: np.ndarray,
                    ticker: str) -> go.Figure:
    """Annualized volatility forecast over the horizon, with historical
    realized-vol context bands (21/63/252-day rolling).

    `returns_pct` is the same percent-scaled log-return series fed to
    fit_garch. `forecast_annual_pct` is the annualized vol per horizon
    step from VolForecast.
    """
    # Rolling realized vol bands (annualized)
    from analysis.risk_garch import rolling_realized_vol_pct
    rvol_21 = rolling_realized_vol_pct(returns_pct, window=21).dropna()
    rvol_63 = rolling_realized_vol_pct(returns_pct, window=63).dropna()
    rvol_252 = rolling_realized_vol_pct(returns_pct, window=252).dropna()

    # Stats over realized history (used as reference lines)
    rvol_median = float(rvol_21.median())
    rvol_p10 = float(rvol_21.quantile(0.10))
    rvol_p90 = float(rvol_21.quantile(0.90))

    horizon = len(forecast_annual_pct)
    x = list(range(1, horizon + 1))

    fig = go.Figure()

    # Reference bands as horizontal lines (median + p10/p90 of 21d rolling realized vol)
    fig.add_hrect(y0=rvol_p10, y1=rvol_p90,
                   fillcolor=_hex_to_rgba(T.INFO, 0.08),
                   layer="below", line_width=0)
    fig.add_hline(y=rvol_median, line=dict(color=T.INFO, width=1, dash="dot"),
                   annotation_text=f"Hist. median 21d realized: {rvol_median:.1f}%",
                   annotation_position="bottom right",
                   annotation_font=dict(color=T.INFO, size=10))

    # GARCH forecast curve
    fig.add_trace(go.Scatter(
        x=x, y=forecast_annual_pct,
        mode="lines+markers",
        name="GARCH forecast vol",
        line=dict(color=T.PRIMARY, width=2.5),
        marker=dict(size=6, color=T.PRIMARY),
        hovertemplate="day +%{x}<br>annualized vol %{y:.2f}%<extra></extra>",
    ))

    fig.update_layout(**T.chart_layout(
        title=f"{ticker} — forecast vs. historical realized volatility (annualized)",
        height=300,
        xaxis=dict(title="Trading days ahead",
                    gridcolor=T.BORDER, linecolor=T.BORDER,
                    tickfont=dict(color=T.TEXT_MUTED)),
        yaxis=dict(title="Annualized vol (%)",
                    gridcolor=T.BORDER, linecolor=T.BORDER,
                    tickfont=dict(color=T.TEXT_MUTED)),
    ))
    return fig


def drawdown_histogram(max_drawdowns: np.ndarray, ticker: str) -> go.Figure:
    """Distribution of max drawdown per simulated path (always <= 0)."""
    # Values are negative; convert to absolute percent for readability
    dd_pct = -max_drawdowns * 100  # positive percent of loss

    p50 = float(np.median(dd_pct))
    p95 = float(np.quantile(dd_pct, 0.95))
    p99 = float(np.quantile(dd_pct, 0.99))

    fig = go.Figure(go.Histogram(
        x=dd_pct,
        nbinsx=40,
        marker_color=_hex_to_rgba(T.DANGER, 0.45),
        marker_line=dict(color=T.DANGER, width=0.5),
        name="Per-path max drawdown",
        hovertemplate="drawdown %{x:.1f}%<br>%{y} paths<extra></extra>",
    ))
    fig.add_vline(x=p50, line=dict(color=T.PRIMARY, width=2, dash="dot"),
                   annotation_text=f"median {p50:.1f}%",
                   annotation_position="top right",
                   annotation_font=dict(color=T.PRIMARY, size=10))
    fig.add_vline(x=p95, line=dict(color=T.DANGER, width=1.5, dash="dash"),
                   annotation_text=f"p95 {p95:.1f}%",
                   annotation_position="top right",
                   annotation_font=dict(color=T.DANGER, size=10))

    fig.update_layout(**T.chart_layout(
        title=f"{ticker} — max-drawdown distribution across simulated paths",
        height=260,
        xaxis=dict(title="Max drawdown (% loss from running peak)",
                    gridcolor=T.BORDER, linecolor=T.BORDER,
                    tickfont=dict(color=T.TEXT_MUTED)),
        yaxis=dict(title="Number of paths",
                    gridcolor=T.BORDER, linecolor=T.BORDER,
                    tickfont=dict(color=T.TEXT_MUTED)),
        showlegend=False,
        bargap=0.05,
    ))
    return fig
