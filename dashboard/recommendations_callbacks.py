import math
import os

import plotly.graph_objects as go
import dash_bootstrap_components as dbc
from dash import Input, Output, State, html
from dash.exceptions import PreventUpdate

from analysis.factor_scores import FactorScoringEngine
from dashboard import theme as T
# Reused single-ticker fast path for the lazy "Get price" cell.
from dashboard.screener_callbacks import _fetch_one_price

FLAG_COLORS = {"high": T.DANGER, "medium": T.WARNING, "low": T.INFO}


def register_recommendations_callbacks(app, db_path: str):
    sector_risk_path = os.path.join(os.path.dirname(db_path), "..", "config",
                                     "sector_risk.yaml")
    if not os.path.exists(sector_risk_path):
        # Fall back to repo-root relative
        sector_risk_path = os.path.join(os.path.dirname(__file__), "..", "config",
                                         "sector_risk.yaml")
    engine = FactorScoringEngine(db_path, sector_risk_path)

    # ----- i18n: flip every translatable element on language change -----
    @app.callback(
        Output("rec-alert-banner", "children"),
        Output("rec-stat-scorable-label", "children"),
        Output("rec-stat-disqualified-label", "children"),
        Output("rec-stat-flagged-label", "children"),
        Output("rec-refresh-btn", "children"),
        Output("rec-weights-title", "children"),
        Output("rec-label-value", "children"),
        Output("rec-label-quality", "children"),
        Output("rec-label-growth", "children"),
        Output("rec-label-sentiment", "children"),
        Output("rec-label-window", "children"),
        Output("rec-label-filters", "children"),
        Output("rec-label-min-composite", "children"),
        Output("rec-label-show", "children"),
        Output("rec-label-sector", "children"),
        Output("rec-show-filter", "options"),
        Output("rec-sector-filter", "placeholder"),
        Output("rec-dist-title", "children"),
        Output("rec-table-title", "children"),
        Output("rec-table", "columns"),
        Input("user-language", "data"),
    )
    def i18n_discovery(lang):
        from dash import html as _h
        from dashboard.i18n import T as I
        lang = lang or "en"
        # Reconstruct the alert banner with translated parts
        alert_children = [
            _h.Strong(I("discovery.alert.title", lang)),
            I("discovery.alert.body", lang),
        ]
        show_options = [
            {"label": " " + I("discovery.filter.include_flagged", lang),
             "value": "include_flagged"},
            {"label": " " + I("discovery.filter.include_dq", lang),
             "value": "include_dq"},
        ]
        cols = [
            {"name": I("discovery.col.ticker", lang),       "id": "ticker"},
            {"name": I("discovery.col.name", lang),         "id": "name"},
            {"name": I("discovery.col.sector", lang),       "id": "sector"},
            {"name": I("discovery.col.price", lang),        "id": "current_price"},
            {"name": I("discovery.col.composite", lang),    "id": "composite_pctile",
             "type": "numeric"},
            {"name": I("discovery.col.value", lang),        "id": "value_pctile",
             "type": "numeric"},
            {"name": I("discovery.col.quality", lang),      "id": "quality_pctile",
             "type": "numeric"},
            {"name": I("discovery.col.growth", lang),       "id": "growth_pctile",
             "type": "numeric"},
            {"name": I("discovery.col.sentiment", lang),    "id": "sentiment_pctile",
             "type": "numeric"},
            {"name": I("discovery.col.articles", lang),     "id": "article_count",
             "type": "numeric"},
            {"name": I("discovery.col.pe", lang),           "id": "trailing_pe",
             "type": "numeric"},
            {"name": I("discovery.col.roe", lang),          "id": "roe_display",
             "type": "numeric"},
            {"name": I("discovery.col.earn_growth", lang),  "id": "earn_growth_display",
             "type": "numeric"},
            {"name": I("discovery.col.mcap_b", lang),       "id": "market_cap_b",
             "type": "numeric"},
            {"name": I("discovery.col.status", lang),       "id": "status_badge"},
        ]
        return (
            alert_children,
            I("discovery.stat.scorable", lang),
            I("discovery.stat.disqualified", lang),
            I("discovery.stat.flagged", lang),
            I("discovery.btn.recompute", lang),
            I("discovery.weights.title", lang),
            I("discovery.weights.value", lang),
            I("discovery.weights.quality", lang),
            I("discovery.weights.growth", lang),
            I("discovery.weights.sentiment", lang),
            I("discovery.filter.window", lang),
            I("discovery.filter.show", lang),
            I("discovery.filter.min_composite", lang),
            I("discovery.filter.show", lang),
            I("discovery.filter.sector", lang),
            show_options,
            I("screener.ph.all_sectors", lang),
            I("discovery.dist_title", lang),
            I("discovery.table.title", lang),
            cols,
        )

    @app.callback(
        Output("rec-table", "data"),
        Output("rec-stat-scorable", "children"),
        Output("rec-stat-disqualified", "children"),
        Output("rec-stat-flagged", "children"),
        Output("rec-row-count", "children"),
        Output("rec-distribution-chart", "figure"),
        Output("rec-diagnostic-banner", "children"),
        Output("rec-diagnostic-banner", "is_open"),
        Output("rec-weights-normalized", "children"),
        Output("rec-sector-filter", "options"),
        Input("rec-auto-refresh", "n_intervals"),
        Input("rec-refresh-btn", "n_clicks"),
        Input("rec-weight-value", "value"),
        Input("rec-weight-quality", "value"),
        Input("rec-weight-growth", "value"),
        Input("rec-weight-sentiment", "value"),
        Input("rec-window-slider", "value"),
        Input("rec-min-composite-filter", "value"),
        Input("rec-show-filter", "value"),
        Input("rec-sector-filter", "value"),
        State("user-language", "data"),
        State("user-market", "data"),
    )
    def update_recommendations(_n, _clicks, w_val, w_qual, w_growth, w_sent,
                                window_days, min_composite, show_filter, sector_filter,
                                lang, market):
        from dashboard.i18n import T as I
        from config.settings import get_sector_label
        lang = lang or "en"
        market = (market or "HK").upper()
        weights = {
            "value":     max(0, int(w_val or 0)),
            "quality":   max(0, int(w_qual or 0)),
            "growth":    max(0, int(w_growth or 0)),
            "sentiment": max(0, int(w_sent or 0)),
        }
        window_days = window_days if window_days else 7
        min_composite = min_composite if min_composite is not None else 0
        show_filter = show_filter or []

        results, diag = engine.compute(
            weights=weights, sentiment_window_days=window_days, market=market,
        )

        # Apply filters
        filtered = []
        for r in results:
            if r.disqualified and "include_dq" not in show_filter:
                continue
            if r.flags and "include_flagged" not in show_filter:
                continue
            if not r.disqualified:
                if r.composite_pctile is None or r.composite_pctile < min_composite:
                    continue
            if sector_filter and r.sector not in sector_filter:
                continue
            filtered.append(r)

        table_data = [_format_row(r, lang=lang) for r in filtered]
        chart = _build_distribution_chart(results, show_filter)

        # Normalized weight display
        total_w = sum(weights.values())
        if total_w > 0:
            norm = {k: 100 * v / total_w for k, v in weights.items()}
            weights_str = I("discovery.weights.normalized", lang,
                              v=norm["value"], q=norm["quality"],
                              g=norm["growth"], s=norm["sentiment"])
        else:
            weights_str = I("discovery.weights.zero", lang)

        sector_set = sorted({r.sector for r in results if r.sector and r.sector != "—"})
        # Translate sector display labels in the dropdown; value stays English
        sector_options = [{"label": get_sector_label(s, lang), "value": s}
                            for s in sector_set]
        total = diag.scorable_count + diag.disqualified_count
        return (
            table_data,
            f"{diag.scorable_count:,}",
            f"{diag.disqualified_count:,}",
            f"{diag.flagged_count:,}",
            I("discovery.row_count", lang, count=len(filtered), total=total),
            chart,
            diag.note,
            bool(diag.note),
            weights_str,
            sector_options,
        )

    # Per-row lazy "Get price" — mirrors the Screener handler.
    @app.callback(
        Output("rec-table", "data", allow_duplicate=True),
        Input("rec-table", "active_cell"),
        State("rec-table", "data"),
        prevent_initial_call=True,
    )
    def fetch_price_on_click(active_cell, table_rows):
        if not active_cell or active_cell.get("column_id") != "current_price":
            raise PreventUpdate
        try:
            row = table_rows[active_cell["row"]]
        except (IndexError, TypeError):
            raise PreventUpdate
        # Accept either-language placeholder so a row populated under one
        # language still detects the re-click correctly under the other.
        from dashboard.i18n import EN, ZH
        placeholders = {EN.get("screener.get_price"), ZH.get("screener.get_price")}
        if row.get("current_price") not in placeholders:
            raise PreventUpdate
        ticker = row.get("ticker")
        if not ticker:
            raise PreventUpdate
        price = _fetch_one_price(db_path, ticker)
        row["current_price"] = round(price, 2) if price is not None else "—"
        return table_rows


