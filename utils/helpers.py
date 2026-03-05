import re
from datetime import datetime
from typing import Optional


_HTML_TAG_RE = re.compile(r'<[^>]+>')
_WHITESPACE_RE = re.compile(r'\s+')


def clean_text(text: str) -> str:
    if not text:
        return ""
    text = _HTML_TAG_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip()


def extract_ticker_hints(text: str, search_terms: dict[str, list[str]]) -> list[str]:
    """
    Find which tickers are mentioned in text by matching company names and aliases.

    search_terms: {ticker: [name, alias1, alias2, ...]}
    Returns list of matched tickers.
    """
    if not text or not search_terms:
        return []

    found = set()
    for ticker, terms in search_terms.items():
        for term in terms:
            if not term:
                continue
            pattern = rf'(?<![A-Za-z]){re.escape(term)}(?![A-Za-z])'
            if re.search(pattern, text, re.IGNORECASE):
                found.add(ticker)
                break  # one match is enough for this ticker
    return list(found)


def normalize_datetime(dt) -> Optional[datetime]:
    """Normalize various datetime types to UTC datetime."""
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.replace(tzinfo=None)
    if isinstance(dt, (int, float)):
        return datetime.utcfromtimestamp(dt)
    # feedparser time struct
    try:
        return datetime(*dt[:6])
    except Exception:
        pass
    return None


def truncate_text(text: str, max_chars: int = 500) -> str:
    if not text:
        return ""
    return text[:max_chars] + ("..." if len(text) > max_chars else "")
