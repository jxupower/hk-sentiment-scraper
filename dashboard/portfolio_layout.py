"""Portfolio Rebalancer tab — max-Sharpe optimisation via MPT.

UI shape: holdings table (editable) + parameter controls + output cards
(three Sharpe hero numbers, weights bar chart, efficient frontier,
walk-forward backtest, candidate marginal-value table).
"""
from dash import dcc, html, dash_table
from dash.dash_table.Format import Format, Scheme
import dash_bootstrap_components as dbc

from dashboard import theme as T


# Lookback in trading days (252/y); 0 = MAX.
LOOKBACK_OPTIONS = [
    {"label": "1Y",  "value": 252},
    {"label": "3Y",  "value": 756},
    {"label": "5Y",  "value": 1260},
    {"label": "MAX", "value": 0},
]
DEFAULT_LOOKBACK = 756

# Rebalance frequency in trading days.
REBALANCE_OPTIONS = [
    {"label": "5d",   "value": 5},
    {"label": "21d (1mo)",  "value": 21},
    {"label": "63d (1qtr)", "value": 63},
    {"label": "252d (1y)",  "value": 252},
]
DEFAULT_REBALANCE = 21

DEFAULT_WEIGHT_CAP_PCT = 30
DEFAULT_RF_PCT = 0.0