def _format_row(r, lang: str = "en") -> dict:
    from dashboard.i18n import T as I
    from config.settings import get_sector_label
    def rnd(v, n=1):
        if v is None:
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        if math.isnan(f) or math.isinf(f):
            return None
        return round(f, n)

    # Status badge: prefer DQ over FLAG over OK
    if r.disqualified:
        status = f"DQ: {r.disqualification_reason[:30]}"
    elif r.flags:
        # Show highest-severity flag
        sev_order = {"high": 0, "medium": 1, "low": 2}
        worst = sorted(r.flags, key=lambda f: sev_order.get(f.severity, 99))[0]
        status = f"FLAG: {worst.label[:36]}"
    else:
        status = "OK"

    sector_raw = (r.sector or "—")[:25]
    return {
        "ticker": r.ticker,
        "name": (r.name or "")[:30],
        "sector": get_sector_label(sector_raw, lang) if sector_raw != "—" else "—",
        # Lazy-fetched: click the cell to populate. See the screener's
        # fetch_price_on_click handler for the matching pattern.
        "current_price": I("screener.get_price", lang),
        "composite_pctile": rnd(r.composite_pctile, 1),
        "value_pctile": rnd(r.value_pctile, 1),
        "quality_pctile": rnd(r.quality_pctile, 1),
        "growth_pctile": rnd(r.growth_pctile, 1),
        "sentiment_pctile": rnd(r.sentiment_pctile, 1),
        "article_count": r.article_count,
        "trailing_pe": rnd(r.trailing_pe, 1),
        "roe_display": rnd((r.return_on_equity or 0) * 100, 1)
                       if r.return_on_equity is not None else None,
        "earn_growth_display": rnd((r.earnings_growth or 0) * 100, 1)
                               if r.earnings_growth is not None else None,
        "market_cap_b": rnd(r.market_cap / 1e9, 1) if r.market_cap else None,
        "status_badge": status,
    }


