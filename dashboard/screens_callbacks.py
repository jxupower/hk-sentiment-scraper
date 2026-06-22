import math
import os

from dash import Input, Output

from analysis.screens import BUILTIN_SCREENS, run_screen


def register_screens_callbacks(app, db_path: str):
    sector_risk_path = os.path.join(os.path.dirname(__file__), "..", "config",
                                     "sector_risk.yaml")

    # One callback per screen — each one independent, only fires when its tab is active
    # (though Dash will compute all on initial load; that's fine, screens are <100ms each).
    for screen in BUILTIN_SCREENS:
        _register_one_screen(app, db_path, sector_risk_path, screen)


def _register_one_screen(app, db_path: str, sector_risk_path: str, screen):
    @app.callback(
        Output(f"screen-{screen.id}-table", "data"),
        Output(f"screen-{screen.id}-count", "children"),
        Output(f"screen-{screen.id}-meta", "children"),
        Input("screens-auto-refresh", "n_intervals"),
        Input("user-market", "data"),
        Input("user-language", "data"),
    )
    def update_screen_table(_n, market, lang, _screen=screen):
        market = (market or "HK").upper()
        lang = lang or "en"
        results = run_screen(db_path, _screen, sector_risk_path, market=market)
        # Bilingual name lookup, batched once per render.
        from storage.repository import SecuritiesReferenceRepository
        from storage.database import Database
        names = SecuritiesReferenceRepository(Database(db_path)).get_names(
            [r.ticker for r in results if r.ticker], lang=lang,
        )
        rows = [_format_row(r, names_by_ticker=names) for r in results]
        count_str = f"{len(results)} matching"
        # Meta: how many watchlist, how many flagged
        wl_n = sum(1 for r in results if r.is_watchlist)
        fl_n = sum(1 for r in results if r.flagged)
        meta_parts = []
        if wl_n:
            meta_parts.append(f"{wl_n} watchlist")
        if fl_n:
            meta_parts.append(f"{fl_n} flagged")
        meta = " · ".join(meta_parts) if meta_parts else ""
        return rows, count_str, meta


def _format_row(r, names_by_ticker: dict | None = None) -> dict:
    def rnd(v, n=1):
        if v is None:
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        if math.isnan(f) or math.isinf(f):
            return None
        return round(f, n)

    badge_parts = []
    if r.is_watchlist:
        badge_parts.append("★ WL")
    if r.flagged:
        badge_parts.append("FLAG")
    if not badge_parts:
        badge_parts.append("OK")

    localised = (names_by_ticker or {}).get(r.ticker)
    return {
        "ticker": r.ticker,
        "name": (localised or r.name or "")[:30],
        "sector": (r.sector or "—")[:25],
        "market_cap_b": rnd(r.market_cap / 1e9, 1) if r.market_cap else None,
        "trailing_pe": rnd(r.trailing_pe, 1),
        "price_to_book": rnd(r.price_to_book, 2),
        "dividend_yield": rnd(r.dividend_yield, 2),
        "roe_display": rnd((r.return_on_equity or 0) * 100, 1)
                       if r.return_on_equity is not None else None,
        "debt_to_equity": rnd(r.debt_to_equity, 1),
        "earn_growth_display": rnd((r.earnings_growth or 0) * 100, 1)
                               if r.earnings_growth is not None else None,
        "status_badge": " · ".join(badge_parts),
    }
