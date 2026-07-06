"""Chart factories for the Portfolio Rebalancer tab. Pure functions of
the bundle (or its sub-components) -> Plotly figures.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from dashboard import theme as T
from dashboard.i18n import T as I


def _hex_to_rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def weights_bar_chart(tickers: list[str], w_current: np.ndarray,
                       w_full_optimal: np.ndarray,
                       lang: str = "en") -> go.Figure:
    """Grouped horizontal bar chart: current vs optimal weight per ticker."""
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=w_current * 100, y=tickers, orientation="h",
        name=I("chart.portfolio.status_quo", lang),
        marker_color=T.TEXT_MUTED,
        text=[f"{w*100:.1f}%" for w in w_current],
        textposition="outside",
        hovertemplate="%{y}<br>%{x:.1f}%<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        x=w_full_optimal * 100, y=tickers, orientation="h",
        name=I("chart.portfolio.optimal", lang),
        marker_color=T.PRIMARY,
        text=[f"{w*100:.1f}%" for w in w_full_optimal],
        textposition="outside",
        hovertemplate="%{y}<br>%{x:.1f}%<extra></extra>",
    ))
    fig.update_layout(**T.chart_layout(
        title=I("chart.portfolio.weights_title", lang),
        height=max(280, 60 + 38 * len(tickers)),
        barmode="group",
        xaxis=dict(title=I("chart.portfolio.weight_axis", lang),
                    gridcolor=T.BORDER,
                    linecolor=T.BORDER, tickfont=dict(color=T.TEXT_MUTED)),
        yaxis=dict(gridcolor=T.BORDER, linecolor=T.BORDER,
                    tickfont=dict(color=T.TEXT_MUTED), autorange="reversed"),
        margin=dict(t=50, b=40, l=110, r=80),
    ))
    return fig


def efficient_frontier_chart(frontier, mu_sigma,
                                m_status_quo: dict, m_current: dict,
                                m_full: dict,
                                lang: str = "en") -> go.Figure:
    """The classic MPT visual: vol on x, expected return on y, the
    frontier curve, the tangency point, the status quo, current-only,
    and each individual ticker plotted as a dot."""
    fig = go.Figure()

    # Frontier curve
    if frontier:
        vols = [p.vol * 100 for p in frontier]
        rets = [p.realised_return * 100 for p in frontier]
        fig.add_trace(go.Scatter(
            x=vols, y=rets, mode="lines",
            name=I("portfolio.efficient_frontier", lang),
            line=dict(color=T.PRIMARY, width=2.5),
            hovertemplate="vol %{x:.1f}%<br>ret %{y:+.1f}%<extra></extra>",
        ))

    # Individual tickers
    tickers = mu_sigma.tickers
    vols_i = np.sqrt(np.diag(mu_sigma.sigma)) * 100
    rets_i = mu_sigma.mu * 100
    fig.add_trace(go.Scatter(
        x=vols_i, y=rets_i, mode="markers+text",
        name=("个股" if lang == "zh" else "Individual stocks"),
        marker=dict(size=10, color=T.TEXT_MUTED,
                     line=dict(color=T.BORDER_STRONG, width=1)),
        text=tickers, textposition="top center",
        textfont=dict(size=10, color=T.TEXT_MUTED),
        hovertemplate="%{text}<br>vol %{x:.1f}%<br>ret %{y:+.1f}%<extra></extra>",
    ))

    # Status-quo dot
    if m_status_quo["sharpe"] != 0.0 or m_status_quo["vol"] > 0:
        fig.add_trace(go.Scatter(
            x=[m_status_quo["vol"] * 100], y=[m_status_quo["return"] * 100],
            mode="markers+text",
            name=I("chart.portfolio.status_quo", lang),
            text=[I("chart.portfolio.status_quo_wf", lang)],
            textposition="bottom center",
            marker=dict(size=14, color=T.WARNING, symbol="square",
                         line=dict(color=T.WARNING, width=2)),
            hovertemplate="vol %{x:.1f}%<br>ret %{y:+.1f}%<extra></extra>",
        ))

    # Current-only optimum
    if m_current["sharpe"] != 0.0:
        fig.add_trace(go.Scatter(
            x=[m_current["vol"] * 100], y=[m_current["return"] * 100],
            mode="markers+text",
            name=I("chart.portfolio.current_only", lang),
            text=[I("chart.portfolio.current_only", lang)],
            textposition="top center",
            marker=dict(size=14, color=T.INFO, symbol="diamond",
                         line=dict(color=T.INFO, width=2)),
            hovertemplate="vol %{x:.1f}%<br>ret %{y:+.1f}%<extra></extra>",
        ))

    # Tangency portfolio (full-universe optimum)
    if m_full["sharpe"] != 0.0:
        fig.add_trace(go.Scatter(
            x=[m_full["vol"] * 100], y=[m_full["return"] * 100],
            mode="markers+text",
            name=I("chart.portfolio.optimal", lang),
            text=[I("chart.portfolio.optimal", lang)], textposition="top right",
            marker=dict(size=16, color=T.SUCCESS, symbol="star",
                         line=dict(color=T.SUCCESS, width=2)),
            hovertemplate="vol %{x:.1f}%<br>ret %{y:+.1f}%<extra></extra>",
        ))

    fig.update_layout(**T.chart_layout(
        title=I("chart.portfolio.frontier_title", lang),
        height=420,
        xaxis=dict(title=I("chart.portfolio.frontier_vol", lang),
                    gridcolor=T.BORDER, linecolor=T.BORDER,
                    tickfont=dict(color=T.TEXT_MUTED)),
        yaxis=dict(title=I("chart.portfolio.frontier_ret", lang),
                    gridcolor=T.BORDER, linecolor=T.BORDER,
                    tickfont=dict(color=T.TEXT_MUTED)),
    ))
    return fig


def backtest_equity_chart(strategies: dict, lang: str = "en") -> go.Figure:
    """Line chart of cumulative equity curves for each backtest strategy."""
    fig = go.Figure()
    palette = [T.PRIMARY, T.INFO, T.TEXT_MUTED]
    # Localise each strategy's display name via a small key map. Falls
    # back to strat.name (English) when a strategy key isn't recognised.
    STRAT_NAMES = {
        "status_quo": I("chart.portfolio.status_quo_wf", lang),
        "current_optimum": I("chart.portfolio.current_only", lang),
        "max_sharpe": I("chart.portfolio.optimal_wf", lang),
    }
    for (key, strat), color in zip(strategies.items(), palette):
        r = strat.daily_returns
        if r.empty:
            continue
        equity = (1.0 + r).cumprod() - 1.0
        display_name = STRAT_NAMES.get(key, strat.name)
        fig.add_trace(go.Scatter(
            x=equity.index, y=equity.values * 100,
            mode="lines",
            name=display_name,
            line=dict(color=color,
                       width=2.5 if key == "max_sharpe" else 1.5,
                       dash="solid" if key == "max_sharpe" else "dot"),
            hovertemplate=f"{display_name}<br>"
                          "%{x|%Y-%m-%d}<br>%{y:+.1f}%<extra></extra>",
        ))
    fig.add_hline(y=0, line=dict(color=T.TEXT_FAINT, width=1, dash="dash"))
    fig.update_layout(**T.chart_layout(
        title=I("chart.portfolio.walkforward", lang),
        height=320,
        xaxis=dict(gridcolor=T.BORDER, linecolor=T.BORDER,
                    tickfont=dict(color=T.TEXT_MUTED)),
        yaxis=dict(title=I("chart.portfolio.cumret_axis", lang),
                    gridcolor=T.BORDER, linecolor=T.BORDER,
                    tickfont=dict(color=T.TEXT_MUTED)),
        hovermode="x unified",
    ))
    return fig
