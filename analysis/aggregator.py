from datetime import datetime, timedelta
import pandas as pd
from utils.logger import get_logger

logger = get_logger(__name__)


class SentimentAggregator:
    def aggregate_by_ticker(self, scores: list[dict], window_hours: int = 24) -> dict[str, float]:
        """Returns {ticker: avg_sentiment} for the given time window."""
        cutoff = datetime.utcnow() - timedelta(hours=window_hours)
        ticker_scores: dict[str, list[float]] = {}
        for row in scores:
            try:
                scored_at = datetime.fromisoformat(str(row["scored_at"]))
            except Exception:
                continue
            if scored_at < cutoff:
                continue
            ticker = row["ticker"]
            ticker_scores.setdefault(ticker, []).append(float(row["final_score"]))

        return {t: sum(v) / len(v) for t, v in ticker_scores.items() if v}

    def aggregate_by_sector(self, ticker_scores: dict[str, float],
                            watchlist: dict) -> dict[str, float]:
        """Returns {sector: avg_sentiment} from per-ticker scores."""
        sector_scores: dict[str, list[float]] = {}
        for sector, tickers in watchlist.get("sectors", {}).items():
            for t in tickers:
                if t in ticker_scores:
                    sector_scores.setdefault(sector, []).append(ticker_scores[t])
        return {s: sum(v) / len(v) for s, v in sector_scores.items() if v}

    def sentiment_timeseries(self, ticker: str, df: pd.DataFrame,
                             bucket: str = "1h") -> pd.DataFrame:
        """Resample per-ticker sentiment scores into time buckets."""
        if df.empty or "scored_at" not in df.columns:
            return pd.DataFrame(columns=["scored_at", "final_score"])
        df = df.copy()
        df["scored_at"] = pd.to_datetime(df["scored_at"])
        df = df.set_index("scored_at")
        resampled = df["final_score"].resample(bucket).mean().dropna().reset_index()
        return resampled
