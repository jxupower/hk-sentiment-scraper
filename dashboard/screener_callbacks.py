import sqlite3
import threading
import time
from datetime import datetime
from statistics import mean, median

import dash
import plotly.graph_objects as go
from dash import Input, Output, State, callback_context
from dash.exceptions import PreventUpdate

from dashboard import theme as T
from dashboard.screener_layout import NUMERIC_FILTERS
from dashboard.screener_presets import INVESTOR_PRESETS


# Module-level lock prevents the manual "Refresh prices now" button from
# spawning concurrent yfinance pulls. APScheduler's own jobs use
# max_instances=1 / coalesce=True; this lock guards the dashboard path.
_manual_price_refresh_lock = threading.Lock()


# Range-filter primitives — moved to analysis/preset_filter.py so the
# Backtest engine can apply preset constraints with identical semantics.
# Re-exported here so the existing call sites below don't need rewriting.
from analysis.preset_filter import _in_range, _xform_value  # noqa: F401


def _extract_clicked_bucket(click_bar, click_ann):
    """Pull the bucket name out of whichever click event just fired.
    Bar clicks expose it as `points[0].y`; annotation clicks as
    `annotation.text`. Uses callback_context.triggered to know which one."""
    triggered = callback_context.triggered or []
    prop_id = triggered[0]["prop_id"] if triggered else ""
    if prop_id.endswith("clickData") and click_bar:
        return click_bar["points"][0]["y"]
    if prop_id.endswith("clickAnnotationData") and click_ann:
        return click_ann["annotation"]["text"]
    return None


# Map of aggregation key -> (display name, function over list of (pe, market_cap) tuples).
# Cap-weighted uses the index methodology: Σ market_cap / Σ earnings,
# where earnings_i = market_cap_i / pe_i. This is what indices like the
# S&P 500's reported P/E actually use — gives heavier weight to mega-caps.
def _agg_median(items):
    return median([pe for pe, _ in items])


def _agg_mean(items):
    return mean([pe for pe, _ in items])


def _agg_cap_weighted(items):
    """Σ market_cap / Σ earnings (index P/E methodology). Drops items
    with missing/zero market_cap; returns None if the bucket has no
    usable cap data (caller falls back to median in that case)."""
    capped = [(pe, mc) for pe, mc in items if mc and mc > 0]
    if not capped:
        return None
    total_cap = sum(mc for _, mc in capped)
    total_earnings = sum(mc / pe for pe, mc in capped)
    if total_earnings <= 0:
        return None
    return total_cap / total_earnings


_AGG = {
    "median":       ("Median",       _agg_median),
    "mean":         ("Mean",         _agg_mean),
    "cap_weighted": ("Cap-weighted", _agg_cap_weighted),
}


def register_screener_callbacks(app, db_path: str):
    # Cell-click on the screener table's ticker column → jump to Research tab
    # and load that ticker. Uses the cross-tab-nav Store as the trigger, which
    # the Research-tab callbacks listen for.
    @app.callback(
        Output("main-tabs", "active_tab", allow_duplicate=True),
        Output("cross-tab-nav", "data", allow_duplicate=True),
        Input("screener-table", "active_cell"),
        State("screener-table", "data"),
        prevent_initial_call=True,
    )
    def jump_to_research_from_screener(active_cell, table_rows):
        if not active_cell or active_cell.get("column_id") != "ticker":
            raise PreventUpdate
        try:
            ticker = table_rows[active_cell["row"]]["ticker"]
        except (KeyError, IndexError, TypeError):
            raise PreventUpdate
        if not ticker:
            raise PreventUpdate
        # `ts` makes the Store payload unique per click — repeated clicks on the
        # same ticker still produce a new dict so render_report re-fires.
        return "tab-stock-research", {"ticker": ticker, "ts": int(time.time() * 1000)}

    # Cell-click on the current_price column → fetch that one ticker's latest
    # adj_close and replace the "Get price" placeholder in just that row.
    # Lazy-loading per row avoids the ~20s Supabase bulk pull at page-load —
    # the user pays the cost only for tickers they care about.
    @app.callback(
        Output("screener-table", "data", allow_duplicate=True),
        Input("screener-table", "active_cell"),
        State("screener-table", "data"),
        prevent_initial_call=True,
    )
    def fetch_price_on_click(active_cell, table_rows):
        if not active_cell or active_cell.get("column_id") != "current_price":
            raise PreventUpdate
        row_idx = active_cell["row"]
        try:
            row = table_rows[row_idx]
        except (IndexError, TypeError):
            raise PreventUpdate
        # Don't refetch — once a row holds a number (or the "—" miss marker),
        # leave it. Clicking again would be a no-op tax on Supabase.
        if row.get("current_price") != "Get price":
            raise PreventUpdate
        ticker = row.get("ticker")
        if not ticker:
            raise PreventUpdate
        price = _fetch_one_price(db_path, ticker)
        row["current_price"] = round(price, 2) if price is not None else "—"
        return table_rows

    # Clear filters button — resets ALL user-driven filters back to their
    # defaults across the 5 accordion groups. Does NOT reset the P/E
    # aggregation toggle (that's a display choice, not a filter), and does
    # NOT collapse the sub-sector chart once loaded (per-session decision).
    clear_outputs = [
        Output("screener-sector-filter", "value", allow_duplicate=True),
        Output("screener-subsector-filter", "value", allow_duplicate=True),
        Output("screener-completeness-filter", "value", allow_duplicate=True),
        Output("screener-ticker-search", "value", allow_duplicate=True),
        Output("screener-name-search", "value", allow_duplicate=True),
    ]
    # Append the slider Outputs for every numeric filter. The slider→inputs
    # sync callback then propagates the reset into the number boxes.
    for _label, slug, lo, hi, _step, _key, _xform in NUMERIC_FILTERS:
        clear_outputs.append(
            Output(f"screener-{slug}-slider", "value", allow_duplicate=True)
        )

    @app.callback(
        *clear_outputs,
        Input("screener-clear-filters-btn", "n_clicks"),
        prevent_initial_call=True,
    )
    def clear_screener_filters(_n):
        slider_defaults = [[lo, hi] for _l, _s, lo, hi, _st, _k, _x in NUMERIC_FILTERS]
        # Order matches clear_outputs above.
        return ([], [], 0.5, "", "", *slider_defaults)

    # Investor preset buttons — one callback per preset. Each click rewrites
    # every numeric range slider: slugs the preset constrains get the preset
    # values, the rest get reset to full-range defaults. Sector/sub-sector
    # dropdowns and text searches are deliberately left untouched so any
    # user-applied narrowing persists when a preset is loaded.
    preset_slider_outputs = [
        Output(f"screener-{slug}-slider", "value", allow_duplicate=True)
        for _l, slug, _lo, _hi, _st, _k, _x in NUMERIC_FILTERS
    ]
    for _preset in INVESTOR_PRESETS:
        _register_preset_callback(app, _preset, preset_slider_outputs)

    # "Refresh prices now" button — kicks off the same yfinance pull as the
    # daily cron (period='5d' over the full active universe) in a background
    # daemon thread. Returns immediately so the dashboard stays responsive.
    # The status text shows "Started at HH:MM"; the next auto-refresh tick
    # (every 5 min) re-queries the DB so new bars surface naturally.
    @app.callback(
        Output("screener-refresh-prices-status", "children"),
        Input("screener-refresh-prices-btn", "n_clicks"),
        prevent_initial_call=True,
    )
    def refresh_prices_manually(_n):
        if not _manual_price_refresh_lock.acquire(blocking=False):
            return "Already running — please wait."
        started = datetime.now().strftime("%H:%M:%S")

        def _run():
            try:
                from storage.database import Database
                from storage.factory import get_prices_repo
                from storage.repository import SecuritiesRepository
                from scrapers.historical_price_scraper import fetch_many
                db = Database(db_path)
                tickers = [s["ticker"] for s in SecuritiesRepository(db).get_universe()]
                fetch_many(tickers, get_prices_repo(db), period="5d")
            finally:
                _manual_price_refresh_lock.release()

        threading.Thread(target=_run, daemon=True).start()
        return (f"Refresh started at {started} — completes in ~5-10 min. "
                "Dashboard auto-refresh will pick up new data.")

    # Click-to-filter on the sector P/E chart. Two click sources, same handler:
    #   * Bar click -> Dash fires `clickData`
    #   * Label click -> Dash fires `clickAnnotationData` (the y-axis labels
    #     are rendered as Plotly annotations with captureevents=True; default
    #     y-tick labels are hidden in _build_pe_chart_by_key).
    # Append-mode (locked): a second click on the same bucket is a no-op;
    # to remove, use the dropdown's × button. The downstream cascade
    # (update_screener) re-fires automatically because the dropdown value
    # is its Input.
    @app.callback(
        Output("screener-sector-filter", "value", allow_duplicate=True),
        Input("screener-sector-pe-chart", "clickData"),
        Input("screener-sector-pe-chart", "clickAnnotationData"),
        State("screener-sector-filter", "value"),
        prevent_initial_call=True,
    )
    def append_sector_filter_on_chart_click(click_bar, click_ann, current):
        clicked = _extract_clicked_bucket(click_bar, click_ann)
        if not clicked:
            raise PreventUpdate
        current = list(current or [])
        if clicked in current:
            raise PreventUpdate
        return current + [clicked]

    # Same pattern for the sub-sector chart. Self-contained (locked): does
    # NOT also fill the parent Sector dropdown; the sub-sector filter alone
    # narrows the table sufficiently.
    @app.callback(
        Output("screener-subsector-filter", "value", allow_duplicate=True),
        Input("screener-subsector-pe-chart", "clickData"),
        Input("screener-subsector-pe-chart", "clickAnnotationData"),
        State("screener-subsector-filter", "value"),
        prevent_initial_call=True,
    )
    def append_subsector_filter_on_chart_click(click_bar, click_ann, current):
        clicked = _extract_clicked_bucket(click_bar, click_ann)
        if not clicked:
            raise PreventUpdate
        current = list(current or [])
        if clicked in current:
            raise PreventUpdate
        return current + [clicked]

    # "Load Sub-Sector P/E Chart" button — flips the Store flag and reveals
    # the Graph. Idempotent (multiple clicks no-op). Once loaded, the chart
    # stays reactive to filter changes for the rest of the session.
    @app.callback(
        Output("screener-subsector-chart-loaded", "data"),
        Output("screener-subsector-chart-loader", "style"),
        Output("screener-subsector-pe-chart", "style"),
        Input("screener-load-subsector-btn", "n_clicks"),
        prevent_initial_call=True,
    )
    def reveal_subsector_chart(_n):
        return True, {"display": "none"}, {"display": "block"}

    # --- Slider ↔ number-input sync, registered in a loop ---
    # For each numeric filter: two tiny callbacks. Slider value drives the
    # number boxes; number boxes drive the slider (clamped + reordered).
    # `prevent_initial_call=True` + `allow_duplicate=True` stops loops on
    # initial render. Equal-value-no-op guard inside each handler keeps the
    # second-leg of a slider->box->slider chain from firing.
    for _label, slug, lo, hi, _step, _key, _xform in NUMERIC_FILTERS:
        _register_range_sync(app, slug, lo, hi)

    # --- Main update callback ---
    # Inputs ordering matters; positional unpacking on the function side has
    # to match exactly. Number-input boxes are NOT Inputs here (slider is
    # the canonical source via the sync callbacks above).
    update_inputs = [
        Input("screener-auto-refresh", "n_intervals"),
        Input("screener-refresh-btn", "n_clicks"),
        Input("screener-sector-filter", "value"),
        Input("screener-subsector-filter", "value"),
        Input("screener-completeness-filter", "value"),
        Input("screener-pe-aggregation", "value"),
        Input("screener-ticker-search", "value"),
        Input("screener-name-search", "value"),
    ]
    for _label, slug, _lo, _hi, _step, _key, _xform in NUMERIC_FILTERS:
        update_inputs.append(Input(f"screener-{slug}-slider", "value"))
    # Store flag is now an Input (was State): clicking "Load Sub-Sector P/E
    # Chart" flips it False→True, which fires update_screener and actually
    # builds the chart. Previously the flag was State, so the chart only
    # appeared on the *next* filter change after Load was clicked.
    update_inputs.append(Input("screener-subsector-chart-loaded", "data"))

    @app.callback(
        Output("screener-table", "data"),
        Output("screener-stat-total", "children"),
        Output("screener-stat-with-data", "children"),
        Output("screener-stat-latest", "children"),
        Output("screener-row-count", "children"),
        Output("screener-sector-pe-chart", "figure"),
        Output("screener-subsector-pe-chart", "figure"),
        Output("screener-sector-pe-header", "children"),
        Output("screener-subsector-pe-header", "children"),
        Output("screener-sector-filter", "options"),
        Output("screener-subsector-filter", "options"),
        *update_inputs,
    )
    def update_screener(_n, _clicks, sector_filter, subsector_filter,
                          min_completeness, pe_aggregation,
                          ticker_query, name_query, *slider_values_then_loaded):
        # Last positional arg is the load-flag Input; everything before it is
        # the slider [lo, hi] for each NUMERIC_FILTERS entry, in declaration order.
        subsector_loaded = slider_values_then_loaded[-1]
        slider_values = slider_values_then_loaded[:-1]

        rows = _query_latest(db_path)
        total_universe = _count_universe(db_path)
        # "Latest snapshot" reflects the freshest *price* date in the
        # historical_prices store — that's what the daily EOD refresh job
        # actually moves. Fundamentals snapshots refresh on a separate
        # (slower) cadence and are not what users read as "data freshness".
        latest_date = _latest_price_date(db_path) or "—"

        # Apply filters in Python (small dataset, simpler than SQL string construction)
        filtered = [r for r in rows
                    if (r.get("data_completeness") or 0) >= (min_completeness or 0)]
        if sector_filter:
            chosen = set(sector_filter)
            filtered = [r for r in filtered if r.get("yf_sector") in chosen]
        if subsector_filter:
            chosen_sub = set(subsector_filter)
            filtered = [r for r in filtered if r.get("sub_sector") in chosen_sub]

        # Text searches — case-insensitive substring match. Empty query = no-op.
        if ticker_query:
            q = ticker_query.strip().lower()
            if q:
                filtered = [r for r in filtered
                            if q in (r.get("ticker") or "").lower()]
        if name_query:
            q = name_query.strip().lower()
            if q:
                filtered = [r for r in filtered
                            if q in (r.get("name") or "").lower()]

        # Numeric range filters — skip ones at their full default range so
        # rows with missing data don't get excluded by un-touched filters.
        for i, (_label, _slug, lo_default, hi_default, _step,
                key, xform_key) in enumerate(NUMERIC_FILTERS):
            slider = slider_values[i] or [lo_default, hi_default]
            lo, hi = slider[0], slider[1]
            if lo == lo_default and hi == hi_default:
                continue
            filtered = [r for r in filtered
                        if _in_range(_xform_value(xform_key, r.get(key)),
                                      lo, hi)]

        table_data = [_format_row(r) for r in filtered]

        # Dropdown options come from the full snapshot population, not filtered view
        sector_set = sorted({r["yf_sector"] for r in rows if r.get("yf_sector")})
        sector_options = [{"label": s, "value": s} for s in sector_set]
        subsector_set = sorted({r["sub_sector"] for r in rows if r.get("sub_sector")})
        subsector_options = [{"label": s, "value": s} for s in subsector_set]

        agg = pe_aggregation or "median"
        chart = _build_sector_pe_chart(filtered, aggregation=agg)
        # Sub-sector chart only builds when the user has clicked "Load chart"
        # at least once this session. Saves ~1s per filter change before then.
        subsector_chart = (_build_subsector_pe_chart(filtered, aggregation=agg)
                            if subsector_loaded else dash.no_update)
        agg_label = _AGG.get(agg, _AGG["median"])[0]
        sector_header = f"{agg_label} P/E by Sector"
        subsector_header = f"{agg_label} P/E by Sub-Sector"

        return (
            table_data,
            f"{total_universe:,}",
            f"{len(rows):,}",
            latest_date,
            f"{len(filtered):,} of {len(rows):,} matching filters",
            chart,
            subsector_chart,
            sector_header,
            subsector_header,
            sector_options,
            subsector_options,
        )


def _register_preset_callback(app, preset: dict, preset_slider_outputs: list):
    """Wire one investor-preset button to the numeric range sliders. The
    callback writes a [lo, hi] for every NUMERIC_FILTERS slug — preset
    overrides if specified, else the slug's full-range default — so the
    preset reads as a complete screen state rather than additive narrowing
    on top of whatever was already set."""
    overrides = preset["sliders"]

    @app.callback(
        *preset_slider_outputs,
        Input(f"screener-preset-{preset['id']}-btn", "n_clicks"),
        prevent_initial_call=True,
    )
    def apply_preset(_n):
        if not _n:
            raise PreventUpdate
        out = []
        for _label, slug, lo, hi, _step, _key, _xform in NUMERIC_FILTERS:
            out.append(overrides.get(slug) or [lo, hi])
        return tuple(out)


def _register_range_sync(app, slug: str, lo: float, hi: float):
    """Two-way slider ↔ number-input sync for one numeric filter. Slider is
    canonical (it's the Input on update_screener); inputs are display +
    typed-entry. The equality guard inside each handler prevents
    a slider->inputs->slider ping-pong from re-firing the main update."""
    @app.callback(
        Output(f"screener-{slug}-min", "value", allow_duplicate=True),
        Output(f"screener-{slug}-max", "value", allow_duplicate=True),
        Input(f"screener-{slug}-slider", "value"),
        State(f"screener-{slug}-min", "value"),
        State(f"screener-{slug}-max", "value"),
        prevent_initial_call=True,
    )
    def _slider_to_inputs(v, cur_min, cur_max):
        if not v:
            raise PreventUpdate
        new_min, new_max = v[0], v[1]
        # Equality guard — bail out if both inputs already match so we don't
        # trigger _inputs_to_slider unnecessarily.
        if cur_min == new_min and cur_max == new_max:
            raise PreventUpdate
        return new_min, new_max

    @app.callback(
        Output(f"screener-{slug}-slider", "value", allow_duplicate=True),
        Input(f"screener-{slug}-min", "value"),
        Input(f"screener-{slug}-max", "value"),
        State(f"screener-{slug}-slider", "value"),
        prevent_initial_call=True,
    )
    def _inputs_to_slider(mn, mx, cur):
        mn_c = lo if mn is None else max(lo, min(hi, mn))
        mx_c = hi if mx is None else max(lo, min(hi, mx))
        if mn_c > mx_c:
            mn_c, mx_c = mx_c, mn_c
        if cur and cur[0] == mn_c and cur[1] == mx_c:
            raise PreventUpdate
        return [mn_c, mx_c]


def _query_latest(db_path: str) -> list[dict]:
    """Latest snapshot per ticker, joined with securities for name + watchlist flag."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT f.ticker, f.snapshot_date, f.trailing_pe, f.forward_pe,
                   f.price_to_book, f.ev_to_ebitda, f.dividend_yield,
                   f.market_cap, f.beta, f.return_on_equity, f.debt_to_equity,
                   f.earnings_growth,
                   f.last_price, f.currency, f.data_completeness,
                   s.name, s.is_watchlist, s.watchlist_sector,
                   s.yf_sector, s.yf_industry, s.sub_sector, s.effective_sector
            FROM fundamentals_snapshots f
            INNER JOIN (
                SELECT ticker, MAX(snapshot_date) AS max_date
                FROM fundamentals_snapshots
                GROUP BY ticker
            ) latest ON f.ticker = latest.ticker AND f.snapshot_date = latest.max_date
            INNER JOIN securities s ON f.ticker = s.ticker
            WHERE s.is_active = 1
            ORDER BY f.market_cap DESC NULLS LAST
        """).fetchall()
        return [dict(r) for r in rows]


def _count_universe(db_path: str) -> int:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM securities WHERE is_active = 1"
        ).fetchone()[0]


def _latest_price_date(db_path: str) -> str | None:
    """Freshest date in historical_prices, routed through the SQLite/cloud
    factory so the pill reflects whichever backend the dashboard is actually
    reading prices from (Supabase when USE_CLOUD_DB=true)."""
    try:
        from storage.database import Database
        from storage.factory import get_prices_repo
        repo = get_prices_repo(Database(db_path))
        return repo.latest_date_any()
    except Exception:
        # Fail soft — the pill just shows "—" if the lookup errors. The
        # screener table itself doesn't depend on this value.
        return None


