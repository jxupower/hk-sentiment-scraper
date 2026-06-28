"""Market tab callbacks.

Four callbacks:
  1. `set_index_options_for_market` — flips the index radio options + default
     value when the user toggles HK ↔ US in the header. Options are i18n-
     aware (option labels translate per user-language).
  2. `i18n_market` — flips every static UI label (alert, "Index" / "Period"
     headings, KPI card headings, constituent-table column headers) when
     user-language changes. Does NOT touch chart/table data values — those
     are owned by the data callbacks below and are translated inline.
  3. `update_index_chart_and_kpis` — fires on index/period change OR a
     language flip. Pulls the index price series via the existing
     `get_or_fetch_prices` cache-aside, slices to the selected period,
     renders the chart + 4 KPI cards. Index name in the chart title is
     translated via `market.index_name.{symbol}` i18n key.
  4. `update_constituent_table` — fires on index change OR a language
     flip. Reads constituent ticker list from `config.index_constituents`
     and filters the same `_query_latest` snapshot the Screener uses to
     those tickers. The "Constituents of X" title + "N of M priced" meta
     string + empty-state message all translate.
"""
from __future__ import annotations

import math
import time
from datetime import date, datetime, timedelta

from dash import Input, Output, State, html, no_update
from dash.exceptions import PreventUpdate

from config.index_constituents import constituents_for, index_meta
from dashboard import theme as T
from dashboard.charts import price_candlestick_chart, price_chart
from dashboard.i18n import T as I
from dashboard.market_layout import (
    CONSTITUENT_COLUMNS,
    DEFAULT_INDEX_HK, DEFAULT_INDEX_US,
    INDEX_OPTIONS_HK, INDEX_OPTIONS_US,
)


# ---- In-process OHLC price cache ----------------------------------------
# Keyed by ticker (NOT by chart style — both Line and Candle read from the
# same OHLC payload; Line just consumes the adj_close column). Slicing to
# the user-selected period happens client-side after the cache hit, so
# period flips never re-touch the DB.
#
# TTL = 15 min, aligned with PRICE_STALE_DAYS in analysis/data_loader.py.
# The Screener's "Refresh prices now" button calls _flush_perf_caches
# which we extend to also clear this cache so an explicit refresh is never
# fooled.
_INDEX_PRICE_CACHE: dict[str, tuple[list[dict], float]] = {}
_INDEX_PRICE_TTL_SECONDS = 15 * 60


def _get_ohlc_cached(ticker: str, db) -> list[dict]:
    """Return the full OHLC series for `ticker`, hitting the in-process
    cache first. On miss: prime via get_or_fetch_prices (handles cold
    yfinance/akshare round-trip + Supabase upsert), then SELECT the OHLC
    columns from the repo and cache the result."""
    now = time.time()
    cached = _INDEX_PRICE_CACHE.get(ticker)
    if cached is not None and cached[1] > now:
        return cached[0]
    from analysis.data_loader import get_or_fetch_prices
    from storage.factory import get_prices_repo
    # Primer call handles cold-cache fetch (akshare/yfinance) + upsert into
    # historical_prices. Return value is the date+adj_close subset we don't
    # use directly; we want the full OHLC, which the next call selects.
    get_or_fetch_prices(ticker, db)
    rows = get_prices_repo(db).get_full_ohlc_series(ticker) or []
    _INDEX_PRICE_CACHE[ticker] = (rows, now + _INDEX_PRICE_TTL_SECONDS)
    return rows


def _flush_index_price_cache() -> None:
    """Hook for the Screener's manual 'Refresh prices now' button so an
    operator-initiated refresh is reflected immediately on the Market tab,
    not after the 15-min TTL expiry."""
    _INDEX_PRICE_CACHE.clear()


def _localised_index_options(market: str, lang: str) -> list[dict]:
    """Return the index-radio option list for `market` with labels in `lang`.
    Falls back to the bundled English label when a translation key is missing."""
    base = INDEX_OPTIONS_US if (market or "HK").upper() == "US" else INDEX_OPTIONS_HK
    out = []
    for opt in base:
        sym = opt["value"]
        fallback = opt["label"]
        out.append({"value": sym,
                     "label": I(f"market.index_opt.{sym}", lang) or fallback})
    return out


def _localised_index_name(symbol: str, lang: str) -> str:
    """Translated full name for chart title / constituent table heading."""
    fallback_meta = index_meta(symbol) or {}
    fallback = fallback_meta.get("name") or symbol
    translated = I(f"market.index_name.{symbol}", lang)
    return translated if (translated and translated != fallback) else fallback


