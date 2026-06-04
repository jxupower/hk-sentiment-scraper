"""Portfolio Rebalancer tab callbacks.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
from dash import Input, Output, State, html
from dash.exceptions import PreventUpdate
import dash_bootstrap_components as dbc

from dashboard import theme as T
from dashboard.portfolio_charts import (
    backtest_equity_chart, efficient_frontier_chart, weights_bar_chart,
)


def register_portfolio_callbacks(app, db_path: str):

    # ----- Cap slider label (cosmetic) -----
    @app.callback(
        Output("portfolio-cap-label", "children"),
        Input("portfolio-cap-slider", "value"),
    )
    def update_cap_label(value):
        return f"{value}%"

    # ----- Add a blank row to the holdings table -----
    @app.callback(
        Output("portfolio-holdings-table", "data", allow_duplicate=True),
        Input("portfolio-add-row-btn", "n_clicks"),
        State("portfolio-holdings-table", "data"),
        prevent_initial_call=True,
    )
    def add_holdings_row(_n, current):
        return (current or []) + [{"ticker": "", "shares": 0}]

    # ----- Main compute callback -----
    @app.callback(
        Output("portfolio-placeholder", "style"),
        Output("portfolio-content", "style"),
        Output("portfolio-sharpe-status", "children"),
        Output("portfolio-sharpe-current", "children"),
        Output("portfolio-sharpe-full", "children"),
        Output("portfolio-sharpe-delta-rebal", "children"),
        Output("portfolio-sharpe-delta-add", "children"),
        Output("portfolio-weights-chart", "figure"),
        Output("portfolio-frontier-chart", "figure"),
        Output("portfolio-backtest-chart", "figure"),
        Output("portfolio-backtest-table", "children"),
        Output("portfolio-candidate-table", "children"),
        Output("portfolio-trade-list", "children"),
        Output("portfolio-diagnostics", "children"),
        Output("portfolio-compute-status", "children"),
        Input("portfolio-compute-btn", "n_clicks"),
        State("portfolio-holdings-table", "data"),
        State("portfolio-lookback", "value"),
        State("portfolio-rebalance", "value"),
        State("portfolio-cap-slider", "value"),
        State("portfolio-rf", "value"),
        prevent_initial_call=True,
    )
    def compute_portfolio(_n, table_data, lookback, rebalance, cap_pct, rf_pct):
        if not table_data:
            return _error_state("Add holdings first.")

        from analysis._portfolio_cache import get_or_build
        from storage.database import Database

        db = Database(db_path)
        weight_cap = (cap_pct or 30) / 100.0
        rf = (rf_pct or 0.0) / 100.0

        try:
            bundle = get_or_build(
                holdings=table_data,
                lookback_days=int(lookback or 0),
                rebalance_days=int(rebalance or 21),
                weight_cap=weight_cap,
                rf=rf,
                db=db,
            )
        except ValueError as e:
            return _error_state(str(e))
        except Exception as e:
            return _error_state(f"Compute failed: {type(e).__name__}: {e}")

        # Hero Sharpe numbers
        sq = bundle.m_status_quo["sharpe"]
        cu = bundle.m_current_optimal["sharpe"]
        fu = bundle.m_full_optimal["sharpe"]
        hero_status = f"{sq:.3f}"
        hero_current = f"{cu:.3f}"
        hero_full = f"{fu:.3f}"

        delta_rebal = _delta_text("Δ from rebalancing within current",
                                    cu - sq)
        delta_add = _delta_text("Δ from adding candidates",
                                  fu - cu)

        # Charts
        weights_fig = weights_bar_chart(bundle.tickers, bundle.w_status_quo,
                                          bundle.w_full_optimal)
        frontier_fig = efficient_frontier_chart(bundle.frontier, bundle.mu_sigma,
                                                  bundle.m_status_quo,
                                                  bundle.m_current_optimal,
                                                  bundle.m_full_optimal)
        backtest_fig = backtest_equity_chart(bundle.backtest)

        # Tables
        backtest_table = _build_backtest_table(bundle.backtest)
        candidate_table = _build_candidate_table(bundle)
        trade_list = _build_trade_list(bundle)
        diagnostics = _build_diagnostics(bundle)

        status = (f"Built {len(bundle.tickers)} tickers, {bundle.mu_sigma.n_obs} "
                  f"returns, data through {bundle.last_price_date}")

        return (
            {"display": "none"}, {"display": "block"},
            hero_status, hero_current, hero_full,
            delta_rebal, delta_add,
            weights_fig, frontier_fig, backtest_fig,
            backtest_table, candidate_table, trade_list, diagnostics,
            status,
        )


# ============== Output builders ==============

def _error_state(msg: str):
    """14-tuple of outputs + a status string for the error path."""
    err = html.Div(html.P(msg, className="text-danger small mb-0"))
    return (
        {"display": "block"}, {"display": "none"},
        "—", "—", "—", "", "", {}, {}, {}, err, err, err, err,
        msg,
    )


def _delta_text(label: str, delta: float):
    color = T.SUCCESS if delta > 0.001 else (T.DANGER if delta < -0.001 else T.TEXT_MUTED)
    sym = "+" if delta >= 0 else ""
    return html.Div([
        html.Span(f"{label}: ", style={"color": T.TEXT_MUTED}),
        html.Span(f"{sym}{delta:.3f}", style={"color": color, "fontWeight": "700"}),
    ])


def _build_backtest_table(backtest: dict) -> html.Table:
    """Per-strategy: total return, ann return, ann vol, Sharpe, max DD, turnover."""
    def fmt_pct(v, signed=False):
        if v is None: return "—"
        s = "+" if signed and v >= 0 else ""
        return f"{s}{v*100:.1f}%"
    def fmt_sharpe(v):
        if v is None: return "—"
        return f"{v:+.3f}"

    rows = [html.Tr([html.Th(c) for c in
                       ["Strategy", "Total ret", "Ann ret", "Ann vol",
                        "Sharpe", "Max DD", "Turnover (/rebal)"]],
                      className="small text-muted")]
    for key, s in backtest.items():
        sharpe_color = T.SUCCESS if (s.sharpe or 0) > 0 else T.DANGER
        rows.append(html.Tr([
            html.Td(s.name, style={"fontWeight": "600"}),
            html.Td(fmt_pct(s.total_return, signed=True)),
            html.Td(fmt_pct(s.annualised_return, signed=True)),
            html.Td(fmt_pct(s.annualised_vol)),
            html.Td(html.Span(fmt_sharpe(s.sharpe),
                                style={"color": sharpe_color, "fontWeight": "700"})),
            html.Td(fmt_pct(s.max_drawdown, signed=True)),
            html.Td(fmt_pct(s.turnover) if s.turnover > 0 else "—"),
        ]))
    return html.Table(rows, className="table table-sm w-100 small")


def _build_candidate_table(bundle) -> html.Div:
    """Marginal value per candidate, sorted descending."""
    if not bundle.candidate_tickers:
        return html.P("No candidates (all rows have shares > 0).",
                       className="text-muted small")

    items = [(t, bundle.candidate_marginal.get(t, 0.0))
              for t in bundle.candidate_tickers]
    items.sort(key=lambda x: -x[1])

    rows = [html.Tr([html.Th(c) for c in
                       ["Candidate", "Δ Sharpe (full - without)",
                        "Optimal weight", "Verdict"]],
                      className="small text-muted")]
    for t, dv in items:
        idx = bundle.tickers.index(t)
        opt_w = bundle.w_full_optimal[idx]
        verdict = ("✓ valuable add" if dv > 0.05
                   else ("~ marginal" if dv > 0.01 else "✗ negligible"))
        v_color = T.SUCCESS if dv > 0.05 else (T.WARNING if dv > 0.01 else T.TEXT_MUTED)
        rows.append(html.Tr([
            html.Td(t, style={"fontWeight": "600"}),
            html.Td(f"+{dv:.3f}" if dv > 0 else f"{dv:.3f}",
                     style={"color": v_color, "fontWeight": "600"}),
            html.Td(f"{opt_w*100:.1f}%"),
            html.Td(verdict, style={"color": v_color}),
        ]))
    return html.Table(rows, className="table table-sm w-100 small")


def _build_trade_list(bundle) -> html.Div:
    """What to buy/sell to move from status-quo to full-optimal weights."""
    if bundle.w_status_quo.sum() == 0:
        return html.P("No current holdings — can't compute trade list.",
                       className="text-muted small")

    # Express in dollar terms using latest prices, scaled to current portfolio value
    total_value = sum(
        (h.get("shares") or 0) * bundle.latest_prices.get(h["ticker"], 0.0)
        for h in bundle.holdings
    )
    if total_value <= 0:
        return html.P("Total portfolio value is zero — can't compute trade list.",
                       className="text-muted small")

    rows = [html.Tr([html.Th(c) for c in
                       ["Ticker", "Current shares", "Target shares",
                        "Δ shares", "Δ HKD"]],
                      className="small text-muted")]
    holdings_by_ticker = {h["ticker"]: float(h.get("shares") or 0) for h in bundle.holdings}
    for i, t in enumerate(bundle.tickers):
        current_shares = holdings_by_ticker.get(t, 0.0)
        price = bundle.latest_prices.get(t, 0.0)
        target_value = bundle.w_full_optimal[i] * total_value
        target_shares = target_value / price if price > 0 else 0.0
        delta = target_shares - current_shares
        delta_hkd = delta * price
        color = T.SUCCESS if delta > 0 else (T.DANGER if delta < 0 else T.TEXT_MUTED)
        rows.append(html.Tr([
            html.Td(t, style={"fontWeight": "600"}),
            html.Td(f"{current_shares:,.0f}"),
            html.Td(f"{target_shares:,.0f}"),
            html.Td(html.Span(f"{delta:+,.0f}",
                                style={"color": color, "fontWeight": "600"})),
            html.Td(html.Span(f"{delta_hkd:+,.0f}",
                                style={"color": color})),
        ]))
    return html.Table(rows, className="table table-sm w-100 small")


def _build_diagnostics(bundle) -> html.Div:
    """Estimation noise + cap info + total portfolio value."""
    ms = bundle.mu_sigma
    total_value = sum(
        (h.get("shares") or 0) * bundle.latest_prices.get(h["ticker"], 0.0)
        for h in bundle.holdings
    )

    rows = [html.Tr([html.Th(c) for c in
                       ["Ticker", "Ann μ", "± SE", "Ann σ",
                        "Status-quo w", "Full-optimal w"]],
                      className="small text-muted")]
    for i, t in enumerate(ms.tickers):
        vol_i = float(np.sqrt(ms.sigma[i, i]))
        rows.append(html.Tr([
            html.Td(t, style={"fontWeight": "600"}),
            html.Td(f"{ms.mu[i]*100:+.1f}%"),
            html.Td(f"±{ms.mu_se[i]*100:.1f}%",
                     style={"color": T.TEXT_MUTED}),
            html.Td(f"{vol_i*100:.1f}%"),
            html.Td(f"{bundle.w_status_quo[i]*100:.1f}%"),
            html.Td(f"{bundle.w_full_optimal[i]*100:.1f}%",
                     style={"color": T.PRIMARY, "fontWeight": "600"}),
        ]))
    table = html.Table(rows, className="table table-sm w-100 small")

    meta = html.Div([
        html.Span(f"T = {ms.n_obs} obs · ", style={"color": T.TEXT_MUTED}),
        html.Span(f"Ledoit-Wolf shrinkage α = {ms.shrinkage:.3f} · ",
                   style={"color": T.TEXT_MUTED}),
        html.Span(f"Total portfolio value: HKD {total_value:,.0f}",
                   style={"color": T.TEXT, "fontWeight": "600"}),
    ], className="small mb-2")

    return html.Div([meta, table])
