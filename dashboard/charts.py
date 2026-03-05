import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

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
                "cursor": "pointer",
            }, id={"type": "sector-card", "index": s["sector"]}),
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


def price_with_sentiment_overlay(price_df: pd.DataFrame, sentiment_df: pd.DataFrame,
                                 label: str) -> go.Figure:
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        subplot_titles=("Price (30 days)", "Avg Sentiment"),
                        row_heights=[0.65, 0.35], vertical_spacing=0.06)

    if not price_df.empty:
        fig.add_trace(go.Candlestick(
            x=price_df.index, open=price_df["Open"], high=price_df["High"],
            low=price_df["Low"], close=price_df["Close"], name="Price",
            increasing_line_color=DIRECTION_COLORS["UP"],
            decreasing_line_color=DIRECTION_COLORS["DOWN"],
        ), row=1, col=1)

    if not sentiment_df.empty and "scored_at" in sentiment_df.columns:
        ts = sentiment_df.copy()
        ts["scored_at"] = pd.to_datetime(ts["scored_at"])
        ts = ts.set_index("scored_at")["final_score"].resample("2h").mean().dropna().reset_index()
        fig.add_trace(go.Bar(
            x=ts["scored_at"], y=ts["final_score"], name="Sentiment",
            marker_color=[DIRECTION_COLORS["UP"] if v >= 0 else DIRECTION_COLORS["DOWN"]
                          for v in ts["final_score"]],
        ), row=2, col=1)

    layout = _dark_layout(f"{label} — Price & Sentiment")
    layout.update(showlegend=False, xaxis_rangeslider_visible=False)
    fig.update_layout(layout)
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
