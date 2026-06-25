"""Risk Forecast tab callbacks — handles the autocomplete and the main
render_risk callback that builds all 5 figures + 3 tables from one cache
lookup.
"""
from __future__ import annotations

import sqlite3
from typing import Optional  # noqa: F401 — used in func annotations after `from __future__ import annotations`

import dash
from dash import Input, Output, State, html
from dash.exceptions import PreventUpdate
import dash_bootstrap_components as dbc

from dashboard import theme as T
from dashboard.risk_charts import drawdown_histogram, fan_chart, vol_cone_chart
from dashboard.risk_layout import INDEX_OPTIONS, index_options_for_market


def register_risk_callbacks(app, db_path: str):
    # ----- i18n: flip every translatable label on language change -----
    @app.callback(
        Output("risk-label-ticker", "children"),
        Output("risk-label-window", "children"),
        Output("risk-label-horizon", "children"),
        Output("risk-load-btn", "children"),
        Output("risk-fan-title", "children"),
        Output("risk-volcone-title", "children"),
        Output("risk-var-title", "children"),
        Output("risk-prob-title", "children"),
        Output("risk-dd-title", "children"),
        Output("risk-alert-banner", "children"),
        Input("user-language", "data"),
        Input("user-market", "data"),
    )
    def i18n_risk(lang, market):
        from dashboard.i18n import T as I
        from dash import html
        lang = lang or "en"
        market = (market or "HK").upper()
        # Market-aware ticker label + banner — the "HK stock" / "US stock"
        # split is short enough to compute inline rather than burn 4 new
        # i18n keys on a single string.
        if market == "US":
            ticker_label = ("代码 (指数或美股)" if lang == "zh"
                              else "Ticker (index or US stock)")
            index_name = "S&P 500" if lang == "en" else "标普 500 指数"
        else:
            ticker_label = ("代码 (指数或港股)" if lang == "zh"
                              else "Ticker (index or HK stock)")
            index_name = ("恒生指数" if lang == "zh" else "Hang Seng index")
        banner = [
            html.Strong("Risk Forecast — GJR-GARCH(1,1) with Student-t innovations. "),
            f"Pick a stock or {index_name}, click Load. The fan chart shows "
            "5,000 Monte Carlo paths over the chosen horizon. ",
            html.Br(),
            html.Em("Honest gaps: GARCH is slow to adapt to regime breaks; "
                    "Monte Carlo with a fitted parametric model can't generate "
                    "tail events outside the fitted distribution; forecasts "
                    "beyond ~5 days are illustrative, not precise."),
        ]
        return (
            ticker_label,
            I("risk.label.window", lang),
            I("risk.label.horizon", lang),
            I("risk.btn.load", lang),
            I("risk.fan_chart", lang),
            I("risk.vol_cone", lang),
            I("risk.var_table", lang),
            I("risk.prob_table", lang),
            I("risk.drawdown_hist", lang),
            banner,
        )

    # Reset the selected ticker when the user flips markets — otherwise a
    # HK→US flip leaves them looking at ^HSI in the US universe (or vice
    # versa). Lands them on the right benchmark for the new market.
    @app.callback(
        Output("risk-ticker-select", "value"),
        Input("user-market", "data"),
        prevent_initial_call=True,
    )
    def reset_risk_ticker_on_market_change(market):
        return "^GSPC" if (market or "HK").upper() == "US" else "^HSI"


    # ----- Ticker autocomplete dropdown -----
    # Indices are always pinned to the top of the list. Stocks come from
    # securities (active universe). Mirrors the dashboard.stock_research_callbacks
    # pattern, including the "always include currently-selected value"
    # safeguard so a selection doesn't vanish on options refresh.
    @app.callback(
        Output("risk-ticker-select", "options"),
        Input("risk-ticker-select", "search_value"),
        Input("user-market", "data"),
        State("risk-ticker-select", "value"),
    )
    def populate_risk_ticker_options(search, market, current_value):
        market = (market or "HK").upper()
        # Market-aware index list pinned to the top (HK indices for HK,
        # US indices for US).
        market_indices = index_options_for_market(market)
        # Saved portfolios become first-class tickers in the Risk Forecast
        # dropdown — `@CORE` runs GARCH on the status-quo series, `@CORE$OPT`
        # runs it on the max-Sharpe optimal-weight series. These are pinned
        # right under the indices so the user can find them fast.
        portfolio_opts = _build_portfolio_options(search)
        # Sub-sector composite tickers (`&BANKS`, `&SEMICONDUCTORS_AND_EQUIPMENT`, …)
        # also surface in the dropdown so the Risk Forecast can fit GARCH on a
        # whole sub-sector. Pinned between portfolios and stocks.
        composite_opts = _build_subsector_options(db_path, search)

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            if search:
                rows = conn.execute("""
                    SELECT ticker, name FROM securities
                    WHERE is_active = 1 AND market = ? AND (
                        UPPER(ticker) LIKE UPPER(?) OR UPPER(name) LIKE UPPER(?)
                    )
                    ORDER BY (is_watchlist = 1) DESC, ticker
                    LIMIT 30
                """, (market, f"{search}%", f"%{search}%")).fetchall()
            else:
                rows = conn.execute("""
                    SELECT ticker, name FROM securities
                    WHERE is_active = 1 AND market = ? AND is_watchlist = 1
                    ORDER BY ticker LIMIT 30
                """, (market,)).fetchall()
            stock_opts = [{"label": f"{r['ticker']} — {r['name']}",
                            "value": r["ticker"]} for r in rows]

            options = list(market_indices) + portfolio_opts + composite_opts + stock_opts

            # Safeguard: always include the currently-selected value, even
            # if it belongs to the other market — the user can still
            # interact with it but new dropdown searches are market-scoped.
            if current_value and not any(o["value"] == current_value for o in options):
                if current_value.startswith("@") or current_value.startswith("&"):
                    options.insert(len(market_indices),
                                    {"label": current_value, "value": current_value})
                else:
                    cur_row = conn.execute(
                        "SELECT ticker, name FROM securities WHERE ticker = ?",
                        (current_value,)
                    ).fetchone()
                    label = (f"{cur_row['ticker']} — {cur_row['name']}"
                             if cur_row else current_value)
                    options.insert(len(market_indices),
                                    {"label": label, "value": current_value})
        return options

    # ----- Main render: one heavy callback per Load click -----
    @app.callback(
        Output("risk-placeholder", "style"),
        Output("risk-content", "style"),
        Output("risk-fan-subtitle", "children"),
        Output("risk-fan-chart", "figure"),
        Output("risk-vol-cone", "figure"),
        Output("risk-var-table", "children"),
        Output("risk-prob-table", "children"),
        Output("risk-drawdown-hist", "figure"),
        Output("risk-diagnostics", "children"),
        Input("risk-load-btn", "n_clicks"),
        State("risk-ticker-select", "value"),
        State("risk-history-window", "value"),
        State("risk-horizon", "value"),
        prevent_initial_call=True,
    )
    def render_risk(_clicks, ticker, history_window, horizon):
        if not ticker:
            raise PreventUpdate

        from analysis._garch_cache import get_or_build
        from analysis.data_loader import get_or_fetch_prices
        from storage.database import Database

        db = Database(db_path)
        try:
            prices = get_or_fetch_prices(ticker, db)
        except Exception as e:
            return _error_state(f"Price fetch failed for {ticker}: {e}")
        if not prices:
            return _error_state(f"No price data for {ticker}.")

        try:
            bundle = get_or_build(ticker, prices,
                                    history_window_trading_days=history_window,
                                    horizon=horizon)
        except ValueError as e:
            return _error_state(str(e))
        except Exception as e:
            return _error_state(f"GARCH fit failed: {e}")

        subtitle = (f"{bundle.fit.n_obs:,} returns fit on data through "
                    f"{bundle.last_price_date} · 5,000 MC paths · "
                    f"horizon = {bundle.metrics.horizon_days}d")

        fan = fan_chart(bundle.prices, bundle.paths, ticker)
        cone = vol_cone_chart(bundle.returns_pct,
                                bundle.forecast.annualised_vol_pct, ticker)
        var_tbl = _build_var_table(bundle.metrics)
        prob_tbl = _build_prob_table(bundle.metrics)
        dd_fig = drawdown_histogram(bundle.metrics.max_drawdowns, ticker)
        diag = _build_diagnostics(bundle.fit)

        return (
            {"display": "none"}, {"display": "block"},
            subtitle, fan, cone, var_tbl, prob_tbl, dd_fig, diag,
        )


