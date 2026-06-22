"""Cache-aside façade for ticker time-series data.

This is the only module callers should import for price + fundamentals reads.
It hides the SQLite-vs-Supabase routing AND the on-demand yfinance/akshare
fetch logic. The pattern:

  1. Try to read from cloud (or local SQLite if USE_CLOUD_DB=false)
  2. If empty / stale, fetch from yfinance/akshare
  3. Upsert into the same repo
  4. Return the freshly-stocked data

Freshness rules (cheap heuristics — bias toward not re-fetching to keep API
calls down):
  - Prices: stale if MAX(date) < (today - 7 days)
  - Annual fundamentals: stale if MAX(snapshot_date) older than 365 days

Callers can pass force_refresh=True to bypass the cache (e.g. cron jobs,
"Refresh" buttons).
"""
import logging
from datetime import date, datetime, timedelta
from typing import Optional

from config import settings
from storage.database import Database
from storage.factory import get_prices_repo, get_fundamentals_repo
from utils.logger import get_logger

log = get_logger(__name__)

PRICE_STALE_DAYS = 7
FUNDAMENTALS_STALE_DAYS = 365


# ============== Prices ==============

def get_or_fetch_prices(ticker: str, db: Database, *,
                         period: str = "10y",
                         force_refresh: bool = False) -> list[dict]:
    """Return list of {date, adj_close} for the ticker. Fetches from yfinance
    on miss/stale. `period` is the yfinance period string used only on miss
    (the cache stores everything we've ever pulled, regardless of period).

    Tickers beginning with "^" (e.g. "^HSI", "^HSCEI", "^HSTECH") are
    routed to get_or_fetch_index_prices() so the same caller-side API
    works for both equities and indices.

    Tickers beginning with "@" (e.g. "@CORE", "@CORE$OPT") are routed to
    get_or_fetch_portfolio_prices() — synthetic series built from user-saved
    portfolio constituents.

    Tickers beginning with "&" (e.g. "&BANKS", "&SEMICONDUCTORS_AND_EQUIPMENT")
    are routed to get_or_fetch_subsector_prices() — cap-weighted, chain-
    linked Laspeyres composite indices built from current sub-sector
    constituents.

    Returns [] if both cache and fetch fail."""
    if ticker.startswith("^"):
        return get_or_fetch_index_prices(ticker, db, force_refresh=force_refresh)
    if ticker.startswith("@"):
        return get_or_fetch_portfolio_prices(ticker, db, force_refresh=force_refresh)
    if ticker.startswith("&"):
        return get_or_fetch_subsector_prices(ticker, db, force_refresh=force_refresh)

    repo = get_prices_repo(db)

    if not force_refresh:
        latest_str = repo.latest_date(ticker) if hasattr(repo, "latest_date") \
                     else _sqlite_latest_date(db, ticker)
        if latest_str and not _is_price_stale(latest_str):
            return _get_full_series(repo, ticker)

    # Cache miss or stale → fetch from yfinance
    log.info("Cache-aside fetch: yfinance prices for %s (period=%s)", ticker, period)
    rows = _fetch_yfinance_prices(ticker, period=period)
    if rows:
        repo.upsert_rows(ticker, rows)
        log.info("  upserted %d rows for %s", len(rows), ticker)
    return _get_full_series(repo, ticker)


def get_or_fetch_index_prices(index_ticker: str, db: Database, *,
                                force_refresh: bool = False) -> list[dict]:
    """Cache-aside read for index prices.

    HK indices (`^HSI`, `^HSCEI`, `^HSTECH`) hit akshare (Eastmoney via
    sina) because yfinance occasionally returns sparse HK index history.
    US indices (`^GSPC`, `^IXIC`, `^DJI`, `^NDX`, `^RUT`, `^VIX`) hit
    yfinance directly — they're natively supported. The "^"-prefix is
    preserved in storage so equity queries are never polluted by index
    rows.
    """
    from utils.market import market_of_ticker
    repo = get_prices_repo(db)

    if not force_refresh:
        latest_str = repo.latest_date(index_ticker) if hasattr(repo, "latest_date") \
                      else _sqlite_latest_date(db, index_ticker)
        if latest_str and not _is_price_stale(latest_str):
            return _get_full_series(repo, index_ticker)

    market = market_of_ticker(index_ticker)
    rows: list[dict] = []
    if market == "HK":
        bare = index_ticker.lstrip("^")
        log.info("Cache-aside fetch: akshare index %s", bare)
        try:
            from scrapers.akshare_price_scraper import fetch_one_index
            rows = fetch_one_index(bare)
        except Exception as e:
            log.warning("akshare index fetch failed for %s: %s", bare, e)
    else:
        log.info("Cache-aside fetch: yfinance index %s", index_ticker)
        rows = _fetch_yfinance_prices(index_ticker, period="10y")

    if rows:
        repo.upsert_rows(index_ticker, rows)
        log.info("  upserted %d rows for %s", len(rows), index_ticker)
    return _get_full_series(repo, index_ticker)


