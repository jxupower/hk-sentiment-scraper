"""Stock Research tab callbacks — the most callback-dense tab.

Three main callbacks:
  1. `populate_ticker_options` — autocomplete dropdown options from securities
  2. `render_report` — heavy callback: ticker + Load click → ~30 outputs (header,
     section data, charts, AI summary, saved notes pre-fill)
  3. `recompute_dcf` — DCF slider changes re-run compute_dcf without reloading
     the rest of the report
  4. `save_notes` — write SWOT / strategy / valuation / thesis textareas back
     to research_notes
  5. `devil_advocate` — Claude prompt for counter-arguments
  6. `export_markdown` — download button
"""
import json
import math
import os
import sqlite3
import sys
from typing import Optional

import dash
import plotly.graph_objects as go
from dash import Input, Output, State, dcc, html, no_update
import dash_bootstrap_components as dbc

from analysis.dcf import DCFInputs, compute_dcf, sensitivity_table
from analysis.factor_scores import FactorScoringEngine
from analysis.research_orchestrator import build_research_report
from dash.exceptions import PreventUpdate
from dashboard import theme as T
from dashboard import financial_statement_tables as fst
from dashboard.charts import (
    multi_year_eps_chart, revenue_yoy_chart, share_count_chart, price_chart,
    historical_multiple_chart, dcf_sensitivity_heatmap, peer_scorecard_heatmap,
)


