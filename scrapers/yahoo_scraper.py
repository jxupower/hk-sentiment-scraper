import time
import yfinance as yf
import pandas as pd
from utils.helpers import clean_text, extract_ticker_hints, normalize_datetime
from utils.logger import get_logger
from scrapers.base_scraper import BaseScraper, RawArticle

logger = get_logger(__name__)


class YahooScraper(BaseScraper):
    def fetch(self, search_terms: dict[str, list[str]]) -> list[RawArticle]:
        """search_terms: {ticker: [name, alias1, ...]}"""
        articles = []
        for ticker in list(search_terms.keys()):
            try:
                news_items = self._get_news(ticker, search_terms)
                articles.extend(news_items)
                logger.info("Yahoo [%s]: %d articles", ticker, len(news_items))
            except Exception as e:
                logger.warning("Yahoo news error [%s]: %s", ticker, e)
            time.sleep(0.5)
        return articles

    def _get_news(self, ticker: str,
                  search_terms: dict[str, list[str]]) -> list[RawArticle]:
        t = yf.Ticker(ticker)
        articles = []
        try:
            news = t.news or []
        except Exception:
            return []

        for item in news:
            content = item.get("content", {})
            title = clean_text(content.get("title", "") or item.get("title", ""))
            url = (content.get("canonicalUrl", {}) or {}).get("url", "") or item.get("link", "")

            if not title or not url:
                continue

            summary = clean_text(content.get("summary", "") or "")
            hints = extract_ticker_hints(f"{title} {summary}", search_terms)
            # Always tag the queried ticker since Yahoo returned this article for it
            if ticker not in hints:
                hints.append(ticker)

            pub_ts = content.get("pubDate") or item.get("providerPublishTime")
            published_at = normalize_datetime(pub_ts) if isinstance(pub_ts, (int, float)) else None
            provider = content.get("provider", {})
            author = provider.get("displayName") if isinstance(provider, dict) else None

            articles.append(RawArticle(
                source="yahoo",
                title=title,
                body=summary,
                url=url,
                ticker_hints=hints,
                published_at=published_at,
                author=author,
            ))
        return articles

    def fetch_price_history(self, ticker: str, period: str = "3mo") -> pd.DataFrame:
        try:
            t = yf.Ticker(ticker)
            df = t.history(period=period)
            return df
        except Exception as e:
            logger.warning("Yahoo price error [%s]: %s", ticker, e)
            return pd.DataFrame()
