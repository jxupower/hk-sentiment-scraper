from dash import dcc, html
import dash_bootstrap_components as dbc

from dashboard import theme as T
from dashboard.i18n import T as i18n_T
from dashboard.market_layout import build_market_tab
from dashboard.screener_layout import build_screener_tab
from dashboard.recommendations_layout import build_recommendations_tab
from dashboard.backtest_layout import build_backtest_tab
from dashboard.stock_research_layout import build_stock_research_tab
from dashboard.risk_layout import build_risk_tab
from dashboard.portfolio_layout import build_portfolio_tab


# Tab definitions are factored out so the language-change callback can
# rebuild them with translated labels via Output("main-tabs", "children").
# Each entry is (slug-key, tab_id, child_builder); the child layout is
# already-built once (DOM stays stable), only the displayed label flips.
#
# Market tab is the default landing view (was Screener; replaced the old
# rule-based Screens tab which was tuned for HK and didn't work for US).
TAB_DEFS = [
    ("tab.market",     "tab-market",          build_market_tab),
    ("tab.screener",   "tab-screener",        build_screener_tab),
    ("tab.discovery",  "tab-recommendations", build_recommendations_tab),
    ("tab.backtest",   "tab-backtest",        build_backtest_tab),
    ("tab.research",   "tab-stock-research",  build_stock_research_tab),
    ("tab.risk",       "tab-risk",            build_risk_tab),
    ("tab.portfolio",  "tab-portfolio",       build_portfolio_tab),
    # Sentiment is the rightmost tab — fundamentals-driven workflow
    # (Market / Screener / Discovery / Research) is the primary entry point.
    ("tab.sentiment",  "tab-sentiment",       None),   # special: needs `sectors`
]


def build_tabs(lang: str, sectors: list[str]) -> list:
    """Construct the dbc.Tab list with labels translated per `lang`. Re-run
    by the language-toggle callback so every tab label flips on click."""
    tabs = []
    for key, tab_id, builder in TAB_DEFS:
        if builder is None:
            children = _sentiment_tab(sectors)
        else:
            children = builder()
        # `id=tab_id` (in addition to `tab_id=tab_id`) is required so the
        # `update_tab_labels` callback in dashboard.callbacks can target
        # this Tab's `label` prop directly on language change without
        # rebuilding the children (which would wipe in-tab editable state).
        tabs.append(dbc.Tab(label=i18n_T(key, lang), id=tab_id,
                              tab_id=tab_id, children=children))
    return tabs


def build_layout(sectors: list[str]) -> html.Div:
    return html.Div([
        # Cross-tab navigation signal (Screener → Research auto-load, etc).
        # Payload: {"ticker": "0700.HK", "ts": 1234567890} — ts is a millisecond
        # epoch so two clicks on the same ticker still trigger a new render.
        dcc.Store(id="cross-tab-nav", data=None),
        # Language toggle state (en / zh). Persisted in browser localStorage
        # so the choice survives page reloads + opening the dashboard in a
        # new browser tab on the same machine.
        dcc.Store(id="user-language", data="en", storage_type="local"),
        # Market toggle state (HK / US). Same persistence as user-language —
        # the choice scopes every tab's data queries (Screener universe,
        # Discovery rank, Stock Research, Backtest benchmark, Risk Forecast
        # indices, Portfolio holdings, Sentiment sector cards).
        dcc.Store(id="user-market", data="HK", storage_type="local"),
        # Startup-modal confirmation flag. Memory store (NOT local) so the
        # modal reappears on every fresh dashboard load — gives the user
        # one explicit chance to pick market + language before they read
        # any data. Data callbacks fire in the background while the modal
        # is up; the Confirm action writes the user's choices to the
        # localStorage-persisted stores above and the data callbacks
        # re-fire with the right language + market.
        dcc.Store(id="dashboard-init-confirmed", data=False,
                    storage_type="memory"),
        _startup_modal(),
        _header_bar(),
        dbc.Container([
            dbc.Tabs(id="main-tabs", active_tab="tab-market",
                       className="mb-4",
                       children=build_tabs("en", sectors)),
        ], fluid=True, className="py-3", style={"maxWidth": "1600px"}),
        # Snapshot of sectors for the tab-rebuild callback. Cheap dict
        # stored client-side; lets the server-side rebuild reuse the same
        # sector list without re-querying the DB on every language flip.
        dcc.Store(id="sentiment-sectors-store", data=sectors),
    ], style={"background": T.BG, "minHeight": "100vh"})


