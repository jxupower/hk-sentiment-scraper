from dataclasses import dataclass
from datetime import datetime
from typing import Optional
import pandas as pd
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class SectorSignal:
    sector: str
    avg_sentiment_24h: float
    avg_sentiment_7d: float
    article_count_24h: int
    avg_price_momentum: float   # average across all tickers in sector
    direction: str              # "UP", "DOWN", "NEUTRAL", "MIXED"
    confidence: float           # 0-1
    computed_at: datetime


@dataclass
class TickerSignal:
    ticker: str
    sector: Optional[str]
    avg_sentiment_24h: float
    avg_sentiment_7d: float
    article_count_24h: int
    price_momentum_5d: float  # % change
    signal: str               # "BUY", "SELL", "HOLD", "WATCH"
    confidence: float         # 0-1
    computed_at: datetime


class SignalGenerator:
    def compute_ticker_signal(self, ticker: str, sector: Optional[str],
                              scores_24h: list[dict], scores_7d: list[dict],
                              price_df: pd.DataFrame) -> TickerSignal:
        avg_24h = self._avg_score(scores_24h)
        avg_7d = self._avg_score(scores_7d)
        count_24h = len(scores_24h)
        momentum = self._price_momentum(price_df)
        signal = self._determine_signal(avg_24h, momentum)
        confidence = min(count_24h / 10.0, 1.0)

        return TickerSignal(
            ticker=ticker,
            sector=sector,
            avg_sentiment_24h=avg_24h,
            avg_sentiment_7d=avg_7d,
            article_count_24h=count_24h,
            price_momentum_5d=momentum,
            signal=signal,
            confidence=confidence,
            computed_at=datetime.utcnow(),
        )

    def _avg_score(self, scores: list[dict]) -> float:
        if not scores:
            return 0.0
        values = [float(s["final_score"]) for s in scores if s.get("final_score") is not None]
        return sum(values) / len(values) if values else 0.0

    def _price_momentum(self, price_df: pd.DataFrame) -> float:
        """5-day % price change. Returns 0 if insufficient data."""
        if price_df.empty or "Close" not in price_df.columns or len(price_df) < 5:
            return 0.0
        recent = price_df["Close"].dropna()
        if len(recent) < 5:
            return 0.0
        start = recent.iloc[-5]
        end = recent.iloc[-1]
        if start == 0:
            return 0.0
        return float((end - start) / start * 100)

    def _determine_signal(self, avg_sentiment_24h: float, price_momentum: float) -> str:
        bullish_sentiment = avg_sentiment_24h > 0.2
        bearish_sentiment = avg_sentiment_24h < -0.2
        positive_momentum = price_momentum > 0
        negative_momentum = price_momentum < 0

        if bullish_sentiment and positive_momentum:
            return "BUY"
        elif bearish_sentiment and negative_momentum:
            return "SELL"
        elif bullish_sentiment and negative_momentum:
            return "WATCH"
        elif bearish_sentiment and positive_momentum:
            return "WATCH"
        return "HOLD"


class SectorSignalGenerator:
    """Aggregates per-ticker data to produce sector-level UP/DOWN/NEUTRAL/MIXED signals."""

    def compute_sector_signal(self, sector: str, scores_24h: list[dict],
                              scores_7d: list[dict],
                              price_dfs: dict[str, pd.DataFrame]) -> SectorSignal:
        avg_24h = self._avg_score(scores_24h)
        avg_7d = self._avg_score(scores_7d)
        count_24h = len(set(s["ticker"] for s in scores_24h if scores_24h))
        avg_momentum = self._avg_price_momentum(price_dfs)
        direction = self._determine_direction(avg_24h, avg_momentum)
        # Confidence: scales with article count and score strength
        article_confidence = min(len(scores_24h) / 20.0, 1.0)
        score_confidence = min(abs(avg_24h) / 0.3, 1.0)
        confidence = (article_confidence + score_confidence) / 2

        return SectorSignal(
            sector=sector,
            avg_sentiment_24h=avg_24h,
            avg_sentiment_7d=avg_7d,
            article_count_24h=len(scores_24h),
            avg_price_momentum=avg_momentum,
            direction=direction,
            confidence=confidence,
            computed_at=datetime.utcnow(),
        )

    def _avg_score(self, scores: list[dict]) -> float:
        if not scores:
            return 0.0
        values = [float(s["final_score"]) for s in scores if s.get("final_score") is not None]
        return sum(values) / len(values) if values else 0.0

    def _avg_price_momentum(self, price_dfs: dict[str, pd.DataFrame]) -> float:
        """Average 5-day price momentum across all tickers that have price data."""
        momenta = []
        for ticker, df in price_dfs.items():
            if df.empty or "Close" not in df.columns:
                continue
            recent = df["Close"].dropna()
            if len(recent) < 5:
                continue
            start, end = recent.iloc[-5], recent.iloc[-1]
            if start != 0:
                momenta.append(float((end - start) / start * 100))
        return sum(momenta) / len(momenta) if momenta else 0.0

    def _determine_direction(self, avg_sentiment_24h: float, avg_momentum: float) -> str:
        bullish = avg_sentiment_24h > 0.15
        bearish = avg_sentiment_24h < -0.15
        up = avg_momentum > 0
        down = avg_momentum < 0

        if bullish and up:
            return "UP"
        elif bearish and down:
            return "DOWN"
        elif bullish and down:
            return "MIXED"
        elif bearish and up:
            return "MIXED"
        return "NEUTRAL"