def get_or_fetch_portfolio_prices(portfolio_ticker: str, db: Database, *,
                                    force_refresh: bool = False) -> list[dict]:
    """Cache-aside read for user-saved portfolio synthetic tickers.

    `@NAME` is the status-quo (constant-share) index; `@NAME$OPT` is the
    cached max-Sharpe optimal-weight index. On miss / staleness we look
    up the portfolio definition in Supabase and recompute the series
    from constituent prices via `analysis/portfolio_synth.py`.

    Returns [] (rather than raising) when the cloud DB isn't configured
    or the portfolio name isn't found — callers degrade gracefully."""
    repo = get_prices_repo(db)

    if not force_refresh:
        latest_str = repo.latest_date(portfolio_ticker) if hasattr(repo, "latest_date") \
                      else _sqlite_latest_date(db, portfolio_ticker)
        from analysis.portfolio_synth import is_synthetic_stale
        if latest_str and not is_synthetic_stale(latest_str):
            return _get_full_series(repo, portfolio_ticker)

    from analysis.portfolio_synth import parse_portfolio_ticker, rebuild_and_upsert
    parsed = parse_portfolio_ticker(portfolio_ticker)
    if not parsed:
        log.warning("not a portfolio ticker: %s", portfolio_ticker)
        return []
    name, _is_optimal = parsed

    # Look up the portfolio definition from Supabase
    try:
        from storage.cloud_db import available
        if not available():
            log.warning("cloud DB not configured; cannot rebuild %s", portfolio_ticker)
            return _get_full_series(repo, portfolio_ticker)
        from storage.cloud_repository import CloudPortfoliosRepository
        portfolio = CloudPortfoliosRepository().get_portfolio(name)
    except Exception as e:
        log.warning("portfolio lookup failed for %s: %s", name, e)
        return _get_full_series(repo, portfolio_ticker)

    if not portfolio:
        log.warning("no saved portfolio named %s", name)
        return []

    log.info("Cache-aside rebuild for portfolio %s", portfolio_ticker)
    try:
        summary = rebuild_and_upsert(name, portfolio, db)
        if summary.get("errors"):
            log.warning("rebuild for %s reported errors: %s", name, summary["errors"])
    except Exception as e:
        log.warning("rebuild_and_upsert failed for %s: %s", name, e)
    return _get_full_series(repo, portfolio_ticker)


def get_or_fetch_subsector_prices(subsector_ticker: str, db: Database, *,
                                     force_refresh: bool = False) -> list[dict]:
    """Cache-aside read for sub-sector composite tickers.

    `&NAME` is the cap-weighted chain-linked Laspeyres index for the
    matching `securities.sub_sector` value. On miss / staleness the
    constituents are reloaded and the index is recomputed via
    `analysis/subsector_synth.rebuild_and_upsert_subsector`.

    Returns [] when the sub-sector slug doesn't resolve to a real
    sub_sector value in `securities` — callers degrade gracefully."""
    repo = get_prices_repo(db)

    if not force_refresh:
        latest_str = repo.latest_date(subsector_ticker) if hasattr(repo, "latest_date") \
                      else _sqlite_latest_date(db, subsector_ticker)
        from analysis.subsector_synth import is_subsector_stale
        if latest_str and not is_subsector_stale(latest_str):
            return _get_full_series(repo, subsector_ticker)

    from analysis.subsector_synth import (
        label_for_ticker, rebuild_and_upsert_subsector,
        parse_subsector_market,
    )
    label = label_for_ticker(subsector_ticker, db)
    if not label:
        log.warning("no sub_sector resolves to ticker %s", subsector_ticker)
        return _get_full_series(repo, subsector_ticker)

    # Market is embedded in namespaced tickers (`&US:BANKS` → 'US'); the
    # legacy un-namespaced `&BANKS` defaults to 'HK' for backwards compat.
    composite_market = parse_subsector_market(subsector_ticker)
    log.info("Cache-aside rebuild for sub-sector composite %s (market=%s)",
              subsector_ticker, composite_market)
    try:
        summary = rebuild_and_upsert_subsector(label, db,
                                                  market=composite_market)
        if summary.get("errors"):
            log.warning("rebuild for %s reported errors: %s",
                         subsector_ticker, summary["errors"])
    except Exception as e:
        log.warning("rebuild_and_upsert_subsector failed for %s: %s",
                     label, e)
    return _get_full_series(repo, subsector_ticker)


