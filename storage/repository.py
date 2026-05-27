import sqlite3
from datetime import datetime, timedelta
from typing import Optional
import pandas as pd

from storage.database import Database
from utils.logger import get_logger

logger = get_logger(__name__)


class ArticleRepository:
    def __init__(self, db: Database):
        self.db = db

    def article_exists(self, url: str) -> bool:
        with self.db.get_connection() as conn:
            row = conn.execute("SELECT id FROM articles WHERE url = ?", (url,)).fetchone()
            return row is not None

    def insert_article(self, source: str, title: str, body: str, url: str,
                       published_at: Optional[datetime], author: Optional[str],
                       raw_score: Optional[float], tickers: list[str]) -> Optional[int]:
        try:
            with self.db.get_connection() as conn:
                cur = conn.execute(
                    """INSERT INTO articles (source, title, body, url, published_at, author, raw_score)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (source, title, body, url,
                     published_at.isoformat() if published_at else None,
                     author, raw_score)
                )
                article_id = cur.lastrowid
                for ticker in tickers:
                    conn.execute(
                        "INSERT OR IGNORE INTO article_tickers (article_id, ticker) VALUES (?, ?)",
                        (article_id, ticker)
                    )
                conn.commit()
                return article_id
        except sqlite3.IntegrityError:
            return None  # Duplicate URL

    def get_recent_articles(self, ticker: str, hours: int = 24) -> list[dict]:
        since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        with self.db.get_connection() as conn:
            rows = conn.execute("""
                SELECT a.id, a.source, a.title, a.url, a.published_at, a.author
                FROM articles a
                JOIN article_tickers at ON a.id = at.article_id
                WHERE at.ticker = ? AND a.fetched_at >= ?
                ORDER BY a.published_at DESC
            """, (ticker, since)).fetchall()
            return [dict(r) for r in rows]

    def prune_old_articles(self, days: int = 90):
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM articles WHERE fetched_at < ?", (cutoff,))
            conn.commit()
        logger.info("Pruned articles older than %d days", days)


class SentimentRepository:
    def __init__(self, db: Database):
        self.db = db

    def insert_score(self, article_id: int, ticker: str, vader_score: float,
                     claude_score: Optional[float], final_score: float, label: str):
        with self.db.get_connection() as conn:
            conn.execute("""
                INSERT INTO sentiment_scores (article_id, ticker, vader_score, claude_score, final_score, label)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (article_id, ticker, vader_score, claude_score, final_score, label))
            conn.commit()

    def get_scores_for_ticker(self, ticker: str, hours: int = 24) -> list[dict]:
        since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        with self.db.get_connection() as conn:
            rows = conn.execute("""
                SELECT s.final_score, s.label, s.scored_at, s.vader_score, s.claude_score,
                       a.source, a.title, a.url, a.published_at
                FROM sentiment_scores s
                JOIN articles a ON s.article_id = a.id
                WHERE s.ticker = ? AND s.scored_at >= ?
                ORDER BY s.scored_at DESC
            """, (ticker, since)).fetchall()
            return [dict(r) for r in rows]

    def get_timeseries(self, ticker: str, hours: int = 168) -> pd.DataFrame:
        since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        with self.db.get_connection() as conn:
            rows = conn.execute("""
                SELECT s.final_score, s.scored_at, a.source
                FROM sentiment_scores s
                JOIN articles a ON s.article_id = a.id
                WHERE s.ticker = ? AND s.scored_at >= ?
                ORDER BY s.scored_at ASC
            """, (ticker, since)).fetchall()
        if not rows:
            return pd.DataFrame(columns=["final_score", "scored_at", "source"])
        df = pd.DataFrame([dict(r) for r in rows])
        df["scored_at"] = pd.to_datetime(df["scored_at"])
        return df

    def get_all_recent_scores(self, hours: int = 24) -> list[dict]:
        since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        with self.db.get_connection() as conn:
            rows = conn.execute("""
                SELECT s.ticker, s.final_score, s.scored_at, a.source
                FROM sentiment_scores s
                JOIN articles a ON s.article_id = a.id
                WHERE s.scored_at >= ?
            """, (since,)).fetchall()
            return [dict(r) for r in rows]

    def get_scores_for_sector(self, tickers: list[str], hours: int = 24) -> list[dict]:
        """Return all sentiment scores for any ticker in the sector."""
        if not tickers:
            return []
        since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        placeholders = ",".join("?" * len(tickers))
        with self.db.get_connection() as conn:
            rows = conn.execute(f"""
                SELECT s.ticker, s.final_score, s.label, s.scored_at,
                       s.vader_score, s.claude_score,
                       a.source, a.title, a.url, a.published_at
                FROM sentiment_scores s
                JOIN articles a ON s.article_id = a.id
                WHERE s.ticker IN ({placeholders}) AND s.scored_at >= ?
                ORDER BY s.scored_at DESC
            """, (*tickers, since)).fetchall()
            return [dict(r) for r in rows]

    def get_sector_timeseries(self, tickers: list[str], hours: int = 168) -> pd.DataFrame:
        """Aggregated sentiment timeseries across all tickers in a sector."""
        if not tickers:
            return pd.DataFrame(columns=["final_score", "scored_at", "source"])
        since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        placeholders = ",".join("?" * len(tickers))
        with self.db.get_connection() as conn:
            rows = conn.execute(f"""
                SELECT s.final_score, s.scored_at, a.source
                FROM sentiment_scores s
                JOIN articles a ON s.article_id = a.id
                WHERE s.ticker IN ({placeholders}) AND s.scored_at >= ?
                ORDER BY s.scored_at ASC
            """, (*tickers, since)).fetchall()
        if not rows:
            return pd.DataFrame(columns=["final_score", "scored_at", "source"])
        df = pd.DataFrame([dict(r) for r in rows])
        df["scored_at"] = pd.to_datetime(df["scored_at"])
        return df