# ============== Saved-portfolio dropdown options ==============

def _build_portfolio_options(search: Optional[str] = None) -> list[dict]:
    """Pull saved portfolios from Supabase and expose each as one or two
    dropdown options (`@NAME` always; `@NAME$OPT` when optimal_weights set).
    Silently empty when the cloud DB isn't configured."""
    try:
        from storage.cloud_db import available
        if not available():
            return []
        from storage.cloud_repository import CloudPortfoliosRepository
        rows = CloudPortfoliosRepository().list_portfolios()
    except Exception:
        return []

    options: list[dict] = []
    needle = (search or "").strip().upper()
    for r in rows:
        name = r["name"]
        sq = f"@{name}"
        if not needle or needle in sq:
            options.append({
                "label": f"⊕ {sq} — saved portfolio ({len(r.get('holdings') or [])} holdings)",
                "value": sq,
            })
        if r.get("optimal_weights"):
            opt = f"@{name}$OPT"
            if not needle or needle in opt:
                options.append({
                    "label": f"⊕ {opt} — max-Sharpe optimal weights",
                    "value": opt,
                })
    return options


# ============== Sub-sector composite dropdown options ==============

def _build_subsector_options(db_path: str,
                                search: Optional[str] = None) -> list[dict]:
    """Pull every distinct active sub-sector and expose its composite
    ticker as a dropdown option (`&NAME`). Filtering matches either the
    composite slug or the human label so 'semi' finds Semiconductors."""
    try:
        from analysis.subsector_synth import list_subsector_composites
        from storage.database import Database
        composites = list_subsector_composites(Database(db_path))
    except Exception:
        return []
    if search:
        q = search.lstrip("&").upper()
        composites = [
            c for c in composites
            if q in c["ticker"].upper() or q in c["sub_sector"].upper()
        ]
    return [
        {"label": f"⊕ {c['ticker']} — {c['sub_sector']} composite "
                   f"({c['n_constituents']} names)",
         "value": c["ticker"]}
        for c in composites
    ]


