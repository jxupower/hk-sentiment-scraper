import sqlite3
from pathlib import Path
from utils.logger import get_logger

logger = get_logger(__name__)


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    def initialize(self):
        with self.get_connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS articles (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    source       TEXT NOT NULL,
                    title        TEXT NOT NULL,
                    body         TEXT,
                    url          TEXT UNIQUE NOT NULL,
                    published_at DATETIME,
                    author       TEXT,
                    raw_score    REAL,
                    fetched_at   DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS article_tickers (
                    article_id INTEGER REFERENCES articles(id) ON DELETE CASCADE,
                    ticker     TEXT NOT NULL,
                    PRIMARY KEY (article_id, ticker)
                );

                CREATE TABLE IF NOT EXISTS sentiment_scores (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    article_id   INTEGER REFERENCES articles(id) ON DELETE CASCADE,
                    ticker       TEXT NOT NULL,
                    vader_score  REAL,
                    claude_score REAL,
                    final_score  REAL,
                    label        TEXT,
                    scored_at    DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS ticker_signals (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker              TEXT NOT NULL,
                    sector              TEXT,
                    avg_sentiment_24h   REAL,
                    avg_sentiment_7d    REAL,
                    article_count_24h   INTEGER,
                    price_momentum_5d   REAL,
                    signal              TEXT,
                    confidence          REAL,
                    computed_at         DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS sector_signals (
                    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                    sector               TEXT NOT NULL,
                    avg_sentiment_24h    REAL,
                    avg_sentiment_7d     REAL,
                    article_count_24h    INTEGER,
                    avg_price_momentum   REAL,
                    direction            TEXT,
                    confidence           REAL,
                    computed_at          DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_at);
                CREATE INDEX IF NOT EXISTS idx_article_tickers_ticker ON article_tickers(ticker);
                CREATE INDEX IF NOT EXISTS idx_sentiment_ticker ON sentiment_scores(ticker, scored_at);
                CREATE INDEX IF NOT EXISTS idx_signals_ticker ON ticker_signals(ticker, computed_at);
                CREATE INDEX IF NOT EXISTS idx_sector_signals ON sector_signals(sector, computed_at);
            """)
        logger.info("Database initialized at %s", self.db_path)

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn
