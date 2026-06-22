"""Postgres-backed mirrors of HistoricalPricesRepository + FundamentalsRepository.

Same method signatures as the SQLite versions in `storage/repository.py` so
callers can be swapped via `storage/factory.py` without logic changes.

Key differences vs SQLite repo:
- Constructor takes no `db` argument (uses module-level connection pool from
  `storage/cloud_db.py`).
- `FundamentalsRepository.get_latest_for_universe()` does NOT join with the
  `securities` table (which lives in local SQLite). It returns fundamentals-only
  rows; callers that need the join must do it client-side.
- `update_security_yf_metadata()` is NOT mirrored here — securities stays local.
"""
from typing import Optional

from psycopg2.extras import execute_values

from storage.cloud_db import cursor


# Columns settable via upsert_snapshot; mirrors the SQLite version's list.
_FUNDAMENTALS_COLS = [
    "trailing_pe", "forward_pe", "price_to_book", "ev_to_ebitda",
    "dividend_yield", "market_cap", "beta", "return_on_equity",
    "debt_to_equity", "last_price", "currency", "data_completeness",
    "earnings_growth", "revenue_growth", "profit_margins",
    "operating_margins", "return_on_assets", "current_ratio",
    "free_cashflow",
    "eps_ttm", "bps", "shares_outstanding",
]


class CloudFundamentalsRepository:
    def upsert_snapshot(self, ticker: str, snapshot_date: str,
                         fields: dict, source: str = "yfinance_daily"):
        """Insert or replace a snapshot for (ticker, snapshot_date, source).
        `source` defaults to 'yfinance_daily' to match the cron caller; the
        bulk akshare loader should pass source='akshare_annual'."""
        from utils.market import market_of_ticker
        market = market_of_ticker(ticker)
        values = [fields.get(c) for c in _FUNDAMENTALS_COLS]
        # +4 placeholders for ticker, market, snapshot_date, source
        placeholders = ", ".join(["%s"] * (len(_FUNDAMENTALS_COLS) + 4))
        update_clause = ", ".join(
            f"{c} = EXCLUDED.{c}" for c in _FUNDAMENTALS_COLS
        )
        sql = f"""
            INSERT INTO fundamentals_snapshots
                (ticker, market, snapshot_date, source, {", ".join(_FUNDAMENTALS_COLS)})
            VALUES ({placeholders})
            ON CONFLICT (ticker, snapshot_date, source) DO UPDATE SET
                market = EXCLUDED.market,
                {update_clause},
                fetched_at = NOW()
        """
        with cursor() as cur:
            cur.execute(sql, (ticker, market, snapshot_date, source, *values))

    def has_snapshot_for_date(self, ticker: str, snapshot_date: str,
                               source: Optional[str] = None) -> bool:
        sql = "SELECT 1 FROM fundamentals_snapshots WHERE ticker=%s AND snapshot_date=%s"
        params = [ticker, snapshot_date]
        if source is not None:
            sql += " AND source=%s"
            params.append(source)
        sql += " LIMIT 1"
        with cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone() is not None

    def get_latest(self, ticker: str) -> Optional[dict]:
        with cursor(dict_rows=True) as cur:
            cur.execute("""
                SELECT * FROM fundamentals_snapshots
                WHERE ticker = %s
                ORDER BY snapshot_date DESC
                LIMIT 1
            """, (ticker,))
            row = cur.fetchone()
            return dict(row) if row else None

    def get_history(self, ticker: str,
                     sources: Optional[list[str]] = None) -> list[dict]:
        """All snapshots for a ticker, oldest first. Optional source filter
        (e.g. ['akshare_annual']) to limit to annual history vs daily snapshots."""
        sql = "SELECT * FROM fundamentals_snapshots WHERE ticker = %s"
        params = [ticker]
        if sources:
            sql += " AND source = ANY(%s)"
            params.append(list(sources))
        sql += " ORDER BY snapshot_date ASC"
        with cursor(dict_rows=True) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def get_latest_for_universe(self) -> list[dict]:
        """Latest snapshot per ticker — fundamentals only (no securities join).
        Caller must join with local SQLite securities table for name/sector."""
        with cursor(dict_rows=True) as cur:
            cur.execute("""
                SELECT DISTINCT ON (ticker) *
                FROM fundamentals_snapshots
                ORDER BY ticker, snapshot_date DESC
            """)
            return [dict(r) for r in cur.fetchall()]

    def get_latest_for_tickers(self, tickers: list[str]) -> list[dict]:
        """Latest snapshot per ticker, restricted to the given list. Used by
        the Research tab when we know exactly which ticker we need."""
        if not tickers:
            return []
        with cursor(dict_rows=True) as cur:
            cur.execute("""
                SELECT DISTINCT ON (ticker) *
                FROM fundamentals_snapshots
                WHERE ticker = ANY(%s)
                ORDER BY ticker, snapshot_date DESC
            """, (list(tickers),))
            return [dict(r) for r in cur.fetchall()]


