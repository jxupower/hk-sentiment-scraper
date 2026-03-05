from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class RawArticle:
    source: str
    title: str
    body: str
    url: str
    ticker_hints: list[str] = field(default_factory=list)
    published_at: Optional[datetime] = None
    author: Optional[str] = None
    raw_score: Optional[float] = None  # upvotes, engagement score, etc.


class BaseScraper(ABC):
    @abstractmethod
    def fetch(self, tickers: list[str]) -> list[RawArticle]:
        ...

    def is_available(self) -> bool:
        return True