class SignalRepository:
    def __init__(self, db: Database):
        self.db = db

    def upsert_signal(self, ticker: str, sector: Optional[str],
                      avg_sentiment_24h: float, avg_sentiment_7d: float,
                      article_count_24h: int, price_momentum_5d: float,
                      signal: str, confidence: float):
        with self.db.get_connection() as conn:
            conn.execute("""
                INSERT INTO ticker_signals
                    (ticker, sector, avg_sentiment_24h, avg_sentiment_7d,
                     article_count_24h, price_momentum_5d, signal, confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (ticker, sector, avg_sentiment_24h, avg_sentiment_7d,
                  article_count_24h, price_momentum_5d, signal, confidence))
            conn.commit()

    def get_latest_signals(self) -> list[dict]:
        with self.db.get_connection() as conn:
            rows = conn.execute("""
                SELECT ts.*
                FROM ticker_signals ts
                INNER JOIN (
                    SELECT ticker, MAX(computed_at) AS max_at
                    FROM ticker_signals
                    GROUP BY ticker
                ) latest ON ts.ticker = latest.ticker AND ts.computed_at = latest.max_at
                ORDER BY ts.ticker
            """).fetchall()
            return [dict(r) for r in rows]

    def get_signal_history(self, ticker: str, days: int = 30) -> pd.DataFrame:
        since = (datetime.utcnow() - timedelta(days=days)).isoformat()
        with self.db.get_connection() as conn:
            rows = conn.execute("""
                SELECT * FROM ticker_signals
                WHERE ticker = ? AND computed_at >= ?
                ORDER BY computed_at ASC
            """, (ticker, since)).fetchall()
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame([dict(r) for r in rows])
        df["computed_at"] = pd.to_datetime(df["computed_at"])
        return df


class SecuritiesRepository:
    def __init__(self, db: Database):
        self.db = db

    def upsert_security(self, ticker: str, hkex_code: str, name: str,
                        listing_category: Optional[str], lot_size: Optional[int]):
        """Insert a security or update its name/category/lot_size if it already exists.
        Watchlist flags and yfinance fields are NOT touched here — they have their own setters."""
        with self.db.get_connection() as conn:
            conn.execute("""
                INSERT INTO securities (ticker, hkex_code, name, listing_category, lot_size,
                                        is_active, last_refreshed)
                VALUES (?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
                ON CONFLICT(ticker) DO UPDATE SET
                    name = excluded.name,
                    listing_category = excluded.listing_category,
                    lot_size = excluded.lot_size,
                    is_active = 1,
                    last_refreshed = CURRENT_TIMESTAMP
            """, (ticker, hkex_code, name, listing_category, lot_size))
            conn.commit()

    def clear_watchlist_flags(self):
        with self.db.get_connection() as conn:
            conn.execute("""
                UPDATE securities
                SET is_watchlist = 0, watchlist_sector = NULL, aliases_json = NULL
            """)
            conn.commit()

    def set_watchlist(self, ticker: str, sector: str, aliases_json: str,
                      hkex_code: Optional[str] = None, name: Optional[str] = None):
        """Mark a ticker as watchlist. If the ticker is not yet in `securities`
        (e.g. listed in YAML but missing from HKEX), insert it as a manual override row
        and require hkex_code + name to be provided so the row is well-formed."""
        with self.db.get_connection() as conn:
            row = conn.execute("SELECT 1 FROM securities WHERE ticker = ?", (ticker,)).fetchone()
            if row:
                # Force is_active=1: being in the YAML watchlist is an explicit user
                # signal that the ticker is wanted, even if it's not in HKEX anymore.
                conn.execute("""
                    UPDATE securities
                    SET is_watchlist = 1, watchlist_sector = ?, aliases_json = ?,
                        is_active = 1
                    WHERE ticker = ?
                """, (sector, aliases_json, ticker))
            else:
                conn.execute("""
                    INSERT INTO securities (ticker, hkex_code, name, is_watchlist,
                                            watchlist_sector, aliases_json, is_active)
                    VALUES (?, ?, ?, 1, ?, ?, 1)
                """, (ticker, hkex_code or "", name or ticker, sector, aliases_json))
            conn.commit()

    def get_all_active(self) -> list[dict]:
        with self.db.get_connection() as conn:
            rows = conn.execute("""
                SELECT * FROM securities WHERE is_active = 1 ORDER BY ticker
            """).fetchall()
            return [dict(r) for r in rows]

    def get_watchlist(self) -> list[dict]:
        with self.db.get_connection() as conn:
            rows = conn.execute("""
                SELECT * FROM securities WHERE is_watchlist = 1 ORDER BY ticker
            """).fetchall()
            return [dict(r) for r in rows]

    def get_universe(self) -> list[dict]:
        """All active securities. The HKEX parser already filters to equities at ingest time,
        so no further category filtering is needed here."""
        with self.db.get_connection() as conn:
            rows = conn.execute("""
                SELECT * FROM securities
                WHERE is_active = 1
                ORDER BY ticker
            """).fetchall()
            return [dict(r) for r in rows]

    def get_by_ticker(self, ticker: str) -> Optional[dict]:
        with self.db.get_connection() as conn:
            row = conn.execute("SELECT * FROM securities WHERE ticker = ?", (ticker,)).fetchone()
            return dict(row) if row else None

    def count_all(self) -> int:
        with self.db.get_connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM securities").fetchone()[0]

    def count_watchlist(self) -> int:
        with self.db.get_connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM securities WHERE is_watchlist = 1").fetchone()[0]

    def deactivate_missing(self, current_tickers: set[str]) -> int:
        """Mark active rows as inactive if their ticker is NOT in current_tickers.

        Returns the number of rows deactivated. Caller MUST guarantee current_tickers
        is the authoritative present-day universe — passing an incomplete set would
        wrongly deactivate live tickers. The reconciler guards against empty input.
        """
        if not current_tickers:
            return 0
        placeholders = ",".join("?" * len(current_tickers))
        with self.db.get_connection() as conn:
            cur = conn.execute(
                f"UPDATE securities SET is_active = 0 "
                f"WHERE is_active = 1 AND ticker NOT IN ({placeholders})",
                tuple(current_tickers),
            )
            conn.commit()
            return cur.rowcount


class FundamentalsRepository:
    def __init__(self, db: Database):
        self.db = db

    def upsert_snapshot(self, ticker: str, snapshot_date: str, fields: dict):
        """Insert or replace today's snapshot for a ticker.

        `fields` is a dict containing any subset of: trailing_pe, forward_pe,
        price_to_book, ev_to_ebitda, dividend_yield, market_cap, beta,
        return_on_equity, debt_to_equity, last_price, currency, data_completeness.
        Missing keys → NULL in the row.
        """
        cols = ["trailing_pe", "forward_pe", "price_to_book", "ev_to_ebitda",
                "dividend_yield", "market_cap", "beta", "return_on_equity",
                "debt_to_equity", "last_price", "currency", "data_completeness",
                # Direction C extended fields
                "earnings_growth", "revenue_growth", "profit_margins",
                "operating_margins", "return_on_assets", "current_ratio",
                "free_cashflow"]
        values = [fields.get(c) for c in cols]
        with self.db.get_connection() as conn:
            conn.execute(f"""
                INSERT INTO fundamentals_snapshots
                    (ticker, snapshot_date, {", ".join(cols)})
                VALUES (?, ?, {", ".join("?" * len(cols))})
                ON CONFLICT(ticker, snapshot_date) DO UPDATE SET
                    {", ".join(f"{c} = excluded.{c}" for c in cols)},
                    captured_at = CURRENT_TIMESTAMP
            """, (ticker, snapshot_date, *values))
            conn.commit()

    def has_snapshot_for_date(self, ticker: str, snapshot_date: str) -> bool:
        with self.db.get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM fundamentals_snapshots WHERE ticker = ? AND snapshot_date = ?",
                (ticker, snapshot_date)
            ).fetchone()
            return row is not None

    def get_latest(self, ticker: str) -> Optional[dict]:
        with self.db.get_connection() as conn:
            row = conn.execute("""
                SELECT * FROM fundamentals_snapshots
                WHERE ticker = ?
                ORDER BY snapshot_date DESC
                LIMIT 1
            """, (ticker,)).fetchone()
            return dict(row) if row else None

    def get_latest_for_universe(self) -> list[dict]:
        """Latest snapshot per active ticker, joined with securities for name + sector.
        Inactive (delisted) tickers are excluded so dashboards don't leak ghost rows."""
        with self.db.get_connection() as conn:
            rows = conn.execute("""
                SELECT f.*, s.name, s.is_watchlist, s.watchlist_sector,
                       s.yf_sector, s.yf_industry, s.listing_category
                FROM fundamentals_snapshots f
                INNER JOIN (
                    SELECT ticker, MAX(snapshot_date) AS max_date
                    FROM fundamentals_snapshots
                    GROUP BY ticker
                ) latest ON f.ticker = latest.ticker AND f.snapshot_date = latest.max_date
                INNER JOIN securities s ON f.ticker = s.ticker
                WHERE s.is_active = 1
                ORDER BY f.ticker
            """).fetchall()
            return [dict(r) for r in rows]

    def update_security_yf_metadata(self, ticker: str, yf_sector: Optional[str],
                                    yf_industry: Optional[str]):
        """Backfill the yf_sector / yf_industry columns on the securities table
        once we've seen them via .info. Runs alongside snapshot upsert."""
        with self.db.get_connection() as conn:
            conn.execute("""
                UPDATE securities
                SET yf_sector = COALESCE(?, yf_sector),
                    yf_industry = COALESCE(?, yf_industry)
                WHERE ticker = ?
            """, (yf_sector, yf_industry, ticker))
            conn.commit()


class SectorSignalRepository:
    def __init__(self, db: Database):
        self.db = db

    def insert_signal(self, sector: str, avg_sentiment_24h: float, avg_sentiment_7d: float,
                      article_count_24h: int, avg_price_momentum: float,
                      direction: str, confidence: float):
        with self.db.get_connection() as conn:
            conn.execute("""
                INSERT INTO sector_signals
                    (sector, avg_sentiment_24h, avg_sentiment_7d,
                     article_count_24h, avg_price_momentum, direction, confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (sector, avg_sentiment_24h, avg_sentiment_7d,
                  article_count_24h, avg_price_momentum, direction, confidence))
            conn.commit()

    def get_latest_signals(self) -> list[dict]:
        with self.db.get_connection() as conn:
            rows = conn.execute("""
                SELECT ss.*
                FROM sector_signals ss
                INNER JOIN (
                    SELECT sector, MAX(computed_at) AS max_at
                    FROM sector_signals
                    GROUP BY sector
                ) latest ON ss.sector = latest.sector AND ss.computed_at = latest.max_at
                ORDER BY ss.sector
            """).fetchall()
            return [dict(r) for r in rows]

    def get_signal_history(self, sector: str, days: int = 30) -> pd.DataFrame:
        since = (datetime.utcnow() - timedelta(days=days)).isoformat()
        with self.db.get_connection() as conn:
            rows = conn.execute("""
                SELECT * FROM sector_signals
                WHERE sector = ? AND computed_at >= ?
                ORDER BY computed_at ASC
            """, (sector, since)).fetchall()
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame([dict(r) for r in rows])
        df["computed_at"] = pd.to_datetime(df["computed_at"])
        return df
