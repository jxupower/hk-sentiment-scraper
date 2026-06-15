from dash import dcc, html, dash_table
import dash_bootstrap_components as dbc

from dashboard import theme as T


# Numeric range filters added to the Screener accordion. Each entry is
# (label, slug, lo_default, hi_default, step, fundamentals_key, xform).
# `xform` adapts the raw row value into the filtered units (e.g. market_cap
# bytes -> billions, ROE fraction -> percent). slug is used for the Dash IDs.
NUMERIC_FILTERS = [
    # (label,            slug,       lo,    hi,   step,  row_key,             xform)
    ("Trailing P/E",     "pe",        0,    200,  0.5,   "trailing_pe",        None),
    ("Forward P/E",      "fwdpe",     0,    200,  0.5,   "forward_pe",         None),
    ("P/B",              "pb",        0,    30,   0.1,   "price_to_book",      None),
    ("EV/EBITDA",        "evebitda",  0,    100,  0.5,   "ev_to_ebitda",       None),
    ("Dividend yield %", "divyield",  0,    20,   0.1,   "dividend_yield",     None),
    ("ROE %",            "roe",      -50,   100,  0.5,   "return_on_equity",   "roe_pct"),
    ("D/E %",            "de",        0,    1000, 5,     "debt_to_equity",     None),
    ("Beta",             "beta",     -2,    5,    0.1,   "beta",               None),
    ("Market cap (B HKD)","mcap",     0,    3000, 10,    "market_cap",         "mcap_b"),
]


def _stat_block(label: str, value_id: str, value_color: str = None):
    """Hero number with small uppercase label above."""
    return html.Div([
        html.Div(label, className="stat-label"),
        html.Div(id=value_id, className="hero-number",
                  style={"color": value_color} if value_color else {}),
    ])


def _range_filter(label: str, slug: str, lo: float, hi: float, step: float):
    """A numeric range filter widget: number-input | range-slider | number-input.
    Slider is the canonical source; inputs are kept in sync by callbacks in
    screener_callbacks.py. None passes the filter (treat as unknown)."""
    return html.Div([
        html.Label(label, className="stat-label mb-1"),
        dbc.Row([
            dbc.Col(dcc.Input(id=f"screener-{slug}-min", type="number",
                              value=lo, step=step, debounce=True,
                              style={"width": "78px", "fontSize": "0.85rem"}),
                    width="auto"),
            dbc.Col(dcc.RangeSlider(id=f"screener-{slug}-slider",
                                     min=lo, max=hi, value=[lo, hi],
                                     step=step, allowCross=False,
                                     tooltip={"placement": "bottom",
                                              "always_visible": False},
                                     marks=None),
                    width=True),
            dbc.Col(dcc.Input(id=f"screener-{slug}-max", type="number",
                              value=hi, step=step, debounce=True,
                              style={"width": "78px", "fontSize": "0.85rem"}),
                    width="auto"),
        ], align="center", className="g-2 mb-3"),
    ])