class CloudHistoricalPricesRepository:
    def upsert_rows(self, ticker: str, rows: list[dict]) -> int:
        """rows: list of dicts with keys date, open, high, low, close, adj_close, volume.

        Uses psycopg2.extras.execute_values to batch all rows into a single
        multi-VALUES INSERT — orders-of-magnitude faster than executemany over
        the Supabase pooler (executemany = one round-trip per row; this is
        one round-trip per call). The `market` column is derived from the
        ticker via market_of_ticker() — single source of truth.
        """
        if not rows:
            return 0
        from utils.market import market_of_ticker
        market = market_of_ticker(ticker)
        sql = """
            INSERT INTO historical_prices
                (ticker, market, date, open, high, low, close, adj_close, volume)
            VALUES %s
            ON CONFLICT (ticker, date) DO UPDATE SET
                market = EXCLUDED.market,
                open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                close = EXCLUDED.close, adj_close = EXCLUDED.adj_close,
                volume = EXCLUDED.volume, fetched_at = NOW()
        """
        params = [(ticker, market, r["date"], r.get("open"), r.get("high"),
                   r.get("low"), r.get("close"), r.get("adj_close"), r.get("volume"))
                   for r in rows]
        with cursor() as cur:
            execute_values(cur, sql, params, page_size=1000)
        return len(rows)

    def get_price_on_or_before(self, ticker: str,
                                target_date: str) -> Optional[float]:
        with cursor() as cur:
            cur.execute("""
                SELECT adj_close FROM historical_prices
                WHERE ticker = %s AND date <= %s
                ORDER BY date DESC LIMIT 1
            """, (ticker, target_date))
            row = cur.fetchone()
            return float(row[0]) if row and row[0] is not None else None

    def get_price_series(self, ticker: str, start_date: str,
                          end_date: str) -> list[dict]:
        with cursor(dict_rows=True) as cur:
            cur.execute("""
                SELECT date, adj_close FROM historical_prices
                WHERE ticker = %s AND date >= %s AND date <= %s
                ORDER BY date ASC
            """, (ticker, start_date, end_date))
            return [{"date": str(r["date"]),
                     "adj_close": float(r["adj_close"]) if r["adj_close"] is not None else None}
                    for r in cur.fetchall()]

    def get_full_series(self, ticker: str) -> list[dict]:
        """All prices for a ticker, no date filter. Used by the Research-tab
        period selector which slices client-side."""
        with cursor(dict_rows=True) as cur:
            cur.execute("""
                SELECT date, adj_close FROM historical_prices
                WHERE ticker = %s
                ORDER BY date ASC
            """, (ticker,))
            return [{"date": str(r["date"]),
                     "adj_close": float(r["adj_close"]) if r["adj_close"] is not None else None}
                    for r in cur.fetchall()]

    def count_rows(self, ticker: Optional[str] = None) -> int:
        with cursor() as cur:
            if ticker:
                cur.execute("SELECT COUNT(*) FROM historical_prices WHERE ticker = %s",
                            (ticker,))
            else:
                cur.execute("SELECT COUNT(*) FROM historical_prices")
            return int(cur.fetchone()[0])

    def earliest_date(self, ticker: str) -> Optional[str]:
        with cursor() as cur:
            cur.execute("SELECT MIN(date) FROM historical_prices WHERE ticker = %s",
                        (ticker,))
            row = cur.fetchone()
            return str(row[0]) if row and row[0] else None

    def latest_date(self, ticker: str) -> Optional[str]:
        """Used by data_loader staleness check."""
        with cursor() as cur:
            cur.execute("SELECT MAX(date) FROM historical_prices WHERE ticker = %s",
                        (ticker,))
            row = cur.fetchone()
            return str(row[0]) if row and row[0] else None

    def latest_date_any(self) -> Optional[str]:
        """Freshest price date across all tickers — used by the Screener
        header pill to show 'data as-of'."""
        with cursor() as cur:
            cur.execute("SELECT MAX(date) FROM historical_prices")
            row = cur.fetchone()
            return str(row[0]) if row and row[0] else None

    def latest_price(self, ticker: str) -> Optional[float]:
        """Single-ticker latest adj_close — backs the Screener/Discovery
        per-row lazy 'Get price' click handler. Indexed lookup, fast
        even over the pooler (single round-trip)."""
        with cursor() as cur:
            cur.execute(
                "SELECT adj_close FROM historical_prices WHERE ticker = %s "
                "AND adj_close IS NOT NULL ORDER BY date DESC LIMIT 1",
                (ticker,),
            )
            row = cur.fetchone()
            return float(row[0]) if row else None

    def bulk_get_price_series(self, tickers: list[str],
                                  start_date: str,
                                  end_date: str) -> dict:
        """{ticker: [{date, adj_close}, …]} for every ticker in one
        round-trip — same shape as the existing get_price_series but
        batched. Cuts subsector/portfolio rebuild latency from O(N×RTT)
        to O(RTT) on the Supabase pool.

        Rows missing adj_close are filtered. Tickers with no rows in the
        window get a `[]` so callers can detect missing series."""
        if not tickers:
            return {}
        out: dict = {t: [] for t in tickers}
        with cursor() as cur:
            cur.execute("""
                SELECT ticker, date, adj_close FROM historical_prices
                WHERE ticker = ANY(%s) AND date >= %s AND date <= %s
                  AND adj_close IS NOT NULL
                ORDER BY ticker, date ASC
            """, (list(tickers), start_date, end_date))
            for t, d, ac in cur.fetchall():
                out[t].append({"date": str(d), "adj_close": float(ac)})
        return out

    def bulk_prices_on_or_before(self, tickers: list[str],
                                    target_date: str) -> dict:
        """{ticker: latest adj_close at or before `target_date`} in one
        round-trip. Used by the backtest engine's as-of enrichment to
        avoid 2,800+ sequential price lookups per snapshot."""
        if not tickers:
            return {}
        with cursor() as cur:
            cur.execute("""
                SELECT DISTINCT ON (ticker) ticker, adj_close
                FROM historical_prices
                WHERE ticker = ANY(%s) AND date <= %s AND adj_close IS NOT NULL
                ORDER BY ticker, date DESC
            """, (list(tickers), target_date))
            return {row[0]: float(row[1]) for row in cur.fetchall()}

    def get_full_ohlc_series(self, ticker: str) -> list[dict]:
        """All historical OHLC rows for a ticker. Used by the Stock Research
        candlestick view; the line chart only needs adj_close so it uses
        get_full_series instead."""
        with cursor(dict_rows=True) as cur:
            cur.execute("""
                SELECT date, open, high, low, close, adj_close, volume
                FROM historical_prices
                WHERE ticker = %s
                ORDER BY date ASC
            """, (ticker,))
            out = []
            for r in cur.fetchall():
                out.append({
                    "date": str(r["date"]),
                    "open": float(r["open"]) if r["open"] is not None else None,
                    "high": float(r["high"]) if r["high"] is not None else None,
                    "low": float(r["low"]) if r["low"] is not None else None,
                    "close": float(r["close"]) if r["close"] is not None else None,
                    "adj_close": float(r["adj_close"]) if r["adj_close"] is not None else None,
                    "volume": int(r["volume"]) if r["volume"] is not None else None,
                })
            return out

    def distinct_tickers(self) -> list[str]:
        """All tickers with at least one price row — used by seed scripts to
        skip already-loaded tickers."""
        with cursor() as cur:
            cur.execute("SELECT DISTINCT ticker FROM historical_prices ORDER BY ticker")
            return [r[0] for r in cur.fetchall()]