# ============== Helper builders (HTML/Bootstrap tables) ==============

def _error_state(msg: str):
    """Return the 9-tuple of outputs needed when render fails."""
    err = html.Div([
        html.P(msg, className="text-danger small mb-0"),
    ])
    return (
        {"display": "block"}, {"display": "none"},
        "", {}, {}, err, "", {}, "",
    )


def _build_var_table(m) -> html.Table:
    """VaR + CVaR at 95/99% over 1d / 5d / horizon."""
    def fmt(v):
        # CN/HK convention: loss (negative VaR) = green, positive = red.
        return html.Span(f"{v:+.2%}",
                          style={"color": T.PRICE_DOWN if v < 0 else T.PRICE_UP,
                                 "fontWeight": "600"})

    rows = [
        html.Tr([html.Th(""), html.Th("VaR 95%"), html.Th("CVaR 95%"),
                 html.Th("VaR 99%"), html.Th("CVaR 99%")],
                className="small text-muted"),
        html.Tr([html.Td("1-day"), html.Td(fmt(m.var_95_1d)),
                  html.Td(fmt(m.cvar_95_1d)), html.Td(fmt(m.var_99_1d)),
                  html.Td(fmt(m.cvar_99_1d))]),
        html.Tr([html.Td("5-day"), html.Td(fmt(m.var_95_5d)),
                  html.Td(fmt(m.cvar_95_5d)), html.Td(fmt(m.var_99_5d)),
                  html.Td(fmt(m.cvar_99_5d))]),
        html.Tr([html.Td(f"{m.horizon_days}-day"), html.Td(fmt(m.var_95_horizon)),
                  html.Td(fmt(m.cvar_95_horizon)), html.Td(fmt(m.var_99_horizon)),
                  html.Td(fmt(m.cvar_99_horizon))]),
    ]
    return html.Table(rows, className="table table-sm w-100 small")