def _localised_columns(lang: str) -> list[dict]:
    """Constituent-table column spec with header `name` translated. The `id`
    fields stay constant so `data` rows still key in correctly."""
    out = []
    for col in CONSTITUENT_COLUMNS:
        cid = col["id"]
        out.append({**col,
                      "name": I(f"market.col.{cid}", lang) or col["name"]})
    return out


def register_market_callbacks(app, db_path: str):
    # =========================================================
    # 1. Index radio options per market (+ language flip)
    # =========================================================
    # Fires on user-market toggle (HK ↔ US in header) AND on language
    # change (so the option labels translate). Resets `value` to the
    # market's default index only when market itself changed — we keep
    # the user's current selection on a pure language flip.
    @app.callback(
        Output("market-index-select", "options"),
        Output("market-index-select", "value"),
        Input("user-market", "data"),
        Input("user-language", "data"),
        State("market-index-select", "value"),
    )
    def set_index_options_for_market(market, lang, current_value):
        from dash import callback_context
        market = (market or "HK").upper()
        lang = lang or "en"
        options = _localised_index_options(market, lang)
        # Language-only change keeps the current selection; market change
        # snaps back to the default index for that market.
        triggered = (callback_context.triggered[0]["prop_id"]
                       if callback_context.triggered else "")
        if "user-market" in triggered:
            new_value = (DEFAULT_INDEX_US if market == "US"
                            else DEFAULT_INDEX_HK)
        else:
            valid_values = {o["value"] for o in options}
            new_value = (current_value if current_value in valid_values
                            else (DEFAULT_INDEX_US if market == "US"
                                    else DEFAULT_INDEX_HK))
        return options, new_value

    # =========================================================
    # 2. i18n flip — static labels, KPI headings, table columns
    # =========================================================
    @app.callback(
        Output("market-alert-strong", "children"),
        Output("market-alert-body", "children"),
        Output("market-label-index", "children"),
        Output("market-label-period", "children"),
        Output("market-label-style", "children"),
        Output("market-chart-style", "options"),
        Output("market-kpi-last-label", "children"),
        Output("market-kpi-period-label", "children"),
        Output("market-kpi-ytd-label", "children"),
        Output("market-kpi-maxdd-label", "children"),
        Output("market-constituent-table", "columns"),
        Input("user-language", "data"),
    )
    def i18n_market(lang):
        lang = lang or "en"
        chart_style_options = [
            {"label": I("market.chart_style.line", lang),   "value": "line"},
            {"label": I("market.chart_style.candle", lang), "value": "candle"},
        ]
        return (
            I("market.alert.strong", lang),
            I("market.alert.body", lang),
            I("market.label.index", lang),
            I("market.label.period", lang),
            I("market.label.style", lang),
            chart_style_options,
            I("market.kpi.last_close", lang),
            I("market.kpi.period_return", lang),
            I("market.kpi.ytd_return", lang),
            I("market.kpi.max_drawdown", lang),
            _localised_columns(lang),
        )

    # =========================================================
    # 3. Index chart + 4 KPI cards (lang-aware)
    # =========================================================
    @app.callback(
        Output("market-index-chart", "figure"),
        Output("market-chart-title", "children"),
        Output("market-chart-period-label", "children"),
        Output("market-kpi-last", "children"),
        Output("market-kpi-period", "children"),
        Output("market-kpi-period", "style"),
        Output("market-kpi-ytd", "children"),
        Output("market-kpi-ytd", "style"),
        Output("market-kpi-maxdd", "children"),
        Input("market-index-select", "value"),
        Input("market-period-select", "value"),
        Input("market-chart-style", "value"),
        Input("user-language", "data"),
    )
    def update_index_chart_and_kpis(index_ticker, period_days, chart_style, lang):
        if not index_ticker:
            raise PreventUpdate
        lang = lang or "en"
        chart_style = (chart_style or "line").lower()

        # Single unified fetch path — _get_ohlc_cached returns the full
        # OHLC payload, served from an in-process 15-min TTL cache. Both
        # Line (reads `adj_close`) and Candle (reads OHLC) consume the
        # same rows, so switching style is a pure Plotly re-render and
        # switching period is a pure Python slice — no DB round-trip
        # either way after the first warm hit.
        from storage.database import Database
        db = Database(db_path)
        try:
            prices = _get_ohlc_cached(index_ticker, db)
        except Exception:
            prices = []

        if not prices:
            empty_kpi_style = _kpi_value_style(T.TEXT_FAINT)
            no_data_msg = I("market.no_data", lang).format(ticker=index_ticker)
            return ({}, no_data_msg, "",
                    "—", "—", empty_kpi_style,
                    "—", empty_kpi_style, "—")

        # Slice to selected period. period_days=0 means MAX (no slice).
        windowed = _slice_window(prices, period_days)
        if not windowed:
            windowed = prices

        # Chart — branch on style. Both helpers accept the same row shape
        # (the candle one just requires open/high/low/close populated).
        # Title built off the translated index name.
        chart_label = _localised_index_name(index_ticker, lang)
        if chart_style == "candle":
            fig = price_candlestick_chart(windowed, label=chart_label)
        else:
            fig = price_chart(windowed, label=chart_label)

        # KPIs
        closes = [p["adj_close"] for p in windowed if p.get("adj_close")]
        last_close = closes[-1] if closes else None
        first_close = closes[0] if closes else None
        period_pct = ((last_close / first_close - 1) * 100
                        if first_close else None)
        ytd_pct = _ytd_return(prices)
        max_dd = _max_drawdown_pct(closes)

        period_lbl = I("market.period_label", lang).format(
            start=windowed[0]["date"],
            end=windowed[-1]["date"],
            n=len(windowed),
        )

        return (
            fig,
            chart_label,
            period_lbl,
            _fmt_index_level(last_close),
            _fmt_signed_pct(period_pct),
            _kpi_value_style(_color_for_pct(period_pct)),
            _fmt_signed_pct(ytd_pct),
            _kpi_value_style(_color_for_pct(ytd_pct)),
            _fmt_signed_pct(max_dd) if max_dd is not None else "—",
        )

    # =========================================================
    # 4. Constituent table (lang-aware)
    # =========================================================
    @app.callback(
        Output("market-constituent-table", "data"),
        Output("market-constituent-title", "children"),
        Output("market-constituent-meta", "children"),
        Output("market-constituent-wrapper", "children", allow_duplicate=True),
        Input("market-index-select", "value"),
        Input("user-language", "data"),
        prevent_initial_call="initial_duplicate",
    )
    def update_constituent_table(index_ticker, lang):
        if not index_ticker:
            raise PreventUpdate
        lang = lang or "en"

        meta = index_meta(index_ticker) or {}
        market = (meta.get("market") or "HK").upper()
        constituent_tickers = set(constituents_for(index_ticker))
        idx_name = _localised_index_name(index_ticker, lang)

        title = I("market.constituents_of", lang).format(name=idx_name)

        # Index without a maintained constituent list (^IXIC, ^RUT) →
        # placeholder rather than a confusing empty table.
        if not constituent_tickers:
            placeholder = I("market.no_constituents", lang)
            return ([], title, placeholder,
                    html.Div(placeholder,
                              className="text-muted fst-italic text-center py-4"))

        # Reuse the Screener's already-cached `_query_latest` slice for the
        # active market; filter to the constituent set in Python. Per
        # screener_callbacks.py: rows are pre-enriched with last_price /
        # market_cap / trailing_pe / price_to_book derived on-read from
        # per-share inputs.
        from dashboard.screener_callbacks import _query_latest
        all_rows = _query_latest(db_path, market=market)
        rows_subset = [r for r in all_rows
                        if r.get("ticker") in constituent_tickers]

        # Sort by market cap desc so the largest constituents land on top
        # of the default page-1 view.
        rows_subset.sort(
            key=lambda r: (r.get("market_cap") or 0), reverse=True,
        )

        # Bilingual name lookup — mirrors the Screener's pattern. The
        # `securities` row's `name` field is the English canonical; the
        # `securities_meta` table (queried via SecuritiesReferenceRepository)
        # holds Chinese names where available. Pre-fetch for ALL subset
        # tickers (one round-trip, ~30-500 rows) so the per-row formatter
        # is a pure dict lookup.
        from storage.database import Database as _Db
        from storage.repository import SecuritiesReferenceRepository
        ticker_keys = [r.get("ticker") for r in rows_subset if r.get("ticker")]
        names_by_ticker = SecuritiesReferenceRepository(
            _Db(db_path)).get_names(ticker_keys, lang=lang) if ticker_keys else {}

        data = [_row_for_table(r, lang=lang,
                                  names_by_ticker=names_by_ticker)
                  for r in rows_subset]
        n_found = len(data)
        n_total = len(constituent_tickers)
        n_missing = n_total - n_found
        missing_note = (I("market.meta.missing", lang).format(n=n_missing)
                          if n_missing > 0 else "")
        meta_str = I("market.meta.priced", lang).format(
            n_found=n_found, n_total=n_total,
            missing=missing_note,
            updated=meta.get("updated", "unknown"),
        )

        return data, title, meta_str, no_update


