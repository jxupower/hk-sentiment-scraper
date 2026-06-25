"""Stock Research tab — Plain Bagel 6-step single-stock deep-dive UI.

Layout strategy: pre-render every section's components into the DOM with a
shared `stock-report-content` wrapper that's hidden until a ticker is selected.
On Enter/Submit, the callback updates ~30 outputs in one shot.
"""
from dash import dcc, html, dash_table
import dash_bootstrap_components as dbc

from dashboard import theme as T

CARD_STYLE = T.CARD_STYLE
INPUT_STYLE = T.INPUT_STYLE
TEXTAREA_STYLE = {**T.INPUT_STYLE, "minHeight": "80px", "fontFamily": "Inter, sans-serif"}

STATUS_OPTIONS = [
    {"label": " Raw (not yet researched)", "value": "raw"},
    {"label": " Researched", "value": "researched"},
    {"label": " Watchlist (good but expensive)", "value": "watchlist"},
    {"label": " Owned", "value": "owned"},
    {"label": " Rejected (researched, decided not to own)", "value": "rejected"},
]

# Period selector for Sections 4-5. 0 = MAX (no filter).
PERIOD_OPTIONS = [
    {"label": "1M",  "value": 30},
    {"label": "3M",  "value": 90},
    {"label": "6M",  "value": 180},
    {"label": "1Y",  "value": 365},
    {"label": "3Y",  "value": 1095},
    {"label": "5Y",  "value": 1825},
    {"label": "MAX", "value": 0},
]
DEFAULT_PERIOD_DAYS = 365