def register_stock_research_callbacks(app, db_path: str):
    sector_risk_path = os.path.join(os.path.dirname(__file__), "..", "config",
                                     "sector_risk.yaml")
    # Lazy engine used by the V/Q/G bar click-through drawer. We re-instantiate
    # here rather than reuse the one inside build_research_report because the
    # orchestrator's engine isn't exposed at module scope; the cost of one extra
    # FactorScoringEngine is trivial (no state besides path strings).
    factor_engine = FactorScoringEngine(db_path, sector_risk_path)

    # ----- Autocomplete dropdown options -----
    # Critical: must always include the currently-selected `value` in the
    # returned options list, otherwise Dash silently clears the selection when
    # the user picks a non-watchlist ticker (the options list refreshes to
    # "watchlist only" when search_value clears, dropping the selected ticker).
    @app.callback(
        Output("sr-ticker-select", "options", allow_duplicate=True),
        Input("sr-ticker-select", "search_value"),
        State("sr-ticker-select", "value"),
        prevent_initial_call=True,
    )
    def populate_ticker_options(search, current_value):
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            if search:
                rows = conn.execute("""
                    SELECT ticker, name FROM securities
                    WHERE is_active = 1 AND (
                        UPPER(ticker) LIKE UPPER(?) OR UPPER(name) LIKE UPPER(?)
                    )
                    ORDER BY (is_watchlist = 1) DESC, ticker
                    LIMIT 30
                """, (f"{search}%", f"%{search}%")).fetchall()
            else:
                # Default to watchlist when no search
                rows = conn.execute("""
                    SELECT ticker, name FROM securities
                    WHERE is_active = 1 AND is_watchlist = 1
                    ORDER BY ticker LIMIT 30
                """).fetchall()
            options = [{"label": f"{r['ticker']} — {r['name']}", "value": r["ticker"]}
                       for r in rows]

            # Always include the current selection so Dash doesn't clear it
            if current_value and current_value not in [o["value"] for o in options]:
                cur_row = conn.execute(
                    "SELECT ticker, name FROM securities WHERE ticker = ?",
                    (current_value,)
                ).fetchone()
                if cur_row:
                    options.insert(0, {
                        "label": f"{cur_row['ticker']} — {cur_row['name']}",
                        "value": cur_row["ticker"],
                    })
                else:
                    # Ticker not in DB; preserve it anyway so the value persists
                    options.insert(0, {"label": current_value, "value": current_value})
        return options

    # ----- Main report render -----
    @app.callback(
        Output("sr-placeholder", "style"),
        Output("sr-content", "style"),
        Output("sr-header-name", "children"),
        Output("sr-header-sector", "children"),
        Output("sr-header-price", "children"),
        Output("sr-header-mcap", "children"),
        Output("sr-header-badges", "children"),
        Output("sr-screen-passes", "children"),
        Output("sr-factor-bars", "figure"),
        Output("sr-business-summary", "children"),
        Output("sr-swot-strengths", "value"),
        Output("sr-swot-weaknesses", "value"),
        Output("sr-swot-opportunities", "value"),
        Output("sr-swot-threats", "value"),
        Output("sr-article-feed", "children"),
        Output("sr-cagr-table", "children"),
        Output("sr-eps-chart", "figure"),
        Output("sr-revenue-chart", "figure"),
        Output("sr-peer-heatmap", "figure"),
        Output("sr-forensic-flags", "children"),
        # Section 3b (financial statements) is fetched lazily by a separate
        # callback when the user clicks "Load Financial Statements" — keeps
        # the main render fast (saves 3-8s on cold-cache tickers).
        Output("sr-strategy-notes", "value"),
        Output("sr-valuation-notes", "value"),
        Output("sr-thesis", "value"),
        Output("sr-status-select", "value"),
        # DCF slider defaults
        Output("sr-dcf-g15", "value"),
        Output("sr-dcf-g610", "value"),
        Output("sr-dcf-tg", "value"),
        Output("sr-dcf-wacc", "value"),
        Output("sr-dcf-g15-provenance", "children"),
        # Mirror the active ticker back into the dropdown so cross-tab nav
        # (Screener cell click → Research tab) updates the visible selection.
        Output("sr-ticker-select", "value", allow_duplicate=True),
        Output("sr-ticker-select", "options", allow_duplicate=True),
        Input("sr-load-btn", "n_clicks"),
        Input("cross-tab-nav", "data"),
        State("sr-ticker-select", "value"),
        State("sr-ticker-select", "options"),
        prevent_initial_call=True,
    )
    def render_report(_clicks, nav_data, state_ticker, state_options):
        # Resolve ticker source: cross-tab nav wins when it triggered, else dropdown State
        triggered = dash.callback_context.triggered_id
        if triggered == "cross-tab-nav":
            if not nav_data or not nav_data.get("ticker"):
                return dash.no_update
            ticker = nav_data["ticker"]
        else:
            ticker = state_ticker
        if not ticker:
            return dash.no_update

        # Ensure the dropdown's options list includes this ticker so the
        # dropdown can display the value (autocomplete only fetches on type).
        opts = list(state_options or [])
        if not any(o.get("value") == ticker for o in opts):
            opts.insert(0, {"label": ticker, "value": ticker})
        r = build_research_report(ticker, db_path, sector_risk_path,
                                    skip_financial_statements=True)
        if r is None:
            return (
                {"display": "block"}, {"display": "none"},
                "", "", "", "", [], [], {},
                "(no data)", "", "", "", "", [],
                [], {}, {}, {}, [],
                "", "", "", None,
                10, 5, 2.5, 9, "",
                ticker, opts,  # mirror selection back to dropdown
            )

        # Header
        name = r.name
        sector = r.sector
        price_str = f"${r.current_price:.2f}" if r.current_price else "NA"
        mcap_str = f"${r.market_cap/1e9:.1f}B" if r.market_cap else "NA"
        badges = []
        if r.is_watchlist:
            badges.append(dbc.Badge("★ Watchlist", color="warning", className="me-1"))
        for f in r.risk_flags:
            badges.append(dbc.Badge(f"⚠ {f.id[:20]}",
                                     color="danger" if f.severity == "high" else "warning",
                                     className="me-1"))

        # Section 1: screens + factor bars
        screen_badges = []
        for s in r.screen_pass_fail:
            color = "success" if s.passed else "secondary"
            sym = "✓" if s.passed else "✗"
            screen_badges.append(dbc.Badge(f"{sym} {s.name}", color=color, className="me-2"))
        factor_fig = _factor_bar_chart(r.factor_result)

        # Section 2: business summary (AI), articles, SWOT
        business_summary = _build_business_summary(r)
        s_swot, w_swot, o_swot, t_swot = _build_default_swot(r)
        article_feed = _build_article_feed(r.recent_articles)

        # Section 3: CAGR table, charts, peer heatmap, forensic
        cagr_table = _build_cagr_table(r)
        eps_fig = multi_year_eps_chart(r.history)
        rev_fig = revenue_yoy_chart(r.history)
        peer_fig = peer_scorecard_heatmap(r.peer_scorecard)
        forensic = _build_forensic_panel(r.red_flags)

        # Section 3b (financial statements) is loaded on demand via
        # `load_financial_statements` — keeps render_report off the 3-8s
        # cold-cache yfinance/akshare fetch.

        # Sections 4 + 5 (period-dependent charts) are handled by a separate,
        # lightweight callback that fires on (ticker, sr-period-select) without
        # re-running FactorScoringEngine.

        # Pre-fill saved notes if any
        saved = r.saved_notes or {}
        s_swot = saved.get("swot_strengths") or s_swot
        w_swot = saved.get("swot_weaknesses") or w_swot
        o_swot = saved.get("swot_opportunities") or o_swot
        t_swot = saved.get("swot_threats") or t_swot
        strat_notes = saved.get("strategy_notes") or ""
        val_notes = saved.get("valuation_notes") or ""
        thesis = saved.get("thesis") or ""
        status = saved.get("research_status")

        # DCF default slider values
        if r.dcf_inputs_default:
            d = r.dcf_inputs_default
            g15 = round(d.growth_y1_5 * 100)
            g610 = round(d.growth_y6_10 * 100)
            tg = round(d.terminal_growth * 100, 2)
            wacc = round(d.wacc * 100)
        else:
            g15, g610, tg, wacc = 10, 5, 2.5, 9
        g15_provenance = _provenance_subtitle(r.dcf_growth_provenance)

        return (
            {"display": "none"}, {"display": "block"},
            name, sector, price_str, mcap_str, badges,
            screen_badges, factor_fig,
            business_summary, s_swot, w_swot, o_swot, t_swot, article_feed,
            cagr_table, eps_fig, rev_fig, peer_fig, forensic,
            strat_notes, val_notes, thesis, status,
            g15, g610, tg, wacc, g15_provenance,
            ticker, opts,  # mirror selection back to dropdown
        )

    # ----- Section 3b: lazy financial statements load -----
    # Fires only when the user clicks "Load Financial Statements". The fetch
    # is cache-aside via analysis.data_loader.get_or_fetch_financial_statements
    # so first-load is 3-8s, repeat loads <500ms.
    @app.callback(
        Output("sr-fs-tabs-wrapper", "style"),
        Output("sr-fs-source-pill", "children"),
        Output("sr-fs-coverage", "children"),
        Output("sr-fs-status", "children"),
        Output("sr-fs-income-chart", "figure"),
        Output("sr-fs-balance-chart", "figure"),
        Output("sr-fs-cashflow-chart", "figure"),
        Output("sr-fs-earnings-chart", "figure"),
        Output("sr-fs-income-table", "children"),
        Output("sr-fs-balance-table", "children"),
        Output("sr-fs-cashflow-table", "children"),
        Output("sr-fs-earnings-table", "children"),
        Input("sr-fs-load-btn", "n_clicks"),
        State("sr-ticker-select", "value"),
        prevent_initial_call=True,
    )
    def load_financial_statements(_clicks, ticker):
        if not ticker:
            return ({"display": "none"}, "", "", "Pick a ticker first.",
                    {}, {}, {}, {}, "", "", "", "")
        from analysis.data_loader import get_or_fetch_financial_statements
        from storage.database import Database
        try:
            fs = get_or_fetch_financial_statements(ticker, Database(db_path))
        except Exception as e:
            return ({"display": "none"}, "", "", f"Fetch failed: {e}",
                    {}, {}, {}, {}, "", "", "", "")
        fs = fs or {"income": [], "balance": [], "cashflow": []}
        source_pill, coverage = _build_fs_meta(fs)
        income_chart = fst.build_statement_chart("income", fs["income"])
        balance_chart = fst.build_statement_chart("balance", fs["balance"])
        cashflow_chart = fst.build_statement_chart("cashflow", fs["cashflow"])
        earnings_chart = fst.build_earnings_chart(fs["income"])
        income_table = (fst.build_statement_table("income", fs["income"])
                         if fs["income"] else fst.build_unavailable_state("income"))
        balance_table = (fst.build_statement_table("balance", fs["balance"])
                          if fs["balance"] else fst.build_unavailable_state("balance"))
        cashflow_table = (fst.build_statement_table("cashflow", fs["cashflow"])
                           if fs["cashflow"] else fst.build_unavailable_state("cashflow"))
        earnings_table = fst.build_earnings_table(fs["income"])
        return ({"display": "block"}, source_pill, coverage, "",
                income_chart, balance_chart, cashflow_chart, earnings_chart,
                income_table, balance_table, cashflow_table, earnings_table)

    # Reset Section 3b when the user picks a different ticker so stale
    # statements from the prior ticker don't bleed through until they click
    # the Load button again.
    @app.callback(
        Output("sr-fs-tabs-wrapper", "style", allow_duplicate=True),
        Output("sr-fs-source-pill", "children", allow_duplicate=True),
        Output("sr-fs-coverage", "children", allow_duplicate=True),
        Output("sr-fs-status", "children", allow_duplicate=True),
        Input("sr-ticker-select", "value"),
        prevent_initial_call=True,
    )
    def reset_fs_section_on_ticker_change(_ticker):
        return {"display": "none"}, "", "", ""

    # ----- Period-driven charts (Sections 4 + 5) -----
    # Fires on ticker change AND period selector change. Does NOT call
    # build_research_report — only loads cheap per-ticker data (annual history +
    # daily prices). Keeps period changes snappy and lets new stocks (<1y) still
    # render their available data instead of empty annual charts.
    @app.callback(
        Output("sr-price-chart", "figure"),
        Output("sr-price-summary", "children"),
        Output("sr-shares-chart", "figure"),
        Output("sr-strategy-stats", "children"),
        Output("sr-pe-history", "figure"),
        Output("sr-pb-history", "figure"),
        Output("sr-period-coverage", "children"),
        Input("sr-ticker-select", "value"),
        Input("sr-period-select", "value"),
        Input("sr-load-btn", "n_clicks"),
        prevent_initial_call=True,
    )
    def update_period_charts(ticker, period_days, _clicks):
        if not ticker:
            return {}, "", {}, "", {}, {}, ""

        history = _load_history(db_path, ticker)
        prices_all = _load_prices(db_path, ticker)

        # MAX (period_days == 0) → no clipping
        cutoff_iso = None
        if period_days and prices_all:
            from datetime import datetime, timedelta
            # Anchor cutoff to the LAST available price date, not "today" —
            # otherwise a ticker that stopped trading or has stale data shows
            # an empty chart for short windows.
            last_date = max(p["date"] for p in prices_all if p.get("date"))
            try:
                last_dt = datetime.fromisoformat(last_date[:10])
                cutoff_iso = (last_dt - timedelta(days=period_days)).isoformat()[:10]
            except (ValueError, TypeError):
                cutoff_iso = None

        prices_window = ([p for p in prices_all if p["date"] >= cutoff_iso]
                         if cutoff_iso else prices_all)
        history_window = ([h for h in history if h.date >= cutoff_iso]
                          if cutoff_iso else history)

        # Charts
        price_fig = price_chart(prices_window, label=f"{ticker}")
        price_summary = _build_price_summary(prices_window)
        shares_fig = share_count_chart(history_window)
        strategy_stats = _build_strategy_stats_window(history_window, prices_window)
        pe_fig = historical_multiple_chart(history, prices_all, "pe",
                                            min_date=cutoff_iso)
        pb_fig = historical_multiple_chart(history, prices_all, "pb",
                                            min_date=cutoff_iso)

        coverage = _build_coverage_text(prices_all, history, prices_window,
                                         history_window, period_days)

        return (price_fig, price_summary, shares_fig, strategy_stats,
                pe_fig, pb_fig, coverage)

    # ----- DCF live recomputation on slider change -----
    # Performance critical: this fires on every slider tick, AND fires 4 times
    # cascading when render_report sets all 4 slider Outputs. Must NOT call
    # build_research_report (which runs FactorScoringEngine over the full universe
    # and every screen). Instead, pull just the per-share fields we need.
    @app.callback(
        Output("sr-dcf-result", "children"),
        Output("sr-dcf-sensitivity", "figure"),
        Output("sr-dcf-walkthrough", "children"),
        Input("sr-dcf-g15", "value"),
        Input("sr-dcf-g610", "value"),
        Input("sr-dcf-tg", "value"),
        Input("sr-dcf-wacc", "value"),
        State("sr-ticker-select", "value"),
        prevent_initial_call=True,
    )
    def recompute_dcf(g15, g610, tg, wacc, ticker):
        if not ticker:
            return "", {}, ""
        dcf_inputs = _load_dcf_inputs_only(db_path, ticker)
        if dcf_inputs is None:
            return (html.Span("Insufficient per-share data for DCF.",
                              className="text-warning small"), {},
                    html.Div("No EPS / shares data — walkthrough unavailable.",
                              className="text-muted small"))

        inputs = DCFInputs(
            base_fcf=dcf_inputs.base_fcf,
            growth_y1_5=g15 / 100.0,
            growth_y6_10=g610 / 100.0,
            terminal_growth=tg / 100.0,
            wacc=wacc / 100.0,
            shares_outstanding=dcf_inputs.shares_outstanding,
            current_price=dcf_inputs.current_price,
        )
        result = compute_dcf(inputs)
        if result.error:
            return (html.Span(f"DCF error: {result.error}",
                              className="text-danger small"), {},
                    html.Div(f"DCF error: {result.error}",
                              className="text-danger small"))

        mos_color = "success" if (result.margin_of_safety or 0) > 0 else "danger"
        mos_str = f"{result.margin_of_safety*100:+.1f}%" if result.margin_of_safety is not None else "NA"
        result_html = html.Div([
            html.Strong("Intrinsic value per share: ", className="me-1"),
            html.Span(f"${result.intrinsic_value_per_share:.2f}",
                      className="text-info fw-bold fs-5 me-3"),
            html.Strong("Current price: ", className="me-1"),
            html.Span(f"${result.current_price:.2f}",
                      style={"color": T.TEXT, "marginRight": "0.75rem"}),
            html.Strong("Margin of safety: ", className="me-1"),
            dbc.Badge(mos_str, color=mos_color, className="fs-6"),
            html.P(f"(EV: ${result.enterprise_value/1e9:.1f}B over "
                   f"{inputs.shares_outstanding/1e9:.2f}B shares; "
                   f"base FCF: ${inputs.base_fcf/1e9:.1f}B)",
                   className="text-muted small mt-2 mb-0"),
        ])

        walkthrough = _render_dcf_walkthrough(inputs, result, dcf_inputs)

        # Sensitivity heatmap: vary growth_y1_5 and wacc
        g_grid = [g15/100 - 0.04, g15/100 - 0.02, g15/100, g15/100 + 0.02, g15/100 + 0.04]
        wacc_grid = [wacc/100 - 0.02, wacc/100 - 0.01, wacc/100,
                     wacc/100 + 0.01, wacc/100 + 0.02]
        try:
            sens_df = sensitivity_table(inputs, "growth_y1_5", "wacc", g_grid, wacc_grid)
            sens_fig = dcf_sensitivity_heatmap(sens_df, current_price=inputs.current_price,
                                                 x_label="Growth Y1-5", y_label="WACC")
        except Exception:
            sens_fig = {}

        return result_html, sens_fig, walkthrough

    # ----- Save all notes back to DB -----
    @app.callback(
        Output("sr-save-status", "children"),
        Input("sr-save-btn", "n_clicks"),
        State("sr-ticker-select", "value"),
        State("sr-status-select", "value"),
        State("sr-swot-strengths", "value"),
        State("sr-swot-weaknesses", "value"),
        State("sr-swot-opportunities", "value"),
        State("sr-swot-threats", "value"),
        State("sr-strategy-notes", "value"),
        State("sr-valuation-notes", "value"),
        State("sr-thesis", "value"),
        prevent_initial_call=True,
    )
    def save_notes(_clicks, ticker, status, s, w, o, t, strat, val, thesis):
        if not ticker:
            return "Select a ticker first."
        from storage.database import Database
        from storage.repository import ResearchNotesRepository
        db = Database(db_path)
        repo = ResearchNotesRepository(db)
        repo.upsert(ticker, research_status=status,
                     swot_strengths=s, swot_weaknesses=w,
                     swot_opportunities=o, swot_threats=t,
                     strategy_notes=strat, valuation_notes=val,
                     thesis=thesis)
        from datetime import datetime
        return f"Saved at {datetime.now().strftime('%H:%M:%S')}"

    # ----- Devil's-advocate AI -----
    @app.callback(
        Output("sr-devil-output", "children"),
        Input("sr-devil-btn", "n_clicks"),
        State("sr-ticker-select", "value"),
        prevent_initial_call=True,
    )
    def devil_advocate(_clicks, ticker):
        if not ticker:
            return ""
        from config.settings import CLAUDE_API_KEY
        if not CLAUDE_API_KEY:
            return html.P("Add CLAUDE_API_KEY to .env to use AI Devil's-Advocate.",
                          className="text-muted small fst-italic")
        r = build_research_report(ticker, db_path, sector_risk_path,
                                    skip_financial_statements=True)
        if r is None:
            return html.P("No data.", className="text-muted small")
        return _generate_devil_advocate(r)

    # ----- Export as Markdown -----
    @app.callback(
        Output("sr-download", "data"),
        Input("sr-export-btn", "n_clicks"),
        State("sr-ticker-select", "value"),
        State("sr-swot-strengths", "value"),
        State("sr-swot-weaknesses", "value"),
        State("sr-swot-opportunities", "value"),
        State("sr-swot-threats", "value"),
        State("sr-strategy-notes", "value"),
        State("sr-valuation-notes", "value"),
        State("sr-thesis", "value"),
        prevent_initial_call=True,
    )
    def export_markdown(_clicks, ticker, s, w, o, t, strat, val, thesis):
        if not ticker:
            return no_update
        r = build_research_report(ticker, db_path, sector_risk_path,
                                    skip_financial_statements=True)
        if r is None:
            return no_update
        md = _report_to_markdown(r, s, w, o, t, strat, val, thesis)
        return dict(content=md, filename=f"{ticker}_research.md")

    # ----- Click-to-explain V/Q/G bars -----
    # Clicking any of the three V/Q/G bars on the Section 1 percentile chart
    # opens a side drawer showing exactly which fundamentals fed the composite
    # signal, the sub-sector peer distribution, and the target's rank position.
    # Sentiment bars are no-ops (sentiment is universe-wide; no breakdown).
    @app.callback(
        Output("sr-factor-breakdown-drawer", "is_open"),
        Output("sr-factor-breakdown-body", "children"),
        Input("sr-factor-bars", "clickData"),
        State("sr-ticker-select", "value"),
        prevent_initial_call=True,
    )
    def open_factor_breakdown_drawer(click, ticker):
        if not click or not ticker:
            raise PreventUpdate
        label = click["points"][0].get("y")
        if label not in {"Value", "Quality", "Growth"}:
            raise PreventUpdate                      # Sentiment / unknown — no breakdown
        factor = label.lower()
        breakdown = factor_engine.breakdown_for(ticker, factor)
        if breakdown is None:
            return True, html.Div(f"{ticker} not found in fundamentals snapshot.",
                                   className="text-muted")
        return True, _render_factor_breakdown(breakdown)


