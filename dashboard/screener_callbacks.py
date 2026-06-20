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


# ---- Tier-1 perf caches ---------------------------------------------------
# All caches are in-process. Flushed in `_flush_perf_caches()` when the
# manual "Refresh prices now" button fires so a price refresh is reflected
# without waiting for natural TTL expiry.

# 60s TTL on the Screener's universe-wide latest-snapshot pull. Underlying
# data refreshes daily via cron; auto-refresh interval is 5 min anyway, so
# 60s lag is well within tolerance.
_QUERY_LATEST_CACHE: dict = {"rows": None, "expires": 0.0}
_QUERY_LATEST_TTL = 60

# 5-min TTL on per-row "Get price" clicks. Same ticker re-clicked during a
# session returns instantly. Cron refreshes prices once a day.
_PRICE_CACHE: dict = {}   # {ticker: (price, expires)}
_PRICE_TTL = 300

# 60s TTL on built P/E figures keyed by (chart_id, aggregation, row_set
# signature). Sliders that drag without changing bucket membership re-use
# the cached figure instead of triggering a fresh Plotly SVG build.
_PE_CHART_CACHE: dict = {}
_PE_CHART_TTL = 60


def _flush_perf_caches() -> None:
    """Wipe price + query + chart caches. Called from the manual price-
    refresh button so freshly-pulled prices surface immediately."""
    _QUERY_LATEST_CACHE["rows"] = None
    _QUERY_LATEST_CACHE["expires"] = 0.0
    _PRICE_CACHE.clear()
    _PE_CHART_CACHE.clear()