def _fetch_one_price(db_path: str, ticker: str) -> float | None:
    """Single-ticker latest adj_close — backs the per-row 'Get price' click.
    Routes through the factory so it hits Supabase under USE_CLOUD_DB=true.
    Indexed lookup; sub-second even on the pooler."""
    try:
        from storage.database import Database
        from storage.factory import get_prices_repo
        repo = get_prices_repo(Database(db_path))
        return repo.latest_price(ticker)
    except Exception:
        return None


def _format_row(r: dict) -> dict:
    """Apply display formatting (rounding, ROE→%, market cap→billions, watchlist star)."""
    import math
    def rnd(v, digits=2):
        if v is None:
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None  # defensive against stale string values like 'Infinity' in old rows
        if math.isnan(f) or math.isinf(f):
            return None
        return round(f, digits)

    market_cap = r.get("market_cap")
    roe = r.get("return_on_equity")
    completeness = r.get("data_completeness")

    return {
        "ticker": r.get("ticker"),
        "name": (r.get("name") or "")[:30],
        "yf_sector": r.get("yf_sector") or r.get("watchlist_sector") or "—",
        "sub_sector": r.get("sub_sector") or "—",
        # Lazy-fetched: click the cell to populate. Avoids the ~20s Supabase
        # bulk pull at page-load and lets the user pay the cost only for
        # tickers they care about.
        "current_price": "Get price",
        "market_cap_b": rnd(market_cap / 1e9, 1) if market_cap else None,
        "trailing_pe": rnd(r.get("trailing_pe"), 1),
        "forward_pe": rnd(r.get("forward_pe"), 1),
        "price_to_book": rnd(r.get("price_to_book"), 2),
        "ev_to_ebitda": rnd(r.get("ev_to_ebitda"), 1),
        "dividend_yield": rnd(r.get("dividend_yield"), 2),
        "return_on_equity_pct": rnd(roe * 100, 1) if roe is not None else None,
        "debt_to_equity": rnd(r.get("debt_to_equity"), 1),
        "beta": rnd(r.get("beta"), 2),
        "completeness_pct": rnd((completeness or 0) * 100, 0),
    }


