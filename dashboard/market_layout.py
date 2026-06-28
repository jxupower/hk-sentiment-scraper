"""Market tab — default landing view of the dashboard.

Shows the major index for the active market (HK → Hang Seng, US → S&P 500
by default), with a big historical price chart + 4 KPI cards + a sortable
filterable constituent table. Replaces the old Screens tab.

Layout (top → bottom):
  1. Caveat banner
  2. Index picker (per-market radio group)
  3. Period selector (1Y / 3Y / 5Y / MAX)
  4. Big price chart
  5. 4 KPI cards (last close · period %Δ · YTD %Δ · max drawdown)
  6. Constituent table
"""
from dash import dcc, html, dash_table
import dash_bootstrap_components as dbc

from dashboard import theme as T


# Per-market index dropdown options. Each index symbol must exist in
# config/index_constituents.yaml AND the cache-aside layer in
# analysis/data_loader.get_or_fetch_prices must be able to resolve it via
# yfinance. ^IXIC and ^RUT have prices but no constituent list (we don't
# scrape Wikipedia for those) — the table degrades gracefully when picked.
INDEX_OPTIONS_HK = [
    {"label": "Hang Seng (^HSI)",       "value": "^HSI"},
    {"label": "Hang Seng Tech (^HSTECH)", "value": "^HSTECH"},
    {"label": "Hang Seng China Ent. (^HSCEI)", "value": "^HSCEI"},
]
INDEX_OPTIONS_US = [
    {"label": "S&P 500 (^GSPC)",         "value": "^GSPC"},
    {"label": "Dow Jones (^DJI)",        "value": "^DJI"},
    {"label": "NASDAQ-100 (^NDX)",       "value": "^NDX"},
    {"label": "NASDAQ Composite (^IXIC)", "value": "^IXIC"},
    {"label": "Russell 2000 (^RUT)",     "value": "^RUT"},
]
DEFAULT_INDEX_HK = "^HSI"
DEFAULT_INDEX_US = "^GSPC"


# Period radio — 1Y default keeps the cold-load fast even when the cache
# has multi-year history.
PERIOD_OPTIONS = [
    {"label": "1Y",  "value": 365},
    {"label": "3Y",  "value": 1095},
    {"label": "5Y",  "value": 1825},
    {"label": "MAX", "value": 0},
]
DEFAULT_PERIOD_DAYS = 365


# Constituent-table column spec. Mirrors the Screener tab's column set so
# the user gets a familiar sortable/filterable view, but scoped down to the
# selected index's members (typically 30-500 rows vs. the Screener's full
# universe of 2,800-4,100).
CONSTITUENT_COLUMNS = [
    {"name": "Ticker",     "id": "ticker"},
    {"name": "Name",       "id": "name"},
    {"name": "Sector",     "id": "sector"},
    {"name": "Mkt Cap (B)", "id": "market_cap_b",  "type": "numeric"},
    {"name": "Last",       "id": "last_price",    "type": "numeric"},
    {"name": "P/E",        "id": "trailing_pe",   "type": "numeric"},
    {"name": "P/B",        "id": "price_to_book", "type": "numeric"},
    {"name": "Div Y %",    "id": "dividend_yield", "type": "numeric"},
    {"name": "ROE %",      "id": "return_on_equity", "type": "numeric"},
    {"name": "D/E",        "id": "debt_to_equity", "type": "numeric"},
    {"name": "Earn Gr %",  "id": "earnings_growth", "type": "numeric"},
]


def _kpi_card(label: str, value_id: str, label_id: str,
                color: str = None) -> dbc.Card:
    """label_id is the i18n-targeted span we flip on language change."""
    return dbc.Card(dbc.CardBody([
        html.Div(label, id=label_id,
                  style={"color": T.TEXT_MUTED, "fontSize": "0.72rem",
                          "fontWeight": "600", "textTransform": "uppercase",
                          "letterSpacing": "0.06em"}),
        html.Div(id=value_id, children="—",
                  style={"fontSize": T.FONT_HERO_SM, "fontWeight": "700",
                          "color": color or T.TEXT, "lineHeight": "1.2",
                          "marginTop": "4px"}),
    ], style={"padding": "10px 14px"}),
        style={**T.CARD_STYLE_SOFT})