def _build_prob_table(m) -> html.Table:
    """P(loss > 10%) and P(loss > 20%) over the chosen horizon."""
    def fmt_pct(p):
        # P(big loss). In CN/HK convention high probability of large loss
        # is bullish-bad → green; low probability of loss = red.
        col = T.PRICE_DOWN if p > 0.10 else (T.WARNING if p > 0.02 else T.PRICE_UP)
        return html.Span(f"{p:.1%}", style={"color": col, "fontWeight": "700"})

    rows = [
        html.Tr([html.Td(f"P(loss > 10%) over {m.horizon_days}d"),
                  html.Td(fmt_pct(m.p_loss_10))]),
        html.Tr([html.Td(f"P(loss > 20%) over {m.horizon_days}d"),
                  html.Td(fmt_pct(m.p_loss_20))]),
    ]
    return html.Table(rows, className="table table-sm w-100 small mb-0")


def _build_diagnostics(fit) -> html.Div:
    """Compact 2-column param grid + persistence/half-life/AIC/BIC."""
    def fmt(v, digits=4):
        if v is None:
            return "—"
        return f"{v:.{digits}f}"

    params = [
        ("μ (mean)", fit.mu),
        ("ω (omega)", fit.omega),
        ("α (alpha)", fit.alpha),
        ("γ (gamma, leverage)", fit.gamma),
        ("β (beta)", fit.beta),
        ("ν (Student-t df)", fit.nu),
    ]
    stats = [
        ("Persistence (α + γ/2 + β)", fmt(fit.persistence)),
        ("Half-life of shock (days)",
         fmt(fit.half_life_days, 1) if fit.half_life_days else "—"),
        ("Long-run annual vol (%)",
         fmt(fit.unconditional_vol_pct, 2)),
        ("AIC", fmt(fit.aic, 1)),
        ("BIC", fmt(fit.bic, 1)),
        ("Returns fit (n)", f"{fit.n_obs:,}"),
    ]

    def _grid(items, value_digits=4):
        return html.Table([
            html.Tbody([
                html.Tr([
                    html.Td(html.Strong(k), className="small",
                             style={"color": T.TEXT_MUTED, "paddingRight": "16px"}),
                    html.Td(v if isinstance(v, str) else fmt(v, value_digits),
                             className="small",
                             style={"color": T.TEXT, "fontWeight": "600"}),
                ]) for k, v in items
            ])
        ], className="w-100")

    return dbc.Row([
        dbc.Col([
            html.Div("Fitted parameters",
                      style={"color": T.TEXT_MUTED, "fontSize": "0.7rem",
                             "textTransform": "uppercase", "letterSpacing": "0.06em",
                             "marginBottom": "6px"}),
            _grid(params, value_digits=5),
        ], width=6),
        dbc.Col([
            html.Div("Derived statistics",
                      style={"color": T.TEXT_MUTED, "fontSize": "0.7rem",
                             "textTransform": "uppercase", "letterSpacing": "0.06em",
                             "marginBottom": "6px"}),
            _grid(stats),
        ], width=6),
    ])
