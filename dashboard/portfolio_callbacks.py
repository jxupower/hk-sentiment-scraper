"""Portfolio Rebalancer tab callbacks.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import dash
from dash import Input, Output, State, html, no_update
from dash.exceptions import PreventUpdate
import dash_bootstrap_components as dbc

from dashboard import theme as T
from dashboard.portfolio_charts import (
    backtest_equity_chart, efficient_frontier_chart, weights_bar_chart,
)


def register_portfolio_callbacks(app, db_path: str):
    # ----- i18n: flip every translatable label on language change -----
    @app.callback(
        Output("portfolio-saved-title", "children"),
        Output("portfolio-label-name", "children"),
        Output("portfolio-label-existing", "children"),
        Output("portfolio-load-btn", "children"),
        Output("portfolio-delete-btn", "children"),
        Output("portfolio-holdings-title", "children"),
        Output("portfolio-add-row-btn", "children"),
        Output("portfolio-params-title", "children"),
        Output("portfolio-label-lookback", "children"),
        Output("portfolio-label-rebal", "children"),
        Output("portfolio-label-weight-cap", "children"),
        Output("portfolio-label-rf", "children"),
        Output("portfolio-compute-btn", "children"),
        Output("portfolio-holdings-table", "columns"),
        Input("user-language", "data"),
    )
    def i18n_portfolio(lang):
        from dashboard.i18n import T as I
        lang = lang or "en"
        cols = [
            {"name": I("backtest.col.ticker", lang), "id": "ticker",
             "type": "text", "editable": True},
            {"name": I("backtest.col.shares", lang), "id": "shares",
             "type": "numeric", "editable": True},
        ]
        return (
            I("portfolio.saved_portfolios", lang),
            I("portfolio.label.name", lang),
            I("portfolio.saved_portfolios", lang),
            I("common.load", lang),
            I("portfolio.btn.delete", lang),
            I("portfolio.holdings_table", lang),
            I("portfolio.btn.add_row", lang),
            "Parameters" if lang == "en" else "参数",
            I("portfolio.label.lookback", lang),
            I("portfolio.label.rebal", lang),
            I("portfolio.label.weight_cap", lang) + " ",
            I("portfolio.label.rf", lang),
            I("portfolio.btn.compute", lang),
            cols,
        )


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

    # ----- Saved portfolios: dropdown options refresh -----
    # Triggered on initial tab load (n_intervals=0 fires once via the input
    # below being non-None) and after any save/delete operation via the
    # `portfolio-save-status` text changing.
    @app.callback(
        Output("portfolio-saved-dropdown", "options"),
        Input("portfolio-save-status", "children"),
        Input("portfolio-compute-btn", "n_clicks"),
    )
    def refresh_saved_dropdown(_status, _n):
        return _build_saved_options()

    # ----- Save STATUS-QUO portfolio (raw shares only) -----
    # Always available — doesn't require a Compute first. Persists the
    # holdings table verbatim and materialises @NAME, the constant-share
    # buy-and-hold index. Preserves any previously-saved optimal_weights
    # under the same name so re-saving status-quo doesn't wipe @NAME$OPT.
    @app.callback(
        Output("portfolio-save-status", "children", allow_duplicate=True),
        Input("portfolio-save-status-btn", "n_clicks"),
        State("portfolio-name-input", "value"),
        State("portfolio-holdings-table", "data"),
        prevent_initial_call=True,
    )
    def save_status_quo(_n, raw_name, table_data):
        from analysis.portfolio_synth import (
            is_valid_name, normalise_name, rebuild_and_upsert,
        )
        from storage.cloud_db import available
        from storage.cloud_repository import CloudPortfoliosRepository
        from storage.database import Database

        name = normalise_name(raw_name or "")
        if not name:
            return _status_error("Enter a name (UPPERCASE / digits / _).")
        if not is_valid_name(name):
            return _status_error(
                f"Invalid name {name!r} — use only A-Z, 0-9, _ (1-32 chars).")
        if not available():
            return _status_error(
                "Cloud DB not configured — set USE_CLOUD_DB=true in .env to save.")

        holdings = _clean_table_data(table_data)
        if not holdings:
            return _status_error("Add holdings before saving.")

        # Preserve any existing optimal_weights for this name — saving the
        # status-quo shouldn't blow away a previously-saved @NAME$OPT series.
        repo = CloudPortfoliosRepository()
        existing = None
        try:
            existing = repo.get_portfolio(name)
        except Exception:
            pass
        preserved_optimal = (existing or {}).get("optimal_weights") if existing else None

        try:
            repo.save_portfolio(name, holdings, preserved_optimal)
        except Exception as e:
            return _status_error(f"Save failed: {type(e).__name__}: {e}")

        try:
            db = Database(db_path)
            summary = rebuild_and_upsert(name, {
                "holdings": holdings,
                "optimal_weights": preserved_optimal,
            }, db)
        except Exception as e:
            return _status_ok(
                f"Saved @{name} (warning: synthetic price rebuild failed: {e})")
        preserved_msg = (" (preserved @OPT)" if preserved_optimal else "")
        return _status_ok(
            f"Saved @{name} status-quo ({summary['status_quo_rows']} rows)"
            f"{preserved_msg}.")

    # ----- Save OPTIMISED portfolio (uses latest Compute bundle) -----
    # Persists holdings + optimal_weights snapshot. Requires a recent
    # Compute click whose ticker set matches the current holdings table;
    # otherwise the snapshot is stale and we refuse.
    @app.callback(
        Output("portfolio-save-status", "children", allow_duplicate=True),
        Input("portfolio-save-optimal-btn", "n_clicks"),
        State("portfolio-name-input", "value"),
        State("portfolio-holdings-table", "data"),
        State("portfolio-latest-optimal", "data"),
        prevent_initial_call=True,
    )
    def save_optimal(_n, raw_name, table_data, latest_optimal):
        from analysis.portfolio_synth import (
            is_valid_name, normalise_name, rebuild_and_upsert,
        )
        from storage.cloud_db import available
        from storage.cloud_repository import CloudPortfoliosRepository
        from storage.database import Database

        name = normalise_name(raw_name or "")
        if not name:
            return _status_error("Enter a name (UPPERCASE / digits / _).")
        if not is_valid_name(name):
            return _status_error(f"Invalid name {name!r} — use A-Z, 0-9, _.")
        if not available():
            return _status_error("Cloud DB not configured.")

        if not latest_optimal:
            return _status_error(
                "Click Compute first to generate optimal weights, then Save.")

        holdings = _clean_table_data(table_data)
        if not holdings:
            return _status_error("Add holdings before saving.")

        table_tickers = [h["ticker"] for h in holdings]
        opt_tickers = list(latest_optimal.get("tickers") or [])
        if opt_tickers != table_tickers:
            return _status_error(
                "Holdings table changed since last Compute. Click Compute "
                "again, then Save optimised.")

        optimal_weights = [
            {"ticker": t, "weight": float(w)}
            for t, w in zip(opt_tickers, latest_optimal["weights"])
        ]
        repo = CloudPortfoliosRepository()
        try:
            repo.save_portfolio(
                name, holdings, optimal_weights,
                rf=latest_optimal.get("rf"),
                weight_cap=latest_optimal.get("weight_cap"),
                lookback_days=latest_optimal.get("lookback_days"),
            )
        except Exception as e:
            return _status_error(f"Save failed: {type(e).__name__}: {e}")

        try:
            db = Database(db_path)
            summary = rebuild_and_upsert(name, {
                "holdings": holdings,
                "optimal_weights": optimal_weights,
            }, db)
        except Exception as e:
            return _status_ok(
                f"Saved @{name}$OPT (warning: rebuild failed: {e})")
        return _status_ok(
            f"Saved @{name} status-quo ({summary['status_quo_rows']} rows) "
            f"AND @{name}$OPT ({summary['optimal_rows']} rows).")

    # ----- Load a saved portfolio into the holdings table -----
    @app.callback(
        Output("portfolio-holdings-table", "data", allow_duplicate=True),
        Output("portfolio-name-input", "value"),
        Output("portfolio-save-status", "children", allow_duplicate=True),
        Output("portfolio-latest-optimal", "data", allow_duplicate=True),
        Input("portfolio-load-btn", "n_clicks"),
        State("portfolio-saved-dropdown", "value"),
        prevent_initial_call=True,
    )
    def load_portfolio(_n, selected_name):
        from storage.cloud_db import available
        from storage.cloud_repository import CloudPortfoliosRepository

        if not selected_name:
            return no_update, no_update, _status_error(
                "Pick a portfolio from the dropdown first."), no_update
        if not available():
            return no_update, no_update, _status_error(
                "Cloud DB not configured."), no_update

        try:
            row = CloudPortfoliosRepository().get_portfolio(selected_name)
        except Exception as e:
            return no_update, no_update, _status_error(
                f"Load failed: {e}"), no_update
        if not row:
            return no_update, no_update, _status_error(
                f"No saved portfolio named {selected_name!r}."), no_update

        new_table_data = [
            {"ticker": h["ticker"], "shares": h.get("shares", 0)}
            for h in (row.get("holdings") or [])
        ]
        return (
            new_table_data,
            selected_name,
            _status_ok(f"Loaded @{selected_name} "
                        f"({len(new_table_data)} holdings)."),
            None,  # clear stale optimal-store; user can recompute
        )

    # ----- Delete a saved portfolio -----
    @app.callback(
        Output("portfolio-save-status", "children", allow_duplicate=True),
        Input("portfolio-delete-btn", "n_clicks"),
        State("portfolio-saved-dropdown", "value"),
        prevent_initial_call=True,
    )
    def delete_portfolio(_n, selected_name):
        from analysis.portfolio_synth import delete_synthetic_rows
        from storage.cloud_db import available
        from storage.cloud_repository import CloudPortfoliosRepository

        if not selected_name:
            return _status_error("Pick a portfolio from the dropdown first.")
        if not available():
            return _status_error("Cloud DB not configured.")

        try:
            removed = CloudPortfoliosRepository().delete_portfolio(selected_name)
            if not removed:
                return _status_error(
                    f"No saved portfolio named {selected_name!r}.")
            price_rows_removed = delete_synthetic_rows(selected_name)
        except Exception as e:
            return _status_error(f"Delete failed: {e}")

        return _status_ok(
            f"Deleted @{selected_name} (+ {price_rows_removed} synthetic "
            "price rows).")

    # ----- Cross-tab handoff: load a portfolio when the Backtest tab
    # writes one to `cross-tab-nav.data`. Mirrors the existing Screener →
    # Research handoff pattern. Refreshes the dropdown options so the
    # newly-saved name appears, then populates the holdings table + name
    # input and emits a status pill so the user sees what happened.
    @app.callback(
        Output("portfolio-saved-dropdown", "options", allow_duplicate=True),
        Output("portfolio-saved-dropdown", "value", allow_duplicate=True),
        Output("portfolio-name-input", "value", allow_duplicate=True),
        Output("portfolio-holdings-table", "data", allow_duplicate=True),
        Output("portfolio-rf", "value", allow_duplicate=True),
        Output("portfolio-save-status", "children", allow_duplicate=True),
        Output("portfolio-latest-optimal", "data", allow_duplicate=True),
        Input("cross-tab-nav", "data"),
        prevent_initial_call=True,
    )
    def load_portfolio_from_nav(nav_data):
        from storage.cloud_db import available
        from storage.cloud_repository import CloudPortfoliosRepository

        if not nav_data or nav_data.get("tab") != "tab-portfolio":
            raise PreventUpdate
        portfolio_name = nav_data.get("portfolio")
        if not portfolio_name:
            raise PreventUpdate
        if not available():
            return (no_update, no_update, no_update, no_update, no_update,
                     _status_error("Cloud DB not configured."), no_update)

        try:
            row = CloudPortfoliosRepository().get_portfolio(portfolio_name)
        except Exception as e:
            return (no_update, no_update, no_update, no_update, no_update,
                     _status_error(f"Cross-tab load failed: {e}"), no_update)
        if not row:
            return (no_update, no_update, no_update, no_update, no_update,
                     _status_error(f"No saved portfolio named "
                                    f"{portfolio_name!r}."), no_update)

        new_table_data = [
            {"ticker": h["ticker"], "shares": h.get("shares", 0)}
            for h in (row.get("holdings") or [])
        ]
        rf_pct = (row.get("rf") or 0) * 100   # stored as decimal; UI uses %
        return (
            _build_saved_options(),
            portfolio_name,
            portfolio_name,
            new_table_data,
            rf_pct if rf_pct > 0 else no_update,
            _status_ok(f"Loaded @{portfolio_name} from Backtest tab "
                        f"({len(new_table_data)} holdings, rf = "
                        f"{rf_pct:.0f}%)."),
            None,
        )

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
        Output("portfolio-latest-optimal", "data"),
        Output("portfolio-cap-warning", "children"),
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

        # Snapshot the optimal weights so the Save button can persist them
        # alongside the raw holdings. We keep this as plain JSON-serialisable
        # dicts; no numpy in the dcc.Store payload.
        latest_optimal = {
            "tickers": list(bundle.tickers),
            "weights": [float(w) for w in bundle.w_full_optimal],
            "lookback_days": int(lookback or 0),
            "rebalance_days": int(rebalance or 21),
            "weight_cap": float(weight_cap),
            "rf": float(rf),
            "last_price_date": bundle.last_price_date,
        }

        cap_warning = _build_cap_warning(bundle, weight_cap, rf)

        return (
            {"display": "none"}, {"display": "block"},
            hero_status, hero_current, hero_full,
            delta_rebal, delta_add,
            weights_fig, frontier_fig, backtest_fig,
            backtest_table, candidate_table, trade_list, diagnostics,
            status, latest_optimal, cap_warning,
        )


# ============== Output builders ==============

def _error_state(msg: str):
    """17-tuple matching compute_portfolio's outputs."""
    err = html.Div(html.P(msg, className="text-danger small mb-0"))
    return (
        {"display": "block"}, {"display": "none"},
        "—", "—", "—", "", "", {}, {}, {}, err, err, err, err,
        msg, None, "",
    )