def build_market_tab() -> html.Div:
    return html.Div([
        # Alert body is i18n-driven — the inner children get replaced by
        # the i18n_market callback on language change. Initial children
        # rendered here in English so first-paint shows something usable.
        dbc.Alert(id="market-alert-banner", color="info",
                    className="small mb-3", dismissable=True,
                    children=[
            html.Strong("Market overview. ", id="market-alert-strong"),
            html.Span(
                "Pick an index above; chart + KPI cards + member table below "
                "auto-load. Constituent membership is cached from Wikipedia "
                "(except Hang Seng Tech which is hand-curated, and NASDAQ "
                "Composite / Russell 2000 which are price-only — no "
                "constituent list maintained). Refresh via ",
                id="market-alert-body"),
            html.Code("python main.py market refresh-constituents"),
            ".",
        ]),

        # ----- Index + period + chart-style controls -----
        # chart-style toggle gates whether the chart callback fetches just
        # adj_close (line — small payload) or full OHLC (candle — bigger
        # payload but only fetched on demand). Default Line for fast first
        # paint; all 8 indices have OHLC in storage so candle works on every
        # index without a re-seed.
        dbc.Card([
            dbc.CardBody([
                dbc.Row([
                    dbc.Col([
                        html.Label("Index",
                                      id="market-label-index",
                                      className="stat-label mb-2"),
                        dbc.RadioItems(
                            id="market-index-select",
                            # populated per-market by callback
                            options=INDEX_OPTIONS_HK,
                            value=DEFAULT_INDEX_HK,
                            inline=True,
                            className="btn-group sr-period-radio",
                            inputClassName="btn-check",
                            labelClassName="btn btn-outline-primary btn-sm",
                            labelCheckedClassName="active",
                        ),
                    ], xs=12, md=6),
                    dbc.Col([
                        html.Label("Period",
                                      id="market-label-period",
                                      className="stat-label mb-2"),
                        dbc.RadioItems(
                            id="market-period-select",
                            options=PERIOD_OPTIONS,
                            value=DEFAULT_PERIOD_DAYS,
                            inline=True,
                            className="btn-group sr-period-radio",
                            inputClassName="btn-check",
                            labelClassName="btn btn-outline-primary btn-sm",
                            labelCheckedClassName="active",
                        ),
                    ], xs=12, md=3),
                    dbc.Col([
                        html.Label("Chart style",
                                      id="market-label-style",
                                      className="stat-label mb-2"),
                        dbc.RadioItems(
                            id="market-chart-style",
                            options=[
                                {"label": "Line",   "value": "line"},
                                {"label": "Candle", "value": "candle"},
                            ],
                            value="line",
                            inline=True,
                            className="btn-group sr-period-radio",
                            inputClassName="btn-check",
                            labelClassName="btn btn-outline-primary btn-sm",
                            labelCheckedClassName="active",
                        ),
                    ], xs=12, md=3),
                ], className="g-3"),
            ]),
        ], style=T.CARD_STYLE, className="mb-3"),

        # ----- Index chart -----
        dbc.Card([
            dbc.CardHeader([
                html.Span(id="market-chart-title", className="fw-bold"),
                html.Span(id="market-chart-period-label",
                            className="text-muted small ms-2"),
            ]),
            dbc.CardBody(
                dcc.Loading(dcc.Graph(id="market-index-chart",
                                            config={"displayModeBar": False},
                                            figure={}),
                              type="default", color=T.PRIMARY)
            ),
        ], style=T.CARD_STYLE, className="mb-3"),

        # ----- 4 KPI cards -----
        # Each card's label is wrapped in its own id so the i18n callback
        # can flip the heading text without touching the (frequently-
        # updated) value span.
        dbc.Row([
            dbc.Col(_kpi_card("Last close", "market-kpi-last",
                                "market-kpi-last-label"),
                      xs=12, sm=6, md=3),
            dbc.Col(_kpi_card("Period return", "market-kpi-period",
                                "market-kpi-period-label",
                                color=T.PRIMARY),
                      xs=12, sm=6, md=3),
            dbc.Col(_kpi_card("YTD return", "market-kpi-ytd",
                                "market-kpi-ytd-label"),
                      xs=12, sm=6, md=3),
            dbc.Col(_kpi_card("Max drawdown (period)", "market-kpi-maxdd",
                                "market-kpi-maxdd-label",
                                color=T.DANGER),
                      xs=12, sm=6, md=3),
        ], className="g-2 mb-3"),

        # ----- Constituent table -----
        dbc.Card([
            dbc.CardHeader([
                html.Span(id="market-constituent-title",
                            className="fw-bold"),
                html.Span(id="market-constituent-meta",
                            className="text-muted small ms-2"),
            ]),
            dbc.CardBody([
                dcc.Loading(
                    html.Div(id="market-constituent-wrapper", children=[
                        dash_table.DataTable(
                            id="market-constituent-table",
                            columns=CONSTITUENT_COLUMNS,
                            data=[],
                            sort_action="native",
                            filter_action="native",
                            page_action="native",
                            page_size=50,
                            style_table={"overflowX": "auto"},
                            style_cell={**T.DATATABLE_CELL,
                                         "minWidth": "90px",
                                         "whiteSpace": "normal"},
                            style_cell_conditional=[
                                {"if": {"column_id": "ticker"},
                                 "textAlign": "left",
                                 "fontWeight": "600",
                                 "color": T.PRIMARY},
                                {"if": {"column_id": "name"},
                                 "textAlign": "left",
                                 "minWidth": "180px"},
                                {"if": {"column_id": "sector"},
                                 "textAlign": "left",
                                 "color": T.TEXT_MUTED},
                            ],
                            style_header=T.DATATABLE_HEADER,
                        ),
                    ]),
                    type="default", color=T.PRIMARY,
                ),
            ]),
        ], style=T.CARD_STYLE),
    ])