def _build_sector_pe_chart(rows: list[dict], aggregation: str = "median") -> go.Figure:
    """Trailing P/E per sector (only sectors with ≥3 tickers, P/E in 0-200 range)."""
    return _build_pe_chart_by_key(
        rows, aggregation=aggregation,
        bucket_key=lambda r: r.get("yf_sector") or r.get("watchlist_sector"),
        empty_message=("Need ≥3 tickers per sector with valid P/E. "
                       "Run 'fundamentals refresh --tickers ALL' to populate."),
    )


def _build_subsector_pe_chart(rows: list[dict], aggregation: str = "median") -> go.Figure:
    """Trailing P/E per SUB-SECTOR — finer-grained companion to the sector
    chart above. Same filters (P/E in (0, 200], ≥3 tickers per bucket), same
    styling, same aggregation as the sector chart (driven by a single toggle).
    Tickers without a sub_sector assignment (yfinance-metadata-NULL) dropped.

    `clickable_labels=False` — with ~75 buckets, the captureevents annotations
    used to make y-tick labels clickable were the dominant client-side render
    cost (each annotation = an SVG element with event listeners). Bars stay
    clickable for filtering; only the text label loses click affordance."""
    return _build_pe_chart_by_key(
        rows, aggregation=aggregation,
        bucket_key=lambda r: r.get("sub_sector"),
        empty_message=("Need ≥3 tickers per sub-sector with valid P/E. "
                       "Filter to a parent sector or backfill yfinance metadata."),
        clickable_labels=False,
    )


