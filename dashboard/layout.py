from dash import dcc, html
import dash_bootstrap_components as dbc

from dashboard import theme as T
from dashboard.screener_layout import build_screener_tab
from dashboard.recommendations_layout import build_recommendations_tab
from dashboard.screens_layout import build_screens_tab
from dashboard.backtest_layout import build_backtest_tab
from dashboard.stock_research_layout import build_stock_research_tab


def build_layout(sectors: list[str]) -> html.Div:
    return html.Div([
        # Cross-tab navigation signal (Screener → Research auto-load, etc).
        # Payload: {"ticker": "0700.HK", "ts": 1234567890} — ts is a millisecond
        # epoch so two clicks on the same ticker still trigger a new render.
        dcc.Store(id="cross-tab-nav", data=None),
        _header_bar(),
        dbc.Container([
            dbc.Tabs([
                dbc.Tab(label="Sentiment", tab_id="tab-sentiment",
                        children=_sentiment_tab(sectors)),
                dbc.Tab(label="Screener", tab_id="tab-screener",
                        children=build_screener_tab()),
                dbc.Tab(label="Discovery", tab_id="tab-recommendations",
                        children=build_recommendations_tab()),
                dbc.Tab(label="Screens", tab_id="tab-screens",
                        children=build_screens_tab()),
                dbc.Tab(label="Backtest", tab_id="tab-backtest",
                        children=build_backtest_tab()),
                dbc.Tab(label="Stock Research", tab_id="tab-stock-research",
                        children=build_stock_research_tab()),
            ], id="main-tabs", active_tab="tab-sentiment", className="mb-4"),
        ], fluid=True, className="py-3", style={"maxWidth": "1600px"}),
    ], style={"background": T.BG, "minHeight": "100vh"})


def _sentiment_tab(sectors: list[str]) -> html.Div:
    return html.Div([
        dcc.Interval(id="auto-refresh", interval=60_000, n_intervals=0),
        dcc.Store(id="selected-sector", data=None),

        # Sector direction cards
        dbc.Row(id="sector-cards", className="mb-3 g-3"),

        # Debug indicator — shows selected sector on click
        html.Div([
            html.Span("Selected: ", className="text-muted small me-1"),
            html.Span(id="debug-selected", children="(none)", className="text-warning small fw-bold"),
        ], className="mb-2 ms-1"),

        # Main content row
        dbc.Row([
            # Left sidebar: controls + heatmap
            dbc.Col([
                _controls_panel(),
                html.Div(id="sector-heatmap-container", className="mt-3"),
            ], width=3),

            # Right: sector detail (always in DOM)
            dbc.Col([
                _sector_detail_panel(),
            ], width=9),
        ]),
    ])


def _header_bar():
    return html.Div([
        dbc.Container([
            dbc.Row([
                dbc.Col([
                    html.Span("HK Research", style={
                        "color": T.PRIMARY, "fontWeight": "800", "fontSize": "1.5rem",
                        "letterSpacing": "-0.02em",
                    }),
                    html.Span(" · Sentiment + Fundamentals + Backtest", style={
                        "color": T.TEXT_MUTED, "fontWeight": "500", "fontSize": "0.95rem",
                        "marginLeft": "8px",
                    }),
                ]),
                dbc.Col([
                    html.Span("Last updated: ",
                              style={"color": T.TEXT_FAINT, "fontSize": "0.8rem"}),
                    html.Span(id="last-updated",
                              style={"color": T.PRIMARY, "fontSize": "0.8rem",
                                     "fontWeight": "500"}),
                ], className="text-end"),
            ], align="center", className="w-100"),
        ], fluid=True, style={"maxWidth": "1600px"}),
    ], style={
        "background": T.CARD_BG,
        "borderBottom": f"1px solid {T.BORDER}",
        "padding": "16px 0",
        "marginBottom": "8px",
    })


def _controls_panel():
    return dbc.Card([
        dbc.CardHeader("Controls"),
        dbc.CardBody([
            dbc.Button("Refresh Now", id="refresh-btn", color="primary",
                       size="sm", className="w-100 mb-3"),
            html.Div(id="scraper-status",
                     style={"fontSize": T.FONT_SM, "color": T.TEXT_MUTED}),
        ]),
    ], style=T.CARD_STYLE)


def _sector_detail_panel():
    """Sector detail panel -- all components always in the DOM."""
    placeholder = html.P("Click a sector card above to see detailed analysis.",
                         style={"color": T.TEXT_MUTED, "textAlign": "center",
                                "padding": "32px 0"})
    return dbc.Card([
        dbc.CardHeader([
            dbc.Row([
                dbc.Col(html.Span(id="sector-detail-title", children="Sector Detail",
                                  style={"fontWeight": "600", "fontSize": "1.1rem",
                                         "color": T.TEXT})),
                dbc.Col([
                    dbc.Badge(id="sector-direction-badge", children="--",
                              color="secondary", className="me-2",
                              style={"fontSize": "0.85rem"}),
                    html.Span(id="sector-confidence-text",
                              style={"color": T.TEXT_MUTED, "fontSize": T.FONT_SM}),
                    html.Br(),
                    html.Span(id="sector-signal-updated",
                              style={"color": T.TEXT_FAINT, "fontSize": "0.7rem"}),
                ], className="text-end"),
            ], align="center"),
        ]),
        dbc.CardBody([
            html.Div(id="sector-detail-placeholder", children=placeholder),
            html.Div(id="sector-detail-content", style={"display": "none"}, children=[
                dbc.Row([
                    dbc.Col(dcc.Graph(id="sector-gauge",
                                     config={"displayModeBar": False}, figure={}),
                            width=5),
                    dbc.Col(dcc.Graph(id="sector-source-pie",
                                     config={"displayModeBar": False}, figure={}),
                            width=7),
                ], className="mb-3"),
                dcc.Graph(id="sector-sentiment-ts",
                          config={"displayModeBar": False}, figure={}),
                dbc.Card([
                    dbc.CardHeader("Ticker Breakdown (within sector)"),
                    dbc.CardBody([
                        dbc.Row([
                            dbc.Col(dcc.Graph(id="ticker-breakdown-bar",
                                             config={"displayModeBar": False},
                                             figure={}), width=5),
                            dbc.Col(html.Div(id="ticker-rows"), width=7),
                        ]),
                    ]),
                ], className="mb-3", style=T.CARD_STYLE_SOFT),
                dbc.Card([
                    dbc.CardHeader("AI Sector Analysis"),
                    dbc.CardBody(
                        dcc.Loading(html.Div(id="sector-ai-analysis"),
                                    type="dot", color=T.PRIMARY)
                    ),
                ], className="mb-3", style=T.CARD_STYLE_SOFT),
                dbc.Card([
                    dbc.CardHeader("Recent Articles"),
                    dbc.CardBody(html.Div(id="sector-article-feed")),
                ], style=T.CARD_STYLE_SOFT),
            ]),
        ]),
    ], style=T.CARD_STYLE)