class CloudFinancialStatementsRepository:
    """Raw income/balance/cashflow statements per ticker. JSONB blob per
    (ticker, statement_type, period_end_date) — line items vary by source so
    we don't flatten into columns."""

    def get_for_ticker(self, ticker: str) -> dict[str, list[dict]]:
        """Return {'income': [...], 'balance': [...], 'cashflow': [...]} with
        rows sorted newest-first. Empty lists if nothing cached."""
        out = {"income": [], "balance": [], "cashflow": []}
        with cursor(dict_rows=True) as cur:
            cur.execute("""
                SELECT statement_type, period_end_date, period_type, source,
                       currency, line_items, fetched_at
                FROM financial_statements
                WHERE ticker = %s
                ORDER BY period_end_date DESC
            """, (ticker,))
            for r in cur.fetchall():
                stype = r["statement_type"]
                if stype not in out:
                    continue
                out[stype].append({
                    "period_end_date": str(r["period_end_date"]),
                    "period_type": r["period_type"],
                    "source": r["source"],
                    "currency": r["currency"],
                    "line_items": r["line_items"],   # psycopg2 returns dict for JSONB
                    "fetched_at": r["fetched_at"].isoformat() if r["fetched_at"] else None,
                })
        return out

    def upsert_statements(self, ticker: str,
                           statements: dict[str, list[dict]]) -> int:
        """Bulk-upsert all rows across all three statement types. Each row in
        the input dict-of-lists is one period; line_items goes to JSONB."""
        from psycopg2.extras import execute_values, Json
        rows: list[tuple] = []
        for stype, period_rows in statements.items():
            for r in period_rows:
                rows.append((
                    ticker,
                    stype,
                    r["period_end_date"],
                    r.get("period_type") or "annual",
                    r.get("source") or "unknown",
                    r.get("currency"),
                    Json(r.get("line_items") or {}),
                ))
        if not rows:
            return 0
        sql = """
            INSERT INTO financial_statements
                (ticker, statement_type, period_end_date, period_type,
                 source, currency, line_items)
            VALUES %s
            ON CONFLICT (ticker, statement_type, period_end_date, period_type)
            DO UPDATE SET
                source = EXCLUDED.source,
                currency = EXCLUDED.currency,
                line_items = EXCLUDED.line_items,
                fetched_at = NOW()
        """
        with cursor() as cur:
            execute_values(cur, sql, rows, page_size=500)
        return len(rows)

    def latest_fetched_at(self, ticker: str) -> Optional[str]:
        """Most recent fetched_at across all this ticker's statement rows.
        Used by the data_loader to decide if the cache is stale."""
        with cursor() as cur:
            cur.execute(
                "SELECT MAX(fetched_at) FROM financial_statements WHERE ticker = %s",
                (ticker,)
            )
            row = cur.fetchone()
            return row[0].isoformat() if row and row[0] else None

    def latest_period_end(self, ticker: str) -> Optional[str]:
        """Newest period_end_date across all this ticker's statements."""
        with cursor() as cur:
            cur.execute(
                "SELECT MAX(period_end_date) FROM financial_statements WHERE ticker = %s",
                (ticker,)
            )
            row = cur.fetchone()
            return str(row[0]) if row and row[0] else None


