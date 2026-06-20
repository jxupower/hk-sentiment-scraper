"""Backtest tab — preset + V/Q/G top-10 walk-forward simulation.

Replaces the previous per-screen optimization-results UI. Workflow:
1. Pick an investor preset (Buffett / Graham / Lynch / Greenblatt /
   Druckenmiller) and a time horizon + rebalance frequency.
2. Engine pulls as-of fundamentals, applies the preset, scores V/Q/G,
   takes the cap-weighted top 10 per rebalance, compounds returns vs ^HSI.
3. Click "Save as portfolio" → all preset survivors at the START date
   land in the Portfolio tab as a 100-share-each saved portfolio with
   rf = 3%, ready for Sharpe optimisation.
"""
from dash import dcc, html, dash_table
import dash_bootstrap_components as dbc

from dashboard import theme as T
from dashboard.screener_presets import INVESTOR_PRESETS


def _stat_block(label: str, value_id: str, color: str = None,
                  label_id: str = None):
    return html.Div([
        html.Div(label, className="stat-label",
                  id=label_id if label_id else f"{value_id}-label"),
        html.Div(id=value_id, className="hero-number",
                  style={"color": color, "fontSize": "1.4rem"} if color
                         else {"fontSize": "1.4rem"}),
    ])


def build_backtest_tab() -> html.Div:
    preset_options = [{"label": p["label"], "value": p["id"]}
                      for p in INVESTOR_PRESETS]

    return html.Div([
        # Stores persisted across run/save callbacks. `bt-survivors-store`
        # holds the preset-filtered ticker list AT THE START date — what
        # gets materialised when Save is clicked.
        dcc.Store(id="bt-survivors-store", data=[]),
        dcc.Store(id="bt-preset-label-store", data=""),

        dbc.Alert(id="bt-alert-banner", color="dark", className="mb-3",
                    children=[
            html.Strong("Preset + V/Q/G top-10 walk-forward backtest. "),
            "At every rebalance date the engine applies the chosen "
            "investor preset to as-of fundamentals, ranks survivors by "
            "composite V/Q/G percentile, and holds the top 10 ",
            html.Em("market-cap-weighted"),
            ". Returns are compounded vs ^HSI; Sharpe uses rf = 3%. ",
            html.Br(),
            html.Strong("Honest caveats: ", className="ms-2"),
            "akshare fundamentals are as-restated (60-day reporting lag "
            "applied); survivor bias from delisted tickers; no transaction "
            "costs; daily rebalances mostly re-equalise cap weights since "
            "snapshots are quarterly/annual. Historical akshare snapshots "
            "do not carry EV/EBITDA or dividend_yield, so Greenblatt's "
            "EV/EBITDA cap and Graham's dividend floor are unenforced in "
            "backtests — only Buffett / Lynch / Druckenmiller filter on "
            "fields available in the historical data.",
        ]),

        # ----- Controls -----
        dbc.Card([
            dbc.CardHeader([
                html.Span("Backtest setup", id="bt-setup-title",
                            className="fw-bold")
            ]),
            dbc.CardBody([
                dbc.Row([
                    dbc.Col([
                        html.Label("Investor preset", id="bt-label-preset",
                                      className="stat-label mb-2"),
                        dbc.RadioItems(
                            id="bt-preset-select",
                            options=preset_options,
                            value="lynch",
                            className="btn-group",
                            inputClassName="btn-check",
                            labelClassName="btn btn-outline-primary btn-sm me-1",
                            labelCheckedClassName="active",
                        ),
                    ], width=12, className="mb-3"),
                ]),
                dbc.Row([
                    dbc.Col([
                        html.Label("Time horizon", id="bt-label-horizon",
                                      className="stat-label mb-2"),
                        dbc.RadioItems(
                            id="bt-horizon-select",
                            options=[{"label": "1 year",  "value": 1},
                                     {"label": "3 years", "value": 3},
                                     {"label": "5 years", "value": 5}],
                            value=3,
                            inline=True,
                            className="btn-group",
                            inputClassName="btn-check",
                            labelClassName="btn btn-outline-primary btn-sm",
                            labelCheckedClassName="active",
                        ),
                    ], width=4),
                    dbc.Col([
                        html.Label("Rebalance frequency",
                                      id="bt-label-rebal",
                                      className="stat-label mb-2"),
                        dbc.RadioItems(
                            id="bt-rebal-select",
                            options=[{"label": "Daily",   "value": "1d"},
                                     {"label": "3-day",   "value": "3d"},
                                     {"label": "Weekly",  "value": "1w"},
                                     {"label": "Monthly", "value": "1m"}],
                            value="1m",
                            inline=True,
                            className="btn-group",
                            inputClassName="btn-check",
                            labelClassName="btn btn-outline-primary btn-sm",
                            labelCheckedClassName="active",
                        ),
                    ], width=4),
                    dbc.Col([
                        html.Label("Max position weight",
                                      id="bt-label-weight-cap",
                                      className="stat-label mb-2"),
                        dcc.Slider(
                            id="bt-weight-cap",
                            min=0.05, max=1.00, step=0.05, value=0.20,
                            marks={0.10: "10%", 0.20: "20%",
                                   0.40: "40%", 1.00: "100%"},
                            tooltip={"placement": "bottom",
                                     "always_visible": False},
                        ),
                    ], width=4),
                ], className="mb-3"),
                dbc.Row([
                    dbc.Col(
                        dbc.Button("Run backtest", id="bt-run-btn",
                                    color="primary", size="md"),
                        width="auto",
                    ),
                    dbc.Col(
                        html.Span(id="bt-run-status",
                                   className="text-muted small ms-2"),
                        className="d-flex align-items-center",
                    ),
                ]),
            ]),
        ], style=T.CARD_STYLE, className="mb-3"),

        # ----- Results -----
        dcc.Loading(type="default", children=[
            # Stats row — 8 columns now (added excess return + ann turnover).
            # Date range strip sits in the card header for one-line reproducibility.
            dbc.Card([
                dbc.CardHeader([
                    html.Span("Performance", id="bt-perf-title",
                              className="fw-bold me-2"),
                    html.Span(id="bt-window-label",
                                className="text-muted small"),
                ]),
                dbc.CardBody([
                    dbc.Row([
                        dbc.Col(_stat_block("Total return",
                                              "bt-stat-total"), width=3),
                        dbc.Col(_stat_block("Annualized",
                                              "bt-stat-annret"), width=3),
                        dbc.Col(_stat_block("Annualized vol",
                                              "bt-stat-vol"), width=3),
                        dbc.Col(_stat_block("Sharpe (rf=3%)",
                                              "bt-stat-sharpe",
                                              color=T.PRIMARY), width=3),
                    ], align="center", className="mb-3"),
                    dbc.Row([
                        dbc.Col(_stat_block("Max drawdown",
                                              "bt-stat-maxdd",
                                              color=T.PRICE_DOWN), width=3),
                        dbc.Col(_stat_block("Hit rate vs ^HSI",
                                              "bt-stat-hit"), width=3),
                        dbc.Col(_stat_block("Excess vs ^HSI",
                                              "bt-stat-excess",
                                              color=T.PRICE_UP), width=3),
                        dbc.Col(_stat_block("Annualized turnover",
                                              "bt-stat-turnover"), width=3),
                    ], align="center"),
                ], style={"padding": "16px 20px"}),
            ], style=T.CARD_STYLE, className="mb-3"),

            # Equity curve + drawdown stacked
            dbc.Card([
                dbc.CardHeader(html.Span("Equity curve vs ^HSI",
                                            id="bt-section-equity",
                                            className="fw-bold")),
                dbc.CardBody(dcc.Graph(id="bt-equity-chart",
                                          config={"displayModeBar": False},
                                          figure={})),
            ], style=T.CARD_STYLE, className="mb-3"),
            dbc.Card([
                dbc.CardHeader(html.Span("Drawdown timeline",
                                            id="bt-section-drawdown",
                                            className="fw-bold")),
                dbc.CardBody(dcc.Graph(id="bt-drawdown-chart",
                                          config={"displayModeBar": False},
                                          figure={})),
            ], style=T.CARD_STYLE, className="mb-3"),

            # Sector breakdown — initial vs final side by side
            dbc.Card([
                dbc.CardHeader(html.Span("Sector breakdown",
                                            id="bt-section-sector",
                                            className="fw-bold")),
                dbc.CardBody(
                    dbc.Row([
                        dbc.Col(dcc.Graph(id="bt-sector-initial",
                                            config={"displayModeBar": False},
                                            figure={}), width=6),
                        dbc.Col(dcc.Graph(id="bt-sector-final",
                                            config={"displayModeBar": False},
                                            figure={}), width=6),
                    ]),
                ),
            ], style=T.CARD_STYLE, className="mb-3"),

            # Initial holdings at the backtest START date
            dbc.Card([
                dbc.CardHeader([
                    html.Span("Initial holdings", id="bt-section-initial",
                                className="fw-bold me-2"),
                    html.Span(id="bt-initial-rebal-date",
                                className="text-muted small"),
                ]),
                dbc.CardBody(
                    dash_table.DataTable(
                        id="bt-initial-table",
                        columns=[
                            {"name": "Ticker",   "id": "ticker"},
                            {"name": "Name",     "id": "name"},
                            {"name": "Price",    "id": "price",
                             "type": "numeric"},
                            {"name": "Weight %", "id": "weight",
                             "type": "numeric"},
                            {"name": "Shares",   "id": "shares",
                             "type": "numeric"},
                        ],
                        data=[],
                        page_size=12,
                        sort_action="native",
                        style_cell=T.DATATABLE_CELL,
                        style_cell_conditional=[
                            {"if": {"column_id": "name"},
                             "textAlign": "left"},
                        ],
                        style_header=T.DATATABLE_HEADER,
                    ),
                ),
            ], style=T.CARD_STYLE, className="mb-3"),

            # All rebalance buy/sell trades after the initial allocation
            dbc.Card([
                dbc.CardHeader([
                    html.Span("Rebalance changes", id="bt-section-changes",
                                className="fw-bold me-2"),
                    html.Span(id="bt-trades-summary",
                                className="text-muted small"),
                ]),
                dbc.CardBody(
                    dash_table.DataTable(
                        id="bt-trades-table",
                        columns=[
                            {"name": "Date",    "id": "date"},
                            {"name": "Ticker",  "id": "ticker"},
                            {"name": "Name",    "id": "name"},
                            {"name": "Action",  "id": "action"},
                            {"name": "Units",   "id": "units",
                             "type": "numeric"},
                            {"name": "Price",   "id": "price",
                             "type": "numeric"},
                        ],
                        data=[],
                        page_size=15,
                        sort_action="native",
                        filter_action="native",
                        style_cell=T.DATATABLE_CELL,
                        style_cell_conditional=[
                            {"if": {"column_id": "name"},
                             "textAlign": "left"},
                            {"if": {"column_id": "action"},
                             "textAlign": "center", "fontWeight": "600"},
                        ],
                        style_data_conditional=[
                            {"if": {"filter_query": '{action} = "BUY"',
                                     "column_id": "action"},
                             "color": T.SUCCESS},
                            {"if": {"filter_query": '{action} = "SELL"',
                                     "column_id": "action"},
                             "color": T.DANGER},
                        ],
                        style_header=T.DATATABLE_HEADER,
                        style_filter=T.DATATABLE_FILTER,
                    ),
                ),
            ], style=T.CARD_STYLE, className="mb-3"),

            # Final-rebalance holdings — with Δ vs initial so position drift
            # is visible at a glance.
            dbc.Card([
                dbc.CardHeader([
                    html.Span("Final holdings", id="bt-section-final",
                                className="fw-bold me-2"),
                    html.Span(id="bt-final-rebal-date",
                                className="text-muted small"),
                ]),
                dbc.CardBody(
                    dash_table.DataTable(
                        id="bt-final-table",
                        columns=[
                            {"name": "Ticker",     "id": "ticker"},
                            {"name": "Name",       "id": "name"},
                            {"name": "Price",      "id": "price",
                             "type": "numeric"},
                            {"name": "Weight %",   "id": "weight",
                             "type": "numeric"},
                            {"name": "Δ weight",   "id": "weight_delta",
                             "type": "numeric"},
                            {"name": "Shares",     "id": "shares",
                             "type": "numeric"},
                            {"name": "Δ shares",   "id": "shares_delta",
                             "type": "numeric"},
                        ],
                        data=[],
                        page_size=12,
                        sort_action="native",
                        style_cell=T.DATATABLE_CELL,
                        style_cell_conditional=[
                            {"if": {"column_id": "name"},
                             "textAlign": "left"},
                        ],
                        style_data_conditional=[
                            # Δ-weight / Δ-shares colour follows the
                            # CN/HK convention: position grew = red, shrank = green.
                            {"if": {"filter_query": "{weight_delta} > 0",
                                     "column_id": "weight_delta"},
                             "color": T.PRICE_UP},
                            {"if": {"filter_query": "{weight_delta} < 0",
                                     "column_id": "weight_delta"},
                             "color": T.PRICE_DOWN},
                            {"if": {"filter_query": "{shares_delta} > 0",
                                     "column_id": "shares_delta"},
                             "color": T.PRICE_UP},
                            {"if": {"filter_query": "{shares_delta} < 0",
                                     "column_id": "shares_delta"},
                             "color": T.PRICE_DOWN},
                        ],
                        style_header=T.DATATABLE_HEADER,
                    ),
                ),
            ], style=T.CARD_STYLE, className="mb-3"),
        ]),

        # ----- Save handoff -----
        dbc.Card([
            dbc.CardHeader([
                html.Span("Save as portfolio", id="bt-save-title",
                            className="fw-bold me-2"),
                html.Span("— start-of-period preset survivors, 100 shares each, "
                           "rf = 3% pre-set",
                           id="bt-save-subtitle",
                           className="text-muted small"),
            ]),
            dbc.CardBody([
                dbc.Row([
                    dbc.Col([
                        html.Div(id="bt-save-preview",
                                  style={"color": T.TEXT_MUTED,
                                         "fontSize": "0.9rem",
                                         "marginBottom": "8px"}),
                        dbc.Button("Save & open in Portfolio tab",
                                    id="bt-save-btn",
                                    color="success", disabled=True),
                    ], width=8),
                    dbc.Col(
                        html.Span(id="bt-save-status",
                                   className="text-muted small"),
                        className="d-flex align-items-center justify-content-end",
                    ),
                ]),
            ]),
        ], style=T.CARD_STYLE),
    ])
