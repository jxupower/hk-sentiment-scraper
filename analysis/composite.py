"""Composite scoring engine — combines sector-relative valuation z-scores with
universe-relative sentiment z-scores into a single recommendation per ticker.

Computed on-demand (no storage table) so the dashboard's weight slider can
re-blend the inputs in real time without touching the DB.
"""
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from statistics import mean, median, stdev
from typing import Optional

# Outlier clipping bounds — values outside these are treated as data errors and
# excluded from sector stats. Same bounds used for individual ticker scoring.
PE_BOUNDS = (0.0, 200.0)
PB_BOUNDS = (0.0, 30.0)

DEFAULT_VALUATION_WEIGHT = 0.6
DEFAULT_SENTIMENT_WINDOW_DAYS = 7
DEFAULT_MIN_ARTICLES = 3
MIN_TICKERS_PER_SECTOR_FOR_STATS = 3


@dataclass
class CompositeResult:
    ticker: str
    name: str
    sector: str
    regime: str                       # 'deep' | 'covered' | 'uncovered'
    valuation_z: Optional[float]
    sentiment_z: Optional[float]
    composite_score: Optional[float]
    recommendation: str
    article_count_7d: int
    trailing_pe: Optional[float]
    price_to_book: Optional[float]
    dividend_yield: Optional[float]
    market_cap: Optional[float]


@dataclass
class EngineDiagnostics:
    """Surfaced in the UI banner so the user can see how thin the data is."""
    fundamentals_tickers: int = 0
    sectors_with_stats: int = 0
    tickers_with_sentiment: int = 0
    sentiment_window_days: int = 0
    sentiment_universe_mean: Optional[float] = None
    sentiment_universe_stdev: Optional[float] = None
    note: str = ""