# ============== helper functions ==============

def _factor_bar_chart(fr) -> go.Figure:
    if fr is None:
        return {}
    metrics = ["Value", "Quality", "Growth", "Sentiment"]
    values = [fr.value_pctile or 0, fr.quality_pctile or 0,
              fr.growth_pctile or 0, fr.sentiment_pctile or 0]
    colors = [T.SUCCESS if v >= 70 else (T.WARNING if v < 30 else T.INFO) for v in values]
    # Hover hint: V/Q/G are click-through to a breakdown drawer; Sentiment is not.
    hover = [
        "<b>Value: %{x:.0f}</b><br>Click for component breakdown<extra></extra>",
        "<b>Quality: %{x:.0f}</b><br>Click for component breakdown<extra></extra>",
        "<b>Growth: %{x:.0f}</b><br>Click for component breakdown<extra></extra>",
        "<b>Sentiment: %{x:.0f}</b><br>(universe-wide; no breakdown)<extra></extra>",
    ]
    fig = go.Figure(go.Bar(x=values, y=metrics, orientation="h",
                            marker_color=colors, marker_line_width=0,
                            text=[f"{v:.0f}" if v else "NA" for v in values],
                            textposition="outside",
                            hovertemplate=hover))
    fig.add_vline(x=50, line_dash="dot", line_color=T.TEXT_MUTED)
    fig.update_layout(**T.chart_layout(
        title="Factor percentile ranks (vs sub-sector peers) — click V/Q/G bars for breakdown",
        xaxis=dict(range=[0, 100], gridcolor=T.BORDER, linecolor=T.BORDER,
                   tickfont=dict(color=T.TEXT_MUTED)),
        height=200, margin=dict(t=40, b=30, l=80, r=20),
    ))
    return fig


def _render_factor_breakdown(b) -> html.Div:
    """Render a FactorBreakdown into the slide-in drawer body. Shows the
    sub-sector bucket, ingredient table, peer-signal distribution stats,
    and the target's exact rank position + percentile formula."""
    if b.empty_reason:
        # Empty-state — explain why no rank exists.
        body = [
            html.H5(f"{b.factor.title()} percentile — not computed",
                     style={"color": T.WARNING}),
            html.P(b.empty_reason, className="text-muted"),
        ]
        if b.bucket:
            body.append(html.Small(f"Bucket: {b.bucket}", className="text-muted"))
        if b.ingredients:
            body.append(html.Hr())
            body.append(html.Small("Ingredient detail (target side):",
                                    className="text-muted"))
            body.append(_factor_ingredient_table(b))
        return html.Div(body)

    pct_color = (T.SUCCESS if b.pctile >= 70 else
                  (T.WARNING if b.pctile < 30 else T.INFO))
    return html.Div([
        # Header — factor name + pctile + bucket
        html.Div([
            html.Span(b.factor.title(),
                       style={"fontSize": "1.4rem", "fontWeight": "700",
                              "color": T.TEXT, "marginRight": "12px"}),
            html.Span(f"= {b.pctile:.1f}",
                       style={"fontSize": "1.4rem", "fontWeight": "700",
                              "color": pct_color}),
        ]),
        html.Div([
            html.Span("Ranked against ", className="text-muted small"),
            html.Span(f"{b.bucket_size - 1} peer(s) ",
                       style={"fontWeight": "600", "color": T.TEXT}),
            html.Span("in sub-sector ", className="text-muted small"),
            html.Span(f"\"{b.bucket}\"",
                       style={"fontWeight": "600", "color": T.PRIMARY}),
        ], className="mb-3"),

        # Section 1 — composite ingredients table
        html.H6("Composite ingredients", className="fw-bold mt-3"),
        html.P("Each row is one raw fundamentals component fed into the "
                "composite signal. Rows marked ✗ were dropped from the average "
                "for the reason shown.",
                className="text-muted small mb-2"),
        _factor_ingredient_table(b),

        # Section 2 — peer signal distribution
        html.H6("Composite signal vs sub-sector peers",
                 className="fw-bold mt-4"),
        html.P("Higher composite signal = higher percentile rank. "
                "The target's signal is the mean of the ingredient "
                "contributions above.",
                className="text-muted small mb-2"),
        html.Div([
            html.Div([
                html.Span(f"{b.ticker} signal: ", className="text-muted small"),
                html.Span(f"{b.target_composite_signal:.5f}",
                           style={"fontWeight": "700", "color": T.PRIMARY,
                                  "fontFamily": "monospace"}),
            ], className="mb-2"),
            html.Table([
                html.Thead(html.Tr([
                    html.Th(c, className="text-muted small")
                    for c in ["Peer min", "P25", "Median", "P75", "Peer max"]
                ])),
                html.Tbody(html.Tr([
                    html.Td(f"{v:.5f}",
                             style={"fontFamily": "monospace",
                                    "fontSize": "0.85rem"})
                    for v in [b.peer_signal_min, b.peer_signal_p25,
                              b.peer_signal_median, b.peer_signal_p75,
                              b.peer_signal_max]
                ])),
            ], className="table table-sm"),
        ], className="mb-3"),

        # Section 3 — side-by-side V/Q/G comparison vs nearest peers in
        # the same sub-sector. Highlights the current factor's ingredient
        # rows so the user knows what drove the rank that opened the drawer.
        html.H6("Side-by-side vs nearest peers", className="fw-bold mt-4"),
        html.P([
            "Closest 3 peers by composite signal, in rank order. ",
            html.Span("Highlighted rows", style={"backgroundColor": T.PRIMARY_SOFT,
                                                  "padding": "0 4px",
                                                  "borderRadius": "3px"}),
            f" are the ingredients of the {b.factor} composite that opened this drawer.",
        ], className="text-muted small mb-2"),
        _peer_comparison_table(b),

        # Section 4 — rank line
        html.Div([
            html.Span("Result: ", className="text-muted small"),
            html.Span(f"{b.ticker} ranks ",
                       style={"fontWeight": "600", "color": T.TEXT}),
            html.Span(f"{b.rank_position} of {b.bucket_size}",
                       style={"fontWeight": "700", "color": T.PRIMARY,
                              "fontSize": "1.05rem"}),
            html.Span(f" → percentile {b.pctile:.1f}",
                       style={"fontWeight": "600", "color": pct_color,
                              "marginLeft": "8px"}),
        ], className="mb-3 mt-4 p-2",
            style={"backgroundColor": T.CARD_BG_SOFT,
                   "border": f"1px solid {T.BORDER}",
                   "borderRadius": "6px"}),

        # Formula footnote
        html.Hr(),
        html.Small([
            html.Strong("Formula: "),
            "percentile = 100 × (peers_below + (ties + 1) / 2) / (bucket_size + 1). ",
            "Higher composite signal = higher rank for all three factors. "
            "Ingredients are dropped when raw values fall outside data-quality bounds "
            "(see PE_BOUNDS / PB_BOUNDS / EV_EBITDA_BOUNDS in analysis/factor_scores.py).",
        ], className="text-muted"),
    ])


def _factor_ingredient_table(b) -> "html.Table":
    """Small html.Table for the ingredients list. Used by both the populated
    and empty-state paths of _render_factor_breakdown."""
    def fmt(v, decimals=4):
        if v is None:
            return html.Span("—", className="text-muted")
        try:
            return html.Span(f"{float(v):.{decimals}f}",
                              style={"fontFamily": "monospace",
                                     "fontSize": "0.85rem"})
        except (TypeError, ValueError):
            return html.Span("—", className="text-muted")

    rows = []
    for ing in b.ingredients:
        if ing.included:
            badge = html.Span("✓ used", style={"color": T.SUCCESS,
                                                  "fontWeight": "600",
                                                  "fontSize": "0.82rem"})
            reason_cell = ""
        else:
            badge = html.Span("✗ dropped", style={"color": T.WARNING,
                                                     "fontWeight": "600",
                                                     "fontSize": "0.82rem"})
            reason_cell = html.Span(ing.reason_excluded,
                                     className="text-muted small")
        # target_contribution now carries the per-ingredient percentile
        # (0-100) post the V/Q/G normalization fix — see
        # analysis/factor_scores.py:_factor_signal_breakdown.
        pctile = ing.target_contribution
        pctile_cell = (html.Span(f"{pctile:.0f}",
                                   style={"fontFamily": "monospace",
                                          "fontSize": "0.85rem",
                                          "fontWeight": "600"})
                        if pctile is not None
                        else html.Span("—", className="text-muted"))
        rows.append(html.Tr([
            html.Td(ing.name, style={"fontSize": "0.85rem"}),
            html.Td(fmt(ing.target_raw)),
            html.Td(pctile_cell),
            html.Td([badge, html.Br(), reason_cell]),
        ]))
    if not rows:
        rows = [html.Tr(html.Td("(no ingredients available)",
                                  colSpan=4,
                                  className="text-muted small"))]
    return html.Table([
        html.Thead(html.Tr([
            html.Th("Ingredient", className="text-muted small"),
            html.Th("Raw value", className="text-muted small"),
            html.Th("Bucket %ile", className="text-muted small"),
            html.Th("Status", className="text-muted small"),
        ])),
        html.Tbody(rows),
    ], className="table table-sm")


_PEER_TABLE_ROWS = [
    # (fundamentals field, display label, factor key for shading, format mode)
    # format mode: "pct" → multiply by 100 + "%"; int → number of decimals
    ("trailing_pe",      "P/E",          "value",    1),
    ("price_to_book",    "P/B",          "value",    2),
    ("ev_to_ebitda",     "EV/EBITDA",    "value",    1),
    ("return_on_equity", "ROE",          "quality",  "pct"),
    ("return_on_assets", "ROA",          "quality",  "pct"),
    ("debt_to_equity",   "D/E %",        "quality",  1),   # already in percent
    ("earnings_growth",  "Earnings YoY", "growth",   "pct"),
    ("revenue_growth",   "Revenue YoY",  "growth",   "pct"),
]


def _peer_comparison_table(b) -> "html.Table":
    """Side-by-side V/Q/G fundamentals for the target + up to 3 nearest peers
    in the same sub-sector. Rows = metrics; columns = target (highlighted) +
    peers (rank-ordered). Rows belonging to the factor that opened the drawer
    get a soft PRIMARY-tinted background so the reader sees which inputs
    drove the rank."""
    if not b.target_snapshot or not b.nearest_peers:
        return html.Div("Not enough peers in this sub-sector for a "
                         "side-by-side comparison.",
                         className="text-muted small")

    def fmt(val, mode):
        if val is None:
            return html.Span("—", className="text-muted")
        try:
            v = float(val)
        except (TypeError, ValueError):
            return html.Span("—", className="text-muted")
        if mode == "pct":
            return f"{v * 100:.1f}%"
        return f"{v:.{mode}f}"

    columns = [b.target_snapshot] + b.nearest_peers
    header = html.Thead(html.Tr([
        html.Th("Metric", className="text-muted small",
                 style={"fontSize": "0.78rem"}),
        *[html.Th([
            html.Div(f"#{c.rank_position}  {c.ticker}",
                      style={"fontWeight": "700",
                             "color": (T.PRIMARY if i == 0 else T.TEXT),
                             "fontSize": "0.82rem"}),
            html.Div(c.name[:14], className="text-muted",
                      style={"fontSize": "0.7rem"}),
          ], style={"textAlign": "center",
                    "borderBottom": f"2px solid {T.PRIMARY if i == 0 else T.BORDER}",
                    "padding": "4px 6px"})
          for i, c in enumerate(columns)],
    ]))

    body_rows = []
    for field_key, label, factor_key, mode in _PEER_TABLE_ROWS:
        is_current = (factor_key == b.factor)
        tint = {"backgroundColor": T.PRIMARY_SOFT} if is_current else {}
        body_rows.append(html.Tr([
            html.Td(label, style={"fontWeight": "600",
                                    "fontSize": "0.82rem", **tint}),
            *[html.Td(fmt(getattr(c, field_key), mode),
                       style={"fontFamily": "monospace",
                              "fontSize": "0.78rem",
                              "textAlign": "right",
                              **({"color": T.PRIMARY, "fontWeight": "700"}
                                  if i == 0 else {}),
                              **tint})
              for i, c in enumerate(columns)],
        ]))

    # Footer row — the composite signal for the active factor + the rank.
    footer_style = {"borderTop": f"2px solid {T.BORDER_STRONG}"}
    body_rows.append(html.Tr([
        html.Td([f"{b.factor.title()} signal"], className="fst-italic",
                 style={"fontSize": "0.78rem", "color": T.TEXT_MUTED,
                        **footer_style}),
        *[html.Td(f"{c.composite_signal:.4f}",
                   style={"fontFamily": "monospace",
                          "fontSize": "0.78rem",
                          "textAlign": "right",
                          **({"color": T.PRIMARY, "fontWeight": "700"}
                              if i == 0 else {}),
                          **footer_style})
          for i, c in enumerate(columns)],
    ]))

    return html.Table([header, html.Tbody(body_rows)],
                       className="table table-sm",
                       style={"marginBottom": "0"})


def _provenance_subtitle(prov) -> "html.Span":
    """Build the small grey text line under the Y1-5 slider explaining where
    the default came from. Used by render_report. Returns "" when no provenance
    is available (e.g. ticker had no per-share data so the resolver wasn't called)."""
    if prov is None:
        return ""
    val = prov.chosen_value * 100
    if prov.winning_tier == "cagr_median":
        parts = []
        for k, v in prov.median_inputs.items():
            if v is None:
                continue
            label = k.replace("_5y_cagr", "")
            parts.append(f"{label} {v*100:+.1f}%")
        body = (f"Default {val:+.1f}% — median of 5y CAGRs "
                 f"({', '.join(parts) if parts else 'no data'})")
    elif prov.winning_tier == "analyst_consensus":
        body = (f"Default {val:+.1f}% — yfinance analyst 5y consensus "
                 "(no historical CAGR available)")
    elif prov.winning_tier == "trailing":
        body = (f"Default {val:+.1f}% — trailing YoY earnings_growth "
                 "(no CAGR or analyst consensus)")
    else:
        body = f"Default {val:+.1f}% — hardcoded floor (no growth data)"
    if prov.clamped and prov.raw_value is not None:
        body += f" (clamped from {prov.raw_value*100:+.1f}%)"
    return html.Span(body)


def _render_dcf_walkthrough(inputs, result, dcf_inputs) -> html.Div:
    """Six-step walkthrough that exposes every intermediate value compute_dcf()
    computes — base FCF, year-by-year projection, terminal, EV, per-share
    intrinsic, MoS. Rendered into sr-dcf-walkthrough by recompute_dcf so it
    re-renders on every slider drag."""
    eps_ttm = getattr(dcf_inputs, "eps_ttm", None)
    shares = inputs.shares_outstanding
    wacc = inputs.wacc
    g15, g610, tg = inputs.growth_y1_5, inputs.growth_y6_10, inputs.terminal_growth

    def _b(v, dp=2):
        """Format a number as 'X.XXB' (billions). Falls back to '—' on None."""
        if v is None:
            return "—"
        try:
            return f"{float(v) / 1e9:.{dp}f}B"
        except (TypeError, ValueError):
            return "—"

    # Step 1 — Base FCF derivation
    net_income = (eps_ttm or 0) * (shares or 0)
    step1 = html.Div([
        html.Div([
            html.Span("Step 1 — ", className="text-muted small"),
            html.Strong("Base free cash flow (proxy)"),
        ]),
        html.Div([
            html.Code(f"Net income = EPS_TTM × Shares = "
                      f"{eps_ttm if eps_ttm is not None else '—':.4f} × "
                      f"{_b(shares)} = {_b(net_income)}"
                      if eps_ttm is not None else "Net income unavailable — using base FCF directly",
                      style={"fontSize": "0.8rem"}),
        ]),
        html.Div([
            html.Code(f"Base FCF = Net income × 0.80 (FCF conversion proxy) "
                       f"= {_b(inputs.base_fcf)}",
                       style={"fontSize": "0.8rem"}),
        ]),
        html.Small("The 0.80 factor assumes ~80% of net income converts to free cash "
                    "flow on average — capex-heavy industries convert less. HK filings "
                    "rarely expose true historical FCF, so this proxy is the honest floor.",
                    className="text-muted d-block mt-1",
                    style={"fontSize": "0.72rem"}),
    ], className="mb-3")

    # Step 2 — Year-by-year projection table
    proj_rows = []
    for b in result.breakdown:
        if b.is_terminal:
            continue
        proj_rows.append(html.Tr([
            html.Td(b.year, style={"textAlign": "right", "fontWeight": "600"}),
            html.Td(f"{b.growth_used*100:+.1f}%",
                     style={"textAlign": "right",
                            "color": (T.PRIMARY if b.year <= 5 else T.INFO),
                            "fontWeight": "500"}),
            html.Td(_b(b.fcf), style={"textAlign": "right",
                                       "fontFamily": "monospace"}),
            html.Td(f"{b.discount_factor:.4f}", style={"textAlign": "right",
                                                         "fontFamily": "monospace"}),
            html.Td(_b(b.pv), style={"textAlign": "right",
                                      "fontFamily": "monospace",
                                      "fontWeight": "600"}),
        ]))
    step2 = html.Div([
        html.Div([
            html.Span("Step 2 — ", className="text-muted small"),
            html.Strong("Year-by-year projection"),
        ]),
        html.Small(f"Years 1-5 grow at {g15*100:+.1f}% (your Y1-5 slider); "
                    f"years 6-10 grow at {g610*100:+.1f}% (Y6-10 slider). "
                    f"Discount factor = 1 / (1 + WACC {wacc*100:.1f}%)^year.",
                    className="text-muted d-block mb-1",
                    style={"fontSize": "0.72rem"}),
        html.Table([
            html.Thead(html.Tr([
                html.Th(c, style={"fontSize": "0.75rem",
                                    "color": T.TEXT_MUTED,
                                    "textAlign": "right",
                                    "padding": "4px 8px"})
                for c in ["Year", "Growth", "FCF",
                          "Discount factor", "Present value"]
            ])),
            html.Tbody(proj_rows),
        ], className="table table-sm",
            style={"fontSize": "0.82rem", "marginBottom": "0"}),
    ], className="mb-3")

    # Step 3 — Terminal value
    terminal_row = next((b for b in result.breakdown if b.is_terminal), None)
    fcf_y10 = result.breakdown[9].fcf if len(result.breakdown) >= 10 else 0
    fcf_y11 = fcf_y10 * (1 + tg)
    df_10 = 1.0 / ((1 + wacc) ** 10)
    step3 = html.Div([
        html.Div([
            html.Span("Step 3 — ", className="text-muted small"),
            html.Strong("Terminal value (Gordon growth)"),
        ]),
        html.Div([
            html.Code(f"FCF_year_11 = FCF_year_10 × (1 + terminal_growth) = "
                      f"{_b(fcf_y10)} × {1+tg:.4f} = {_b(fcf_y11)}",
                      style={"fontSize": "0.8rem"}),
        ]),
        html.Div([
            html.Code(f"Terminal value = FCF_11 / (WACC − terminal) = "
                      f"{_b(fcf_y11)} / ({wacc*100:.1f}% − {tg*100:.1f}%) = "
                      f"{_b(result.terminal_value)}",
                      style={"fontSize": "0.8rem"}),
        ]),
        html.Div([
            html.Code(f"PV of terminal = Terminal × {df_10:.4f} (year-10 DF) = "
                      f"{_b(result.discounted_terminal)}",
                      style={"fontSize": "0.8rem"}),
        ]),
        html.Small("Terminal value captures all FCF beyond year 10 as a perpetuity "
                    f"growing at {tg*100:.1f}% forever. This is usually 60-80% of EV — "
                    "the model is most sensitive to terminal growth + WACC.",
                    className="text-muted d-block mt-1",
                    style={"fontSize": "0.72rem"}),
    ], className="mb-3")

    # Step 4 — Enterprise value
    sum_pv_explicit = sum(b.pv for b in result.breakdown if not b.is_terminal)
    step4 = html.Div([
        html.Div([
            html.Span("Step 4 — ", className="text-muted small"),
            html.Strong("Enterprise value"),
        ]),
        html.Div([
            html.Code(f"EV = Σ PV_y1..10 + PV_terminal = "
                      f"{_b(sum_pv_explicit)} + {_b(result.discounted_terminal)} "
                      f"= {_b(result.enterprise_value)}",
                      style={"fontSize": "0.8rem"}),
        ]),
    ], className="mb-3")

    # Step 5 — Per-share intrinsic
    step5 = html.Div([
        html.Div([
            html.Span("Step 5 — ", className="text-muted small"),
            html.Strong("Per-share intrinsic value"),
        ]),
        html.Div([
            html.Code(f"Intrinsic / share = EV / Shares = "
                      f"{_b(result.enterprise_value)} / {_b(shares)} = "
                      f"${result.intrinsic_value_per_share:.2f}",
                      style={"fontSize": "0.8rem"}),
        ]),
    ], className="mb-3")

    # Step 6 — Margin of safety
    if result.margin_of_safety is None:
        mos_html = html.Code("MoS = N/A (no current price)",
                              style={"fontSize": "0.8rem"})
    else:
        mos_pct = result.margin_of_safety * 100
        mos_color = (T.SUCCESS if mos_pct > 0
                      else (T.DANGER if mos_pct < -20 else T.WARNING))
        mos_html = html.Div([
            html.Code(f"MoS = (Intrinsic − Current) / Intrinsic = "
                      f"(${result.intrinsic_value_per_share:.2f} − "
                      f"${result.current_price:.2f}) / "
                      f"${result.intrinsic_value_per_share:.2f}",
                      style={"fontSize": "0.8rem"}),
            html.Div([
                html.Span("Result: ", className="text-muted me-2"),
                html.Span(f"{mos_pct:+.1f}%",
                           style={"color": mos_color, "fontWeight": "700",
                                  "fontSize": "1.1rem"}),
                html.Span(" undervalued" if mos_pct > 0
                            else (" overvalued" if mos_pct < 0 else ""),
                           className="text-muted ms-1"),
            ], className="mt-1"),
        ])
    step6 = html.Div([
        html.Div([
            html.Span("Step 6 — ", className="text-muted small"),
            html.Strong("Margin of safety"),
        ]),
        mos_html,
    ])

    return html.Div([step1, step2, step3, step4, step5, step6])


def _build_business_summary(r) -> html.Div:
    from config.settings import CLAUDE_API_KEY
    if not CLAUDE_API_KEY:
        return html.P("Add CLAUDE_API_KEY to .env to enable AI business summaries.",
                      className="text-muted small fst-italic")
    if not r.recent_articles:
        return html.P("No recent articles to summarize. (Most universe tickers don't have "
                      "broad news coverage; try a watchlist name.)",
                      className="text-muted small")
    try:
        import anthropic
        article_text = "\n".join(
            f"- [{(a.get('final_score') or 0):+.2f}] {a.get('title','')}"
            for a in r.recent_articles[:20]
        )
        sector = r.sector
        prompt = (
            f"You are a sell-side equity analyst. Write a 2-paragraph business summary "
            f"for {r.name} ({r.ticker}), a {sector} company listed on HKEX. "
            f"Use the recent news headlines below to identify the current business themes, "
            f"any recent catalysts or risks, and competitive positioning. "
            f"Avoid generic phrases; be specific. "
            f"Recent news (sentiment + headline):\n{article_text}\n\n"
            f"Format as plain text paragraphs, no markdown."
        )
        client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        return html.Div([html.P(p, style={"color": T.TEXT, "fontSize": "0.85rem",
                                          "marginBottom": "0.5rem"})
                          for p in text.split("\n") if p.strip()])
    except Exception as e:
        return html.P(f"AI summary unavailable: {e}",
                      className="text-muted small fst-italic")


def _build_default_swot(r) -> tuple[str, str, str, str]:
    """Auto-populate SWOT from factor percentiles + risk flags + forensic flags."""
    s_list, w_list, o_list, t_list = [], [], [], []
    fr = r.factor_result
    if fr:
        if (fr.quality_pctile or 0) >= 70:
            s_list.append(f"Top {100 - (fr.quality_pctile or 0):.0f}% on Quality (ROE/ROA/D-E) within sector")
        if (fr.growth_pctile or 0) >= 70:
            s_list.append(f"Top {100 - (fr.growth_pctile or 0):.0f}% on Growth within sector")
        if (fr.value_pctile or 0) >= 70:
            s_list.append(f"Top {100 - (fr.value_pctile or 0):.0f}% on Value (cheap vs peers)")
        if (fr.quality_pctile or 100) < 30:
            w_list.append(f"Bottom {fr.quality_pctile:.0f}% on Quality — weak ROE/ROA or leveraged")
        if (fr.growth_pctile or 100) < 30:
            w_list.append(f"Bottom {fr.growth_pctile:.0f}% on Growth — earnings/revenue not growing well")
        if (fr.value_pctile or 100) < 30:
            w_list.append(f"Bottom {fr.value_pctile:.0f}% on Value — expensive vs peers")
        if fr.sentiment_pctile is not None and fr.sentiment_pctile >= 70:
            o_list.append("Recent news sentiment in top quartile of universe")
        if fr.sentiment_pctile is not None and fr.sentiment_pctile < 30:
            t_list.append("Recent news sentiment in bottom quartile of universe")
    for f in r.risk_flags:
        t_list.append(f"Risk flag: {f.label} ({f.severity})")
    for rf in r.red_flags:
        if rf.severity in ("high", "medium"):
            t_list.append(f"Forensic: {rf.title}")
    return ("\n".join(f"• {x}" for x in s_list) or "(none auto-detected)",
            "\n".join(f"• {x}" for x in w_list) or "(none auto-detected)",
            "\n".join(f"• {x}" for x in o_list) or "(none auto-detected)",
            "\n".join(f"• {x}" for x in t_list) or "(none auto-detected)")


def _build_article_feed(articles: list) -> html.Div:
    if not articles:
        return html.P("No recent articles for this ticker in last 30 days.",
                      className="text-muted small")
    rows = []
    for a in articles[:20]:
        score = a.get("final_score", 0) or 0
        color = T.SUCCESS if score > 0.05 else (T.DANGER if score < -0.05 else T.TEXT_FAINT)
        rows.append(html.Tr([
            html.Td((a.get("published_at") or "")[:10], className="text-muted small"),
            html.Td(dbc.Badge((a.get("source") or "").upper(), color="info",
                               className="small")),
            html.Td(html.A(a.get("title", ""), href=a.get("url", "#"), target="_blank",
                            style={"color": T.PRIMARY, "fontSize": "0.85rem",
                                   "textDecoration": "none"})),
            html.Td(html.Span(f"{score:+.2f}", style={"color": color, "fontWeight": "bold"})),
        ]))
    return html.Table([html.Tbody(rows)],
                       className="table table-sm table-hover w-100 small")


def _build_cagr_table(r) -> html.Div:
    """Render multi-horizon CAGR for revenue / earnings / BPS."""
    def fmt(v):
        if v is None: return "—"
        return f"{v*100:+.1f}%"
    headers = ["Horizon", "Revenue", "Earnings", "BPS"]
    rows = [
        html.Tr([html.Th(h, className="small text-muted") for h in headers]),
    ]
    for h in [5, 10, 15]:
        rev = (r.cagr_revenue or {}).get(h)
        earn = (r.cagr_earnings or {}).get(h)
        bps = (r.cagr_bps or {}).get(h)
        rows.append(html.Tr([
            html.Td(f"{h}y", className="small fw-bold",
                    style={"color": T.TEXT}),
            html.Td(fmt(rev), className="small", style={"color": T.TEXT}),
            html.Td(fmt(earn), className="small", style={"color": T.TEXT}),
            html.Td(fmt(bps), className="small", style={"color": T.TEXT}),
        ]))
    return html.Table(rows, className="table table-sm w-100 small")


def _build_forensic_panel(red_flags) -> html.Div:
    if not red_flags:
        return html.P("No forensic red flags detected.",
                      className="text-success small")
    items = []
    for rf in red_flags:
        color = {"high": "danger", "medium": "warning", "low": "info"}.get(rf.severity, "secondary")
        items.append(html.Div([
            dbc.Badge(rf.severity.upper(), color=color, className="me-2"),
            html.Strong(rf.title, className="small me-2",
                        style={"color": T.TEXT}),
            html.Span(rf.detail, className="text-muted small"),
        ], className="mb-2"))
    return html.Div(items)


def _build_strategy_stats_window(history_window: list,
                                  prices_window: list) -> html.Div:
    """Annual-fundamental stats (ROE / earnings vol / D/E) scoped to the
    selected period, plus price return for the same window. Designed to be
    informative even when the window holds zero annual snapshots — common
    for new IPOs or short look-backs."""
    import statistics
    roe_series = [h.return_on_equity for h in history_window
                  if h.return_on_equity is not None]
    eg_series = [h.earnings_growth for h in history_window
                 if h.earnings_growth is not None]
    de_series = [h.debt_to_equity for h in history_window
                 if h.debt_to_equity is not None]

    avg_roe = (sum(roe_series) / len(roe_series)) if roe_series else None
    eg_vol = statistics.stdev(eg_series) if len(eg_series) >= 2 else None
    latest_de = de_series[-1] if de_series else None

    def fmt_pct(v):
        return f"{v*100:.1f}%" if v is not None else "—"

    # Price-based stats — always work when we have any price data
    closes = [p["adj_close"] for p in prices_window if p.get("adj_close")]
    if closes:
        first, last = closes[0], closes[-1]
        ret_pct = (last / first - 1) * 100 if first else 0
        hi, lo = max(closes), min(closes)
        ret_str = f"{ret_pct:+.1f}%"
        ret_color = T.SUCCESS if ret_pct >= 0 else T.DANGER
        price_items = [
            ("Period return", html.Span(ret_str,
                                          style={"color": ret_color, "fontWeight": "700"})),
            ("Period high / low", f"${hi:.2f} / ${lo:.2f}"),
        ]
    else:
        price_items = [("Period return", "—"), ("Period high / low", "—")]

    eg_vol_str = "—"
    if eg_vol:
        label = "high (cyclical)" if eg_vol > 0.5 else "low (stalwart)"
        eg_vol_str = f"{eg_vol*100:.0f}% — {label}"

    annual_items = [
        ("Avg ROE (window)", fmt_pct(avg_roe)),
        ("Earnings volatility (stdev YoY)", eg_vol_str),
        ("Latest D/E (in window)",
         f"{latest_de:.0f}%" if latest_de is not None else "—"),
        ("Annual snapshots in window", f"{len(history_window)}"),
    ]

    rows = []
    for k, v in price_items + annual_items:
        rows.append(html.Tr([
            html.Td(html.Strong(k), className="small", style={"color": T.TEXT}),
            html.Td(v if not isinstance(v, str) else
                    html.Span(v, style={"color": T.PRIMARY}),
                    className="small"),
        ]))
    return html.Table(rows, className="table table-sm w-100")


def _build_price_summary(prices_window: list) -> str:
    closes = [p["adj_close"] for p in prices_window if p.get("adj_close")]
    if not closes:
        return ""
    first, last = closes[0], closes[-1]
    pct = (last / first - 1) * 100 if first else 0
    return f"${first:.2f} → ${last:.2f}  ·  {pct:+.1f}%  ·  {len(closes)} trading days"


def _build_coverage_text(prices_all: list, history_all: list,
                          prices_window: list, history_window: list,
                          period_days: int) -> str:
    """Tells the user what was available vs what fit in the window — important
    when a short window holds zero annual snapshots, or a new ticker has less
    history than the requested window."""
    parts = []
    if not prices_all:
        parts.append("no price history")
    else:
        first_date = min(p["date"] for p in prices_all)[:10]
        parts.append(f"prices from {first_date}")
    if period_days and prices_all and prices_window:
        n_total = len(prices_all)
        n_win = len(prices_window)
        if n_win < n_total * 0.95 and len(history_window) < len(history_all):
            parts.append(f"{len(history_window)}/{len(history_all)} annual snapshots in window")
    return "  ·  ".join(parts)


def _load_history(db_path: str, ticker: str) -> list:
    """Pull annual fundamentals snapshots as HistoryPoint objects.

    Uses the cache-aside loader so first-time tickers self-heal from akshare.
    Returns HistoryPoint instances matching
    analysis.research_orchestrator.HistoryPoint, so chart factories accept them.
    """
    from analysis.data_loader import get_or_fetch_fundamentals_history
    from analysis.research_orchestrator import HistoryPoint
    from storage.database import Database

    db = Database(db_path)
    rows = get_or_fetch_fundamentals_history(ticker, db)
    return [HistoryPoint(
        date=_coerce_date(r.get("snapshot_date")),
        eps_ttm=_coerce_float(r.get("eps_ttm")),
        bps=_coerce_float(r.get("bps")),
        shares_outstanding=_coerce_float(r.get("shares_outstanding")),
        return_on_equity=_coerce_float(r.get("return_on_equity")),
        return_on_assets=_coerce_float(r.get("return_on_assets")),
        profit_margins=_coerce_float(r.get("profit_margins")),
        debt_to_equity=_coerce_float(r.get("debt_to_equity")),
        earnings_growth=_coerce_float(r.get("earnings_growth")),
        revenue_growth=_coerce_float(r.get("revenue_growth")),
    ) for r in rows]


def _load_prices(db_path: str, ticker: str) -> list[dict]:
    """Pull all historical prices for a ticker via the cache-aside loader.
    Cache hit: instant. Cache miss: fetches from yfinance (~3-5s)."""
    from analysis.data_loader import get_or_fetch_prices
    from storage.database import Database

    db = Database(db_path)
    rows = get_or_fetch_prices(ticker, db, period="10y")
    return [{"date": _coerce_date_str(r.get("date")),
             "adj_close": _coerce_float(r.get("adj_close"))} for r in rows]