def _build_pe_chart_by_key(rows: list[dict], bucket_key, empty_message: str,
                             aggregation: str = "median",
                             clickable_labels: bool = True) -> go.Figure:
    """Shared horizontal-bar P/E chart builder. `bucket_key` is a callable
    taking a row and returning the string label to bucket on (or falsy to
    skip). `aggregation` is one of "median" / "mean" / "cap_weighted" —
    see the _AGG dict at the top of the module. Each bucket stores
    (pe, market_cap) tuples so cap-weighted aggregation has what it needs;
    median/mean simply ignore the second tuple element."""
    agg_label, agg_fn = _AGG.get(aggregation, _AGG["median"])

    by_bucket: dict[str, list[tuple[float, float]]] = {}
    for r in rows:
        key = bucket_key(r)
        pe = r.get("trailing_pe")
        if not key or pe is None or pe <= 0 or pe > 200:
            continue
        mc = r.get("market_cap") or 0.0
        by_bucket.setdefault(key, []).append((float(pe), float(mc)))

    eligible = []
    for s, items in by_bucket.items():
        if len(items) < 3:
            continue
        agg_value = agg_fn(items)
        if agg_value is None:
            # Cap-weighted may fall back to None if no tickers in the bucket
            # have market_cap data; quietly degrade to median so the bar
            # still renders rather than disappearing.
            agg_value = _agg_median(items)
        eligible.append((s, agg_value))
    eligible.sort(key=lambda x: x[1])

    if not eligible:
        fig = go.Figure()
        fig.add_annotation(text=empty_message,
                           xref="paper", yref="paper", x=0.5, y=0.5,
                           showarrow=False, font=dict(size=13, color="#90a4ae"))
        fig.update_layout(_dark_layout(""), height=180)
        return fig

    buckets = [s for s, _ in eligible]
    values = [v for _, v in eligible]
    counts = [len(by_bucket[s]) for s in buckets]

    # When clickable_labels=True, replace the default y-axis tick labels with
    # captureevents annotations (each fires clickAnnotationData). This makes
    # the text labels themselves clickable for filtering. For large bucket
    # counts (e.g. the 75-bucket sub-sector chart), the per-annotation event
    # listeners dominate client-side render time, so we fall back to plain
    # tick labels — the bars themselves stay clickable via clickData.
    if clickable_labels:
        label_annotations = [
            dict(
                x=0, y=bucket,
                xref="x", yref="y",
                text=bucket,
                showarrow=False,
                xanchor="right", yanchor="middle",
                xshift=-8,
                font=dict(size=11, color=T.TEXT),
                captureevents=True,
                hovertext=f"Click to filter to: {bucket}",
            )
            for bucket in buckets
        ]
        yaxis_kwargs = dict(showticklabels=False)
    else:
        label_annotations = []
        yaxis_kwargs = dict(showticklabels=True,
                            tickfont=dict(size=11, color=T.TEXT))

    fig = go.Figure(go.Bar(
        x=values, y=buckets, orientation="h",
        marker_color="#1976d2",
        text=[f"{v:.1f} (n={c})" for v, c in zip(values, counts)],
        textposition="outside",
        hovertemplate=f"<b>%{{y}}</b><br>{agg_label} P/E: %{{x:.1f}}<br>Tickers: %{{text}}<extra></extra>",
    ))
    fig.update_layout(_dark_layout(""),
                      xaxis_title=f"{agg_label} Trailing P/E",
                      yaxis=yaxis_kwargs,
                      annotations=label_annotations,
                      height=max(220, len(buckets) * 28 + 80),
                      margin=dict(l=220, r=80, t=20, b=40),
                      # Stable uirevision lets Plotly diff-render rather than
                      # rebuilding the whole SVG on filter changes — big win
                      # when only a couple of buckets shift values.
                      uirevision="pe-chart")
    return fig


def _dark_layout(title: str) -> dict:
    base = T.chart_layout(title=title, height=240)
    base["legend"] = dict(bgcolor=T.CARD_BG, bordercolor=T.BORDER, borderwidth=1,
                          font=dict(color=T.TEXT))
    return base