class CloudPortfoliosRepository:
    """User-saved portfolios. Each row materialises into one or two synthetic
    tickers in historical_prices (@NAME and optionally @NAME$OPT), so
    downstream tabs can treat a portfolio like any other ticker.

    Name is the natural key (one row per unique uppercase-alphanumeric name).
    Application code is responsible for normalising the name before save.
    """

    def list_portfolios(self) -> list[dict]:
        """All saved portfolios, most-recently-updated first."""
        with cursor(dict_rows=True) as cur:
            cur.execute("""
                SELECT name, holdings, optimal_weights, rf, weight_cap,
                       lookback_days, notes, created_at, updated_at
                FROM portfolios
                ORDER BY updated_at DESC
            """)
            return [_serialise_portfolio_row(dict(r)) for r in cur.fetchall()]

    def get_portfolio(self, name: str) -> Optional[dict]:
        with cursor(dict_rows=True) as cur:
            cur.execute("""
                SELECT name, holdings, optimal_weights, rf, weight_cap,
                       lookback_days, notes, created_at, updated_at
                FROM portfolios WHERE name = %s
            """, (name,))
            row = cur.fetchone()
            return _serialise_portfolio_row(dict(row)) if row else None

    def save_portfolio(self, name: str, holdings: list[dict],
                        optimal_weights: Optional[list[dict]] = None,
                        *, rf: Optional[float] = None,
                        weight_cap: Optional[float] = None,
                        lookback_days: Optional[int] = None,
                        notes: Optional[str] = None) -> None:
        """Upsert. `holdings` is a list of {ticker, shares}; optional
        `optimal_weights` is a list of {ticker, weight} (already aligned
        with the same tickers, weights summing to 1)."""
        from psycopg2.extras import Json
        with cursor() as cur:
            cur.execute("""
                INSERT INTO portfolios
                    (name, holdings, optimal_weights, rf, weight_cap,
                     lookback_days, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (name) DO UPDATE SET
                    holdings = EXCLUDED.holdings,
                    optimal_weights = EXCLUDED.optimal_weights,
                    rf = EXCLUDED.rf,
                    weight_cap = EXCLUDED.weight_cap,
                    lookback_days = EXCLUDED.lookback_days,
                    notes = EXCLUDED.notes,
                    updated_at = NOW()
            """, (name, Json(holdings),
                  Json(optimal_weights) if optimal_weights is not None else None,
                  rf, weight_cap, lookback_days, notes))

    def delete_portfolio(self, name: str) -> bool:
        """Returns True if a row was deleted. Caller is responsible for
        cleaning up the matching @NAME / @NAME$OPT rows in historical_prices."""
        with cursor() as cur:
            cur.execute("DELETE FROM portfolios WHERE name = %s", (name,))
            return cur.rowcount > 0

    def list_names_starting_with(self, prefix: str) -> list[str]:
        """All portfolio names beginning with `prefix`. Used by the backtest
        save flow to pick the next free '<Strategy> backtest #N' number."""
        with cursor() as cur:
            cur.execute(
                "SELECT name FROM portfolios WHERE name LIKE %s",
                (prefix + "%",),
            )
            return [row[0] for row in cur.fetchall()]


