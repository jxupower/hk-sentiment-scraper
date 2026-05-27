import math
import os

import plotly.graph_objects as go
from dash import Input, Output, html
import dash_bootstrap_components as dbc

from analysis.factor_scores import FactorScoringEngine

FLAG_COLORS = {"high": "#d50000", "medium": "#ff8a65", "low": "#90caf9"}


def register_recommendations_callbacks(app, db_path: str):
    sector_risk_path = os.path.join(os.path.dirname(db_path), "..", "config",
                                     "sector_risk.yaml")
    if not os.path.exists(sector_risk_path):
        # Fall back to repo-root relative
        sector_risk_path = os.path.join(os.path.dirname(__file__), "..", "config",
                                         "sector_risk.yaml")
    engine = FactorScoringEngine(db_path, sector_risk_path)

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
    )
    def update_recommendations(_n, _clicks, w_val, w_qual, w_growth, w_sent,
                                window_days, min_composite, show_filter, sector_filter):
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
            weights=weights, sentiment_window_days=window_days,
        )

        # Apply filters
        filtered = []
        for r in results:
            if r.disqualified and "include_dq" not in show_filter:
                continue
            if r.flags and "include_flagged" not in show_filter:
                continue
            if "watchlist" in show_filter and not r.is_watchlist:
                continue
            if not r.disqualified:
                if r.composite_pctile is None or r.composite_pctile < min_composite:
                    continue
            if sector_filter and r.sector not in sector_filter:
                continue
            filtered.append(r)

        table_data = [_format_row(r) for r in filtered]
        chart = _build_distribution_chart(results, show_filter)

        # Normalized weight display
        total_w = sum(weights.values())
        if total_w > 0:
            norm = {k: 100 * v / total_w for k, v in weights.items()}
            weights_str = (f"(normalized: V {norm['value']:.0f}% / "
                           f"Q {norm['quality']:.0f}% / "
                           f"G {norm['growth']:.0f}% / "
                           f"S {norm['sentiment']:.0f}%)")
        else:
            weights_str = "(weights all zero — please set at least one)"

        sector_set = sorted({r.sector for r in results if r.sector and r.sector != "—"})
        sector_options = [{"label": s, "value": s} for s in sector_set]

        return (
            table_data,
            f"{diag.scorable_count:,}",
            f"{diag.disqualified_count:,}",
            f"{diag.flagged_count:,}",
            f"{len(filtered):,} of {diag.scorable_count + diag.disqualified_count:,} after filters",
            chart,
            diag.note,
            bool(diag.note),
            weights_str,
            sector_options,
        )


def _format_row(r) -> dict:
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
    elif r.is_watchlist:
        status = "★ WL"
    else:
        status = "OK"

    return {
        "ticker": r.ticker,
        "name": (r.name or "")[:30],
        "sector": (r.sector or "—")[:25],
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
            x=ok_scores, name="OK", marker_color="#69f0ae",
            opacity=0.85, xbins=dict(size=5),
        ))
    if flag_scores:
        fig.add_trace(go.Histogram(
            x=flag_scores, name="Flagged", marker_color="#ff8a65",
            opacity=0.85, xbins=dict(size=5),
        ))

    if not ok_scores and not flag_scores:
        fig.add_annotation(text="No scorable results.", xref="paper", yref="paper",
                           x=0.5, y=0.5, showarrow=False,
                           font=dict(size=13, color="#90a4ae"))

    # Reference lines for typical percentile tiers
    for x, color in [(75, "#69f0ae"), (90, "#00c853"), (25, "#ff8a65")]:
        fig.add_vline(x=x, line_dash="dot", line_color=color, opacity=0.5)

    fig.update_layout(
        paper_bgcolor="#1a1a2e", plot_bgcolor="#16213e",
        font=dict(color="#eceff1", size=11),
        barmode="stack",
        xaxis_title="Composite Percentile (0=worst, 100=best)",
        yaxis_title="Ticker count",
        xaxis=dict(range=[0, 100]),
        height=240,
        margin=dict(l=60, r=30, t=20, b=40),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1,
                    bgcolor="rgba(0,0,0,0)"),
    )
    return fig