def _coerce_float(v):
    """Postgres NUMERIC -> Python Decimal; coerce to float for downstream
    chart factories that expect plain numerics. None passes through."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _coerce_date(v):
    """SQLite returns date strings, Postgres returns datetime.date. Normalize
    to ISO string so HistoryPoint comparisons (`h.date >= cutoff_iso`) work
    the same regardless of backend."""
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)[:10]


def _coerce_date_str(v):
    return _coerce_date(v)


def _build_fs_meta(fs: dict) -> tuple:
    """Return (source_pill, coverage) Spans for Section 3b header."""
    all_rows = (fs.get("income", []) + fs.get("balance", []) + fs.get("cashflow", []))
    if not all_rows:
        return (dbc.Badge("unavailable", color="secondary", className="small"), "")
    sources = {r.get("source") for r in all_rows if r.get("source")}
    src_label = "/".join(sorted(sources)) if sources else "unknown"
    src_color = "info" if src_label == "yfinance" else (
        "warning" if "akshare" in src_label else "secondary")
    pill = dbc.Badge(f"source: {src_label}", color=src_color, className="small")

    # Coverage: oldest -> newest period_end_date per statement type
    n = {k: len(fs.get(k, [])) for k in ("income", "balance", "cashflow")}
    coverage = f"{n['income']} income · {n['balance']} balance · {n['cashflow']} cashflow periods"
    return (pill, coverage)


def _load_dcf_inputs_only(db_path: str, ticker: str) -> Optional[DCFInputs]:
    """Fast path: build only what DCF needs, without running FactorScoringEngine
    or screen predicates over the universe. Used by the recompute_dcf slider
    callback which fires on every slider tick.

    Reads via the storage factory so cloud DB is used when USE_CLOUD_DB=true.
    Assumes the data is already cached (caller invoked _load_history /
    _load_prices via cache-aside earlier in render_report).

    Stashes the raw `eps_ttm` on the returned DCFInputs (via a side attribute
    set after instantiation) so the Section 5 walkthrough can display
    "EPS_TTM × Shares = Net income" without re-querying. Plain DCFInputs
    callers ignore the attribute."""
    from analysis.dcf import default_inputs_from_snapshot
    from storage.database import Database
    from storage.factory import get_prices_repo, get_fundamentals_repo

    db = Database(db_path)
    funds = get_fundamentals_repo(db)
    prices = get_prices_repo(db)

    # All fundamentals snapshots, newest first (annual + any daily yfinance)
    if hasattr(funds, "get_history"):
        rows = funds.get_history(ticker)
    else:
        rows = funds.get_history(ticker) if hasattr(funds, "get_history") else []
        if not rows:
            # SQLite fallback path
            with db.get_connection() as conn:
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM fundamentals_snapshots WHERE ticker=? ORDER BY snapshot_date ASC",
                    (ticker,)
                ).fetchall()]
    rows.reverse()  # newest first

    # Latest cached price (no fetch — keep slider snappy)
    if hasattr(prices, "latest_date"):
        latest_date = prices.latest_date(ticker)
        current_price = (prices.get_price_on_or_before(ticker, latest_date)
                          if latest_date else None)
    else:
        current_price = None
        with db.get_connection() as conn:
            r = conn.execute(
                "SELECT adj_close FROM historical_prices WHERE ticker=? ORDER BY date DESC LIMIT 1",
                (ticker,)
            ).fetchone()
            if r:
                current_price = r[0]

    growths = [_coerce_float(r.get("earnings_growth")) for r in rows[:5]]
    growths = [g for g in growths if g is not None]
    if growths:
        sorted_g = sorted(growths)
        median_g = sorted_g[len(sorted_g) // 2]
        default_growth = max(-0.05, min(0.20, median_g))
    else:
        default_growth = 0.08

    for r in rows:
        eps = _coerce_float(r.get("eps_ttm"))
        sh = _coerce_float(r.get("shares_outstanding"))
        if eps is not None and sh is not None:
            snap = {
                "eps_ttm": eps,
                "shares_outstanding": sh,
                "earnings_growth": default_growth,
                "last_price": current_price if current_price is not None
                              else _coerce_float(r.get("last_price")),
            }
            resolved = default_inputs_from_snapshot(snap)
            if resolved is None:
                return None
            # Resolver now returns (DCFInputs, GrowthProvenance). The slider
            # callback only needs DCFInputs but we stash eps_ttm on it so the
            # Section 5 walkthrough can show "EPS_TTM × Shares = Net income".
            inputs, _provenance = resolved
            inputs.eps_ttm = eps
            return inputs
    return None


def _generate_devil_advocate(r) -> html.Div:
    """Claude prompt for bear-case arguments."""
    from config.settings import CLAUDE_API_KEY
    try:
        import anthropic
        # Build a context dump
        fr = r.factor_result
        context = [f"Ticker: {r.ticker} ({r.name})", f"Sector: {r.sector}"]
        if fr:
            context.append(f"Composite percentile: {fr.composite_pctile}")
            context.append(f"V/Q/G/S: {fr.value_pctile}/{fr.quality_pctile}/"
                            f"{fr.growth_pctile}/{fr.sentiment_pctile}")
        passed = [s.name for s in r.screen_pass_fail if s.passed]
        if passed:
            context.append(f"Passes screens: {', '.join(passed)}")
        if r.risk_flags:
            context.append(f"Risk flags: {', '.join(f.label for f in r.risk_flags)}")
        if r.red_flags:
            context.append(f"Forensic flags: {', '.join(rf.title for rf in r.red_flags[:4])}")
        if r.default_dcf:
            mos = r.default_dcf.margin_of_safety
            context.append(f"DCF margin of safety: {mos*100:+.0f}%" if mos else "DCF MoS: NA")

        prompt = (
            "You are a skeptical short-seller. The user is considering BUYING the stock "
            "described below. Your job is to construct the strongest 3 arguments AGAINST "
            "this investment. Be specific to this ticker — use the numbers and flags "
            "provided. Focus on what could go wrong, not generic risks. Avoid hedging.\n\n"
            "Context:\n" + "\n".join(context) +
            "\n\nFormat: 3 numbered bullet points, each 2-3 sentences. Plain text, no markdown."
        )
        client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        return html.Div([html.P(p, style={"color": T.TEXT, "fontSize": "0.85rem",
                                          "marginBottom": "0.5rem"})
                          for p in text.split("\n") if p.strip()])
    except Exception as e:
        return html.P(f"Devil's advocate unavailable: {e}",
                      className="text-muted small fst-italic")


def _report_to_markdown(r, swot_s, swot_w, swot_o, swot_t,
                         strat_notes, val_notes, thesis) -> str:
    """Render a markdown export of the report including user notes."""
    lines = []
    lines.append(f"# Stock Research Report — {r.ticker}")
    lines.append("")
    lines.append(f"**Company**: {r.name}  ")
    lines.append(f"**Sector**: {r.sector}  ")
    lines.append(f"**Market cap**: ${r.market_cap/1e9:.1f}B  " if r.market_cap else "**Market cap**: NA  ")
    lines.append(f"**Current price**: ${r.current_price:.2f}  " if r.current_price else "**Current price**: NA  ")
    lines.append(f"**Watchlist**: {'Yes' if r.is_watchlist else 'No'}")
    lines.append("")

    fr = r.factor_result
    if fr:
        lines.append("## Factor Percentile Ranks (vs sector)")
        lines.append(f"- Value: {fr.value_pctile}")
        lines.append(f"- Quality: {fr.quality_pctile}")
        lines.append(f"- Growth: {fr.growth_pctile}")
        lines.append(f"- Sentiment: {fr.sentiment_pctile}")
        lines.append(f"- **Composite: {fr.composite_pctile}**")
        lines.append("")

    if r.screen_pass_fail:
        lines.append("## Screen Pass/Fail")
        for s in r.screen_pass_fail:
            mark = "PASS" if s.passed else "FAIL"
            lines.append(f"- {s.name}: **{mark}**")
        lines.append("")

    if r.risk_flags:
        lines.append("## Risk Flags")
        for f in r.risk_flags:
            lines.append(f"- {f.severity.upper()}: {f.label}")
        lines.append("")

    if r.red_flags:
        lines.append("## Forensic Red Flags")
        for rf in r.red_flags:
            lines.append(f"- **{rf.severity.upper()}** — {rf.title}: {rf.detail}")
        lines.append("")

    lines.append("## SWOT")
    lines.append(f"### Strengths\n{swot_s or '(none)'}\n")
    lines.append(f"### Weaknesses\n{swot_w or '(none)'}\n")
    lines.append(f"### Opportunities\n{swot_o or '(none)'}\n")
    lines.append(f"### Threats\n{swot_t or '(none)'}\n")

    lines.append("## CAGR")
    lines.append("| Horizon | Revenue | Earnings | BPS |")
    lines.append("|---|---|---|---|")
    for h in [5, 10, 15]:
        rev = (r.cagr_revenue or {}).get(h)
        earn = (r.cagr_earnings or {}).get(h)
        bps = (r.cagr_bps or {}).get(h)
        def fmt(v): return f"{v*100:+.1f}%" if v is not None else "—"
        lines.append(f"| {h}y | {fmt(rev)} | {fmt(earn)} | {fmt(bps)} |")
    lines.append("")

    if r.default_dcf:
        d = r.default_dcf
        lines.append("## DCF (default inputs)")
        lines.append(f"- Intrinsic value per share: ${d.intrinsic_value_per_share:.2f}")
        lines.append(f"- Current price: ${d.current_price:.2f}")
        mos = f"{d.margin_of_safety*100:+.0f}%" if d.margin_of_safety is not None else "NA"
        lines.append(f"- Margin of safety: {mos}")
        lines.append("")

    if strat_notes:
        lines.append("## Strategy Notes\n" + strat_notes + "\n")
    if val_notes:
        lines.append("## Valuation Notes\n" + val_notes + "\n")
    if thesis:
        lines.append("## Investment Thesis\n" + thesis + "\n")

    from datetime import datetime
    lines.append(f"\n---\n*Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} by hk-sentiment-scraper Stock Research.*")
    return "\n".join(lines)
