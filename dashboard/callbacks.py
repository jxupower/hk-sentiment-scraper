from datetime import datetime
from dash import Input, Output, ALL, html, dcc
import dash_bootstrap_components as dbc

from dashboard.charts import (
    sector_direction_cards, sector_sentiment_timeseries, ticker_breakdown_bar,
    sector_heatmap, direction_gauge, source_breakdown_pie,
    DIRECTION_COLORS,
)

DIRECTION_BADGE_COLOR = {"UP": "success", "DOWN": "danger", "MIXED": "warning", "NEUTRAL": "secondary"}


def register_callbacks(app, db_path: str, settings, watchlist: dict, yahoo_scraper):
    from storage.database import Database
    from storage.repository import (ArticleRepository, SentimentRepository,
                                     SignalRepository, SectorSignalRepository)

    db = Database(db_path)
    db.initialize()
    article_repo = ArticleRepository(db)
    sentiment_repo = SentimentRepository(db)
    signal_repo = SignalRepository(db)
    sector_signal_repo = SectorSignalRepository(db)

    @app.callback(Output("last-updated", "children"),
                  Input("auto-refresh", "n_intervals"),
                  Input("refresh-btn", "n_clicks"))
    def update_timestamp(*_):
        return datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    @app.callback(Output("sector-cards", "children"),
                  Input("auto-refresh", "n_intervals"),
                  Input("refresh-btn", "n_clicks"))
    def update_sector_cards(*_):
        signals = sector_signal_repo.get_latest_signals()
        return sector_direction_cards(signals)

    @app.callback(Output("sector-heatmap-container", "children"),
                  Input("auto-refresh", "n_intervals"),
                  Input("refresh-btn", "n_clicks"))
    def update_sector_heatmap(*_):
        signals = sector_signal_repo.get_latest_signals()
        fig = sector_heatmap(signals)
        return dbc.Card([
            dbc.CardHeader("Heatmap", className="fw-bold small"),
            dbc.CardBody(dcc.Graph(figure=fig, config={"displayModeBar": False})),
        ], style={"background": "#1a1a2e", "border": "1px solid #37474f"})

    app.clientside_callback(
        """
        function(n_clicks_list) {
            var ctx = dash_clientside.callback_context;
            if (!ctx.triggered || !ctx.triggered.length) return dash_clientside.no_update;
            var t = ctx.triggered[0];
            if (!t.value) return dash_clientside.no_update;
            try {
                return JSON.parse(t.prop_id.split('.')[0]).index;
            } catch(e) {
                return dash_clientside.no_update;
            }
        }
        """,
        Output("selected-sector", "data"),
        Input({"type": "sector-card", "index": ALL}, "n_clicks"),
        prevent_initial_call=True,
    )

    @app.callback(Output("debug-selected", "children"),
                  Input("selected-sector", "data"))
    def debug_show_selected(sector):
        return sector if sector else "(none)"

    @app.callback(
        Output("sector-detail-title", "children"),
        Output("sector-detail-placeholder", "style"),
        Output("sector-detail-content", "style"),
        Output("sector-gauge", "figure"),
        Output("sector-source-pie", "figure"),
        Output("sector-sentiment-ts", "figure"),
        Output("ticker-breakdown-bar", "figure"),
        Output("ticker-rows", "children"),
        Output("sector-ai-analysis", "children"),
        Output("sector-article-feed", "children"),
        Output("sector-direction-badge", "children"),
        Output("sector-direction-badge", "color"),
        Output("sector-confidence-text", "children"),
        Output("sector-signal-updated", "children"),
        Input("selected-sector", "data"),
        Input("auto-refresh", "n_intervals"),
    )
    def update_sector_detail(sector, _):
        if not sector:
            return (
                "Sector Detail",
                {"display": "block"},    # show placeholder
                {"display": "none"},     # hide content
                {}, {}, {}, {},
                [], None, None,
                "--", "secondary", "", "",
            )

        tickers = settings.get_tickers_for_sector(sector, watchlist)
        scores_24h = sentiment_repo.get_scores_for_sector(tickers, hours=24)
        sentiment_ts = sentiment_repo.get_sector_timeseries(tickers, hours=168)

        sector_signals = sector_signal_repo.get_latest_signals()
        sig = next((s for s in sector_signals if s["sector"] == sector), None)
        direction = sig["direction"] if sig else "NEUTRAL"
        confidence = sig["confidence"] if sig else 0.0
        avg_sent = sig["avg_sentiment_24h"] if sig else 0.0
        momentum = sig["avg_price_momentum"] if sig else 0.0
        computed_at = sig["computed_at"] if sig else None

        ticker_signals = signal_repo.get_latest_signals()
        sector_ticker_sigs = [s for s in ticker_signals if s.get("sector") == sector]

        gauge = direction_gauge(direction, confidence, avg_sent or 0)
        pie = source_breakdown_pie(scores_24h)
        ts_chart = sector_sentiment_timeseries(sentiment_ts, sector)
        breakdown = ticker_breakdown_bar(sector_ticker_sigs)
        ticker_rows = _build_ticker_rows(sector_ticker_sigs)
        ai_analysis = _generate_sector_analysis(sector, scores_24h, sig)
        feed = _build_article_feed(scores_24h)
        badge_color = DIRECTION_BADGE_COLOR.get(direction, "secondary")
        confidence_text = f"Confidence: {confidence:.0%} | Momentum: {momentum:+.2f}%"
        updated_text = f"Signal computed: {computed_at[:16]} UTC" if computed_at else ""

        return (
            sector,
            {"display": "none"},      # hide placeholder
            {"display": "block"},     # show content
            gauge, pie, ts_chart, breakdown,
            ticker_rows, ai_analysis, feed,
            direction, badge_color, confidence_text, updated_text,
        )

    @app.callback(Output("scraper-status", "children"),
                  Input("auto-refresh", "n_intervals"))
    def update_scraper_status(_):
        items = [
            ("RSS Feeds", True, ""),
            ("Yahoo Finance", True, ""),
            ("Reddit", settings.reddit_configured(), "Add keys in .env"),
            ("Claude AI", settings.claude_configured(), "Add key in .env"),
        ]
        return html.Div([
            html.P("Data Sources", className="text-muted fw-bold mb-1 small"),
            *[html.Div([
                html.Span("[ON] " if ok else "[--] ",
                          style={"color": "#00c853" if ok else "#607d8b"}),
                html.Span(name, className="text-light"),
                html.Span(f" - {note}" if note and not ok else "", className="text-muted"),
            ], className="small") for name, ok, note in items],
        ])


def _get_representative_price(tickers, yahoo_scraper):
    import pandas as pd
    for ticker in tickers:
        try:
            df = yahoo_scraper.fetch_price_history(ticker, period="1mo")
            if not df.empty:
                return df
        except Exception:
            pass
    return pd.DataFrame()


def _generate_sector_analysis(sector: str, scores: list, sig) -> html.Div:
    from config.settings import CLAUDE_API_KEY

    if not scores:
        return html.P("No recent articles available to generate analysis.",
                      className="text-muted small")

    if not CLAUDE_API_KEY:
        return html.P(
            "Add CLAUDE_API_KEY to .env to enable AI sector analysis.",
            className="text-muted small fst-italic",
        )

    article_lines = []
    for s in scores[:30]:
        score = s.get("final_score", 0) or 0
        label = "bullish" if score >= 0.05 else ("bearish" if score <= -0.05 else "neutral")
        title = s.get("title", "").replace(chr(10), " ")
        article_lines.append(f"- [{label:8s} {score:+.2f}] {title}")

    direction = sig["direction"] if sig else "NEUTRAL"
    avg_sent = sig["avg_sentiment_24h"] if sig else 0.0
    momentum = sig["avg_price_momentum"] if sig else 0.0
    article_count = sig["article_count_24h"] if sig else len(scores)

    articles_text = chr(10).join(article_lines)
    prompt = (
        f"You are a financial analyst specializing in Hong Kong and China equity markets. "
        f"Provide a concise but comprehensive analysis of the {sector} sector based on the "
        f"following recent news sentiment data." + chr(10) + chr(10) +
        f"Sector signal: {direction} (avg sentiment 24h: {avg_sent:+.3f}, "
        f"price momentum: {momentum:+.2f}%, articles: {article_count})" + chr(10) + chr(10) +
        f"Recent article headlines with sentiment scores:" + chr(10) +
        articles_text + chr(10) + chr(10) +
        "Provide:" + chr(10) +
        "1. A 2-3 sentence overview of the current market sentiment for this sector" + chr(10) +
        "2. Key themes or catalysts driving sentiment (positive and negative)" + chr(10) +
        "3. A brief outlook based on the signal and momentum" + chr(10) + chr(10) +
        "Keep the analysis factual and grounded in the headlines provided. "
        "Format as plain text paragraphs with numbered labels. No markdown."
    )

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        analysis_text = response.content[0].text.strip()
        paragraphs = [p.strip() for p in analysis_text.split(chr(10)) if p.strip()]
        return html.Div([
            html.P(para, className="text-light small mb-2") for para in paragraphs
        ])
    except Exception as e:
        return html.P(f"Analysis unavailable: {e}", className="text-muted small fst-italic")


def _build_ticker_rows(ticker_signals):
    if not ticker_signals:
        return [html.P("No ticker data yet.", className="text-muted small")]

    rows = []
    for s in ticker_signals:
        sent = s.get("avg_sentiment_24h") or 0
        momentum = s.get("price_momentum_5d") or 0
        color = DIRECTION_COLORS["UP"] if sent >= 0.05 else (
            DIRECTION_COLORS["DOWN"] if sent <= -0.05 else "#90a4ae"
        )
        rows.append(dbc.Row([
            dbc.Col(html.Strong(s["ticker"], className="text-light small"), width=3),
            dbc.Col(html.Span(f"{sent:+.3f}", style={"color": color, "fontWeight": "bold",
                                                      "fontSize": "0.85rem"}), width=3),
            dbc.Col(html.Span(f"{momentum:+.2f}%",
                              style={"color": "#00c853" if momentum >= 0 else "#d50000",
                                     "fontSize": "0.85rem"}), width=3),
            dbc.Col(html.Span(f"{s.get('article_count_24h', 0)} art",
                              className="text-muted", style={"fontSize": "0.75rem"}), width=3),
        ], className="mb-1 align-items-center"))
    return [
        dbc.Row([
            dbc.Col(html.Span("Ticker", className="text-muted",
                              style={"fontSize": "0.75rem"}), width=3),
            dbc.Col(html.Span("Sent 24h", className="text-muted",
                              style={"fontSize": "0.75rem"}), width=3),
            dbc.Col(html.Span("Mom 5d", className="text-muted",
                              style={"fontSize": "0.75rem"}), width=3),
            dbc.Col(html.Span("Volume", className="text-muted",
                              style={"fontSize": "0.75rem"}), width=3),
        ], className="mb-1"),
        *rows,
    ]


def _build_article_feed(scores):
    if not scores:
        return html.P("No recent articles for this sector.", className="text-muted small")

    rows = []
    for s in scores[:60]:
        score = s.get("final_score", 0) or 0
        color = "#00c853" if score >= 0.05 else ("#d50000" if score <= -0.05 else "#90a4ae")
        source_badge = {"rss": "info", "reddit": "warning", "yahoo": "primary"}.get(
            s.get("source", ""), "secondary")
        pub = s.get("published_at", "") or ""
        rows.append(html.Tr([
            html.Td(pub[:16] if pub else "--",
                    className="text-muted small", style={"whiteSpace": "nowrap"}),
            html.Td(dbc.Badge(s.get("source", "").upper(), color=source_badge, className="small")),
            html.Td(html.Span(s.get("ticker", ""), className="text-muted small fw-bold")),
            html.Td(html.A(s.get("title", ""), href=s.get("url", "#"),
                           target="_blank", className="text-light small")),
            html.Td(html.Span(f"{score:+.3f}", style={"color": color, "fontWeight": "bold"})),
        ]))

    return html.Table([
        html.Thead(html.Tr([
            html.Th("Date", className="text-muted small"),
            html.Th("Source", className="text-muted small"),
            html.Th("Ticker", className="text-muted small"),
            html.Th("Title", className="text-muted small"),
            html.Th("Score", className="text-muted small"),
        ])),
        html.Tbody(rows),
    ], className="table table-dark table-sm table-hover w-100")