# ============================================================================
# helpers
# ============================================================================

def _slice_window(prices: list[dict], period_days: int) -> list[dict]:
    """Trim a prices list (oldest-first) to the last `period_days` calendar
    days. period_days=0 ⇒ no slice (MAX)."""
    if not period_days or period_days <= 0:
        return prices
    try:
        cutoff = date.today() - timedelta(days=int(period_days))
        cutoff_str = cutoff.isoformat()
    except Exception:
        return prices
    return [p for p in prices if str(p.get("date", ""))[:10] >= cutoff_str]


def _ytd_return(prices: list[dict]) -> float | None:
    """Year-to-date return: last close vs first trading day of current year."""
    if not prices:
        return None
    yr = datetime.now().year
    yr_start_str = f"{yr}-01-01"
    yr_prices = [p for p in prices
                  if p.get("adj_close")
                  and str(p.get("date", ""))[:10] >= yr_start_str]
    if not yr_prices:
        return None
    first = yr_prices[0]["adj_close"]
    last = yr_prices[-1]["adj_close"]
    if not first:
        return None
    return (last / first - 1) * 100


def _max_drawdown_pct(closes: list[float]) -> float | None:
    """Worst peak-to-trough decline over the supplied close series, as a
    NEGATIVE percent. Returns None for empty inputs."""
    if not closes:
        return None
    peak = closes[0]
    worst = 0.0
    for c in closes:
        if c is None:
            continue
        if c > peak:
            peak = c
        if peak > 0:
            dd = (c - peak) / peak * 100
            if dd < worst:
                worst = dd
    return worst


