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
