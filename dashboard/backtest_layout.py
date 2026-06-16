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


def _stat_block(label: str, value_id: str, color: str = None):
    return html.Div([
        html.Div(label, className="stat-label"),
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

        dbc.Alert([
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
            "snapshots are quarterly/annual.",
        ], color="dark", className="mb-3"),

        # ----- Controls -----
        dbc.Card([
            dbc.CardHeader([
                html.Span("Backtest setup", className="fw-bold")
            ]),
            dbc.CardBody([
                dbc.Row([
                    dbc.Col([
                        html.Label("Investor preset", className="stat-label mb-2"),
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
                        html.Label("Time horizon", className="stat-label mb-2"),
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
                    ], width=6),
                    dbc.Col([
                        html.Label("Rebalance frequency",
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
                    ], width=6),
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
            # Stats row
            dbc.Card([
                dbc.CardHeader(html.Span("Performance",
                                            className="fw-bold")),
                dbc.CardBody([
                    dbc.Row([
                        dbc.Col(_stat_block("Total return",
                                              "bt-stat-total"), width=2),
                        dbc.Col(_stat_block("Annualized",
                                              "bt-stat-annret"), width=2),
                        dbc.Col(_stat_block("Annualized vol",
                                              "bt-stat-vol"), width=2),
                        dbc.Col(_stat_block("Sharpe (rf=3%)",
                                              "bt-stat-sharpe",
                                              color=T.PRIMARY), width=2),
                        dbc.Col(_stat_block("Max drawdown",
                                              "bt-stat-maxdd",
                                              color=T.DANGER), width=2),
                        dbc.Col(_stat_block("Hit rate vs ^HSI",
                                              "bt-stat-hit"), width=2),
                    ], align="center"),
                ], style={"padding": "16px 20px"}),
            ], style=T.CARD_STYLE, className="mb-3"),

            # Equity curve
            dbc.Card([
                dbc.CardHeader(html.Span("Equity curve vs ^HSI",
                                            className="fw-bold")),
                dbc.CardBody(dcc.Graph(id="bt-equity-chart",
                                          config={"displayModeBar": False},
                                          figure={})),
            ], style=T.CARD_STYLE, className="mb-3"),

            # Final-rebalance holdings
            dbc.Card([
                dbc.CardHeader([
                    html.Span("Final rebalance holdings",
                                className="fw-bold me-2"),
                    html.Span(id="bt-final-rebal-date",
                                className="text-muted small"),
                ]),
                dbc.CardBody(
                    dash_table.DataTable(
                        id="bt-holdings-table",
                        columns=[
                            {"name": "Ticker", "id": "ticker"},
                            {"name": "Weight %", "id": "weight",
                             "type": "numeric"},
                        ],
                        data=[],
                        page_size=12,
                        style_cell=T.DATATABLE_CELL,
                        style_header=T.DATATABLE_HEADER,
                    ),
                ),
            ], style=T.CARD_STYLE, className="mb-3"),
        ]),

        # ----- Save handoff -----
        dbc.Card([
            dbc.CardHeader([
                html.Span("Save as portfolio", className="fw-bold me-2"),
                html.Span("— start-of-period preset survivors, 100 shares each, "
                           "rf = 3% pre-set",
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