def _get_full_series(repo, ticker: str) -> list[dict]:
    """Read all cached prices, handling both repo types (cloud has
    get_full_series, sqlite uses get_price_series with a wide date range)."""
    if hasattr(repo, "get_full_series"):
        return repo.get_full_series(ticker)
    # SQLite fallback — read everything by passing a huge window
    return repo.get_price_series(ticker, "1900-01-01", "2999-12-31")


def _sqlite_latest_date(db: Database, ticker: str) -> Optional[str]:
    """SQLite repo doesn't expose latest_date; query directly."""
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT MAX(date) FROM historical_prices WHERE ticker = ?",
            (ticker,)
        ).fetchone()
        return row[0] if row and row[0] else None


def _is_price_stale(latest_date_str: str) -> bool:
    try:
        latest = datetime.strptime(latest_date_str[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return True
    return (date.today() - latest).days > PRICE_STALE_DAYS


def _fetch_yfinance_prices(ticker: str, period: str) -> list[dict]:
    """Pull prices via the existing fetch_one from historical_price_scraper."""
    try:
        from scrapers.historical_price_scraper import fetch_one
        return fetch_one(ticker, period=period)
    except Exception as e:
        log.warning("yfinance fetch failed for %s: %s", ticker, e)
        return []


# ============== Fundamentals — annual history ==============

def get_or_fetch_fundamentals_history(ticker: str, db: Database, *,
                                       force_refresh: bool = False) -> list[dict]:
    """Return all annual snapshots for a ticker, oldest first.
    Fetches from akshare on miss/stale. Returns [] on total failure."""
    repo = get_fundamentals_repo(db)

    if not force_refresh:
        history = _get_history_annual(repo, ticker, db)
        if history and not _is_fundamentals_stale(history[-1].get("snapshot_date")):
            return history

    log.info("Cache-aside fetch: akshare fundamentals for %s", ticker)
    fetched = _fetch_akshare_history(ticker)
    if fetched:
        for snapshot_date, snapshot in fetched:
            if hasattr(repo, "upsert_snapshot"):
                # Cloud repo signature accepts a `source` kwarg; SQLite doesn't.
                try:
                    repo.upsert_snapshot(ticker, snapshot_date, snapshot,
                                          source="akshare_annual")
                except TypeError:
                    repo.upsert_snapshot(ticker, snapshot_date, snapshot)
        log.info("  upserted %d annual snapshots for %s", len(fetched), ticker)
    return _get_history_annual(repo, ticker, db)


def _get_history_annual(repo, ticker: str, db: Database) -> list[dict]:
    """Get history filtered to annual snapshots (source='akshare_annual'),
    handling repo type variation."""
    if hasattr(repo, "get_history"):
        return repo.get_history(ticker, sources=["akshare_annual"])
    # SQLite repo doesn't filter by source — return everything; akshare rows
    # have unique snapshot_dates from yfinance daily anyway, so dedup happens
    # by date in callers.
    with db.get_connection() as conn:
        rows = conn.execute("""
            SELECT * FROM fundamentals_snapshots
            WHERE ticker = ?
            ORDER BY snapshot_date ASC
        """, (ticker,)).fetchall()
        return [dict(r) for r in rows]


def _is_fundamentals_stale(latest_date) -> bool:
    if latest_date is None:
        return True
    if isinstance(latest_date, str):
        try:
            latest = datetime.strptime(latest_date[:10], "%Y-%m-%d").date()
        except ValueError:
            return True
    elif isinstance(latest_date, date):
        latest = latest_date
    else:
        return True
    return (date.today() - latest).days > FUNDAMENTALS_STALE_DAYS


def _fetch_akshare_history(ticker: str) -> list:
    # akshare is HK-only — skip silently for US tickers so the cache-aside
    # caller falls through to yfinance .info / .financials.
    from utils.market import market_of_ticker
    if market_of_ticker(ticker) != "HK":
        return []
    try:
        from scrapers.akshare_historical_scraper import fetch_history
        return fetch_history(ticker)
    except Exception as e:
        log.warning("akshare fetch failed for %s: %s", ticker, e)
        return []


# ============== Latest single-snapshot fundamentals (current ratios) ==============

def get_or_fetch_latest_fundamentals(ticker: str, db: Database, *,
                                       force_refresh: bool = False) -> Optional[dict]:
    """Get the most-recent fundamentals snapshot for a ticker. If nothing in
    cache or it's older than a year, fetch fresh ratios from yfinance.info."""
    repo = get_fundamentals_repo(db)

    if not force_refresh:
        latest = repo.get_latest(ticker)
        if latest and not _is_fundamentals_stale(latest.get("snapshot_date")):
            return latest

    log.info("Cache-aside fetch: yfinance .info for %s", ticker)
    snap = _fetch_yfinance_info(ticker)
    if snap:
        today = date.today().isoformat()
        try:
            repo.upsert_snapshot(ticker, today, snap, source="yfinance_daily")
        except TypeError:
            repo.upsert_snapshot(ticker, today, snap)
    return repo.get_latest(ticker)


# ============== Financial statements (income/balance/cashflow) ==============

# Stale rule: refetch when BOTH conditions hold —
#   (a) cache was filled more than 90 days ago AND
#   (b) newest period_end_date in cache is older than 180 days
# 90d alone over-fetches mid-year; 180d alone misses fresh filings during
# reporting season. The AND combines them so we refetch right after a likely
# new filing has landed.
FS_FETCHED_STALE_DAYS = 90
FS_PERIOD_STALE_DAYS = 180


def get_or_fetch_financial_statements(ticker: str, db: Database, *,
                                       force_refresh: bool = False
                                       ) -> dict[str, list[dict]]:
    """Return income/balance/cashflow statements for a ticker.
    Cache-aside: hits Supabase first, falls back to yfinance + akshare on
    miss / stale. Returns {'income': [], 'balance': [], 'cashflow': []}
    on total failure."""
    from storage.cloud_db import available as cloud_available

    if not cloud_available():
        # Dev path (USE_CLOUD_DB=false): no cache, fetch every call.
        log.info("Cloud DB off; fetching financial statements live for %s", ticker)
        return _fetch_statements_from_sources(ticker)

    from storage.cloud_repository import CloudFinancialStatementsRepository
    repo = CloudFinancialStatementsRepository()

    if not force_refresh:
        cached = repo.get_for_ticker(ticker)
        if _statements_cache_fresh(cached, repo, ticker):
            return cached

    log.info("Cache-aside fetch: financial statements for %s", ticker)
    fetched = _fetch_statements_from_sources(ticker)
    if any(fetched.get(s) for s in ("income", "balance", "cashflow")):
        repo.upsert_statements(ticker, fetched)
    return repo.get_for_ticker(ticker)


def _fetch_statements_from_sources(ticker: str) -> dict[str, list[dict]]:
    try:
        from scrapers.financial_statements_scraper import fetch_statements
        return fetch_statements(ticker)
    except Exception as e:
        log.warning("financial statements scraper failed for %s: %s", ticker, e)
        return {"income": [], "balance": [], "cashflow": []}


def _statements_cache_fresh(cached: dict, repo, ticker: str) -> bool:
    """Returns False (=refetch) when both staleness gates trip OR cache is empty."""
    has_any = any(cached.get(s) for s in ("income", "balance", "cashflow"))
    if not has_any:
        return False
    from datetime import date, datetime
    fetched_at_str = repo.latest_fetched_at(ticker)
    period_end_str = repo.latest_period_end(ticker)
    if not fetched_at_str or not period_end_str:
        return False
    try:
        fetched_at = datetime.fromisoformat(fetched_at_str.replace("Z", "+00:00")).date()
        period_end = datetime.strptime(period_end_str[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False
    today = date.today()
    fetched_old = (today - fetched_at).days > FS_FETCHED_STALE_DAYS
    period_old = (today - period_end).days > FS_PERIOD_STALE_DAYS
    return not (fetched_old and period_old)


# ============== Cross-table helpers used by analysis modules ==============

def push_securities_reference(db: Database) -> dict:
    """Push the reconciler-resolved sector taxonomy + bilingual names from
    LOCAL SQLite (`securities.{effective_sector, sub_sector}` + `securities_meta`)
    UP to the Supabase `securities_reference` table. Call after every
    `universe-us seed` / `universe refresh` so the cloud source-of-truth
    stays in lockstep with what the reconciler just computed.
    """
    import time
    from storage.repository import SecuritiesRepository
    from storage.factory import get_securities_reference_repo

    t0 = time.perf_counter()
    securities_repo = SecuritiesRepository(db)
    cloud_ref = get_securities_reference_repo(db)

    rows = securities_repo.get_all_active()
    # One pass over securities_meta — gives us both EN + ZH names per ticker.
    with db.get_connection() as conn:
        name_rows = conn.execute(
            "SELECT ticker, english_name, chinese_name FROM securities_meta"
        ).fetchall()
    en_lookup = {r["ticker"]: r["english_name"] for r in name_rows}
    zh_lookup = {r["ticker"]: r["chinese_name"] for r in name_rows}

    payload = []
    for r in rows:
        ticker = r["ticker"]
        payload.append({
            "ticker":        ticker,
            "english_name":  en_lookup.get(ticker) or r.get("name"),
            "chinese_name":  zh_lookup.get(ticker),
            "parent_sector": r.get("effective_sector"),
            "sub_sector":    r.get("sub_sector"),
        })
    n = cloud_ref.upsert_many(payload)
    t1 = time.perf_counter()
    log.info("push_securities_reference: %d rows in %.1fs", n, t1 - t0)
    return {"pushed": n, "elapsed_s": round(t1 - t0, 1)}


def refresh_securities_reference_cache(db: Database) -> dict:
    """Pull the Supabase `securities_reference` table down into the LOCAL
    SQLite mirror so the dashboard's read path is sub-millisecond. Called
    on dashboard startup + after any explicit cloud refresh."""
    import time
    from storage.repository import SecuritiesReferenceRepository
    from storage.factory import get_securities_reference_repo

    t0 = time.perf_counter()
    cloud_ref = get_securities_reference_repo(db)
    local_ref = SecuritiesReferenceRepository(db)
    if isinstance(cloud_ref, SecuritiesReferenceRepository):
        # USE_CLOUD_DB=false → cloud == local; nothing to copy
        return {"fetched": 0, "written": 0, "elapsed_s": 0.0, "skipped": True}
    rows = cloud_ref.get_all()
    n = local_ref.upsert_many(rows)
    t1 = time.perf_counter()
    log.info("refresh_securities_reference_cache: %d rows in %.1fs",
              n, t1 - t0)
    return {"fetched": len(rows), "written": n,
             "elapsed_s": round(t1 - t0, 1)}


def refresh_latest_prices_cache(db: Database, *,
                                   tickers: Optional[list[str]] = None) -> dict:
    """Refresh the local `latest_prices` SQLite cache from Supabase
    `historical_prices`. One Supabase round-trip + one local SQLite bulk
    upsert — invoked nightly by the EOD price cron and on-demand via
    `python main.py historical refresh-latest-prices`.

    `tickers=None` (default) refreshes every active ticker in `securities`
    that has any price history. Pass a specific list to refresh a subset
    (e.g. for a just-seeded batch).

    Returns summary dict for the CLI to print.
    """
    import time
    from storage.repository import LatestPricesRepository, SecuritiesRepository
    from datetime import date as date_cls

    securities_repo = SecuritiesRepository(db)
    latest_repo = LatestPricesRepository(db)
    prices_repo = get_prices_repo(db)

    if tickers is None:
        rows = securities_repo.get_all_active()
        tickers = [r["ticker"] for r in rows]
    n_in = len(tickers)
    if not n_in:
        return {"requested": 0, "fetched": 0, "written": 0, "elapsed_s": 0.0}

    t0 = time.perf_counter()
    if hasattr(prices_repo, "bulk_prices_on_or_before_with_date"):
        # Future-proof: if the cloud repo grows a date-aware bulk fetch
        # that also returns the asof_date, prefer it.
        price_map = prices_repo.bulk_prices_on_or_before_with_date(
            tickers, date_cls.today().isoformat())
        upsert_rows = [{"ticker": t, "adj_close": p, "asof_date": d}
                        for t, (p, d) in price_map.items()]
    elif hasattr(prices_repo, "bulk_prices_on_or_before"):
        price_map = prices_repo.bulk_prices_on_or_before(
            tickers, date_cls.today().isoformat())
        # No per-ticker asof_date available from the simple helper —
        # use today as a best-effort marker (we know the value is the
        # latest as-of-today snapshot).
        today_iso = date_cls.today().isoformat()
        upsert_rows = [{"ticker": t, "adj_close": p, "asof_date": today_iso}
                        for t, p in price_map.items() if p is not None]
    else:
        # SQLite fallback — slow but correct
        log.warning("prices_repo has no bulk helper; doing sequential lookups")
        upsert_rows = []
        for t in tickers:
            p = prices_repo.latest_price(t)
            if p is not None:
                upsert_rows.append({"ticker": t, "adj_close": p,
                                       "asof_date": date_cls.today().isoformat()})

    t1 = time.perf_counter()
    n_fetched = len(upsert_rows)
    n_written = latest_repo.bulk_upsert(upsert_rows)
    t2 = time.perf_counter()

    log.info("refresh_latest_prices_cache: requested=%d, fetched=%d, "
              "written=%d, fetch=%.1fs, write=%.2fs",
              n_in, n_fetched, n_written, t1 - t0, t2 - t1)
    return {
        "requested": n_in,
        "fetched": n_fetched,
        "written": n_written,
        "elapsed_s": round(t2 - t0, 1),
    }


def get_universe_fundamentals(db: Database, *,
                                as_of_date: Optional[str] = None,
                                market: Optional[str] = None,
                                ) -> list[dict]:
    """Return latest fundamentals snapshot per active ticker, joined with
    securities (name, is_watchlist, yf_sector, watchlist_sector, yf_industry,
    listing_category, market) for downstream factor/screen/peer use.

    `as_of_date` (ISO date string) clips to the latest snapshot *at or before*
    that date — used by the backtest engine. Default None = absolute latest.
    `market` (optional) restricts to one market — required for any per-market
    analysis (research cache, peer scorecard, factor engine) so HK and US
    don't pollute each other's percentile ranks.

    Replaces the raw `sqlite3.connect(...).execute("SELECT f.*, s.name ...
    INNER JOIN securities ...")` pattern duplicated across 5 modules.

    Routing:
      - If USE_CLOUD_DB: fetches fundamentals from Postgres (one round-trip),
        fetches securities from local SQLite (one query), joins in Python.
      - Else: single JOIN query in local SQLite.
    """
    repo = get_fundamentals_repo(db)
    is_cloud = type(repo).__name__.startswith("Cloud")
    market_clause = "" if market is None else " AND s.market = ?"
    market_params = () if market is None else (market.upper(),)

    if not is_cloud:
        # SQLite path — preserve the original single-query JOIN for speed.
        # sub_sector + effective_sector come from securities (populated by
        # universe/reconciler.py from config/sub_sectors.yaml) and feed
        # factor_scores / peer_comparison percentile peer-grouping.
        with db.get_connection() as conn:
            if as_of_date is None:
                rows = conn.execute(f"""
                    SELECT f.*, s.name, s.is_watchlist, s.yf_sector,
                           s.watchlist_sector, s.yf_industry, s.listing_category,
                           s.sub_sector, s.effective_sector, s.market
                    FROM fundamentals_snapshots f
                    INNER JOIN (
                        SELECT ticker, MAX(snapshot_date) AS max_date
                        FROM fundamentals_snapshots GROUP BY ticker
                    ) latest ON f.ticker = latest.ticker AND f.snapshot_date = latest.max_date
                    INNER JOIN securities s ON f.ticker = s.ticker
                    WHERE s.is_active = 1{market_clause}
                """, market_params).fetchall()
            else:
                rows = conn.execute(f"""
                    SELECT f.*, s.name, s.is_watchlist, s.yf_sector,
                           s.watchlist_sector, s.yf_industry, s.listing_category,
                           s.sub_sector, s.effective_sector, s.market
                    FROM fundamentals_snapshots f
                    INNER JOIN (
                        SELECT ticker, MAX(snapshot_date) AS max_date
                        FROM fundamentals_snapshots
                        WHERE snapshot_date <= ?
                        GROUP BY ticker
                    ) latest ON f.ticker = latest.ticker AND f.snapshot_date = latest.max_date
                    INNER JOIN securities s ON f.ticker = s.ticker
                    WHERE s.is_active = 1{market_clause}
                """, (as_of_date, *market_params)).fetchall()
            return [dict(r) for r in rows]

    # Cloud path — fundamentals from Postgres, securities from local SQLite.
    # The market filter is applied on the securities side (the smaller join)
    # — fund_rows comes through unfiltered, then the in-Python join naturally
    # drops US tickers when market='HK' (and vice versa) because they won't
    # be in the sec_by_ticker dict.
    from storage.cloud_db import cursor as cloud_cursor
    if as_of_date is None:
        sql = """
            SELECT DISTINCT ON (ticker) *
            FROM fundamentals_snapshots
            ORDER BY ticker, snapshot_date DESC
        """
        params = ()
    else:
        sql = """
            SELECT DISTINCT ON (ticker) *
            FROM fundamentals_snapshots
            WHERE snapshot_date <= %s
            ORDER BY ticker, snapshot_date DESC
        """
        params = (as_of_date,)
    with cloud_cursor(dict_rows=True) as cur:
        cur.execute(sql, params)
        fund_rows = [dict(r) for r in cur.fetchall()]

    sec_market_clause = "" if market is None else " AND market = ?"
    with db.get_connection() as conn:
        sec_rows = conn.execute(f"""
            SELECT ticker, name, is_watchlist, yf_sector, watchlist_sector,
                   yf_industry, listing_category, sub_sector, effective_sector,
                   market
            FROM securities WHERE is_active = 1{sec_market_clause}
        """, market_params).fetchall()
        sec_by_ticker = {r["ticker"]: dict(r) for r in sec_rows}

    out = []
    for f in fund_rows:
        sec = sec_by_ticker.get(f["ticker"])
        if not sec:
            continue  # not in local active universe (or wrong market)
        merged = {**f, **sec}  # securities columns win on name/is_watchlist
        out.append(_coerce_decimals(merged))
    return out


def get_ticker_history(db: Database, ticker: str) -> list[dict]:
    """All snapshots for a ticker, oldest first. Used by forensic + research
    orchestrator. Routes via the factory."""
    repo = get_fundamentals_repo(db)
    if hasattr(repo, "get_history"):
        rows = repo.get_history(ticker)
    else:
        # SQLite repo
        with db.get_connection() as conn:
            rows = [dict(r) for r in conn.execute("""
                SELECT * FROM fundamentals_snapshots
                WHERE ticker = ?
                ORDER BY snapshot_date ASC
            """, (ticker,)).fetchall()]
    return [_coerce_decimals(r) for r in rows]


def get_price_on_or_before(db: Database, ticker: str,
                             target_date: str) -> Optional[float]:
    """As-of price lookup. Routes via the factory."""
    repo = get_prices_repo(db)
    if hasattr(repo, "get_price_on_or_before"):
        v = repo.get_price_on_or_before(ticker, target_date)
        return float(v) if v is not None else None
    return None


def bulk_get_prices(db: Database, tickers: list[str],
                     start_date: str, end_date: str) -> dict[str, list[dict]]:
    """Fetch prices for many tickers in ONE round-trip per backend. Used by
    backtest to amortize Postgres latency over thousands of as-of lookups —
    each ticker would otherwise cost ~80ms via the SG pooler."""
    if not tickers:
        return {}
    repo = get_prices_repo(db)
    is_cloud = type(repo).__name__.startswith("Cloud")

    out: dict[str, list[dict]] = {t: [] for t in tickers}
    if is_cloud:
        from storage.cloud_db import cursor as cloud_cursor
        with cloud_cursor(dict_rows=True) as cur:
            cur.execute("""
                SELECT ticker, date, adj_close
                FROM historical_prices
                WHERE ticker = ANY(%s) AND date >= %s AND date <= %s
                ORDER BY ticker, date ASC
            """, (list(tickers), start_date, end_date))
            for row in cur.fetchall():
                out.setdefault(row["ticker"], []).append({
                    "date": str(row["date"]),
                    "adj_close": float(row["adj_close"]) if row["adj_close"] is not None else None,
                })
    else:
        with db.get_connection() as conn:
            placeholders = ",".join("?" * len(tickers))
            rows = conn.execute(f"""
                SELECT ticker, date, adj_close
                FROM historical_prices
                WHERE ticker IN ({placeholders}) AND date >= ? AND date <= ?
                ORDER BY ticker, date ASC
            """, (*tickers, start_date, end_date)).fetchall()
            for r in rows:
                out.setdefault(r["ticker"], []).append({"date": r["date"], "adj_close": r["adj_close"]})
    return out


def _coerce_decimals(row: dict) -> dict:
    """Postgres returns NUMERIC as Decimal + DATE as datetime.date. Coerce to
    float/str so downstream code that does arithmetic or string comparisons
    on these fields doesn't break."""
    from decimal import Decimal
    from datetime import date as _date, datetime as _dt
    out = {}
    for k, v in row.items():
        if isinstance(v, Decimal):
            out[k] = float(v)
        elif isinstance(v, (_date, _dt)):
            out[k] = v.isoformat()[:10] if isinstance(v, _date) and not isinstance(v, _dt) \
                     else v.isoformat()
        else:
            out[k] = v
    return out


# ============== yfinance .info fallback ==============

def _fetch_yfinance_info(ticker: str) -> Optional[dict]:
    """Pull .info via existing fundamentals_scraper if available, else
    construct minimal snapshot. Returns dict in the same shape as
    FundamentalsRepository.upsert_snapshot expects."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
        return {
            "trailing_pe": info.get("trailingPE"),
            "forward_pe": info.get("forwardPE"),
            "price_to_book": info.get("priceToBook"),
            "ev_to_ebitda": info.get("enterpriseToEbitda"),
            "dividend_yield": info.get("dividendYield"),
            "market_cap": info.get("marketCap"),
            "beta": info.get("beta"),
            "return_on_equity": info.get("returnOnEquity"),
            "return_on_assets": info.get("returnOnAssets"),
            "debt_to_equity": info.get("debtToEquity"),
            "earnings_growth": info.get("earningsGrowth"),
            "revenue_growth": info.get("revenueGrowth"),
            "profit_margins": info.get("profitMargins"),
            "operating_margins": info.get("operatingMargins"),
            "current_ratio": info.get("currentRatio"),
            "free_cashflow": info.get("freeCashflow"),
            "last_price": (info.get("currentPrice")
                            or info.get("regularMarketPrice")),
            "currency": info.get("currency"),
        }
    except Exception as e:
        log.warning("yfinance .info failed for %s: %s", ticker, e)
        return None


# ============== Analyst forward-growth cache (Section 5 default tier 2) ==============

ANALYST_GROWTH_STALE_DAYS = 7


def get_or_fetch_analyst_growth(ticker: str, db: Database) -> Optional[float]:
    """Return yfinance's "+5y" analyst growth estimate as a fraction (e.g. 0.12).
    Cache-aside against the local `analyst_growth_cache` SQLite table with a
    7-day TTL — analyst estimates don't move daily, and yfinance often returns
    nothing for HK names so the cache also stops us from hammering the API.

    Returns None when yfinance has no estimate for the ticker (the None is
    cached too, so the next call returns quickly)."""
    # 1. Cache read
    try:
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT growth_5y, fetched_at FROM analyst_growth_cache WHERE ticker = ?",
                (ticker,),
            ).fetchone()
    except Exception as e:
        log.warning("analyst_growth_cache read failed for %s: %s", ticker, e)
        row = None
    if row is not None:
        # sqlite3 row tuple OR Row object — both indexable
        growth_5y = row[0] if not hasattr(row, "keys") else row["growth_5y"]
        fetched_at = row[1] if not hasattr(row, "keys") else row["fetched_at"]
        if not _analyst_stale(fetched_at):
            return float(growth_5y) if growth_5y is not None else None

    # 2. Live fetch (yfinance Ticker.growth_estimates)
    growth_5y = _fetch_yfinance_growth_5y(ticker)

    # 3. Cache write (including the miss)
    try:
        with db.get_connection() as conn:
            conn.execute("""
                INSERT INTO analyst_growth_cache (ticker, growth_5y, fetched_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(ticker) DO UPDATE SET
                    growth_5y = excluded.growth_5y,
                    fetched_at = CURRENT_TIMESTAMP
            """, (ticker, growth_5y))
            conn.commit()
    except Exception as e:
        log.warning("analyst_growth_cache write failed for %s: %s", ticker, e)
    return growth_5y


def _analyst_stale(fetched_at_str) -> bool:
    if not fetched_at_str:
        return True
    try:
        ts = (fetched_at_str if isinstance(fetched_at_str, datetime)
                else datetime.fromisoformat(str(fetched_at_str).replace(" ", "T")))
        return (datetime.utcnow() - ts).days > ANALYST_GROWTH_STALE_DAYS
    except (TypeError, ValueError):
        return True


def _fetch_yfinance_growth_5y(ticker: str) -> Optional[float]:
    """Probe Ticker.growth_estimates for the '+5y' analyst consensus column.
    yfinance schema has shifted over versions — some installs expose a
    DataFrame indexed by period (e.g. '+5y'), some return None entirely for
    HK names. We try a couple of column-name conventions and silently return
    None on any mismatch."""
    try:
        import yfinance as yf
        ge = yf.Ticker(ticker).growth_estimates
    except Exception as e:
        log.debug("yfinance growth_estimates failed for %s: %s", ticker, e)
        return None
    if ge is None:
        return None
    try:
        # DataFrame: rows = ['0q','+1q','0y','+1y','+5y','-5y']; columns vary
        # by yfinance version. We try the most common cells in order.
        if hasattr(ge, "loc"):
            for row_key in ("+5y", "5y", "+5Y"):
                if row_key in ge.index:
                    row = ge.loc[row_key]
                    for col in ("stockTrend", "stock_trend",
                                  "growthEstimate", "growth_estimate"):
                        if col in row.index:
                            v = row[col]
                            return _coerce_growth(v)
                    # Fallback: first numeric cell in the row
                    for v in row.values:
                        out = _coerce_growth(v)
                        if out is not None:
                            return out
        elif isinstance(ge, dict):
            for k in ("+5y", "5y"):
                v = ge.get(k)
                out = _coerce_growth(v)
                if out is not None:
                    return out
    except Exception as e:
        log.debug("growth_estimates parse failed for %s: %s", ticker, e)
    return None


def _coerce_growth(v) -> Optional[float]:
    """yfinance reports growth as either a fraction (0.12) or a percent (12.0).
    Detect & normalise to a fraction; reject NaN / non-numeric."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    import math as _math
    if _math.isnan(f) or _math.isinf(f):
        return None
    # If the value looks like a percent (>1.5 in magnitude), divide by 100.
    if abs(f) > 1.5:
        f = f / 100.0
    return f
