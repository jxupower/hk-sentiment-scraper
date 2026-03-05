from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class SentimentResult:
    article_url: str
    ticker_hints: list[str]
    vader_score: float      # compound score [-1, 1]
    claude_score: Optional[float]
    final_score: float      # primary score used for signals
    label: str              # "BULLISH", "BEARISH", "NEUTRAL"
    scored_at: datetime


def _score_to_label(score: float) -> str:
    if score >= 0.05:
        return "BULLISH"
    elif score <= -0.05:
        return "BEARISH"
    return "NEUTRAL"


class SentimentAnalyzer:
    def __init__(self, claude_api_key: str = ""):
        self._vader = SentimentIntensityAnalyzer()
        self._claude_key = claude_api_key
        self._claude_client = None
        if claude_api_key:
            try:
                import anthropic
                self._claude_client = anthropic.Anthropic(api_key=claude_api_key)
                logger.info("Claude API enabled for sentiment analysis")
            except Exception as e:
                logger.warning("Claude API init failed: %s", e)

    def score_article(self, title: str, body: str, url: str,
                      ticker_hints: list[str]) -> SentimentResult:
        text = f"{title}. {body[:500]}"
        vader_score = self._vader_score(text)
        claude_score = None

        if self._claude_client:
            try:
                claude_score = self._claude_score(title, body[:300])
            except Exception as e:
                logger.debug("Claude scoring failed: %s", e)

        final_score = claude_score if claude_score is not None else vader_score
        return SentimentResult(
            article_url=url,
            ticker_hints=ticker_hints,
            vader_score=vader_score,
            claude_score=claude_score,
            final_score=final_score,
            label=_score_to_label(final_score),
            scored_at=datetime.utcnow(),
        )

    def _vader_score(self, text: str) -> float:
        scores = self._vader.polarity_scores(text)
        return scores["compound"]

    def _claude_score(self, title: str, body: str) -> Optional[float]:
        prompt = (
            f"Analyze the financial sentiment of this news article for stock market implications.\n\n"
            f"Title: {title}\n"
            f"Summary: {body}\n\n"
            f"Respond with ONLY a single decimal number between -1.0 (very bearish) and 1.0 (very bullish). "
            f"0.0 means neutral. No other text."
        )
        response = self._claude_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        return float(raw)