class ScoreEngine:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def compute(self, valuation_weight: float = DEFAULT_VALUATION_WEIGHT,
                sentiment_window_days: int = DEFAULT_SENTIMENT_WINDOW_DAYS,
                min_articles: int = DEFAULT_MIN_ARTICLES,
                ) -> tuple[list[CompositeResult], EngineDiagnostics]:
        valuation_weight = max(0.0, min(1.0, valuation_weight))
        sentiment_weight = 1.0 - valuation_weight

        fund_rows, sent_by_ticker = self._load(sentiment_window_days)
        sector_stats = self._compute_sector_stats(fund_rows)
        sent_universe_mean, sent_universe_std, eligible_count = self._compute_sentiment_universe_stats(
            sent_by_ticker, min_articles
        )

        results: list[CompositeResult] = []
        for f in fund_rows:
            ticker = f["ticker"]
            sector = f.get("yf_sector") or f.get("watchlist_sector") or ""
            sent_data = sent_by_ticker.get(ticker)
            article_count = sent_data["n"] if sent_data else 0

            val_z = self._valuation_z(f, sector, sector_stats)
            sent_z = self._sentiment_z(sent_data, min_articles, sent_universe_mean, sent_universe_std)

            if f.get("is_watchlist"):
                regime = "deep"
            elif sent_z is not None:
                regime = "covered"
            else:
                regime = "uncovered"

            composite = self._blend(val_z, sent_z, valuation_weight, sentiment_weight)
            recommendation = self._classify(composite)

            results.append(CompositeResult(
                ticker=ticker,
                name=f.get("name") or ticker,
                sector=sector or "—",
                regime=regime,
                valuation_z=val_z,
                sentiment_z=sent_z,
                composite_score=composite,
                recommendation=recommendation,
                article_count_7d=article_count,
                trailing_pe=f.get("trailing_pe"),
                price_to_book=f.get("price_to_book"),
                dividend_yield=f.get("dividend_yield"),
                market_cap=f.get("market_cap"),
            ))

        results.sort(key=lambda r: (r.composite_score is None, -(r.composite_score or 0)))
        diag = EngineDiagnostics(
            fundamentals_tickers=len(fund_rows),
            sectors_with_stats=len(sector_stats["pe"]),
            tickers_with_sentiment=eligible_count,
            sentiment_window_days=sentiment_window_days,
            sentiment_universe_mean=sent_universe_mean,
            sentiment_universe_stdev=sent_universe_std,
            note=self._diagnostic_note(len(fund_rows), eligible_count, len(sector_stats["pe"])),
        )
        return results, diag

    def _load(self, sentiment_window_days: int):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            fund = conn.execute("""
                SELECT f.ticker, f.trailing_pe, f.price_to_book, f.ev_to_ebitda,
                       f.dividend_yield, f.market_cap,
                       s.name, s.is_watchlist, s.yf_sector, s.watchlist_sector
                FROM fundamentals_snapshots f
                INNER JOIN (
                    SELECT ticker, MAX(snapshot_date) AS max_date
                    FROM fundamentals_snapshots
                    GROUP BY ticker
                ) latest ON f.ticker = latest.ticker AND f.snapshot_date = latest.max_date
                LEFT JOIN securities s ON f.ticker = s.ticker
            """).fetchall()
            sent = conn.execute(f"""
                SELECT ticker,
                       AVG(final_score) AS avg_sent,
                       COUNT(*) AS n
                FROM sentiment_scores
                WHERE scored_at >= datetime('now', '-{int(sentiment_window_days)} days')
                GROUP BY ticker
            """).fetchall()
        return [dict(r) for r in fund], {r["ticker"]: dict(r) for r in sent}

    def _compute_sector_stats(self, fund_rows: list[dict]) -> dict:
        pe_by, pb_by = defaultdict(list), defaultdict(list)
        for f in fund_rows:
            sector = f.get("yf_sector") or f.get("watchlist_sector")
            if not sector:
                continue
            pe = f.get("trailing_pe")
            if pe is not None and PE_BOUNDS[0] < pe < PE_BOUNDS[1]:
                pe_by[sector].append(pe)
            pb = f.get("price_to_book")
            if pb is not None and PB_BOUNDS[0] < pb < PB_BOUNDS[1]:
                pb_by[sector].append(pb)

        def _stats(by_sector):
            out = {}
            for sec, vals in by_sector.items():
                if len(vals) < MIN_TICKERS_PER_SECTOR_FOR_STATS:
                    continue
                out[sec] = (median(vals), max(stdev(vals) if len(vals) >= 2 else 1.0, 0.01))
            return out

        return {"pe": _stats(pe_by), "pb": _stats(pb_by)}

    def _compute_sentiment_universe_stats(self, sent_by_ticker: dict, min_articles: int):
        eligible = [s["avg_sent"] for s in sent_by_ticker.values()
                    if s["n"] >= min_articles and s["avg_sent"] is not None]
        if len(eligible) < 2:
            return 0.0, 1.0, len(eligible)
        m = mean(eligible)
        s = max(stdev(eligible), 0.01)
        return m, s, len(eligible)

    def _valuation_z(self, f: dict, sector: str, sector_stats: dict) -> Optional[float]:
        if not sector:
            return None
        zs = []
        pe = f.get("trailing_pe")
        if pe is not None and PE_BOUNDS[0] < pe < PE_BOUNDS[1] and sector in sector_stats["pe"]:
            m, s = sector_stats["pe"][sector]
            zs.append(-(pe - m) / s)
        pb = f.get("price_to_book")
        if pb is not None and PB_BOUNDS[0] < pb < PB_BOUNDS[1] and sector in sector_stats["pb"]:
            m, s = sector_stats["pb"][sector]
            zs.append(-(pb - m) / s)
        if not zs:
            return None
        return sum(zs) / len(zs)

    def _sentiment_z(self, sent_data: Optional[dict], min_articles: int,
                     mean_: float, std_: float) -> Optional[float]:
        if not sent_data or sent_data["n"] < min_articles or sent_data["avg_sent"] is None:
            return None
        return (sent_data["avg_sent"] - mean_) / std_

    def _blend(self, val_z, sent_z, w_val, w_sent):
        if val_z is None and sent_z is None:
            return None
        if sent_z is None:
            return val_z
        if val_z is None:
            return sent_z
        return w_val * val_z + w_sent * sent_z

    def _classify(self, composite: Optional[float]) -> str:
        if composite is None:
            return "—"
        if composite >= 1.5:
            return "STRONG BUY"
        if composite >= 0.5:
            return "BUY"
        if composite <= -1.5:
            return "STRONG SELL"
        if composite <= -0.5:
            return "SELL"
        return "HOLD"

    def _diagnostic_note(self, fund_count: int, sent_count: int, sector_count: int) -> str:
        parts = []
        if fund_count < 100:
            parts.append(f"Only {fund_count} tickers have fundamentals — "
                         "run 'python main.py fundamentals refresh --tickers ALL' to populate the universe.")
        if sent_count < 20:
            parts.append(f"Only {sent_count} tickers have ≥3 articles in the sentiment window — "
                         "let the scraper accumulate articles for several days.")
        if sector_count < 5:
            parts.append(f"Only {sector_count} sectors have enough tickers for sector-relative valuation — "
                         "more sector coverage will sharpen the valuation z-scores.")
        return " ".join(parts) if parts else "Data depth looks reasonable."
