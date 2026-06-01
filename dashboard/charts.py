import pandas as pd
import plotly.graph_objects as go

from dashboard import theme as T

# Re-export for backward-compat: existing imports `from dashboard.charts import DIRECTION_COLORS`
DIRECTION_COLORS = T.DIRECTION_COLORS
DIRECTION_ICONS  = {"UP": "▲", "DOWN": "▼", "MIXED": "◆", "NEUTRAL": "●"}
SENTIMENT_COLORSCALE = [[0, T.DANGER], [0.5, T.NEUTRAL], [1, T.SUCCESS]]


# ============== Sentiment tab — sector direction cards (clickable) ==============

def sector_direction_cards(sector_signals: list[dict]) -> list:
    from dash import html
    import dash_bootstrap_components as dbc
    if not sector_signals:
        return [html.P("No sector data yet. Run a scrape first.",
                       style={"color": T.TEXT_MUTED})]

    cards = []
    for s in sector_signals:
        direction = s.get("direction", "NEUTRAL")
        color = DIRECTION_COLORS.get(direction, T.NEUTRAL)
        icon = DIRECTION_ICONS.get(direction, "●")
        confidence = s.get("confidence") or 0
        sent = s.get("avg_sentiment_24h") or 0
        momentum = s.get("avg_price_momentum") or 0
        articles = s.get("article_count_24h") or 0

        cards.append(dbc.Col(
            html.Div([
                dbc.Card([
                    # Colored top accent bar
                    html.Div(style={
                        "background": color, "height": "4px",
                        "borderTopLeftRadius": "12px", "borderTopRightRadius": "12px",
                    }),
                    dbc.CardBody([
                        # Sector name + direction badge
                        html.Div([
                            html.H6(s["sector"], style={
                                "color": T.TEXT, "fontWeight": "600",
                                "fontSize": "0.95rem", "margin": "0", "flex": "1",
                            }),
                            html.Span([
                                html.Span(icon, style={"marginRight": "4px"}),
                                direction,
                            ], style={
                                "color": color, "fontWeight": "600",
                                "fontSize": "0.7rem", "letterSpacing": "0.05em",
                                "padding": "3px 8px", "borderRadius": "6px",
                                "background": f"{color}1a",  # 10% alpha tint
                            }),
                        ], style={"display": "flex", "alignItems": "center",
                                  "justifyContent": "space-between"}),

                        # Big sentiment number — the hero metric
                        html.Div(f"{sent:+.3f}", style={
                            "fontSize": "1.8rem", "fontWeight": "700",
                            "color": color, "lineHeight": "1.1",
                            "marginTop": "10px", "letterSpacing": "-0.02em",
                        }),
                        html.Div("Sentiment 24h", style={
                            "color": T.TEXT_FAINT, "fontSize": "0.7rem",
                            "textTransform": "uppercase", "letterSpacing": "0.05em",
                            "marginBottom": "12px",
                        }),

                        # Sub-metrics row
                        html.Div([
                            html.Div([
                                html.Div("Momentum",
                                          style={"color": T.TEXT_FAINT, "fontSize": "0.7rem",
                                                 "textTransform": "uppercase",
                                                 "letterSpacing": "0.05em"}),
                                html.Div(f"{momentum:+.2f}%", style={
                                    "color": T.SUCCESS if momentum >= 0 else T.DANGER,
                                    "fontWeight": "600", "fontSize": "0.95rem",
                                }),
                            ], style={"flex": "1"}),
                            html.Div([
                                html.Div("Articles 24h",
                                          style={"color": T.TEXT_FAINT, "fontSize": "0.7rem",
                                                 "textTransform": "uppercase",
                                                 "letterSpacing": "0.05em"}),
                                html.Div(str(articles), style={
                                    "color": T.TEXT,
                                    "fontWeight": "600", "fontSize": "0.95rem",
                                }),
                            ], style={"flex": "1", "textAlign": "right"}),
                        ], style={"display": "flex", "marginBottom": "8px"}),

                        # Confidence progress
                        dbc.Progress(value=int(confidence * 100),
                                     style={"height": "4px",
                                            "background": T.BORDER},
                                     color="primary"),
                        html.Div(f"Confidence {confidence:.0%}", style={
                            "color": T.TEXT_FAINT, "fontSize": "0.65rem",
                            "marginTop": "4px",
                        }),
                    ], style={"padding": "16px"}),
                ], style={
                    "background": T.CARD_BG,
                    "border": f"1px solid {T.BORDER}",
                    "borderRadius": "12px",
                    "boxShadow": T.SHADOW_SM,
                    "overflow": "hidden",
                    "transition": "all 0.15s ease",
                    "cursor": "pointer",
                }, className="sector-card-hover"),
            ], id={"type": "sector-card", "index": s["sector"]},
               n_clicks=0),
            width=12, md=6, lg=4, xl=3, className="mb-3",
        ))
    return cards


# ============== Sentiment / sector charts ==============

def sector_sentiment_timeseries(df: pd.DataFrame, sector: str) -> go.Figure:
    if df.empty:
        fig = go.Figure()
        fig.add_annotation(text="No sentiment data yet for this sector",
                           xref="paper", yref="paper", x=0.5, y=0.5,
                           showarrow=False, font=dict(size=13, color=T.TEXT_FAINT))
        fig.update_layout(T.chart_layout(f"{sector} — Sentiment History"))
        return fig

    ts = df.copy()
    ts["scored_at"] = pd.to_datetime(ts["scored_at"])
    ts = ts.set_index("scored_at")["final_score"].resample("2h").mean().dropna().reset_index()
    ts.columns = ["time", "score"]

    colors = [T.SUCCESS if v >= 0 else T.DANGER for v in ts["score"]]
    fig = go.Figure(go.Bar(x=ts["time"], y=ts["score"], marker_color=colors,
                            marker_line_width=0))
    fig.add_hline(y=0.15, line_dash="dot", line_color=T.SUCCESS, opacity=0.5,
                  annotation_text="Bullish", annotation_font_color=T.SUCCESS)
    fig.add_hline(y=-0.15, line_dash="dot", line_color=T.DANGER, opacity=0.5,
                  annotation_text="Bearish", annotation_font_color=T.DANGER)
    fig.update_layout(T.chart_layout(f"{sector} — Sector Sentiment (2h buckets)"),
                      yaxis_title="Avg Sentiment Score", yaxis=dict(range=[-1.1, 1.1]),
                      showlegend=False)
    return fig


def ticker_breakdown_bar(ticker_signals: list[dict]) -> go.Figure:
    if not ticker_signals:
        fig = go.Figure()
        fig.update_layout(T.chart_layout("Ticker Sentiment Breakdown"))
        return fig

    tickers = [s["ticker"] for s in ticker_signals]
    scores = [s.get("avg_sentiment_24h") or 0 for s in ticker_signals]
    colors = [T.SUCCESS if v >= 0 else T.DANGER for v in scores]

    fig = go.Figure(go.Bar(
        x=scores, y=tickers, orientation="h",
        marker_color=colors, marker_line_width=0,
        text=[f"{v:+.3f}" for v in scores],
        textposition="outside",
        textfont=dict(color=T.TEXT_MUTED, size=11),
    ))
    fig.add_vline(x=0, line_color=T.BORDER_STRONG, line_width=1)
    fig.update_layout(T.chart_layout("Ticker Sentiment (24h)"),
                      xaxis=dict(range=[-1.1, 1.1]),
                      height=max(220, len(tickers) * 35 + 80))
    return fig


def sector_heatmap(sector_signals: list[dict]) -> go.Figure:
    if not sector_signals:
        fig = go.Figure()
        fig.add_annotation(text="No sector data yet", xref="paper", yref="paper",
                           x=0.5, y=0.5, showarrow=False,
                           font=dict(size=13, color=T.TEXT_FAINT))
        fig.update_layout(T.chart_layout("Sector Heatmap"))
        return fig

    sectors = [s["sector"] for s in sector_signals]
    scores = [s.get("avg_sentiment_24h") or 0 for s in sector_signals]
    fig = go.Figure(go.Heatmap(
        z=[[v] for v in scores], y=sectors, x=["Sentiment 24h"],
        colorscale=SENTIMENT_COLORSCALE, zmin=-1, zmax=1,
        text=[[f"{v:+.2f}"] for v in scores],
        texttemplate="%{text}",
        textfont=dict(color="white", size=11),
        showscale=False,
    ))
    fig.update_layout(T.chart_layout("Sector Sentiment Heatmap"),
                      height=max(250, len(sectors) * 40 + 60))
    return fig


def direction_gauge(direction: str, confidence: float, avg_sentiment: float) -> go.Figure:
    color = DIRECTION_COLORS.get(direction, T.NEUTRAL)
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=avg_sentiment,
        gauge={
            "axis": {"range": [-1, 1], "tickcolor": T.TEXT_MUTED,
                     "tickfont": {"color": T.TEXT_MUTED, "size": 10}},
            "bar": {"color": color, "thickness": 0.7},
            "bgcolor": T.PLOT_BG,
            "borderwidth": 1,
            "bordercolor": T.BORDER,
            "steps": [
                {"range": [-1, -0.15], "color": T.DANGER_SOFT},
                {"range": [-0.15, 0.15], "color": T.PLOT_BG},
                {"range": [0.15, 1], "color": T.SUCCESS_SOFT},
            ],
        },
        title={"text": f"<b style='color:{T.TEXT}'>{direction}</b>"
                       f"<br><span style='color:{T.TEXT_MUTED};font-size:0.8em'>"
                       f"Confidence {confidence:.0%}</span>",
               "font": {"size": 16}},
        number={"font": {"color": color, "size": 32, "family": "Inter"}},
    ))
    fig.update_layout(T.chart_layout(""), height=260, margin=dict(t=70, b=10, l=20, r=20))
    return fig


def source_breakdown_pie(scores: list[dict]) -> go.Figure:
    if not scores:
        fig = go.Figure()
        fig.add_annotation(text="No data", xref="paper", yref="paper",
                           x=0.5, y=0.5, showarrow=False,
                           font=dict(color=T.TEXT_FAINT))
        fig.update_layout(T.chart_layout("Source Breakdown"))
        return fig

    from collections import Counter
    counts = Counter(s["source"] for s in scores)
    fig = go.Figure(go.Pie(
        labels=list(counts.keys()), values=list(counts.values()),
        hole=0.5,
        marker=dict(colors=[T.PRIMARY, T.ACCENT_2, T.ACCENT_4, T.ACCENT_3],
                   line=dict(color=T.CARD_BG, width=2)),
        textfont=dict(color="white", size=11, family="Inter"),
    ))
    fig.update_layout(T.chart_layout("Article Sources"), height=260)
    return fig


# ============== Per-ticker chart factories (Stock Research tab) ==============

def _empty_fig(title: str, msg: str, height: int = 220) -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(text=msg, xref="paper", yref="paper",
                       x=0.5, y=0.5, showarrow=False,
                       font=dict(color=T.TEXT_FAINT, size=12))
    fig.update_layout(T.chart_layout(title), height=height)
    return fig


def multi_year_eps_chart(history: list) -> go.Figure:
    dates = [h.date for h in history if h.eps_ttm is not None]
    eps = [h.eps_ttm for h in history if h.eps_ttm is not None]
    if not dates:
        return _empty_fig("EPS history", "No EPS history available")
    colors = [T.SUCCESS if e >= 0 else T.DANGER for e in eps]
    fig = go.Figure(go.Bar(
        x=dates, y=eps, marker_color=colors, marker_line_width=0,
        text=[f"{e:.2f}" for e in eps], textposition="outside",
        textfont=dict(color=T.TEXT_MUTED, size=10),
    ))
    fig.add_hline(y=0, line_color=T.BORDER_STRONG, line_width=1)
    fig.update_layout(T.chart_layout("EPS history (annual)"),
                      yaxis_title="EPS",
                      height=240, margin=dict(t=40, b=40, l=60, r=20))
    return fig


def revenue_yoy_chart(history: list) -> go.Figure:
    dates = [h.date for h in history if h.revenue_growth is not None]
    rg = [h.revenue_growth * 100 for h in history if h.revenue_growth is not None]
    if not dates:
        return _empty_fig("Revenue YoY %", "No revenue-growth history")
    colors = [T.SUCCESS if r >= 0 else T.DANGER for r in rg]
    fig = go.Figure(go.Bar(
        x=dates, y=rg, marker_color=colors, marker_line_width=0,
        text=[f"{r:+.1f}%" for r in rg], textposition="outside",
        textfont=dict(color=T.TEXT_MUTED, size=10),
    ))
    fig.add_hline(y=0, line_color=T.BORDER_STRONG, line_width=1)
    fig.update_layout(T.chart_layout("Revenue growth YoY (%)"),
                      yaxis_title="Growth %",
                      height=240, margin=dict(t=40, b=40, l=60, r=20))
    return fig


def share_count_chart(history: list) -> go.Figure:
    dates = [h.date for h in history if h.shares_outstanding is not None]
    shares_b = [h.shares_outstanding / 1e9 for h in history if h.shares_outstanding is not None]
    if not dates:
        return _empty_fig("Share count", "No annual share-count snapshots in window")
    fig = go.Figure(go.Scatter(
        x=dates, y=shares_b, mode="lines+markers",
        line=dict(color=T.PRIMARY, width=2.5),
        marker=dict(size=8, color=T.PRIMARY, line=dict(color=T.CARD_BG, width=1.5)),
        text=[f"{s:.2f}B" for s in shares_b],
        fill="tozeroy", fillcolor="rgba(126, 92, 240, 0.08)",
    ))
    fig.update_layout(T.chart_layout("Shares outstanding (billions)"),
                      yaxis_title="Shares (B)",
                      height=240, margin=dict(t=40, b=40, l=60, r=20))
    return fig


def price_chart(prices: list, label: str = "Price") -> go.Figure:
    """Daily price line chart with period-return annotation. `prices` is a list
    of dicts {date, adj_close}, already filtered to the desired window."""
    points = [(p["date"], p["adj_close"]) for p in prices
              if p.get("adj_close") is not None]
    if not points:
        return _empty_fig(label, "No price data for this period")
    dates, closes = zip(*points)
    first, last = closes[0], closes[-1]
    pct = (last / first - 1) * 100 if first else 0
    color = T.SUCCESS if pct >= 0 else T.DANGER
    fill_rgba = ("rgba(22, 163, 74, 0.08)" if pct >= 0
                 else "rgba(220, 38, 38, 0.08)")
    fig = go.Figure(go.Scatter(
        x=list(dates), y=list(closes), mode="lines",
        line=dict(color=color, width=2),
        fill="tozeroy", fillcolor=fill_rgba,
        hovertemplate="%{x}<br>$%{y:.2f}<extra></extra>",
    ))
    fig.add_hline(y=first, line_dash="dot", line_color=T.TEXT_FAINT,
                   annotation_text=f"start ${first:.2f}",
                   annotation_font_color=T.TEXT_MUTED,
                   annotation_position="bottom right")
    fig.update_layout(T.chart_layout(f"{label} — {pct:+.1f}% over period"),
                      yaxis_title="Price",
                      height=280, margin=dict(t=40, b=40, l=60, r=20))
    return fig


def historical_multiple_chart(history: list, prices: list,
                              multiple: str = "pe",
                              min_date: str = None) -> go.Figure:
    """Reconstruct historical P/E or P/B by joining annual per-share data with
    daily prices. `min_date` optionally clips to a period window (ISO date)."""
    pe_points = []
    price_by_date = {p["date"]: p.get("adj_close") for p in prices}
    sorted_price_dates = sorted(price_by_date.keys())
    for h in history:
        if min_date and h.date < min_date:
            continue
        per_share = h.eps_ttm if multiple == "pe" else h.bps
        if per_share is None or per_share <= 0:
            continue
        eligible = [d for d in sorted_price_dates if d <= h.date]
        if not eligible:
            continue
        px = price_by_date[eligible[-1]]
        if px is None or px <= 0:
            continue
        ratio = px / per_share
        if 0 < ratio < 200:
            pe_points.append((h.date, ratio))

    label = "P/E" if multiple == "pe" else "P/B"
    if not pe_points:
        return _empty_fig(f"Historical {label}",
                          f"Insufficient annual data for {label} in this window")

    dates, ratios = zip(*pe_points)
    fig = go.Figure(go.Scatter(
        x=list(dates), y=list(ratios), mode="lines+markers",
        line=dict(color=T.PRIMARY, width=2.5),
        marker=dict(size=8, color=T.PRIMARY, line=dict(color=T.CARD_BG, width=1.5)),
        name=label, text=[f"{r:.1f}x" for r in ratios],
        fill="tozeroy", fillcolor="rgba(126, 92, 240, 0.08)",
    ))
    avg = sum(ratios) / len(ratios)
    fig.add_hline(y=avg, line_dash="dot", line_color=T.ACCENT_3,
                  annotation_text=f"avg {avg:.1f}x",
                  annotation_font_color=T.ACCENT_3)
    fig.update_layout(T.chart_layout(f"Historical {label} (annual)"),
                      yaxis_title=f"{label} multiple",
                      height=240, margin=dict(t=40, b=40, l=60, r=20))
    return fig


def dcf_sensitivity_heatmap(grid_df, current_price: float = None,
                             x_label: str = "x", y_label: str = "y") -> go.Figure:
    if grid_df is None or grid_df.empty:
        return _empty_fig("DCF sensitivity", "No sensitivity grid")

    z = grid_df.values
    if current_price and current_price > 0:
        mos_z = [[(v / current_price - 1) * 100 if v else None for v in row] for row in z]
        text = [[f"{v:.1f}<br>({(v/current_price - 1)*100:+.0f}%)" if v else ""
                  for v in row] for row in z]
        colorscale = [[0, T.DANGER], [0.5, "#f8f7fc"], [1, T.SUCCESS]]
    else:
        mos_z = z
        text = [[f"{v:.1f}" if v else "" for v in row] for row in z]
        colorscale = "Viridis"

    fig = go.Figure(go.Heatmap(
        z=mos_z, x=grid_df.columns, y=grid_df.index,
        colorscale=colorscale, text=text, texttemplate="%{text}",
        textfont=dict(color=T.TEXT, size=11, family="Inter"),
        colorbar=dict(title="MoS %" if current_price else "Intrinsic",
                     tickfont=dict(color=T.TEXT_MUTED, size=10)),
    ))
    fig.update_layout(T.chart_layout("DCF sensitivity (intrinsic / margin of safety)"),
                      xaxis_title=x_label, yaxis_title=y_label,
                      height=320, margin=dict(t=50, b=50, l=80, r=20))
    return fig


def peer_scorecard_heatmap(scorecard) -> go.Figure:
    if scorecard is None or not scorecard.metrics:
        return _empty_fig("Peer comparison", "No peer data", height=180)

    labels = [m.name for m in scorecard.metrics]
    pctiles = [m.target_percentile for m in scorecard.metrics]
    text_cells = []
    for m in scorecard.metrics:
        tv = f"{m.target_value:.2f}" if m.target_value is not None else "NA"
        pm = f"{m.peer_median:.2f}" if m.peer_median is not None else "NA"
        pct = f"{m.target_percentile:.0f}%" if m.target_percentile is not None else "NA"
        text_cells.append(f"{tv}<br>(pm {pm}, {pct})")

    fig = go.Figure(go.Heatmap(
        z=[pctiles], x=labels, y=[scorecard.target_ticker],
        colorscale=[[0, T.DANGER], [0.5, "#f8f7fc"], [1, T.SUCCESS]],
        zmin=0, zmax=100,
        text=[text_cells], texttemplate="%{text}",
        textfont=dict(color=T.TEXT, size=10, family="Inter"),
        showscale=False,
    ))
    fig.update_layout(T.chart_layout(
        f"vs {scorecard.n_peers} peers in {scorecard.sector}"),
        height=160, margin=dict(t=50, b=40, l=80, r=20))
    return fig