def build_portfolio_tab() -> html.Div:
    return html.Div([
        # Stash the most-recent compute bundle's optimal-weight snapshot so
        # the Save button can persist it alongside raw holdings. Cleared on
        # Load (a load means the user is leaving the previous compute behind).
        dcc.Store(id="portfolio-latest-optimal", data=None),

        dbc.Alert(id="portfolio-alert-banner", color="info",
                    className="small mb-3", dismissable=True,
                    children=[
            html.Strong("Portfolio Rebalancer — Max-Sharpe via Modern Portfolio Theory. "),
            "Enter your holdings (ticker + shares), add candidate tickers with 0 shares, "
            "pick a lookback + rebalance frequency, click Compute. The full universe (current + candidates) "
            "is fed through Ledoit-Wolf shrunk Σ and SLSQP to find the long-only, capped, max-Sharpe portfolio.",
            html.Br(),
            html.Em("Honest gaps: sample means are very noisy, so 'optimal weights' are a directional guide; "
                    "in-sample Sharpe is biased up by construction (the walk-forward backtest shows out-of-sample reality); "
                    "no transaction costs / taxes modelled."),
        ]),

        # ============== Saved portfolios bar ==============
        # Two explicit save buttons:
        #   • "Save status-quo" persists raw shares -> @NAME synthetic ticker
        #     (always available; no Compute needed first).
        #   • "Save w/ optimal" also persists the max-Sharpe weight snapshot
        #     from the latest Compute -> @NAME$OPT synthetic ticker. Disabled
        #     until you've clicked Compute on the same set of holdings.
        # The Risk Forecast tab consumes those @-tickers like normal stocks.
        dbc.Card([
            dbc.CardHeader([
                html.Span("Saved portfolios", id="portfolio-saved-title",
                          className="fw-bold"),
                html.Span(" — name + Save to persist the current holdings to "
                          "Supabase. Once saved, they show up as synthetic "
                          "tickers (e.g. @CORE, @CORE$OPT) in the Risk "
                          "Forecast tab.",
                          id="portfolio-saved-hint",
                          style={"color": T.TEXT_MUTED, "fontSize": "0.8rem",
                                 "marginLeft": "8px"}),
            ]),
            dbc.CardBody([
                # Row 1 — name input + load/delete of an existing portfolio
                dbc.Row([
                    dbc.Col([
                        html.Label("Portfolio name",
                                    id="portfolio-label-name",
                                    className="text-muted small mb-1"),
                        dbc.Input(
                            id="portfolio-name-input",
                            type="text",
                            placeholder="UPPERCASE / digits / _ — e.g. CORE",
                            maxLength=32,
                            style={**T.INPUT_STYLE, "width": "100%",
                                   "padding": "5px 10px"},
                        ),
                    ], xs=12, md=5),
                    dbc.Col([
                        html.Label("Existing portfolios",
                                    id="portfolio-label-existing",
                                    className="text-muted small mb-1"),
                        dcc.Dropdown(
                            id="portfolio-saved-dropdown",
                            options=[],
                            placeholder="Load a saved portfolio…",
                            clearable=True,
                        ),
                    ], xs=12, md=4),
                    dbc.Col([
                        html.Label(" ", className="small mb-1"),
                        dbc.ButtonGroup([
                            dbc.Button("Load", id="portfolio-load-btn",
                                        color="secondary", outline=True, size="sm"),
                            dbc.Button("Delete", id="portfolio-delete-btn",
                                        color="danger", outline=True, size="sm"),
                        ], className="w-100"),
                    ], xs=12, md=3),
                ], className="g-2 mb-2"),

                # Row 2 — explicit save buttons (status-quo vs optimised)
                dbc.Row([
                    dbc.Col([
                        dbc.Button(
                            [html.I(className="me-1"),
                             "Save status-quo portfolio  →  @NAME"],
                            id="portfolio-save-status-btn",
                            color="primary", size="sm", className="w-100",
                        ),
                        html.Div("Materialises the constant-share buy-and-hold "
                                  "index from the holdings table above.",
                                  id="portfolio-save-status-blurb",
                                  style={"color": T.TEXT_MUTED,
                                         "fontSize": "0.72rem",
                                         "marginTop": "4px"}),
                    ], xs=12, md=6),
                    dbc.Col([
                        dbc.Button(
                            [html.I(className="me-1"),
                             "Save optimised portfolio  →  @NAME$OPT"],
                            id="portfolio-save-optimal-btn",
                            color="success", size="sm", className="w-100",
                        ),
                        html.Div("Materialises the latest max-Sharpe optimal "
                                  "weight series. Requires Compute first "
                                  "(same tickers as the table).",
                                  id="portfolio-save-optimal-blurb",
                                  style={"color": T.TEXT_MUTED,
                                         "fontSize": "0.72rem",
                                         "marginTop": "4px"}),
                    ], xs=12, md=6),
                ], className="g-2"),

                html.Div(id="portfolio-save-status",
                          style={"color": T.TEXT_MUTED, "fontSize": "0.8rem",
                                 "marginTop": "8px"}),
            ]),
        ], style=T.CARD_STYLE, className="mb-3"),

        # ============== Holdings table ==============
        dbc.Card([
            dbc.CardHeader([
                html.Span("Holdings", id="portfolio-holdings-title",
                          className="fw-bold"),
                html.Span(" — type ticker (e.g. 0700.HK or ^HSI) + shares. "
                          "Use shares=0 for candidates you're considering.",
                          id="portfolio-holdings-hint",
                          style={"color": T.TEXT_MUTED, "fontSize": "0.8rem",
                                 "marginLeft": "8px"}),
            ]),
            dbc.CardBody([
                dash_table.DataTable(
                    id="portfolio-holdings-table",
                    columns=[
                        {"name": "Ticker", "id": "ticker", "type": "text",
                         "editable": True},
                        {"name": "Shares", "id": "shares", "type": "numeric",
                         "editable": True},
                        # Auto-populated read-only column — see
                        # `autofill_holdings_prices` in portfolio_callbacks.py.
                        # Currency in the header flips per market via the
                        # i18n callback.
                        {"name": "Price (HKD)", "id": "current_price",
                         "type": "numeric", "editable": False,
                         "format": Format(precision=2, scheme=Scheme.fixed)},
                    ],
                    data=[
                        {"ticker": "0700.HK", "shares": 100},
                        {"ticker": "0005.HK", "shares": 200},
                        {"ticker": "9988.HK", "shares": 50},
                        {"ticker": "1810.HK", "shares": 0},
                        {"ticker": "0823.HK", "shares": 0},
                    ],
                    editable=True,
                    row_deletable=True,
                    style_cell=T.DATATABLE_CELL,
                    style_header=T.DATATABLE_HEADER,
                    style_cell_conditional=[
                        {"if": {"column_id": "ticker"}, "textAlign": "left",
                         "fontWeight": "600", "color": T.PRIMARY,
                         "width": "45%"},
                        {"if": {"column_id": "shares"}, "textAlign": "right",
                         "width": "30%"},
                        {"if": {"column_id": "current_price"}, "textAlign": "right",
                         "width": "25%", "color": T.TEXT_MUTED},
                    ],
                ),
                html.Div([
                    dbc.Button("+ Add row", id="portfolio-add-row-btn",
                                color="secondary", size="sm",
                                outline=True, className="mt-2 me-2"),
                    html.Span(id="portfolio-holdings-status",
                              style={"color": T.TEXT_MUTED, "fontSize": "0.8rem"}),
                ]),
            ]),
        ], style=T.CARD_STYLE, className="mb-3"),

        # ============== Parameters ==============
        # Responsive: stack on phones (xs=12), 2-up on tablets (sm=6),
        # 4-up on desktop (md=…). Combined with the .sr-period-radio CSS
        # flex-wrap fix, this prevents the button-group overflow that used
        # to push 1Y/3Y/5Y buttons into the 5d/21d/63d column.
        dbc.Card([
            dbc.CardHeader("Parameters", id="portfolio-params-title",
                              className="fw-bold"),
            dbc.CardBody([
                dbc.Row([
                    dbc.Col([
                        html.Label("Lookback (estimation window)",
                                    id="portfolio-label-lookback",
                                    className="text-muted small mb-1"),
                        dbc.RadioItems(
                            id="portfolio-lookback",
                            options=LOOKBACK_OPTIONS, value=DEFAULT_LOOKBACK,
                            inline=True,
                            className="btn-group sr-period-radio",
                            inputClassName="btn-check",
                            labelClassName="btn btn-outline-primary btn-sm",
                            labelCheckedClassName="active",
                        ),
                    ], xs=12, sm=6, md=3),
                    dbc.Col([
                        html.Label("Rebalance frequency (backtest)",
                                    id="portfolio-label-rebal",
                                    className="text-muted small mb-1"),
                        dbc.RadioItems(
                            id="portfolio-rebalance",
                            options=REBALANCE_OPTIONS, value=DEFAULT_REBALANCE,
                            inline=True,
                            className="btn-group sr-period-radio",
                            inputClassName="btn-check",
                            labelClassName="btn btn-outline-primary btn-sm",
                            labelCheckedClassName="active",
                        ),
                    ], xs=12, sm=6, md=4),
                    dbc.Col([
                        html.Label([
                            html.Span("Per-asset cap ",
                                          id="portfolio-label-weight-cap"),
                            html.Span(id="portfolio-cap-label",
                                       style={"color": T.PRIMARY,
                                              "fontWeight": "600"}),
                        ], className="text-muted small mb-1"),
                        dcc.Slider(
                            id="portfolio-cap-slider",
                            min=5, max=100, step=5,
                            value=DEFAULT_WEIGHT_CAP_PCT,
                            marks={10: "10%", 30: "30%", 50: "50%", 100: "100%"},
                            tooltip={"placement": "bottom"},
                        ),
                    ], xs=12, sm=6, md=3),
                    dbc.Col([
                        html.Label("Risk-free rate (%, ann.)",
                                    id="portfolio-label-rf",
                                    className="text-muted small mb-1"),
                        dcc.Input(
                            id="portfolio-rf",
                            type="number", min=0, max=20, step=0.5,
                            value=DEFAULT_RF_PCT,
                            style={**T.INPUT_STYLE, "width": "100%",
                                   "padding": "5px 10px"},
                        ),
                    ], xs=12, sm=6, md=2),
                ], className="g-3"),
                html.Div([
                    dbc.Button("Compute optimal portfolio",
                                id="portfolio-compute-btn",
                                color="primary", size="sm", className="mt-3"),
                    html.Span(id="portfolio-compute-status",
                              style={"color": T.TEXT_MUTED, "fontSize": "0.8rem",
                                     "marginLeft": "12px"}),
                ]),
            ]),
        ], style=T.CARD_STYLE, className="mb-3"),

        # ============== Placeholder + content ==============
        html.Div(id="portfolio-placeholder",
                  children=html.P("Enter holdings, pick parameters, then click Compute.",
                                  id="portfolio-placeholder-text",
                                  className="text-muted text-center py-5"),
                  style={"display": "block"}),

        html.Div(id="portfolio-content", style={"display": "none"}, children=[

            # Three Sharpe hero numbers
            dbc.Card([
                dbc.CardBody([
                    dbc.Row([
                        dbc.Col(_sharpe_hero("Status quo",
                                              "portfolio-sharpe-status",
                                              T.TEXT_MUTED,
                                              label_id="portfolio-sharpe-status-label"),
                                  width=4),
                        dbc.Col(_sharpe_hero("Current-only optimum",
                                              "portfolio-sharpe-current",
                                              T.INFO,
                                              label_id="portfolio-sharpe-current-label"),
                                  width=4),
                        dbc.Col(_sharpe_hero("Full-universe optimum",
                                              "portfolio-sharpe-full",
                                              T.SUCCESS,
                                              label_id="portfolio-sharpe-full-label"),
                                  width=4),
                    ]),
                    dbc.Row([
                        dbc.Col(html.Div(id="portfolio-sharpe-delta-rebal",
                                          className="text-center small"),
                                 width=6),
                        dbc.Col(html.Div(id="portfolio-sharpe-delta-add",
                                          className="text-center small"),
                                 width=6),
                    ], className="mt-2"),
                    # Cap-infeasibility warning — populated when any status-quo
                    # weight exceeds the per-asset cap, which means the
                    # optimiser is solving a tighter problem than the user is
                    # actually holding (so current-only optimum can fall below
                    # status-quo even at rf=0).
                    html.Div(id="portfolio-cap-warning", className="mt-2"),
                ]),
            ], style=T.CARD_STYLE, className="mb-3"),

            # Weights bar + frontier
            dbc.Row([
                dbc.Col([
                    dbc.Card([
                        dbc.CardHeader("Weights — current vs. optimal",
                                        id="portfolio-weights-header",
                                        className="fw-bold small"),
                        dbc.CardBody(dcc.Graph(id="portfolio-weights-chart",
                                                config={"displayModeBar": False},
                                                figure={})),
                    ], style=T.CARD_STYLE),
                ], width=6),
                dbc.Col([
                    dbc.Card([
                        dbc.CardHeader("Efficient frontier",
                                        id="portfolio-frontier-header",
                                        className="fw-bold small"),
                        dbc.CardBody(dcc.Graph(id="portfolio-frontier-chart",
                                                config={"displayModeBar": False},
                                                figure={})),
                    ], style=T.CARD_STYLE),
                ], width=6),
            ], className="mb-3"),

            # Walk-forward backtest
            dbc.Card([
                dbc.CardHeader("Walk-forward backtest",
                                id="portfolio-backtest-header",
                                className="fw-bold small"),
                dbc.CardBody([
                    dcc.Graph(id="portfolio-backtest-chart",
                              config={"displayModeBar": False}, figure={}),
                    html.Div(id="portfolio-backtest-table", className="mt-2"),
                ]),
            ], style=T.CARD_STYLE, className="mb-3"),

            # Candidate marginal value + rebalance trade list
            dbc.Row([
                dbc.Col([
                    dbc.Card([
                        dbc.CardHeader("Candidate marginal value",
                                        id="portfolio-candidate-header",
                                        className="fw-bold small"),
                        dbc.CardBody(html.Div(id="portfolio-candidate-table")),
                    ], style=T.CARD_STYLE),
                ], width=6),
                dbc.Col([
                    dbc.Card([
                        dbc.CardHeader("Rebalance trade list (to reach full-optimal)",
                                        id="portfolio-trade-header",
                                        className="fw-bold small"),
                        dbc.CardBody(html.Div(id="portfolio-trade-list")),
                    ], style=T.CARD_STYLE),
                ], width=6),
            ], className="mb-3"),

            # Diagnostics
            dbc.Card([
                dbc.CardHeader("Estimation diagnostics",
                                id="portfolio-diagnostics-header",
                                className="fw-bold small"),
                dbc.CardBody(html.Div(id="portfolio-diagnostics")),
            ], style=T.CARD_STYLE),
        ]),
    ])


def _sharpe_hero(label: str, value_id: str, color: str,
                  label_id: str | None = None):
    return html.Div([
        html.Div(label, id=label_id,
                  style={"color": T.TEXT_MUTED, "fontSize": "0.72rem",
                          "fontWeight": "600", "letterSpacing": "0.05em",
                          "textTransform": "uppercase",
                          "textAlign": "center"}),
        html.Div(id=value_id, style={**T.HERO_NUMBER_STYLE,
                                       "color": color,
                                       "textAlign": "center"}),
    ])
