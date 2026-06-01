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
        values = [fields.get(c) for c in _FUNDAMENTALS_COLS]
        placeholders = ", ".join(["%s"] * (len(_FUNDAMENTALS_COLS) + 3))
        update_clause = ", ".join(
            f"{c} = EXCLUDED.{c}" for c in _FUNDAMENTALS_COLS
        )
        sql = f"""
            INSERT INTO fundamentals_snapshots
                (ticker, snapshot_date, source, {", ".join(_FUNDAMENTALS_COLS)})
            VALUES ({placeholders})
            ON CONFLICT (ticker, snapshot_date, source) DO UPDATE SET
                {update_clause},
                fetched_at = NOW()
        """
        with cursor() as cur:
            cur.execute(sql, (ticker, snapshot_date, source, *values))

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
        """rows: list of dicts with keys date, open, high, low, close, adj_close, volume."""
        if not rows:
            return 0
        sql = """
            INSERT INTO historical_prices
                (ticker, date, open, high, low, close, adj_close, volume)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (ticker, date) DO UPDATE SET
                open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                close = EXCLUDED.close, adj_close = EXCLUDED.adj_close,
                volume = EXCLUDED.volume, fetched_at = NOW()
        """
        params = [(ticker, r["date"], r.get("open"), r.get("high"), r.get("low"),
                   r.get("close"), r.get("adj_close"), r.get("volume")) for r in rows]
        with cursor() as cur:
            cur.executemany(sql, params)
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

    def distinct_tickers(self) -> list[str]:
        """All tickers with at least one price row — used by seed scripts to
        skip already-loaded tickers."""
        with cursor() as cur:
            cur.execute("SELECT DISTINCT ticker FROM historical_prices ORDER BY ticker")
            return [r[0] for r in cur.fetchall()]