def build_screener_tab() -> html.Div:
    return html.Div([
        dcc.Interval(id="screener-auto-refresh", interval=300_000, n_intervals=0),
        # Per-session flag: has the user clicked "Load Sub-Sector P/E Chart"?
        # Once True, the sub-sector chart stays reactive to filter changes for
        # the rest of the session. Saves ~1s/render before that click.
        dcc.Store(id="screener-subsector-chart-loaded", data=False),

        # Header strip — 3 hero stats + action button
        dbc.Card([
            dbc.CardBody([
                dbc.Row([
                    dbc.Col(_stat_block("Universe size", "screener-stat-total"), width=3),
                    dbc.Col(_stat_block("With fundamentals", "screener-stat-with-data",
                                          value_color=T.PRIMARY), width=3),
                    dbc.Col(_stat_block("Latest snapshot", "screener-stat-latest"), width=4),
                    dbc.Col([
                        # Two refresh actions side-by-side:
                        # * "Refresh" re-reads the cached DB (fast, no API call)
                        # * "Refresh prices now" pulls fresh bars from yfinance
                        #   in a background thread (~5-10 min on full universe)
                        dbc.Button("Refresh", id="screener-refresh-btn",
                                    color="primary", size="sm",
                                    className="float-end mt-2"),
                        dbc.Button("Refresh prices now",
                                    id="screener-refresh-prices-btn",
                                    color="warning", outline=True, size="sm",
                                    className="float-end mt-2 me-2"),
                        html.Div(id="screener-refresh-prices-status",
                                  className="text-end small text-muted mt-1",
                                  style={"clear": "both", "fontSize": "0.72rem"}),
                    ], width=2),
                ], align="center"),
            ], style={"padding": "20px 24px"}),
        ], style=T.CARD_STYLE, className="mb-3"),

        # Filters card — accordion-grouped to keep the page short. Search +
        # Classification open by default (most-used); Valuation / Quality /
        # Size & data collapsed so the page stays compact for users who only
        # want broad screens.
        dbc.Card([
            dbc.CardHeader(
                dbc.Row([
                    dbc.Col(html.Span("Filters", className="fw-bold"),
                            width="auto"),
                    dbc.Col(
                        dbc.Button("Clear filters",
                                    id="screener-clear-filters-btn",
                                    color="secondary", outline=True, size="sm"),
                        className="text-end",
                    ),
                ], align="center", className="g-0"),
            ),
            dbc.CardBody([
                dbc.Accordion([
                    # --- Search group ---
                    dbc.AccordionItem([
                        dbc.Row([
                            dbc.Col([
                                html.Label("Ticker contains",
                                            className="stat-label mb-2"),
                                dcc.Input(id="screener-ticker-search",
                                          type="text", value="", debounce=True,
                                          placeholder="e.g. 0700, 9988",
                                          style={"width": "100%",
                                                 "fontSize": "0.9rem"}),
                            ], xs=12, md=6),
                            dbc.Col([
                                html.Label("Name contains",
                                            className="stat-label mb-2"),
                                dcc.Input(id="screener-name-search",
                                          type="text", value="", debounce=True,
                                          placeholder="e.g. Tencent, semiconductor",
                                          style={"width": "100%",
                                                 "fontSize": "0.9rem"}),
                            ], xs=12, md=6),
                        ], className="g-3"),
                    ], title="Search", item_id="search"),

                    # --- Classification group (existing 4 controls live here) ---
                    dbc.AccordionItem([
                        dbc.Row([
                            dbc.Col([
                                html.Label("Sector", className="stat-label mb-2"),
                                dcc.Dropdown(id="screener-sector-filter",
                                              multi=True,
                                              placeholder="All sectors"),
                            ], xs=12, md=4),
                            dbc.Col([
                                html.Label("Sub-sector", className="stat-label mb-2"),
                                dcc.Dropdown(id="screener-subsector-filter",
                                              multi=True,
                                              placeholder="All sub-sectors"),
                            ], xs=12, md=4),
                            dbc.Col([
                                html.Label("Min data completeness",
                                            className="stat-label mb-2"),
                                dcc.Slider(
                                    id="screener-completeness-filter",
                                    min=0, max=1, step=0.1, value=0.5,
                                    marks={i / 10: f"{i*10}%"
                                            for i in range(0, 11, 2)},
                                    tooltip={"placement": "bottom",
                                              "always_visible": False},
                                ),
                            ], xs=12, md=4),
                        ], className="g-3"),
                    ], title="Classification", item_id="classification"),

                    # --- Valuation group ---
                    dbc.AccordionItem([
                        html.Div("Range filters pass-through tickers with "
                                 "missing data. To exclude them, raise "
                                 "Min data completeness in the Classification "
                                 "group.",
                                 className="text-muted small mb-3",
                                 style={"fontStyle": "italic"}),
                        _range_filter("Trailing P/E", "pe", 0, 200, 0.5),
                        _range_filter("Forward P/E", "fwdpe", 0, 200, 0.5),
                        _range_filter("P/B", "pb", 0, 30, 0.1),
                        _range_filter("EV/EBITDA", "evebitda", 0, 100, 0.5),
                        _range_filter("Dividend yield %", "divyield", 0, 20, 0.1),
                    ], title="Valuation", item_id="valuation"),

                    # --- Quality group ---
                    dbc.AccordionItem([
                        _range_filter("ROE %", "roe", -50, 100, 0.5),
                        _range_filter("D/E %", "de", 0, 1000, 5),
                        _range_filter("Beta", "beta", -2, 5, 0.1),
                    ], title="Quality", item_id="quality"),

                    # --- Size group ---
                    dbc.AccordionItem([
                        _range_filter("Market cap (B HKD)", "mcap",
                                      0, 3000, 10),
                    ], title="Size", item_id="size"),
                ], active_item=["search", "classification"], always_open=True,
                    flush=True),
            ]),
        ], style=T.CARD_STYLE, className="mb-3"),

        # P/E aggregation toggle — single control feeds both charts below.
        # Median is the default (robust to outliers). Mean is a simple
        # arithmetic average. Cap-weighted uses the index methodology
        # (Σ market_cap / Σ earnings), giving more weight to mega-caps.
        dbc.Card([
            dbc.CardBody([
                dbc.Row([
                    dbc.Col([
                        html.Label("P/E aggregation",
                                    className="stat-label mb-1"),
                        dbc.RadioItems(
                            id="screener-pe-aggregation",
                            options=[
                                {"label": "Median", "value": "median"},
                                {"label": "Mean", "value": "mean"},
                                {"label": "Cap-weighted", "value": "cap_weighted"},
                            ],
                            value="median",
                            inline=True,
                            className="btn-group sr-period-radio",
                            inputClassName="btn-check",
                            labelClassName="btn btn-outline-primary btn-sm",
                            labelCheckedClassName="active",
                        ),
                    ], xs=12),
                ]),
            ], style={"padding": "12px 16px"}),
        ], style=T.CARD_STYLE, className="mb-2"),

        # Sector summary chart — aggregation method comes from the toggle above
        dbc.Card([
            dbc.CardHeader(id="screener-sector-pe-header",
                            children="Median P/E by Sector"),
            dbc.CardBody([
                dcc.Graph(id="screener-sector-pe-chart",
                          config={"displayModeBar": False}, figure={}),
            ]),
        ], style=T.CARD_STYLE, className="mb-3"),

        # Sub-sector summary chart — deferred behind a button. The card body
        # starts as just a loader-pill; clicking the button flips the Store
        # above and reveals the Graph for the rest of the session.
        dbc.Card([
            dbc.CardHeader(id="screener-subsector-pe-header",
                            children="Median P/E by Sub-Sector"),
            dbc.CardBody([
                html.Div(id="screener-subsector-chart-loader",
                          children=[
                              dbc.Button("Load Sub-Sector P/E Chart",
                                          id="screener-load-subsector-btn",
                                          color="primary", outline=True,
                                          size="sm"),
                              html.Span(" — defer-loaded to keep filter changes fast.",
                                         className="text-muted small ms-2"),
                          ]),
                dcc.Graph(id="screener-subsector-pe-chart",
                          config={"displayModeBar": False}, figure={},
                          style={"display": "none"}),
            ]),
        ], style=T.CARD_STYLE, className="mb-3"),

        # The big table
        dbc.Card([
            dbc.CardHeader([
                html.Span("Tickers", style={"fontWeight": "600", "marginRight": "10px"}),
                html.Span(id="screener-row-count",
                          style={"color": T.TEXT_MUTED, "fontSize": "0.85rem"}),
            ]),
            dbc.CardBody([
                dash_table.DataTable(
                    id="screener-table",
                    columns=[
                        {"name": "Ticker", "id": "ticker"},
                        {"name": "Name", "id": "name"},
                        {"name": "Sector", "id": "yf_sector"},
                        {"name": "Sub-sector", "id": "sub_sector"},
                        {"name": "Price", "id": "current_price"},
                        {"name": "Mkt Cap (B HKD)", "id": "market_cap_b", "type": "numeric"},
                        {"name": "P/E", "id": "trailing_pe", "type": "numeric"},
                        {"name": "Fwd P/E", "id": "forward_pe", "type": "numeric"},
                        {"name": "P/B", "id": "price_to_book", "type": "numeric"},
                        {"name": "EV/EBITDA", "id": "ev_to_ebitda", "type": "numeric"},
                        {"name": "Div Yield (%)", "id": "dividend_yield", "type": "numeric"},
                        {"name": "ROE (%)", "id": "return_on_equity_pct", "type": "numeric"},
                        {"name": "D/E (%)", "id": "debt_to_equity", "type": "numeric"},
                        {"name": "Beta", "id": "beta", "type": "numeric"},
                        {"name": "Completeness", "id": "completeness_pct", "type": "numeric"},
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
                        {"if": {"column_id": "name"}, "textAlign": "left",
                         "fontFamily": "Inter, sans-serif"},
                        {"if": {"column_id": "yf_sector"}, "textAlign": "left",
                         "fontFamily": "Inter, sans-serif", "color": T.TEXT_MUTED},
                        {"if": {"column_id": "sub_sector"}, "textAlign": "left",
                         "fontFamily": "Inter, sans-serif", "color": T.TEXT_MUTED,
                         "fontSize": "0.85rem"},
                        # Lazy "Get price" cell — visually clickable.
                        {"if": {"column_id": "current_price"},
                         "cursor": "pointer", "color": T.PRIMARY,
                         "textDecoration": "underline"},
                    ],
                    style_header=T.DATATABLE_HEADER,
                    style_data_conditional=[],
                    style_filter=T.DATATABLE_FILTER,
                ),
            ]),
        ], style=T.CARD_STYLE),
    ])