def _build_distribution_chart(results: list, show_filter: list[str]) -> go.Figure:
    """Histogram of composite percentile, separated by status (OK / Flagged / DQ)."""
    ok_scores = [r.composite_pctile for r in results
                 if not r.disqualified and not r.flags and r.composite_pctile is not None]
    flag_scores = [r.composite_pctile for r in results
                   if not r.disqualified and r.flags and r.composite_pctile is not None]

    fig = go.Figure()
    if ok_scores:
        fig.add_trace(go.Histogram(
            x=ok_scores, name="OK", marker_color=T.SUCCESS,
            opacity=0.85, xbins=dict(size=5),
        ))
    if flag_scores:
        fig.add_trace(go.Histogram(
            x=flag_scores, name="Flagged", marker_color=T.WARNING,
            opacity=0.85, xbins=dict(size=5),
        ))

    if not ok_scores and not flag_scores:
        fig.add_annotation(text="No scorable results.", xref="paper", yref="paper",
                           x=0.5, y=0.5, showarrow=False,
                           font=dict(size=13, color=T.TEXT_MUTED))

    # Reference lines for typical percentile tiers
    for x, color in [(75, T.SUCCESS), (90, T.PRIMARY), (25, T.WARNING)]:
        fig.add_vline(x=x, line_dash="dot", line_color=color, opacity=0.5)

    fig.update_layout(**T.chart_layout(
        barmode="stack",
        xaxis_title="Composite Percentile (0=worst, 100=best)",
        yaxis_title="Ticker count",
        xaxis=dict(range=[0, 100], gridcolor=T.BORDER, linecolor=T.BORDER,
                   tickfont=dict(color=T.TEXT_MUTED)),
        height=240,
        margin=dict(l=60, r=30, t=20, b=40),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1,
                    bgcolor="rgba(255,255,255,0)"),
    ))
    return fig