def _serialise_portfolio_row(row: dict) -> dict:
    """Coerce psycopg2 types (datetime, Decimal) to JSON-friendly primitives."""
    from decimal import Decimal
    from datetime import datetime
    out = {}
    for k, v in row.items():
        if isinstance(v, Decimal):
            out[k] = float(v)
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


class CloudSecuritiesReferenceRepository:
    """Per-ticker reference rows: bilingual names + resolved sector taxonomy.
    Schema in scripts/supabase_schema.sql:`securities_reference`.

    Read API mirrors the local-SQLite shape so the factory router can swap
    implementations transparently. Writes are bulk-only (one round-trip per
    sync run) to keep latency predictable on the Supabase free-tier pooler.
    """

    def upsert_many(self, rows: list[dict]) -> int:
        """rows: [{ticker, english_name, chinese_name, parent_sector, sub_sector}, ...].
        ON CONFLICT updates every column + the `updated_at` stamp."""
        if not rows:
            return 0
        sql = """
            INSERT INTO securities_reference
                (ticker, english_name, chinese_name, parent_sector, sub_sector)
            VALUES %s
            ON CONFLICT (ticker) DO UPDATE SET
                english_name  = EXCLUDED.english_name,
                chinese_name  = EXCLUDED.chinese_name,
                parent_sector = EXCLUDED.parent_sector,
                sub_sector    = EXCLUDED.sub_sector,
                updated_at    = NOW()
        """
        params = [
            (r.get("ticker"), r.get("english_name"), r.get("chinese_name"),
              r.get("parent_sector"), r.get("sub_sector"))
            for r in rows if r.get("ticker")
        ]
        with cursor() as cur:
            execute_values(cur, sql, params, page_size=1000)
        return len(params)

    def get_one(self, ticker: str) -> Optional[dict]:
        with cursor(dict_rows=True) as cur:
            cur.execute(
                "SELECT ticker, english_name, chinese_name, "
                "parent_sector, sub_sector, updated_at "
                "FROM securities_reference WHERE ticker = %s",
                (ticker,),
            )
            row = cur.fetchone()
            return _serialise_portfolio_row(row) if row else None

    def get_many(self, tickers: list[str]) -> dict[str, dict]:
        """{ticker: full_row_dict} for the requested tickers (missing rows omitted).
        One Supabase round-trip via `ticker = ANY(%s)`."""
        if not tickers:
            return {}
        with cursor(dict_rows=True) as cur:
            cur.execute(
                "SELECT ticker, english_name, chinese_name, "
                "parent_sector, sub_sector, updated_at "
                "FROM securities_reference WHERE ticker = ANY(%s)",
                (list(tickers),),
            )
            return {row["ticker"]: _serialise_portfolio_row(row) for row in cur.fetchall()}

    def get_all(self) -> list[dict]:
        """Every row — used by the cache-aside refresh that mirrors cloud
        into local SQLite for the dashboard's read path."""
        with cursor(dict_rows=True) as cur:
            cur.execute(
                "SELECT ticker, english_name, chinese_name, "
                "parent_sector, sub_sector, updated_at "
                "FROM securities_reference ORDER BY ticker"
            )
            return [_serialise_portfolio_row(r) for r in cur.fetchall()]

    def count(self) -> int:
        with cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM securities_reference")
            return int(cur.fetchone()[0])