def _delta_text(label: str, delta: float):
    # Portfolio metric delta (e.g. Sharpe lift): positive return-direction
    # signal = red in CN/HK convention.
    color = T.PRICE_UP if delta > 0.001 else (T.PRICE_DOWN if delta < -0.001 else T.TEXT_MUTED)
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
        sharpe_color = T.PRICE_UP if (s.sharpe or 0) > 0 else T.PRICE_DOWN
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
        # Marginal Sharpe lift: positive = bullish "add this name" = red.
        v_color = T.PRICE_UP if dv > 0.05 else (T.WARNING if dv > 0.01 else T.TEXT_MUTED)
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
        # Trade list delta: BUY (positive shares Δ) = red, SELL = green.
        color = T.PRICE_UP if delta > 0 else (T.PRICE_DOWN if delta < 0 else T.TEXT_MUTED)
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


def _status_ok(msg: str) -> html.Span:
    return html.Span(msg, style={"color": T.SUCCESS, "fontWeight": "600"})


def _status_error(msg: str) -> html.Span:
    return html.Span(msg, style={"color": T.DANGER, "fontWeight": "600"})


def _build_cap_warning(bundle, weight_cap: float, rf: float):
    """Surface an alert when the per-asset cap is binding in a way that
    makes the three-Sharpe comparison non-apples-to-apples.

    The math: max-Sharpe over a feasible set is >= the Sharpe of any point
    in that set. The status-quo's Sharpe can exceed the capped optimum's
    only when status-quo lives OUTSIDE the feasible set — i.e. at least
    one status-quo weight exceeds the cap.

    We always show this warning when an infeasible holding exists, even
    if the optimum still beat status-quo this time — the comparison is
    constrained either way, and seeing the uncapped Sharpe alongside
    makes that explicit.
    """
    import numpy as np
    from analysis.portfolio_optimizer import (
        max_sharpe_portfolio, portfolio_metrics,
    )

    over_cap = [
        (t, float(w)) for t, w in zip(bundle.tickers, bundle.w_status_quo)
        if w > weight_cap + 1e-6
    ]
    if not over_cap:
        return ""

    # Compute the uncapped current-only optimum so the user sees what
    # the cap is actually costing them.
    current_tickers = bundle.current_tickers
    uncapped_msg = ""
    if len(current_tickers) >= 2:
        try:
            idx_current = [bundle.tickers.index(t) for t in current_tickers]
            mu_c = bundle.mu_sigma.mu[idx_current]
            sigma_c = bundle.mu_sigma.sigma[np.ix_(idx_current, idx_current)]
            w_uncapped = max_sharpe_portfolio(mu_c, sigma_c, rf=rf,
                                                weight_cap=1.0)
            s_uncapped = portfolio_metrics(w_uncapped, mu_c, sigma_c,
                                            rf=rf)["sharpe"]
            s_capped = bundle.m_current_optimal.get("sharpe", 0.0)
            uncapped_msg = (
                f" Uncapped current-only Sharpe would be "
                f"{s_uncapped:+.3f} (vs capped {s_capped:+.3f}) — the cap is "
                f"costing you {s_uncapped - s_capped:+.3f} of Sharpe."
            )
        except Exception:
            pass

    over_cap_text = ", ".join(
        f"{t} {w*100:.1f}%" for t, w in sorted(over_cap, key=lambda x: -x[1])
    )

    return dbc.Alert(
        [
            html.Span("⚠ ", style={"fontWeight": "800"}),
            html.Strong("Status-quo exceeds your per-asset cap. "),
            f"Cap is {weight_cap*100:.0f}%; over-cap holdings: ",
            html.Code(over_cap_text, style={"fontSize": "0.85em"}),
            ". The capped optimum is solving a tighter problem than your "
            "actual portfolio, so the current-only optimum can have a LOWER "
            "Sharpe than status-quo even at rf=0 — that's expected, not a bug.",
            html.Br(),
            html.Em(uncapped_msg),
        ],
        color="warning",
        className="small mb-0 mt-2 py-2",
        dismissable=False,
    )


def _clean_table_data(table_data) -> list[dict]:
    """Strip empty tickers; coerce shares to non-negative float."""
    out: list[dict] = []
    for row in table_data or []:
        t = (row.get("ticker") or "").strip().upper()
        if not t:
            continue
        try:
            s = max(0.0, float(row.get("shares") or 0))
        except (TypeError, ValueError):
            s = 0.0
        out.append({"ticker": t, "shares": s})
    return out


def _build_saved_options() -> list[dict]:
    """Pull the saved-portfolios list from Supabase and format for dropdown.
    Silent empty list on cloud-unavailable so the rest of the tab still works."""
    try:
        from storage.cloud_db import available
        if not available():
            return []
        from storage.cloud_repository import CloudPortfoliosRepository
        rows = CloudPortfoliosRepository().list_portfolios()
    except Exception:
        return []

    options: list[dict] = []
    for r in rows:
        name = r["name"]
        n_holdings = len(r.get("holdings") or [])
        has_opt = bool(r.get("optimal_weights"))
        suffix = " · @OPT available" if has_opt else ""
        options.append({
            "label": f"@{name} — {n_holdings} holdings{suffix}",
            "value": name,
        })
    return options


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
