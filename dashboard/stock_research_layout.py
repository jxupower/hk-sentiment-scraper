"""Stock Research tab — Plain Bagel 6-step single-stock deep-dive UI.

Layout strategy: pre-render every section's components into the DOM with a
shared `stock-report-content` wrapper that's hidden until a ticker is selected.
On Enter/Submit, the callback updates ~30 outputs in one shot.
"""
from dash import dcc, html, dash_table
import dash_bootstrap_components as dbc

CARD_STYLE = {"background": "#1a1a2e", "border": "1px solid #37474f"}
INPUT_STYLE = {"background": "#263238", "color": "#eceff1", "border": "1px solid #37474f"}
TEXTAREA_STYLE = {**INPUT_STYLE, "minHeight": "80px", "fontFamily": "inherit"}

STATUS_OPTIONS = [
    {"label": " Raw (not yet researched)", "value": "raw"},
    {"label": " Researched", "value": "researched"},
    {"label": " Watchlist (good but expensive)", "value": "watchlist"},
    {"label": " Owned", "value": "owned"},
    {"label": " Rejected (researched, decided not to own)", "value": "rejected"},
]


def build_stock_research_tab() -> html.Div:
    return html.Div([
        # Caveat banner
        dbc.Alert([
            html.Strong("Single-stock deep research, "),
            "structured as Richard Coffin / The Plain Bagel's 6-step framework. ",
            "Honest gaps: DCF uses an FCF proxy (EPS × shares × 0.8) — adjust the slider; ",
            "akshare data is as-restated, not point-in-time; ",
            "no capex/insider/management-compensation data; ",
            "forensic detector is heuristic only.",
        ], color="info", className="small mb-3", dismissable=True),

        # Ticker selector
        dbc.Card([
            dbc.CardBody([
                dbc.Row([
                    dbc.Col([
                        html.Label("Ticker (e.g. 0700.HK)", className="text-muted small mb-1"),
                        dcc.Dropdown(
                            id="sr-ticker-select",
                            placeholder="Type to search HK tickers...",
                            search_value="", value=None, clearable=True,
                            style={"background": "#263238", "color": "#000"},
                        ),
                    ], width=6),
                    dbc.Col([
                        html.Label("Research status", className="text-muted small mb-1"),
                        dcc.Dropdown(
                            id="sr-status-select",
                            options=STATUS_OPTIONS, value=None,
                            placeholder="(not set)", clearable=True,
                            style={"background": "#263238", "color": "#000"},
                        ),
                    ], width=4),
                    dbc.Col([
                        html.Label(" ", className="small mb-1"),
                        dbc.Button("Load report", id="sr-load-btn",
                                   color="primary", size="sm", className="w-100"),
                    ], width=2),
                ], align="end"),
            ], style={"padding": "12px 16px"}),
        ], style=CARD_STYLE, className="mb-3"),

        # Placeholder shown until a ticker is loaded
        html.Div(id="sr-placeholder",
                 children=html.P("Pick a ticker above to generate a deep-research report.",
                                 className="text-muted text-center py-5"),
                 style={"display": "block"}),

        # Main report content — all in DOM, hidden until ticker loaded
        html.Div(id="sr-content", style={"display": "none"}, children=[
            # Header strip
            dbc.Card([
                dbc.CardBody([
                    dbc.Row([
                        dbc.Col([
                            html.H4(id="sr-header-name", className="text-light mb-0"),
                            html.Span(id="sr-header-sector", className="text-muted small"),
                        ], width=5),
                        dbc.Col([
                            html.Div([
                                html.Span("Current: ", className="text-muted small"),
                                html.Span(id="sr-header-price", className="text-info fw-bold"),
                            ]),
                            html.Div([
                                html.Span("Mkt cap: ", className="text-muted small"),
                                html.Span(id="sr-header-mcap", className="text-light"),
                            ]),
                        ], width=4),
                        dbc.Col(html.Div(id="sr-header-badges"), width=3,
                                className="text-end"),
                    ], align="center"),
                ], style={"padding": "12px 16px"}),
            ], style=CARD_STYLE, className="mb-3"),

            # Section 1 — Idea & Screening Context
            _section_card("1. Idea & Screening Context", "sr-section-idea", [
                html.Div(id="sr-screen-passes", className="mb-3"),
                dcc.Graph(id="sr-factor-bars", config={"displayModeBar": False}, figure={}),
            ]),

            # Section 2 — Business Overview
            _section_card("2. Business Overview", "sr-section-business", [
                # AI-generated description
                dbc.Card([
                    dbc.CardHeader("AI business summary", className="fw-bold small"),
                    dbc.CardBody(dcc.Loading(html.Div(id="sr-business-summary"),
                                             type="dot", color="#00c853")),
                ], style=CARD_STYLE, className="mb-2"),
                # SWOT 2x2
                dbc.Row([
                    dbc.Col(_swot_card("Strengths", "sr-swot-strengths", "#00c853"), width=6),
                    dbc.Col(_swot_card("Weaknesses", "sr-swot-weaknesses", "#ff8a65"), width=6),
                ], className="mb-2"),
                dbc.Row([
                    dbc.Col(_swot_card("Opportunities", "sr-swot-opportunities", "#90caf9"), width=6),
                    dbc.Col(_swot_card("Threats", "sr-swot-threats", "#d50000"), width=6),
                ], className="mb-3"),
                # Article feed
                dbc.Card([
                    dbc.CardHeader("Recent articles (30d)", className="fw-bold small"),
                    dbc.CardBody(html.Div(id="sr-article-feed",
                                          style={"maxHeight": "300px", "overflowY": "auto"})),
                ], style=CARD_STYLE),
            ]),

            # Section 3 — Financial Analysis
            _section_card("3. Financial Analysis", "sr-section-finance", [
                dbc.Row([
                    dbc.Col([
                        html.H6("CAGR (compound annual growth rate)",
                                className="text-light small mb-2"),
                        html.Div(id="sr-cagr-table"),
                    ], width=4),
                    dbc.Col([
                        dcc.Graph(id="sr-eps-chart", config={"displayModeBar": False},
                                  figure={}),
                    ], width=4),
                    dbc.Col([
                        dcc.Graph(id="sr-revenue-chart", config={"displayModeBar": False},
                                  figure={}),
                    ], width=4),
                ], className="mb-3"),
                dbc.Card([
                    dbc.CardHeader("Peer comparison (vs sector)",
                                  className="fw-bold small"),
                    dbc.CardBody(dcc.Graph(id="sr-peer-heatmap",
                                           config={"displayModeBar": False}, figure={})),
                ], style=CARD_STYLE, className="mb-2"),
                dbc.Card([
                    dbc.CardHeader("Forensic red flags",
                                  className="fw-bold small text-warning"),
                    dbc.CardBody(html.Div(id="sr-forensic-flags")),
                ], style=CARD_STYLE),
            ]),

            # Section 4 — Strategy & Management
            _section_card("4. Strategy & Management", "sr-section-strategy", [
                dbc.Row([
                    dbc.Col(dcc.Graph(id="sr-shares-chart",
                                       config={"displayModeBar": False}, figure={}), width=6),
                    dbc.Col(html.Div(id="sr-strategy-stats"), width=6),
                ], className="mb-3"),
                dbc.Card([
                    dbc.CardHeader("Strategy notes (your own)", className="fw-bold small"),
                    dbc.CardBody([
                        dbc.Textarea(id="sr-strategy-notes",
                                     placeholder="Notes on management quality, "
                                     "capital allocation, recent strategic moves, "
                                     "competitive positioning, etc.",
                                     style=TEXTAREA_STYLE),
                        html.P("Note: yfinance/akshare don't provide capex, insider trading, "
                               "or management compensation. Add manually based on filings.",
                               className="text-muted small fst-italic mt-1"),
                    ]),
                ], style=CARD_STYLE),
            ]),

            # Section 5 — Valuation
            _section_card("5. Valuation", "sr-section-valuation", [
                dbc.Card([
                    dbc.CardHeader("Relative valuation (vs sector + historical)",
                                  className="fw-bold small"),
                    dbc.CardBody([
                        dbc.Row([
                            dbc.Col(dcc.Graph(id="sr-pe-history",
                                               config={"displayModeBar": False},
                                               figure={}), width=6),
                            dbc.Col(dcc.Graph(id="sr-pb-history",
                                               config={"displayModeBar": False},
                                               figure={}), width=6),
                        ]),
                    ]),
                ], style=CARD_STYLE, className="mb-3"),
                dbc.Card([
                    dbc.CardHeader("DCF calculator (2-stage Gordon growth)",
                                  className="fw-bold small"),
                    dbc.CardBody([
                        dbc.Row([
                            dbc.Col([
                                html.Label("Growth Y1-5 (%)",
                                           className="text-muted small mb-1"),
                                dcc.Slider(id="sr-dcf-g15", min=-10, max=30, step=1, value=10,
                                           marks={-10: "-10", 0: "0", 15: "15", 30: "30"},
                                           tooltip={"placement": "bottom"}),
                            ], width=3),
                            dbc.Col([
                                html.Label("Growth Y6-10 (%)",
                                           className="text-muted small mb-1"),
                                dcc.Slider(id="sr-dcf-g610", min=0, max=15, step=1, value=5,
                                           marks={0: "0", 5: "5", 10: "10", 15: "15"},
                                           tooltip={"placement": "bottom"}),
                            ], width=3),
                            dbc.Col([
                                html.Label("Terminal growth (%)",
                                           className="text-muted small mb-1"),
                                dcc.Slider(id="sr-dcf-tg", min=0, max=4, step=0.25, value=2.5,
                                           marks={0: "0", 2: "2", 4: "4"},
                                           tooltip={"placement": "bottom"}),
                            ], width=3),
                            dbc.Col([
                                html.Label("WACC / discount rate (%)",
                                           className="text-muted small mb-1"),
                                dcc.Slider(id="sr-dcf-wacc", min=5, max=15, step=0.5, value=9,
                                           marks={5: "5", 9: "9", 15: "15"},
                                           tooltip={"placement": "bottom"}),
                            ], width=3),
                        ], className="mb-3"),
                        html.Div(id="sr-dcf-result", className="text-light"),
                        dcc.Graph(id="sr-dcf-sensitivity",
                                  config={"displayModeBar": False}, figure={}),
                    ]),
                ], style=CARD_STYLE, className="mb-3"),
                dbc.Card([
                    dbc.CardHeader("Valuation notes (your own)", className="fw-bold small"),
                    dbc.CardBody([
                        dbc.Textarea(id="sr-valuation-notes",
                                     placeholder="Your valuation thesis: how you arrived at "
                                     "growth/WACC assumptions, alternative scenarios, etc.",
                                     style=TEXTAREA_STYLE),
                    ]),
                ], style=CARD_STYLE),
            ]),

            # Section 6 — Notes & Review
            _section_card("6. Notes & Review", "sr-section-review", [
                dbc.Card([
                    dbc.CardHeader("Investment thesis", className="fw-bold small"),
                    dbc.CardBody([
                        dbc.Textarea(id="sr-thesis",
                                     placeholder="2-3 sentence bottom-line thesis. "
                                     "What's the core reason to own or avoid this stock?",
                                     style={**TEXTAREA_STYLE, "minHeight": "60px"}),
                    ]),
                ], style=CARD_STYLE, className="mb-2"),
                dbc.Card([
                    dbc.CardHeader([
                        html.Span("Devil's-advocate AI", className="fw-bold small me-2"),
                        dbc.Button("Generate counter-arguments", id="sr-devil-btn",
                                   color="warning", size="sm"),
                    ]),
                    dbc.CardBody(dcc.Loading(html.Div(id="sr-devil-output"),
                                             type="dot", color="#ff8a65")),
                ], style=CARD_STYLE, className="mb-2"),
                html.Div([
                    dbc.Button("Save all notes", id="sr-save-btn",
                               color="success", size="sm", className="me-2"),
                    dbc.Button("Export as Markdown", id="sr-export-btn",
                               color="info", size="sm"),
                    html.Span(id="sr-save-status", className="text-muted small ms-3"),
                    dcc.Download(id="sr-download"),
                ], className="text-end mb-3"),
            ]),
        ]),
    ])


def _section_card(title: str, section_id: str, body_components: list) -> dbc.Card:
    return dbc.Card([
        dbc.CardHeader(title, className="fw-bold"),
        dbc.CardBody(body_components),
    ], id=section_id, style=CARD_STYLE, className="mb-3")


def _swot_card(label: str, ta_id: str, color: str) -> dbc.Card:
    return dbc.Card([
        dbc.CardHeader(label, className="fw-bold small",
                       style={"color": color, "borderBottom": f"1px solid {color}"}),
        dbc.CardBody([
            dbc.Textarea(id=ta_id, placeholder="(auto-populated; edit freely)",
                          style={**TEXTAREA_STYLE, "minHeight": "100px"}),
        ]),
    ], style=CARD_STYLE)
