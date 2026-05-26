from collections import Counter

import plotly.graph_objects as go
from dash import Input, Output, html
import dash_bootstrap_components as dbc

from analysis.composite import ScoreEngine
from dashboard.recommendations_layout import REC_COLORS

REC_ORDER = ["STRONG BUY", "BUY", "HOLD", "SELL", "STRONG SELL"]


def register_recommendations_callbacks(app, db_path: str):
    engine = ScoreEngine(db_path)

    @app.callback(
        Output("rec-table", "data"),
        Output("rec-stat-total", "children"),
        Output("rec-stat-breakdown", "children"),
        Output("rec-row-count", "children"),
        Output("rec-distribution-chart", "figure"),
        Output("rec-diagnostic-banner", "children"),
        Output("rec-diagnostic-banner", "is_open"),
        Output("rec-weight-display", "children"),
        Input("rec-auto-refresh", "n_intervals"),
        Input("rec-refresh-btn", "n_clicks"),
        Input("rec-weight-slider", "value"),
        Input("rec-window-slider", "value"),
        Input("rec-regime-filter", "value"),
        Input("rec-tag-filter", "value"),
    )
    def update_recommendations(_n, _clicks, weight_pct, window_days, regime_filter, tag_filter):
        weight_pct = weight_pct if weight_pct is not None else 60
        window_days = window_days if window_days is not None else 7
        regime_filter = regime_filter or []
        tag_filter = tag_filter or []

        results, diag = engine.compute(
            valuation_weight=weight_pct / 100.0,
            sentiment_window_days=window_days,
        )

        filtered = [r for r in results
                    if (not regime_filter or r.regime in regime_filter)
                    and (not tag_filter or r.recommendation in tag_filter)]

        table_data = [_format_row(r) for r in filtered]
        breakdown = _build_breakdown(results)
        chart = _build_distribution_chart(results, regime_filter)
        weight_display = f"{weight_pct}% valuation / {100 - weight_pct}% sentiment"

        return (
            table_data,
            f"{len(results):,}",
            breakdown,
            f"{len(filtered):,} of {len(results):,} after filters",
            chart,
            diag.note,
            bool(diag.note) and diag.note != "Data depth looks reasonable.",
            weight_display,
        )


def _format_row(r) -> dict:
    import math
    def rnd(v, n=2):
        if v is None:
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        if math.isnan(f) or math.isinf(f):
            return None
        return round(f, n)
    return {
        "ticker": r.ticker,
        "name": (r.name or "")[:30],
        "sector": (r.sector or "—")[:25],
        "regime": r.regime,
        "recommendation": r.recommendation,
        "composite_score": rnd(r.composite_score, 2),
        "valuation_z": rnd(r.valuation_z, 2),
        "sentiment_z": rnd(r.sentiment_z, 2),
        "article_count_7d": r.article_count_7d,
        "trailing_pe": rnd(r.trailing_pe, 1),
        "price_to_book": rnd(r.price_to_book, 2),
        "dividend_yield": rnd(r.dividend_yield, 2),
        "market_cap_b": rnd(r.market_cap / 1e9, 1) if r.market_cap else None,
    }


def _build_breakdown(results: list):
    counts = Counter(r.recommendation for r in results)
    badges = []
    for rec in REC_ORDER:
        n = counts.get(rec, 0)
        color = REC_COLORS.get(rec, "#37474f")
        badges.append(
            html.Span([
                html.Span("●", style={"color": color, "marginRight": "4px"}),
                html.Span(rec, className="text-light small me-1",
                          style={"fontWeight": "bold"}),
                html.Span(f"({n})", className="text-muted small me-3"),
            ])
        )
    return html.Div(badges, className="d-flex flex-wrap")


def _build_distribution_chart(results: list, regime_filter: list[str]) -> go.Figure:
    """Stacked histogram of composite scores, colored by regime."""
    if not regime_filter:
        regime_filter = ["deep", "covered", "uncovered"]

    regime_colors = {"deep": "#ffd600", "covered": "#90caf9", "uncovered": "#607d8b"}
    fig = go.Figure()
    has_any = False
    for regime in regime_filter:
        scores = [r.composite_score for r in results
                  if r.regime == regime and r.composite_score is not None]
        if not scores:
            continue
        has_any = True
        fig.add_trace(go.Histogram(
            x=scores, name=regime.capitalize(),
            marker_color=regime_colors.get(regime, "#90a4ae"),
            opacity=0.85, xbins=dict(size=0.25),
        ))

    if not has_any:
        fig.add_annotation(text="No composite scores to display.",
                           xref="paper", yref="paper", x=0.5, y=0.5,
                           showarrow=False, font=dict(size=13, color="#90a4ae"))
        fig.update_layout(_dark_layout(""), height=200)
        return fig

    # Threshold lines
    for x, color in [(-1.5, "#d50000"), (-0.5, "#ff8a65"),
                     (0.5, "#69f0ae"), (1.5, "#00c853")]:
        fig.add_vline(x=x, line_dash="dot", line_color=color, opacity=0.5)

    fig.update_layout(
        _dark_layout(""),
        barmode="stack",
        xaxis_title="Composite Score (z-units)",
        yaxis_title="Ticker count",
        height=260,
        margin=dict(l=60, r=30, t=20, b=40),
        legend=dict(orientation="h", yanchor="bottom", y=1.02,
                    xanchor="right", x=1, bgcolor="rgba(0,0,0,0)"),
    )
    return fig


def _dark_layout(title: str) -> dict:
    return dict(
        title={"text": title, "font": {"color": "#eceff1", "size": 13}},
        paper_bgcolor="#1a1a2e",
        plot_bgcolor="#16213e",
        font=dict(color="#eceff1", size=11),
    )