def _sentiment_tab(sectors: list[str]) -> html.Div:
    return html.Div([
        dcc.Interval(id="auto-refresh", interval=60_000, n_intervals=0),
        dcc.Store(id="selected-sector", data=None),

        # Sector direction cards
        dbc.Row(id="sector-cards", className="mb-3 g-3"),

        # Debug indicator — shows selected sector on click
        html.Div([
            html.Span("Selected: ", id="sentiment-selected-prefix",
                          className="text-muted small me-1"),
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


def _startup_modal() -> dbc.Modal:
    """Centered first-load modal that asks the user for market + language
    before they read any data. The data callbacks fire in the background
    while the modal is up; the Confirm click writes the picks to the
    localStorage-persisted `user-market` / `user-language` stores so the
    callbacks re-fire with the right language + market and replace any
    placeholder text the user might glimpse behind the modal.

    The modal is gated on `dashboard-init-confirmed` (memory store, fresh
    per page load), so a hard refresh re-shows it. The header EN / 中文
    and HK / US toggles remain as a mid-session quick-switch — they go
    through the same Stores."""
    return dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle("Welcome · 欢迎"), close_button=False),
        dbc.ModalBody([
            html.P("Pick your market and language to start. "
                    "数据正在后台加载，请先选择以确保以正确语言显示。",
                    className="text-muted small mb-3"),
            dbc.Row([
                dbc.Col([
                    html.Label("Market · 市场",
                                  className="stat-label mb-2 fw-bold"),
                    dbc.RadioItems(
                        id="startup-market-select",
                        options=[
                            {"label": "Hong Kong · 香港", "value": "HK"},
                            {"label": "United States · 美国", "value": "US"},
                        ],
                        value="HK",
                        className="d-block",
                        labelClassName="d-block mb-2",
                    ),
                ], width=6),
                dbc.Col([
                    html.Label("Language · 语言",
                                  className="stat-label mb-2 fw-bold"),
                    dbc.RadioItems(
                        id="startup-lang-select",
                        options=[
                            {"label": "English", "value": "en"},
                            {"label": "中文", "value": "zh"},
                        ],
                        value="en",
                        className="d-block",
                        labelClassName="d-block mb-2",
                    ),
                ], width=6),
            ]),
        ]),
        dbc.ModalFooter([
            dbc.Button("Confirm · 确认",
                         id="startup-confirm-btn",
                         color="primary", size="md"),
        ]),
    ],
        id="startup-modal",
        is_open=True,            # opens on every fresh load
        backdrop="static",       # click-outside cannot dismiss
        keyboard=False,          # Esc cannot dismiss
        centered=True,
        size="md",
    )


def _header_bar():
    return html.Div([
        dbc.Container([
            dbc.Row([
                dbc.Col([
                    html.Span(id="header-app-title",
                                children="Croissant Stock Analyser",
                                style={
                        "color": T.PRIMARY, "fontWeight": "800", "fontSize": "1.5rem",
                        "letterSpacing": "-0.02em",
                    }),
                    html.Span(id="header-app-tagline",
                                children=" · Sentiment + Fundamentals + Backtest",
                                style={
                        "color": T.TEXT_MUTED, "fontWeight": "500", "fontSize": "0.95rem",
                        "marginLeft": "8px",
                    }),
                ], width=8),
                dbc.Col([
                    html.Span(id="header-last-updated-prefix",
                                children="Last updated: ",
                                style={"color": T.TEXT_FAINT, "fontSize": "0.8rem"}),
                    html.Span(id="last-updated",
                                style={"color": T.PRIMARY, "fontSize": "0.8rem",
                                         "fontWeight": "500", "marginRight": "12px"}),
                    # Market toggle (HK / US). Both buttons start `outline=True`
                    # (neutral gray); the clientside init-sync callback in
                    # dashboard.callbacks paints the active one based on the
                    # localStorage-hydrated `user-market` store on first render.
                    # Hardcoding one as `outline=False` here would force the
                    # WRONG button to look active on first paint when the user's
                    # last session ended on the other market.
                    dbc.ButtonGroup([
                        dbc.Button("HK", id="market-hk-btn", color="primary",
                                     size="sm", outline=True, n_clicks=0),
                        dbc.Button("US", id="market-us-btn", color="primary",
                                     size="sm", outline=True, n_clicks=0),
                    ], size="sm", className="me-2"),
                    # English / 中文 segmented toggle. Same neutral-start pattern
                    # as the market toggle above — init-sync clientside callback
                    # paints the active button from the `user-language` Store on
                    # first render. Hardcoding "EN active" used to leave returning
                    # ZH users with a broken-looking toggle ("EN highlighted but
                    # page is Chinese").
                    dbc.ButtonGroup([
                        dbc.Button("EN", id="lang-en-btn", color="primary",
                                     size="sm", outline=True, n_clicks=0),
                        dbc.Button("中文", id="lang-zh-btn", color="primary",
                                     size="sm", outline=True, n_clicks=0),
                    ], size="sm"),
                ], width=4,
                    className="text-end d-flex align-items-center justify-content-end"),
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
        dbc.CardHeader("Controls", id="sentiment-controls-title"),
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
                    dbc.CardHeader("Ticker Breakdown (within sector)",
                                       id="sentiment-ticker-breakdown-title"),
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
                    dbc.CardHeader("AI Sector Analysis",
                                       id="sentiment-ai-title"),
                    dbc.CardBody(
                        dcc.Loading(html.Div(id="sector-ai-analysis"),
                                    type="dot", color=T.PRIMARY)
                    ),
                ], className="mb-3", style=T.CARD_STYLE_SOFT),
                dbc.Card([
                    dbc.CardHeader("Recent Articles",
                                       id="sentiment-articles-title"),
                    dbc.CardBody(html.Div(id="sector-article-feed")),
                ], style=T.CARD_STYLE_SOFT),
            ]),
        ]),
    ], style=T.CARD_STYLE)