def _fmt_index_level(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_signed_pct(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):+.2f}%"
    except (TypeError, ValueError):
        return "—"


def _color_for_pct(v) -> str:
    """Standard finance convention (green positive, red negative) — NOT the
    CN/HK price-up-red convention used elsewhere on this dashboard. KPI
    chips read as universal returns; the chart fill colour still follows
    the CN/HK theme per `price_chart`."""
    if v is None:
        return T.TEXT_FAINT
    try:
        f = float(v)
    except (TypeError, ValueError):
        return T.TEXT_FAINT
    if f > 0:
        return T.SUCCESS
    if f < 0:
        return T.DANGER
    return T.TEXT_MUTED


def _kpi_value_style(color: str) -> dict:
    return {"fontSize": T.FONT_HERO_SM, "fontWeight": "700",
            "color": color, "lineHeight": "1.2", "marginTop": "4px"}


def _row_for_table(r: dict, lang: str = "en",
                      names_by_ticker: dict | None = None) -> dict:
    """Coerce a fundamentals row into the constituent-table column shape.
    Mirrors the Screener's row formatting where possible — Mkt Cap shown
    in billions, percentages multiplied to 100 (yfinance stores ROE etc.
    as fractions e.g. 0.25 = 25%).

    Bilingual fields (added per user request 2026-06):
      * `name` — looks up `names_by_ticker[ticker]` (populated by the
        caller via SecuritiesReferenceRepository.get_names(lang=lang)).
        Falls back to the row's canonical English `name`.
      * `sector` — `yf_sector` is the English raw value from yfinance;
        `get_sector_label(sector_raw, lang)` translates via the
        parent_sectors_zh map in config/sub_sectors.yaml.
    """
    from config.settings import get_sector_label

    def _b(v, scale=1, decimals=2):
        if v is None:
            return None
        try:
            f = float(v) * scale
            if math.isnan(f) or math.isinf(f):
                return None
            return round(f, decimals)
        except (TypeError, ValueError):
            return None

    ticker_key = r.get("ticker") or ""
    localised_name = (names_by_ticker or {}).get(ticker_key)
    display_name = (localised_name or r.get("name") or "")[:50]

    sector_raw = r.get("yf_sector") or r.get("sector") or "—"
    sector_display = (get_sector_label(sector_raw, lang)
                        if sector_raw != "—" else "—")

    return {
        "ticker": ticker_key,
        "name": display_name,
        "sector": sector_display,
        "market_cap_b": _b((r.get("market_cap") or 0) / 1e9, decimals=2)
                          if r.get("market_cap") else None,
        "last_price": _b(r.get("last_price"), decimals=2),
        "trailing_pe": _b(r.get("trailing_pe"), decimals=2),
        "price_to_book": _b(r.get("price_to_book"), decimals=2),
        "dividend_yield": _b(r.get("dividend_yield"), decimals=2),
        "return_on_equity": _b(r.get("return_on_equity"), scale=100,
                                  decimals=1),
        "debt_to_equity": _b(r.get("debt_to_equity"), decimals=2),
        "earnings_growth": _b(r.get("earnings_growth"), scale=100,
                                 decimals=1),
    }