def build_stock_research_tab() -> html.Div:
    return html.Div([
        # Caveat banner
        dbc.Alert(id="sr-alert-banner", color="info",
                    className="small mb-3", dismissable=True,
                    children="Single-stock deep research, "
                             "structured as Richard Coffin / The Plain Bagel's "
                             "6-step framework. Honest gaps: DCF uses an FCF "
                             "proxy (EPS × shares × 0.8) — adjust the slider; "
                             "akshare data is as-restated, not point-in-time; "
                             "no capex/insider/management-compensation data; "
                             "forensic detector is heuristic only."),

        # Ticker selector
        dbc.Card([
            dbc.CardBody([
                dbc.Row([
                    dbc.Col([
                        html.Label("Ticker (e.g. 0700.HK)",
                                      id="sr-label-ticker",
                                      className="text-muted small mb-1"),
                        dcc.Dropdown(
                            id="sr-ticker-select",
                            placeholder="Type to search HK tickers...",
                            search_value="", value=None, clearable=True,
                        ),
                    ], width=6),
                    dbc.Col([
                        html.Label("Research status",
                                      id="sr-label-status",
                                      className="text-muted small mb-1"),
                        dcc.Dropdown(
                            id="sr-status-select",
                            options=STATUS_OPTIONS, value=None,
                            placeholder="(not set)", clearable=True,
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

        # One-shot trigger to populate the browse table once after the
        # initial page render. max_intervals=1 means it fires exactly once
        # and stops, no recurring overhead.
        dcc.Interval(id="sr-browse-trigger", interval=500, n_intervals=0,
                      max_intervals=1),

        # Placeholder shown until a ticker is loaded. Browse-mode for the
        # 75 sub-sector composites — clicking a row populates the ticker
        # dropdown with the `&NAME` value and auto-fires "Load report".
        # The whole placeholder Div is hidden as soon as the report renders.
        html.Div(id="sr-placeholder", style={"display": "block"}, children=[
            html.P("Pick a ticker above, or browse a sub-sector composite below.",
                    id="sr-placeholder-text",
                    className="text-muted text-center py-2"),
            dbc.Card([
                dbc.CardHeader([
                    html.Span("Sub-sector composites",
                                id="sr-browse-title",
                                className="fw-bold me-2"),
                    html.Span("— click a row to load the composite report",
                                id="sr-browse-subtitle",
                                className="text-muted small"),
                ]),
                dbc.CardBody(
                    dash_table.DataTable(
                        id="sr-subsector-browse-table",
                        columns=[
                            {"name": "Ticker",       "id": "ticker"},
                            {"name": "Sub-sector",   "id": "sub_sector"},
                            {"name": "Parent sector","id": "parent_sector"},
                            {"name": "Constituents", "id": "n_constituents",
                             "type": "numeric"},
                        ],
                        data=[],
                        page_size=20,
                        sort_action="native",
                        filter_action="native",
                        style_cell=T.DATATABLE_CELL,
                        style_cell_conditional=[
                            {"if": {"column_id": "ticker"}, "textAlign": "left",
                             "fontWeight": "600", "color": T.PRIMARY,
                             "cursor": "pointer", "textDecoration": "underline"},
                            {"if": {"column_id": "sub_sector"}, "textAlign": "left",
                             "cursor": "pointer"},
                            {"if": {"column_id": "parent_sector"}, "textAlign": "left",
                             "color": T.TEXT_MUTED, "fontSize": "0.85rem"},
                        ],
                        style_header=T.DATATABLE_HEADER,
                        style_filter=T.DATATABLE_FILTER,
                    ),
                ),
            ], style=CARD_STYLE),
        ]),

        # Main report content — all in DOM, hidden until ticker loaded
        html.Div(id="sr-content", style={"display": "none"}, children=[
            # Header strip
            dbc.Card([
                dbc.CardBody([
                    dbc.Row([
                        dbc.Col([
                            html.H4(id="sr-header-name", className="mb-0",
                                    style={"fontWeight": "700", "color": T.TEXT,
                                           "fontSize": "1.6rem", "letterSpacing": "-0.02em"}),
                            html.Div(html.Span(id="sr-header-sector",
                                      style={"color": T.TEXT_MUTED, "fontSize": "0.9rem"})),
                            # Sub-sector sits on its own line below the sector
                            # so peer-grouping context is visible at a glance.
                            html.Div(html.Span(id="sr-header-subsector",
                                      style={"color": T.TEXT_FAINT,
                                             "fontSize": "0.8rem",
                                             "fontStyle": "italic"})),
                        ], width=5),
                        dbc.Col([
                            html.Div("Current price", className="stat-label"),
                            html.Div(id="sr-header-price",
                                      style={"fontSize": "1.6rem", "fontWeight": "700",
                                             "color": T.PRIMARY, "lineHeight": "1.1"}),
                            html.Div([
                                html.Span("Mkt cap: ", style={"color": T.TEXT_FAINT,
                                                                "fontSize": "0.75rem"}),
                                html.Span(id="sr-header-mcap",
                                          style={"color": T.TEXT, "fontWeight": "600",
                                                 "fontSize": "0.85rem"}),
                            ], style={"marginTop": "4px"}),
                        ], width=4),
                        dbc.Col(html.Div(id="sr-header-badges"), width=3,
                                className="text-end"),
                    ], align="center"),
                ], style={"padding": "12px 16px"}),
            ], style=CARD_STYLE, className="mb-3"),

            # Composite-only cards. Default hidden; the render callback
            # toggles them to display:block when the user loads a `&NAME`
            # sub-sector composite ticker. The single-stock cards below
            # get the inverse treatment.
            dbc.Card([
                dbc.CardHeader([
                    html.Span("Sub-sector composite", className="fw-bold me-2"),
                    html.Span(id="sr-composite-summary",
                                className="text-muted small"),
                ]),
                dbc.CardBody([
                    dbc.Row([
                        dbc.Col(_stat_block("Constituents",
                                              "sr-composite-stat-count"),
                                width=2),
                        dbc.Col(_stat_block("Total mkt cap",
                                              "sr-composite-stat-mcap"),
                                width=2),
                        dbc.Col(_stat_block("Median P/E",
                                              "sr-composite-stat-pe"),
                                width=2),
                        dbc.Col(_stat_block("Median P/B",
                                              "sr-composite-stat-pb"),
                                width=2),
                        dbc.Col(_stat_block("Median ROE",
                                              "sr-composite-stat-roe"),
                                width=2),
                        dbc.Col(_stat_block("Median yield",
                                              "sr-composite-stat-div"),
                                width=2),
                    ], align="center"),
                ], style={"padding": "12px 16px"}),
            ], id="sr-section-composite-aggregate", style={**CARD_STYLE, "display": "none"},
                className="mb-3"),

            dbc.Card([
                dbc.CardHeader([
                    html.Span("Constituents", className="fw-bold me-2"),
                    html.Span("— click a ticker to drill into the single-stock view",
                                className="text-muted small"),
                ]),
                dbc.CardBody(
                    dash_table.DataTable(
                        id="sr-composite-table",
                        columns=[
                            {"name": "Ticker",        "id": "ticker"},
                            {"name": "Name",          "id": "name"},
                            {"name": "Sector",        "id": "sector"},
                            {"name": "Mkt cap (B)",   "id": "market_cap_b",
                             "type": "numeric"},
                            {"name": "Weight %",      "id": "weight",
                             "type": "numeric"},
                            {"name": "P/E",           "id": "trailing_pe",
                             "type": "numeric"},
                            {"name": "P/B",           "id": "price_to_book",
                             "type": "numeric"},
                            {"name": "ROE %",         "id": "roe_pct",
                             "type": "numeric"},
                            {"name": "Earn growth %", "id": "growth_pct",
                             "type": "numeric"},
                            {"name": "Yield %",       "id": "div_yield",
                             "type": "numeric"},
                            {"name": "Data %",        "id": "completeness_pct",
                             "type": "numeric"},
                        ],
                        data=[],
                        page_size=25,
                        sort_action="native",
                        filter_action="native",
                        style_cell=T.DATATABLE_CELL,
                        style_cell_conditional=[
                            {"if": {"column_id": "ticker"}, "textAlign": "left",
                             "fontWeight": "600", "color": T.PRIMARY,
                             "cursor": "pointer", "textDecoration": "underline"},
                            {"if": {"column_id": "name"}, "textAlign": "left"},
                            {"if": {"column_id": "sector"}, "textAlign": "left",
                             "color": T.TEXT_MUTED, "fontSize": "0.85rem"},
                        ],
                        style_header=T.DATATABLE_HEADER,
                        style_filter=T.DATATABLE_FILTER,
                    ),
                ),
            ], id="sr-section-composite-constituents",
                style={**CARD_STYLE, "display": "none"}, className="mb-3"),

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
                                             type="dot", color=T.PRIMARY)),
                ], style=CARD_STYLE, className="mb-2"),
                # SWOT 2x2
                dbc.Row([
                    dbc.Col(_swot_card("Strengths", "sr-swot-strengths", T.SUCCESS), width=6),
                    dbc.Col(_swot_card("Weaknesses", "sr-swot-weaknesses", T.WARNING), width=6),
                ], className="mb-2"),
                dbc.Row([
                    dbc.Col(_swot_card("Opportunities", "sr-swot-opportunities", T.INFO), width=6),
                    dbc.Col(_swot_card("Threats", "sr-swot-threats", T.DANGER), width=6),
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
                                style={"color": T.TEXT, "fontWeight": "600",
                                       "fontSize": "0.9rem", "marginBottom": "8px"}),
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

            # Section 3b — Financial Statements (Income / Balance / Cash Flow / Earnings)
            # Deferred load: render_report leaves this empty + hidden. The user
            # clicks "Load Financial Statements" to fetch them on demand. Saves
            # 3-8s on cold-cache tickers and ~300ms on warm-cache ones.
            _section_card("3b. Financial Statements", "sr-section-financials", [
                dbc.Row([
                    dbc.Col([
                        dbc.Button("Load Financial Statements",
                                   id="sr-fs-load-btn", color="primary",
                                   size="sm"),
                        html.Span(id="sr-fs-status", className="ms-2 small",
                                  style={"color": T.TEXT_MUTED}),
                    ], width="auto"),
                    dbc.Col(html.Span(id="sr-fs-source-pill",
                                        style={"color": T.TEXT_MUTED,
                                                "fontSize": "0.8rem"}),
                            width="auto"),
                    dbc.Col(html.Span(id="sr-fs-coverage",
                                        style={"color": T.TEXT_FAINT,
                                                "fontSize": "0.75rem"}),
                            className="text-end"),
                ], align="center", className="mb-2 g-2"),

                dcc.Loading(type="dot", color=T.PRIMARY, children=[
                    html.Div(id="sr-fs-tabs-wrapper", style={"display": "none"},
                              children=[
                        dbc.Tabs(id="sr-fs-tabs", active_tab="income",
                                  className="mb-3", children=[
                            dbc.Tab(label="Income", tab_id="income", children=[
                                dcc.Graph(id="sr-fs-income-chart",
                                          config={"displayModeBar": False}, figure={}),
                                html.Details([
                                    html.Summary("Show full income statement",
                                                  style={"color": T.PRIMARY, "cursor": "pointer",
                                                         "fontSize": "0.85rem", "fontWeight": "600",
                                                         "marginBottom": "8px"}),
                                    html.Div(id="sr-fs-income-table"),
                                ], className="mt-2"),
                            ]),
                            dbc.Tab(label="Balance Sheet", tab_id="balance", children=[
                                dcc.Graph(id="sr-fs-balance-chart",
                                          config={"displayModeBar": False}, figure={}),
                                html.Details([
                                    html.Summary("Show full balance sheet",
                                                  style={"color": T.PRIMARY, "cursor": "pointer",
                                                         "fontSize": "0.85rem", "fontWeight": "600",
                                                         "marginBottom": "8px"}),
                                    html.Div(id="sr-fs-balance-table"),
                                ], className="mt-2"),
                            ]),
                            dbc.Tab(label="Cash Flow", tab_id="cashflow", children=[
                                dcc.Graph(id="sr-fs-cashflow-chart",
                                          config={"displayModeBar": False}, figure={}),
                                html.Details([
                                    html.Summary("Show full cash flow statement",
                                                  style={"color": T.PRIMARY, "cursor": "pointer",
                                                         "fontSize": "0.85rem", "fontWeight": "600",
                                                         "marginBottom": "8px"}),
                                    html.Div(id="sr-fs-cashflow-table"),
                                ], className="mt-2"),
                            ]),
                            dbc.Tab(label="Earnings", tab_id="earnings", children=[
                                dcc.Graph(id="sr-fs-earnings-chart",
                                          config={"displayModeBar": False}, figure={}),
                                html.Div(id="sr-fs-earnings-table", className="mt-2"),
                            ]),
                        ]),
                    ]),
                ]),

                # AI Forensic Review — separate Claude pass over the cached
                # statement rows. Distinct from Section 3's rule-based
                # `Forensic red flags` card (that one is hand-coded heuristics
                # in analysis/forensic.py; this one is an LLM scan of the
                # actual line items).
                dbc.Card([
                    dbc.CardBody([
                        dbc.Row([
                            dbc.Col([
                                dbc.Button("AI Forensic Review",
                                           id="sr-forensic-ai-btn",
                                           color="primary", size="sm"),
                            ], width="auto"),
                            dbc.Col(
                                html.Span(
                                    "AI reviews quantitative line items only "
                                    "— no access to footnotes, MD&A, or "
                                    "auditor letters.",
                                    id="sr-forensic-ai-note",
                                    style={"color": T.TEXT_MUTED,
                                            "fontSize": "0.75rem",
                                            "fontStyle": "italic"}),
                            ),
                        ], align="center", className="g-2 mb-2"),
                        dcc.Loading(type="dot", color=T.PRIMARY, children=[
                            html.Div(id="sr-forensic-ai-output",
                                      className="mt-2"),
                        ]),
                    ], style={"padding": "14px 16px"}),
                ], style=CARD_STYLE, className="mt-3"),

                # AI Bull / Bear Stress Test — paired argumentation across the
                # full research context (factor scores, screens, risk + red
                # flags, DCF). Asks Claude to argue each side at maximum
                # conviction without balancing, then list 12-month monitoring
                # KPIs. Complements the Section 6 devil's-advocate (bear-only).
                dbc.Card([
                    dbc.CardBody([
                        dbc.Row([
                            dbc.Col([
                                dbc.Button("AI Bull / Bear Stress Test",
                                           id="sr-bullbear-btn",
                                           color="primary", size="sm"),
                            ], width="auto"),
                            dbc.Col(
                                html.Span(
                                    "Strongest case for each side, then "
                                    "3-5 KPIs to monitor over the next 12 "
                                    "months. Not balanced — read both.",
                                    id="sr-bullbear-note",
                                    style={"color": T.TEXT_MUTED,
                                            "fontSize": "0.75rem",
                                            "fontStyle": "italic"}),
                            ),
                        ], align="center", className="g-2 mb-2"),
                        dcc.Loading(type="dot", color=T.PRIMARY, children=[
                            html.Div(id="sr-bullbear-output",
                                      className="mt-2"),
                        ]),
                    ], style={"padding": "14px 16px"}),
                ], style=CARD_STYLE, className="mt-3"),
            ]),

            # Period selector — drives Sections 4 + 5
            dbc.Card([
                dbc.CardBody([
                    dbc.Row([
                        dbc.Col([
                            html.Span("Time period",
                                       style={"color": T.TEXT_MUTED,
                                              "fontSize": "0.75rem",
                                              "fontWeight": "600",
                                              "letterSpacing": "0.05em",
                                              "textTransform": "uppercase"}),
                            html.Span(" — sections 4 & 5 below",
                                       style={"color": T.TEXT_FAINT,
                                              "fontSize": "0.75rem"}),
                        ], width="auto", className="d-flex flex-column justify-content-center"),
                        dbc.Col(
                            dbc.RadioItems(
                                id="sr-period-select",
                                options=PERIOD_OPTIONS,
                                value=DEFAULT_PERIOD_DAYS,
                                inline=True,
                                className="btn-group sr-period-radio",
                                inputClassName="btn-check",
                                labelClassName="btn btn-outline-primary btn-sm",
                                labelCheckedClassName="active",
                            ),
                            width="auto",
                        ),
                        dbc.Col(
                            html.Span(id="sr-period-coverage",
                                       style={"color": T.TEXT_MUTED, "fontSize": "0.75rem"}),
                            className="text-end d-flex align-items-center justify-content-end"),
                    ], align="center", className="g-3"),
                ], style={"padding": "12px 16px"}),
            ], style=CARD_STYLE, className="mb-3"),

            # Section 4 — Strategy & Management
            _section_card("4. Strategy & Management", "sr-section-strategy", [
                # Price chart — primary canvas (daily resolution, scales to any period)
                dbc.Card([
                    dbc.CardHeader([
                        dbc.Row([
                            dbc.Col([
                                html.Span("Price history",
                                           className="fw-bold small me-2"),
                                html.Span(id="sr-price-summary",
                                           style={"color": T.TEXT_MUTED,
                                                  "fontSize": "0.8rem"}),
                            ], width="auto",
                                className="d-flex align-items-center"),
                            dbc.Col(
                                # Chart style toggle — line is the default
                                # (lightest payload + scales to any window);
                                # candle reveals intraday open/high/low/close
                                # using the same OHLC payload already loaded.
                                dbc.RadioItems(
                                    id="sr-price-chart-style",
                                    options=[
                                        {"label": "Line",   "value": "line"},
                                        {"label": "Candle", "value": "candle"},
                                    ],
                                    value="line", inline=True,
                                    className="btn-group",
                                    inputClassName="btn-check",
                                    labelClassName="btn btn-outline-primary btn-sm",
                                    labelCheckedClassName="active",
                                ),
                                className="text-end",
                            ),
                        ], align="center", className="g-2"),
                    ]),
                    dbc.CardBody(dcc.Graph(id="sr-price-chart",
                                           config={"displayModeBar": False}, figure={})),
                ], style=CARD_STYLE, className="mb-3"),
                # Annual strategy metrics — filtered to the selected window where applicable
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
                                html.Small(id="sr-dcf-g15-provenance",
                                            className="text-muted",
                                            style={"fontSize": "0.72rem",
                                                   "lineHeight": "1.2",
                                                   "display": "block",
                                                   "marginTop": "4px"}),
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
                        # Step-by-step DCF walkthrough — populated by
                        # recompute_dcf in lockstep with slider changes. Shows
                        # base FCF derivation, year-by-year projection, terminal
                        # value, sum-to-EV, per-share intrinsic, and MoS — so
                        # the result block below isn't a black box.
                        dbc.Card([
                            dbc.CardHeader(
                                "DCF walkthrough — every step from your "
                                "growth rates to the margin of safety",
                                className="fw-bold small"),
                            dbc.CardBody(
                                dcc.Loading(
                                    html.Div(id="sr-dcf-walkthrough"),
                                    type="dot", color=T.PRIMARY,
                                ),
                                style={"padding": "12px 16px"},
                            ),
                        ], style=CARD_STYLE, className="mb-3"),
                        html.Div(id="sr-dcf-result", style={"color": T.TEXT}),
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
                                             type="dot", color=T.WARNING)),
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

        # Slide-in drawer that opens when the user clicks a V/Q/G bar in the
        # Section 1 factor-percentile chart. Body is populated by a callback
        # in stock_research_callbacks.py that calls
        # FactorScoringEngine.breakdown_for(ticker, factor) on click.
        dbc.Offcanvas(
            id="sr-factor-breakdown-drawer",
            placement="end", is_open=False, scrollable=True,
            title="Factor percentile — how it was computed",
            children=html.Div(id="sr-factor-breakdown-body"),
            style={"width": "520px"},
        ),
    ])


def _stat_block(label: str, value_id: str) -> html.Div:
    """Hero number with small uppercase label above — used by the
    sub-sector composite aggregate-stats card."""
    return html.Div([
        html.Div(label, className="stat-label"),
        html.Div(id=value_id, className="hero-number",
                  style={"fontSize": "1.3rem"}),
    ])


def _section_card(title: str, section_id: str, body_components: list) -> dbc.Card:
    # Card header gets its own id (`<section_id>-title`) so the i18n
    # callback can flip the title text on language change without rebuilding
    # the whole card.
    return dbc.Card([
        dbc.CardHeader(title, id=f"{section_id}-title", className="fw-bold"),
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