def _row_set_signature(rows: list) -> tuple:
    """Cheap signature that flips whenever the filtered row set's identity
    or aggregate stats would change which bucket-sums the P/E chart sees.
    Hashes ticker count + cumulative trailing_pe + market_cap so adding,
    removing, or value-shifting any row in the filtered set invalidates
    the cached figure."""
    n = len(rows)
    sum_pe = 0.0
    sum_mc = 0.0
    for r in rows:
        pe = r.get("trailing_pe")
        mc = r.get("market_cap")
        if pe:
            try:
                sum_pe += float(pe)
            except (TypeError, ValueError):
                pass
        if mc:
            try:
                sum_mc += float(mc)
            except (TypeError, ValueError):
                pass
    return (n, round(sum_pe, 2), round(sum_mc, 0))


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
    # ----- i18n: flip every translatable element when the language Store changes -----
    # Fires on language toggle. Outputs every visible label on the Screener
    # tab. DataTable columns + dropdown placeholders + button children + stat
    # labels + accordion titles + preset button text are all updated in one
    # shot here (single round-trip on language change).
    @app.callback(
        # Stat-block labels
        Output("screener-stat-total-label", "children"),
        Output("screener-stat-with-data-label", "children"),
        Output("screener-stat-latest-label", "children"),
        # Buttons (children) and refresh-prices button label
        Output("screener-refresh-btn", "children"),
        Output("screener-refresh-prices-btn", "children"),
        Output("screener-clear-filters-btn", "children"),
        Output("screener-load-subsector-btn", "children"),
        # Presets card
        Output("screener-presets-title", "children"),
        Output("screener-presets-subtitle", "children"),
        # Filters card title + accordion titles
        Output("screener-filters-title", "children"),
        Output("screener-acc-search", "title"),
        Output("screener-acc-classification", "title"),
        Output("screener-acc-valuation", "title"),
        Output("screener-acc-quality", "title"),
        Output("screener-acc-size", "title"),
        # Labels inside Search + Classification accordion items
        Output("screener-label-ticker-contains", "children"),
        Output("screener-label-name-contains", "children"),
        Output("screener-label-sector", "children"),
        Output("screener-label-sub-sector", "children"),
        Output("screener-label-min-completeness", "children"),
        Output("screener-label-pe-agg", "children"),
        # Placeholders
        Output("screener-ticker-search", "placeholder"),
        Output("screener-name-search", "placeholder"),
        Output("screener-sector-filter", "placeholder"),
        Output("screener-subsector-filter", "placeholder"),
        # Range filter labels (Valuation + Quality + Size)
        Output("screener-pe-range-label", "children"),
        Output("screener-fwdpe-range-label", "children"),
        Output("screener-pb-range-label", "children"),
        Output("screener-evebitda-range-label", "children"),
        Output("screener-divyield-range-label", "children"),
        Output("screener-roe-range-label", "children"),
        Output("screener-egrowth-range-label", "children"),
        Output("screener-de-range-label", "children"),
        Output("screener-beta-range-label", "children"),
        Output("screener-mcap-range-label", "children"),
        # P/E aggregation toggle options
        Output("screener-pe-aggregation", "options"),
        # Range hint blurb
        Output("screener-range-hint", "children"),
        # Sub-sector chart loader blurb
        Output("screener-subsector-loader-blurb", "children"),
        # Table title + columns
        Output("screener-table-title", "children"),
        Output("screener-table", "columns"),
        # Investor preset button labels + sub-titles
        *[Output(f"screener-preset-{p['id']}-label", "children")
          for p in INVESTOR_PRESETS],
        *[Output(f"screener-preset-{p['id']}-title", "children")
          for p in INVESTOR_PRESETS],
        Input("user-language", "data"),
    )
    def i18n_screener(lang):
        from dashboard.i18n import T as I
        lang = lang or "en"
        # Range filter labels keyed by slug — order must match the Outputs above
        range_labels = [
            I("screener.label.trailing_pe", lang),
            I("screener.label.forward_pe", lang),
            I("screener.label.pb", lang),
            I("screener.label.evebitda", lang),
            I("screener.label.dividend_yield", lang),
            I("screener.label.roe", lang),
            I("screener.label.earnings_growth", lang),
            I("screener.label.de", lang),
            I("screener.label.beta", lang),
            I("screener.label.mcap", lang),
        ]
        agg_options = [
            {"label": I("common.median", lang), "value": "median"},
            {"label": I("common.mean", lang), "value": "mean"},
            {"label": I("common.cap_weighted", lang), "value": "cap_weighted"},
        ]
        cols = [
            {"name": I("screener.col.ticker", lang),       "id": "ticker"},
            {"name": I("screener.col.name", lang),         "id": "name"},
            {"name": I("screener.col.sector", lang),       "id": "yf_sector"},
            {"name": I("screener.col.sub_sector", lang),   "id": "sub_sector"},
            {"name": I("screener.col.price", lang),        "id": "current_price"},
            {"name": I("screener.col.mcap_b", lang),       "id": "market_cap_b",
             "type": "numeric"},
            {"name": I("screener.col.pe", lang),           "id": "trailing_pe",
             "type": "numeric"},
            {"name": I("screener.col.fwd_pe", lang),       "id": "forward_pe",
             "type": "numeric"},
            {"name": I("screener.col.pb", lang),           "id": "price_to_book",
             "type": "numeric"},
            {"name": I("screener.col.evebitda", lang),     "id": "ev_to_ebitda",
             "type": "numeric"},
            {"name": I("screener.col.div_yield", lang),    "id": "dividend_yield",
             "type": "numeric"},
            {"name": I("screener.col.roe", lang),          "id": "return_on_equity_pct",
             "type": "numeric"},
            {"name": I("screener.col.de", lang),           "id": "debt_to_equity",
             "type": "numeric"},
            {"name": I("screener.col.beta", lang),         "id": "beta",
             "type": "numeric"},
            {"name": I("screener.col.completeness", lang), "id": "completeness_pct",
             "type": "numeric"},
        ]
        preset_labels = [I(f"screener.preset.{p['id']}.label", lang)
                          for p in INVESTOR_PRESETS]
        preset_titles = [I(f"screener.preset.{p['id']}.title", lang)
                          for p in INVESTOR_PRESETS]
        return (
            I("screener.stat.universe", lang),
            I("screener.stat.with_data", lang),
            I("screener.stat.latest", lang),
            I("screener.btn.refresh", lang),
            I("screener.btn.refresh_prices", lang),
            I("screener.btn.clear_filters", lang),
            I("screener.btn.load_subsector_chart", lang),
            I("screener.presets.title", lang),
            I("screener.presets.subtitle", lang),
            I("screener.filters", lang),
            I("screener.accordion.search", lang),
            I("screener.accordion.classification", lang),
            I("screener.accordion.valuation", lang),
            I("screener.accordion.quality", lang),
            I("screener.accordion.size", lang),
            I("screener.label.ticker_contains", lang),
            I("screener.label.name_contains", lang),
            I("screener.label.sector", lang),
            I("screener.label.sub_sector", lang),
            I("screener.label.min_completeness", lang),
            I("screener.label.pe_aggregation", lang),
            I("screener.ph.ticker", lang),
            I("screener.ph.name", lang),
            I("screener.ph.all_sectors", lang),
            I("screener.ph.all_subsectors", lang),
            *range_labels,
            agg_options,
            I("screener.range_hint", lang),
            I("screener.chart.defer_blurb", lang),
            I("screener.table.title", lang),
            cols,
            *preset_labels,
            *preset_titles,
        )

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
        # Accept the English placeholder OR its translated form so a row
        # populated under one language still recognises a re-click under the
        # other.
        from dashboard.i18n import EN, ZH
        placeholders = {EN.get("screener.get_price"), ZH.get("screener.get_price")}
        if row.get("current_price") not in placeholders:
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
        State("user-language", "data"),
        prevent_initial_call=True,
    )
    def refresh_prices_manually(_n, lang):
        from dashboard.i18n import T as I
        lang = lang or "en"
        if not _manual_price_refresh_lock.acquire(blocking=False):
            return I("screener.status.refresh_running", lang)
        started = datetime.now().strftime("%H:%M:%S")
        # Flush perf caches immediately so the user's next interaction
        # picks up the refreshed snapshot rather than waiting for natural
        # TTL expiry. (The refresh itself runs in a daemon thread; cache
        # warming for new prices happens on the next click after it lands.)
        _flush_perf_caches()

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
                # Second flush after the refresh lands so any stale-cached
                # rows from BEFORE the refresh started don't linger.
                _flush_perf_caches()

        threading.Thread(target=_run, daemon=True).start()
        return I("screener.status.refresh_started", lang, time=started)

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
        State("user-language", "data"),
    )
    def update_screener(_n, _clicks, sector_filter, subsector_filter,
                          min_completeness, pe_aggregation,
                          ticker_query, name_query, *slider_values_then_loaded_lang):
        from dashboard.i18n import T as I
        from config.settings import get_sector_label, get_subsector_label
        # Last positional arg is the user-language State; second-to-last is
        # the load-flag Input; everything before is the slider [lo, hi].
        lang = slider_values_then_loaded_lang[-1] or "en"
        subsector_loaded = slider_values_then_loaded_lang[-2]
        slider_values = slider_values_then_loaded_lang[:-2]

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

        table_data = [_format_row(r, lang) for r in filtered]

        # Dropdown options come from the full snapshot population, not filtered
        # view. The displayed label translates via the sub_sectors_zh / parent_
        # sectors_zh maps in config/sub_sectors.yaml; the underlying `value`
        # stays English so the SQL filter keeps matching the DB column.
        sector_set = sorted({r["yf_sector"] for r in rows if r.get("yf_sector")})
        sector_options = [{"label": get_sector_label(s, lang), "value": s}
                            for s in sector_set]
        subsector_set = sorted({r["sub_sector"] for r in rows
                                  if r.get("sub_sector")})
        subsector_options = [{"label": get_subsector_label(s, lang), "value": s}
                              for s in subsector_set]

        agg = pe_aggregation or "median"
        chart = _build_sector_pe_chart(filtered, aggregation=agg)
        # Sub-sector chart only builds when the user has clicked "Load chart"
        # at least once this session. Saves ~1s per filter change before then.
        subsector_chart = (_build_subsector_pe_chart(filtered, aggregation=agg)
                            if subsector_loaded else dash.no_update)
        agg_lookup = {"median": "common.median", "mean": "common.mean",
                       "cap_weighted": "common.cap_weighted"}
        agg_label_translated = I(agg_lookup.get(agg, "common.median"), lang)
        sector_header = I("screener.chart.sector_pe", lang,
                            agg=agg_label_translated)
        subsector_header = I("screener.chart.subsector_pe", lang,
                                agg=agg_label_translated)

        return (
            table_data,
            f"{total_universe:,}",
            f"{len(rows):,}",
            latest_date,
            I("screener.row_count", lang,
                count=len(filtered), total=len(rows)),
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
    """Latest snapshot per ticker, joined with securities for name + watchlist flag.
    60s TTL cached: every filter slider + auto-refresh tick used to re-run
    this 500-800ms query; now it hits warm cache for the next minute."""
    now = time.time()
    if (_QUERY_LATEST_CACHE["rows"] is not None
            and _QUERY_LATEST_CACHE["expires"] > now):
        return _QUERY_LATEST_CACHE["rows"]
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
        out = [dict(r) for r in rows]
        _QUERY_LATEST_CACHE["rows"] = out
        _QUERY_LATEST_CACHE["expires"] = now + _QUERY_LATEST_TTL
        return out


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
    Indexed lookup; sub-second even on the pooler. 5-min TTL cache means
    re-clicking the same ticker (or returning to the Screener within the
    session) is instant."""
    now = time.time()
    hit = _PRICE_CACHE.get(ticker)
    if hit and hit[1] > now:
        return hit[0]
    try:
        from storage.database import Database
        from storage.factory import get_prices_repo
        repo = get_prices_repo(Database(db_path))
        price = repo.latest_price(ticker)
    except Exception:
        price = None
    _PRICE_CACHE[ticker] = (price, now + _PRICE_TTL)
    return price


def _format_row(r: dict, lang: str = "en") -> dict:
    """Apply display formatting (rounding, ROE→%, market cap→billions, watchlist star).
    `lang` translates the sector / sub-sector labels (display only) and the
    'Get price' placeholder."""
    import math
    from dashboard.i18n import T as I
    from config.settings import get_sector_label, get_subsector_label
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
    sector_raw = r.get("yf_sector") or r.get("watchlist_sector") or "—"
    sub_raw = r.get("sub_sector") or "—"

    return {
        "ticker": r.get("ticker"),
        "name": (r.get("name") or "")[:30],
        "yf_sector": get_sector_label(sector_raw, lang) if sector_raw != "—" else "—",
        "sub_sector": get_subsector_label(sub_raw, lang) if sub_raw != "—" else "—",
        # Lazy-fetched: click the cell to populate. Avoids the ~20s Supabase
        # bulk pull at page-load and lets the user pay the cost only for
        # tickers they care about.
        "current_price": I("screener.get_price", lang),
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
    """Trailing P/E per sector (only sectors with ≥3 tickers, P/E in 0-200 range).
    Memoised by (chart_id, aggregation, row-set signature) — slider drags
    that don't change membership re-use the cached Plotly figure instead of
    re-doing ~2s of SVG generation."""
    now = time.time()
    key = ("sector", aggregation, _row_set_signature(rows))
    cached = _PE_CHART_CACHE.get(key)
    if cached and cached[1] > now:
        return cached[0]
    fig = _build_pe_chart_by_key(
        rows, aggregation=aggregation,
        bucket_key=lambda r: r.get("yf_sector") or r.get("watchlist_sector"),
        empty_message=("Need ≥3 tickers per sector with valid P/E. "
                       "Run 'fundamentals refresh --tickers ALL' to populate."),
    )
    _PE_CHART_CACHE[key] = (fig, now + _PE_CHART_TTL)
    return fig


def _build_subsector_pe_chart(rows: list[dict], aggregation: str = "median") -> go.Figure:
    """Trailing P/E per SUB-SECTOR — finer-grained companion to the sector
    chart above. Same filters (P/E in (0, 200], ≥3 tickers per bucket), same
    styling, same aggregation as the sector chart (driven by a single toggle).
    Tickers without a sub_sector assignment (yfinance-metadata-NULL) dropped.

    `clickable_labels=False` — with ~75 buckets, the captureevents annotations
    used to make y-tick labels clickable were the dominant client-side render
    cost (each annotation = an SVG element with event listeners). Bars stay
    clickable for filtering; only the text label loses click affordance.

    Memoised the same way as _build_sector_pe_chart."""
    now = time.time()
    key = ("subsector", aggregation, _row_set_signature(rows))
    cached = _PE_CHART_CACHE.get(key)
    if cached and cached[1] > now:
        return cached[0]
    fig = _build_pe_chart_by_key(
        rows, aggregation=aggregation,
        bucket_key=lambda r: r.get("sub_sector"),
        empty_message=("Need ≥3 tickers per sub-sector with valid P/E. "
                       "Filter to a parent sector or backfill yfinance metadata."),
        clickable_labels=False,
    )
    _PE_CHART_CACHE[key] = (fig, now + _PE_CHART_TTL)
    return fig


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
