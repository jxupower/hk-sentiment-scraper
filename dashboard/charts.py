import pandas as pd
import plotly.graph_objects as go

DIRECTION_COLORS = {"UP": "#00c853", "DOWN": "#d50000", "MIXED": "#ffd600", "NEUTRAL": "#90a4ae"}
DIRECTION_ICONS  = {"UP": "▲", "DOWN": "▼", "MIXED": "◆", "NEUTRAL": "●"}
SENTIMENT_COLORSCALE = [[0, "#d50000"], [0.5, "#607d8b"], [1, "#00c853"]]


def sector_direction_cards(sector_signals: list[dict]) -> list:
    from dash import html
    import dash_bootstrap_components as dbc
    if not sector_signals:
        return [html.P("No sector data yet. Run a scrape first.", className="text-muted")]

    cards = []
    for s in sector_signals:
        direction = s.get("direction", "NEUTRAL")
        color = DIRECTION_COLORS.get(direction, "#90a4ae")
        icon = DIRECTION_ICONS.get(direction, "●")
        confidence = s.get("confidence") or 0
        sent = s.get("avg_sentiment_24h") or 0
        momentum = s.get("avg_price_momentum") or 0
        articles = s.get("article_count_24h") or 0

        cards.append(dbc.Col(
            html.Div([
                dbc.Card([
                    dbc.CardBody([
                        html.Div(icon, style={"fontSize": "2rem", "color": color, "lineHeight": "1"}),
                        html.H5(s["sector"], className="mb-1 mt-1 text-light fw-bold"),
                        html.Div(direction, style={"color": color, "fontWeight": "bold", "fontSize": "1rem"}),
                        html.Hr(style={"borderColor": "#37474f", "margin": "8px 0"}),
                        html.Div([
                            html.Span("Sentiment: ", className="text-muted small"),
                            html.Span(f"{sent:+.3f}", style={"color": color, "fontWeight": "bold"}),
                        ]),
                        html.Div([
                            html.Span("Momentum: ", className="text-muted small"),
                            html.Span(f"{momentum:+.2f}%",
                                      style={"color": "#00c853" if momentum >= 0 else "#d50000"}),
                        ]),
                        html.Div([
                            html.Span("Articles 24h: ", className="text-muted small"),
                            html.Span(str(articles), className="text-light"),
                        ]),
                        dbc.Progress(value=int(confidence * 100), color="info",
                                     style={"height": "4px", "marginTop": "8px"},
                                     className="bg-dark"),
                        html.Div(f"Confidence: {confidence:.0%}",
                                 className="text-muted", style={"fontSize": "0.7rem"}),
                    ], style={"padding": "12px"}),
                ], style={
                    "background": "#1a1a2e",
                    "border": f"2px solid {color}",
                }),
            ], id={"type": "sector-card", "index": s["sector"]},
               n_clicks=0,
               style={"cursor": "pointer"}),
            width=12, md=6, lg=4, xl=3, className="mb-3",
        ))
    return cards


def sector_sentiment_timeseries(df: pd.DataFrame, sector: str) -> go.Figure:
    if df.empty:
        fig = go.Figure()
        fig.add_annotation(text="No sentiment data yet for this sector",
                           xref="paper", yref="paper", x=0.5, y=0.5,
                           showarrow=False, font=dict(size=14, color="#90a4ae"))
        fig.update_layout(_dark_layout(f"{sector} — Sentiment History"))
        return fig

    ts = df.copy()
    ts["scored_at"] = pd.to_datetime(ts["scored_at"])
    ts = ts.set_index("scored_at")["final_score"].resample("2h").mean().dropna().reset_index()
    ts.columns = ["time", "score"]

    colors = [DIRECTION_COLORS["UP"] if v >= 0 else DIRECTION_COLORS["DOWN"] for v in ts["score"]]
    fig = go.Figure(go.Bar(x=ts["time"], y=ts["score"], marker_color=colors))
    fig.add_hline(y=0.15, line_dash="dot", line_color=DIRECTION_COLORS["UP"],
                  annotation_text="Bullish")
    fig.add_hline(y=-0.15, line_dash="dot", line_color=DIRECTION_COLORS["DOWN"],
                  annotation_text="Bearish")
    fig.update_layout(_dark_layout(f"{sector} — Sector Sentiment (2h buckets)"),
                      yaxis_title="Avg Sentiment Score", yaxis=dict(range=[-1.1, 1.1]),
                      showlegend=False)
    return fig


def ticker_breakdown_bar(ticker_signals: list[dict]) -> go.Figure:
    if not ticker_signals:
        fig = go.Figure()
        fig.update_layout(_dark_layout("Ticker Sentiment Breakdown"))
        return fig

    tickers = [s["ticker"] for s in ticker_signals]
    scores = [s.get("avg_sentiment_24h") or 0 for s in ticker_signals]
    colors = [DIRECTION_COLORS["UP"] if v >= 0 else DIRECTION_COLORS["DOWN"] for v in scores]

    fig = go.Figure(go.Bar(
        x=scores, y=tickers, orientation="h",
        marker_color=colors,
        text=[f"{v:+.3f}" for v in scores],
        textposition="outside",
    ))
    fig.add_vline(x=0, line_color="#607d8b", line_width=1)
    fig.update_layout(_dark_layout("Ticker Sentiment (24h)"),
                      xaxis=dict(range=[-1.1, 1.1]),
                      height=max(220, len(tickers) * 35 + 80))
    return fig


def sector_heatmap(sector_signals: list[dict]) -> go.Figure:
    if not sector_signals:
        fig = go.Figure()
        fig.add_annotation(text="No sector data yet", xref="paper", yref="paper",
                           x=0.5, y=0.5, showarrow=False, font=dict(size=14, color="#90a4ae"))
        fig.update_layout(_dark_layout("Sector Heatmap"))
        return fig

    sectors = [s["sector"] for s in sector_signals]
    scores = [s.get("avg_sentiment_24h") or 0 for s in sector_signals]
    fig = go.Figure(go.Heatmap(
        z=[[v] for v in scores], y=sectors, x=["Sentiment 24h"],
        colorscale=SENTIMENT_COLORSCALE, zmin=-1, zmax=1,
        text=[[f"{v:+.2f}"] for v in scores],
        texttemplate="%{text}",
    ))
    fig.update_layout(_dark_layout("Sector Sentiment Heatmap"),
                      height=max(250, len(sectors) * 40 + 60))
    return fig


def direction_gauge(direction: str, confidence: float, avg_sentiment: float) -> go.Figure:
    color = DIRECTION_COLORS.get(direction, "#90a4ae")
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=avg_sentiment,
        gauge={
            "axis": {"range": [-1, 1]},
            "bar": {"color": color},
            "steps": [
                {"range": [-1, -0.15], "color": "#3e0000"},
                {"range": [-0.15, 0.15], "color": "#263238"},
                {"range": [0.15, 1], "color": "#003300"},
            ],
        },
        title={"text": f"Direction: <b>{direction}</b><br><sup>Confidence: {confidence:.0%}</sup>"},
        number={"font": {"color": color}},
    ))
    fig.update_layout(_dark_layout(""), height=260, margin=dict(t=70, b=10, l=20, r=20))
    return fig


def source_breakdown_pie(scores: list[dict]) -> go.Figure:
    if not scores:
        fig = go.Figure()
        fig.add_annotation(text="No data", xref="paper", yref="paper",
                           x=0.5, y=0.5, showarrow=False)
        fig.update_layout(_dark_layout("Source Breakdown"))
        return fig

    from collections import Counter
    counts = Counter(s["source"] for s in scores)
    fig = go.Figure(go.Pie(
        labels=list(counts.keys()), values=list(counts.values()),
        hole=0.4,
        marker=dict(colors=["#1565c0", "#00838f", "#6a1b9a"]),
    ))
    fig.update_layout(_dark_layout("Article Sources"), height=260)
    return fig


def _dark_layout(title: str) -> dict:
    return dict(
        title={"text": title, "font": {"color": "#eceff1", "size": 13}},
        paper_bgcolor="#1a1a2e",
        plot_bgcolor="#16213e",
        font=dict(color="#eceff1", size=12),
        margin=dict(t=50, b=40, l=50, r=20),
        legend=dict(bgcolor="#1a1a2e", bordercolor="#37474f", borderwidth=1),
    )


# ============== Per-ticker chart factories (Stock Research tab) ==============

def multi_year_eps_chart(history: list) -> go.Figure:
    """Bar chart of EPS over years. history is list of HistoryPoint dataclass instances."""
    dates = [h.date for h in history if h.eps_ttm is not None]
    eps = [h.eps_ttm for h in history if h.eps_ttm is not None]
    if not dates:
        fig = go.Figure()
        fig.add_annotation(text="No EPS history available", xref="paper", yref="paper",
                           x=0.5, y=0.5, showarrow=False, font=dict(color="#90a4ae"))
        fig.update_layout(_dark_layout("EPS history"), height=220)
        return fig
    colors = [DIRECTION_COLORS["UP"] if e >= 0 else DIRECTION_COLORS["DOWN"] for e in eps]
    fig = go.Figure(go.Bar(
        x=dates, y=eps, marker_color=colors,
        text=[f"{e:.2f}" for e in eps], textposition="outside",
    ))
    fig.add_hline(y=0, line_color="#607d8b", line_width=1)
    fig.update_layout(_dark_layout("EPS history (annual)"),
                      yaxis_title="EPS",
                      height=240, margin=dict(t=40, b=40, l=60, r=20))
    return fig


def revenue_yoy_chart(history: list) -> go.Figure:
    """Bar chart of YoY revenue growth (%)."""
    dates = [h.date for h in history if h.revenue_growth is not None]
    rg = [h.revenue_growth * 100 for h in history if h.revenue_growth is not None]
    if not dates:
        fig = go.Figure()
        fig.add_annotation(text="No revenue-growth history", xref="paper", yref="paper",
                           x=0.5, y=0.5, showarrow=False, font=dict(color="#90a4ae"))
        fig.update_layout(_dark_layout("Revenue YoY %"), height=220)
        return fig
    colors = [DIRECTION_COLORS["UP"] if r >= 0 else DIRECTION_COLORS["DOWN"] for r in rg]
    fig = go.Figure(go.Bar(
        x=dates, y=rg, marker_color=colors,
        text=[f"{r:+.1f}%" for r in rg], textposition="outside",
    ))
    fig.add_hline(y=0, line_color="#607d8b", line_width=1)
    fig.update_layout(_dark_layout("Revenue growth YoY (%)"),
                      yaxis_title="Growth %",
                      height=240, margin=dict(t=40, b=40, l=60, r=20))
    return fig


def share_count_chart(history: list) -> go.Figure:
    """Line chart of shares_outstanding over time — dilution visualization."""
    dates = [h.date for h in history if h.shares_outstanding is not None]
    shares_b = [h.shares_outstanding / 1e9 for h in history if h.shares_outstanding is not None]
    if not dates:
        fig = go.Figure()
        fig.add_annotation(text="No share-count history", xref="paper", yref="paper",
                           x=0.5, y=0.5, showarrow=False, font=dict(color="#90a4ae"))
        fig.update_layout(_dark_layout("Share count"), height=220)
        return fig
    fig = go.Figure(go.Scatter(
        x=dates, y=shares_b, mode="lines+markers",
        line=dict(color="#ffd600", width=2), marker=dict(size=8),
        text=[f"{s:.2f}B" for s in shares_b],
    ))
    fig.update_layout(_dark_layout("Shares outstanding (billions)"),
                      yaxis_title="Shares (B)",
                      height=240, margin=dict(t=40, b=40, l=60, r=20))
    return fig


def historical_multiple_chart(history: list, prices: list, multiple: str = "pe") -> go.Figure:
    """Reconstruct historical P/E (or P/B) from per-share fields + as-of prices.
    For each history point with eps_ttm and a price near that date, plot the ratio."""
    pe_points = []
    # Build (date, ratio) by looking up the price at each history snapshot_date
    price_by_date = {p["date"]: p.get("adj_close") for p in prices}
    sorted_price_dates = sorted(price_by_date.keys())
    for h in history:
        per_share = h.eps_ttm if multiple == "pe" else h.bps
        if per_share is None or per_share <= 0:
            continue
        # Find latest price at-or-before this date
        eligible = [d for d in sorted_price_dates if d <= h.date]
        if not eligible:
            continue
        px = price_by_date[eligible[-1]]
        if px is None or px <= 0:
            continue
        ratio = px / per_share
        if 0 < ratio < 200:  # outlier clip
            pe_points.append((h.date, ratio))

    label = "P/E" if multiple == "pe" else "P/B"
    if not pe_points:
        fig = go.Figure()
        fig.add_annotation(text=f"Insufficient data to reconstruct {label} history",
                           xref="paper", yref="paper", x=0.5, y=0.5,
                           showarrow=False, font=dict(color="#90a4ae"))
        fig.update_layout(_dark_layout(f"Historical {label}"), height=220)
        return fig

    dates, ratios = zip(*pe_points)
    fig = go.Figure(go.Scatter(
        x=list(dates), y=list(ratios), mode="lines+markers",
        line=dict(color="#90caf9", width=2), marker=dict(size=8),
        name=label, text=[f"{r:.1f}x" for r in ratios],
    ))
    avg = sum(ratios) / len(ratios)
    fig.add_hline(y=avg, line_dash="dot", line_color="#ffd600",
                  annotation_text=f"avg {avg:.1f}x")
    fig.update_layout(_dark_layout(f"Historical {label} (year-end)"),
                      yaxis_title=f"{label} multiple",
                      height=240, margin=dict(t=40, b=40, l=60, r=20))
    return fig


def dcf_sensitivity_heatmap(grid_df, current_price: float = None,
                             x_label: str = "x", y_label: str = "y") -> go.Figure:
    """Heatmap of intrinsic value across a 2-variable grid (pd.DataFrame)."""
    if grid_df is None or grid_df.empty:
        fig = go.Figure()
        fig.add_annotation(text="No sensitivity grid", xref="paper", yref="paper",
                           x=0.5, y=0.5, showarrow=False, font=dict(color="#90a4ae"))
        fig.update_layout(_dark_layout("DCF sensitivity"), height=220)
        return fig

    z = grid_df.values
    # Color by margin-of-safety if current_price provided, else by value
    if current_price and current_price > 0:
        mos_z = [[(v / current_price - 1) * 100 if v else None for v in row] for row in z]
        text = [[f"{v:.1f}<br>({(v/current_price - 1)*100:+.0f}%)" if v else "" for v in row] for row in z]
        colorscale = [[0, "#d50000"], [0.5, "#263238"], [1, "#00c853"]]
    else:
        mos_z = z
        text = [[f"{v:.1f}" if v else "" for v in row] for row in z]
        colorscale = "Viridis"

    fig = go.Figure(go.Heatmap(
        z=mos_z, x=grid_df.columns, y=grid_df.index,
        colorscale=colorscale, text=text, texttemplate="%{text}",
        colorbar=dict(title="MoS %" if current_price else "Intrinsic"),
    ))
    fig.update_layout(_dark_layout("DCF sensitivity (intrinsic / margin of safety)"),
                      xaxis_title=x_label, yaxis_title=y_label,
                      height=320, margin=dict(t=50, b=50, l=80, r=20))
    return fig


def peer_scorecard_heatmap(scorecard) -> go.Figure:
    """Single-row heatmap of target ticker's percentile rank vs peers, by metric.
    Green = top of peer group on that metric (in higher-is-better sense)."""
    if scorecard is None or not scorecard.metrics:
        fig = go.Figure()
        fig.add_annotation(text="No peer data", xref="paper", yref="paper",
                           x=0.5, y=0.5, showarrow=False, font=dict(color="#90a4ae"))
        fig.update_layout(_dark_layout("Peer comparison"), height=180)
        return fig

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
        colorscale=[[0, "#d50000"], [0.5, "#263238"], [1, "#00c853"]],
        zmin=0, zmax=100,
        text=[text_cells], texttemplate="%{text}",
        showscale=False,
    ))
    fig.update_layout(_dark_layout(
        f"vs {scorecard.n_peers} peers in {scorecard.sector}"),
        height=160, margin=dict(t=50, b=40, l=80, r=20))
    return fig

