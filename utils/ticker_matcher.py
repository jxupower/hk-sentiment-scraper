import re
from typing import Optional

MIN_TERM_LENGTH = 4  # filters out 2-3 char names like "JD", "AIA", "MTR" that cause noise


class TickerMatcher:
    """Compiled multi-keyword matcher: maps text → list of matched tickers.

    Designed for one-build-per-scrape-cycle reuse. Scales to ~3,000 search terms
    by compiling a single big alternation regex with word-boundary anchors. Per-article
    match is fast because Python's regex engine handles alternation efficiently.

    Args:
        search_terms: {ticker: [term1, term2, ...]}. The first term in each list is
                      treated as the canonical name (priority for tie-breaking).
        watchlist_tickers: set of tickers that should be preferred when capping results.
                           Watchlist always wins over universe in the top-N selection.
    """

    def __init__(self, search_terms: dict[str, list[str]],
                 watchlist_tickers: Optional[set[str]] = None):
        self.watchlist_tickers = watchlist_tickers or set()

        # Term → set of tickers that claim this term. One term may belong to multiple tickers
        # (rare, but happens with generic broad terms like "China bank").
        term_to_tickers: dict[str, set[str]] = {}
        for ticker, terms in search_terms.items():
            for term in terms:
                if not term:
                    continue
                clean = term.strip()
                if len(clean) < MIN_TERM_LENGTH:
                    continue
                term_to_tickers.setdefault(clean.lower(), set()).add(ticker)

        if not term_to_tickers:
            self._pattern = None
            self._term_to_tickers: dict[str, set[str]] = {}
            return

        # Sort longest-first so longer matches "win" in regex alternation
        sorted_terms = sorted(term_to_tickers.keys(), key=len, reverse=True)
        escaped = [re.escape(t) for t in sorted_terms]
        # Pre/post negative lookaround on letters — deliberately allows matches
        # adjacent to digits and punctuation (e.g. "Tencent." or "(Tencent)").
        self._pattern = re.compile(
            r"(?<![A-Za-z])(?:" + "|".join(escaped) + r")(?![A-Za-z])",
            re.IGNORECASE,
        )
        self._term_to_tickers = term_to_tickers

    def match(self, text: str, max_tags: int = 5) -> list[str]:
        """Return up to `max_tags` tickers mentioned in text.

        Tie-breaking when more than max_tags hit:
          1. Watchlist tickers come first (richer alias coverage = higher signal).
          2. Within a tier, more matches in the article = higher rank.
          3. Stable alphabetical for determinism.
        """
        if self._pattern is None or not text:
            return []
        match_counts: dict[str, int] = {}
        for m in self._pattern.finditer(text):
            term = m.group(0).lower()
            for ticker in self._term_to_tickers.get(term, ()):
                match_counts[ticker] = match_counts.get(ticker, 0) + 1
        if not match_counts:
            return []
        ranked = sorted(
            match_counts.items(),
            key=lambda kv: (kv[0] not in self.watchlist_tickers, -kv[1], kv[0]),
        )
        return [ticker for ticker, _ in ranked[:max_tags]]
