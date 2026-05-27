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

                CREATE TABLE IF NOT EXISTS securities (
                    ticker            TEXT PRIMARY KEY,
                    hkex_code         TEXT NOT NULL,
                    name              TEXT NOT NULL,
                    listing_category  TEXT,
                    lot_size          INTEGER,
                    is_watchlist      INTEGER NOT NULL DEFAULT 0,
                    watchlist_sector  TEXT,
                    aliases_json      TEXT,
                    yf_sector         TEXT,
                    yf_industry       TEXT,
                    is_active         INTEGER NOT NULL DEFAULT 1,
                    first_seen        DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_refreshed    DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS fundamentals_snapshots (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker            TEXT NOT NULL,
                    snapshot_date     DATE NOT NULL,
                    trailing_pe       REAL,
                    forward_pe        REAL,
                    price_to_book     REAL,
                    ev_to_ebitda      REAL,
                    dividend_yield    REAL,
                    market_cap        REAL,
                    beta              REAL,
                    return_on_equity  REAL,
                    debt_to_equity    REAL,
                    last_price        REAL,
                    currency          TEXT,
                    data_completeness REAL,
                    -- Direction C additions (Stage 1): growth + quality + liquidity
                    earnings_growth   REAL,
                    revenue_growth    REAL,
                    profit_margins    REAL,
                    operating_margins REAL,
                    return_on_assets  REAL,
                    current_ratio     REAL,
                    free_cashflow     REAL,
                    captured_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(ticker, snapshot_date)
                );

                CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_at);
                CREATE INDEX IF NOT EXISTS idx_article_tickers_ticker ON article_tickers(ticker);
                CREATE INDEX IF NOT EXISTS idx_sentiment_ticker ON sentiment_scores(ticker, scored_at);
                CREATE INDEX IF NOT EXISTS idx_signals_ticker ON ticker_signals(ticker, computed_at);
                CREATE INDEX IF NOT EXISTS idx_sector_signals ON sector_signals(sector, computed_at);
                CREATE INDEX IF NOT EXISTS idx_securities_watchlist ON securities(is_watchlist);
                CREATE INDEX IF NOT EXISTS idx_securities_category ON securities(listing_category);
                CREATE INDEX IF NOT EXISTS idx_fundamentals_ticker_date ON fundamentals_snapshots(ticker, snapshot_date);
                CREATE INDEX IF NOT EXISTS idx_fundamentals_date ON fundamentals_snapshots(snapshot_date);
            """)
            # Migration: add Direction C columns to fundamentals_snapshots if missing
            # (CREATE TABLE IF NOT EXISTS won't add columns to a pre-existing table).
            self._add_columns_if_missing(conn, "fundamentals_snapshots", [
                ("earnings_growth",   "REAL"),
                ("revenue_growth",    "REAL"),
                ("profit_margins",    "REAL"),
                ("operating_margins", "REAL"),
                ("return_on_assets",  "REAL"),
                ("current_ratio",     "REAL"),
                ("free_cashflow",     "REAL"),
            ])
        logger.info("Database initialized at %s", self.db_path)

    def _add_columns_if_missing(self, conn, table: str, columns: list[tuple[str, str]]):
        """Idempotently add columns; SQLite has no IF NOT EXISTS for ADD COLUMN."""
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col_name, col_type in columns:
            if col_name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
                logger.info("Migration: added %s.%s", table, col_name)
        conn.commit()

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn
