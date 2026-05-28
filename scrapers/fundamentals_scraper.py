import math
import time
from datetime import date
from typing import Optional

import yfinance as yf

from utils.logger import get_logger

logger = get_logger(__name__)


def _coerce_finite(v):
    """Return v as a finite float, or None for None / NaN / Infinity / non-numeric.

    yfinance occasionally returns math.inf or the string 'Infinity' for tickers
    with extreme P/E (tiny positive earnings denominator). Storing those values
    poisons downstream consumers (round(), comparisons in composite scoring).
    """
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f

# yfinance .info field name → our column name. Order matters for completeness scoring.
# Core valuation + balance-sheet ratios — same fields used since Phase 2.
RATIO_FIELDS = {
    "trailingPE":         "trailing_pe",
    "forwardPE":          "forward_pe",
    "priceToBook":        "price_to_book",
    "enterpriseToEbitda": "ev_to_ebitda",
    "dividendYield":      "dividend_yield",
    "marketCap":          "market_cap",
    "beta":               "beta",
    "returnOnEquity":     "return_on_equity",
    "debtToEquity":       "debt_to_equity",
}

# Direction C additions: growth + quality + liquidity fields used by the new
# multi-factor ScoreEngine and rule-based screens. Empirically confirmed
# reliable for HK names (large + mid cap; small caps sparser).
EXTENDED_FIELDS = {
    "earningsGrowth":   "earnings_growth",    # YoY earnings growth, fraction
    "revenueGrowth":    "revenue_growth",     # YoY revenue growth, fraction
    "profitMargins":    "profit_margins",     # net profit margin, fraction
    "operatingMargins": "operating_margins",  # operating margin, fraction
    "returnOnAssets":   "return_on_assets",   # ROA, fraction
    "currentRatio":     "current_ratio",      # liquidity ratio
    "freeCashflow":     "free_cashflow",      # FCF in local currency
    # Backtest stage 1: per-share metrics so historical snapshots can produce
    # as-of P/E and P/B by joining with historical_prices. yfinance live
    # snapshots populate them alongside trailing_pe etc.
    "trailingEps":      "eps_ttm",            # trailing 12-month EPS
    "bookValue":        "bps",                # book value per share
    "sharesOutstanding": "shares_outstanding",
}


class FundamentalsScraper:
    """Pulls yfinance .info ratios per ticker. NOT a BaseScraper — returns rows, not articles.

    Rate-limited by `throttle_seconds` between requests. Default 1.5s gives ~2,400 req/hr,
    above the documented ~360/hr limit but in line with what the community reports working
    for HK tickers in practice. Bump higher (e.g. 5-10s) if 429s start appearing.
    """

    def __init__(self, throttle_seconds: float = 1.5):
        self.throttle = throttle_seconds

    def fetch_one(self, ticker: str) -> Optional[dict]:
        """Returns a snapshot dict suitable for FundamentalsRepository.upsert_snapshot,
        or None if yfinance fails entirely. Per-field None values are normal for small-caps."""
        try:
            t = yf.Ticker(ticker)
            info = t.info or {}
        except Exception as e:
            logger.warning("yfinance .info failed [%s]: %s", ticker, e)
            return None

        snapshot = {}
        non_null_count = 0
        # Core ratios drive the data_completeness score (kept stable for backward compat).
        for src_key, col in RATIO_FIELDS.items():
            value = _coerce_finite(info.get(src_key))
            if value is not None:
                non_null_count += 1
            snapshot[col] = value
        # Extended fields (Direction C) are stored but don't affect completeness scoring.
        for src_key, col in EXTENDED_FIELDS.items():
            snapshot[col] = _coerce_finite(info.get(src_key))

        snapshot["last_price"] = _coerce_finite(
            info.get("currentPrice")
            or info.get("regularMarketPrice")
            or info.get("previousClose")
        )
        snapshot["currency"] = info.get("currency")
        snapshot["data_completeness"] = round(non_null_count / len(RATIO_FIELDS), 3)
        snapshot["yf_sector"] = info.get("sector")
        snapshot["yf_industry"] = info.get("industry")
        return snapshot

    def fetch_many(self, tickers: list[str], fundamentals_repo,
                   skip_if_today_exists: bool = True) -> dict:
        """Loop over tickers, write each snapshot to the repo immediately so a crash
        mid-run doesn't lose progress. Returns a summary dict."""
        today = date.today().isoformat()
        attempted, written, skipped, failed = 0, 0, 0, 0

        for i, ticker in enumerate(tickers, start=1):
            attempted += 1
            if skip_if_today_exists and fundamentals_repo.has_snapshot_for_date(ticker, today):
                skipped += 1
                continue

            snapshot = self.fetch_one(ticker)
            if snapshot is None:
                failed += 1
            else:
                fundamentals_repo.upsert_snapshot(ticker, today, snapshot)
                fundamentals_repo.update_security_yf_metadata(
                    ticker, snapshot.get("yf_sector"), snapshot.get("yf_industry"),
                )
                written += 1

            if i % 25 == 0:
                logger.info("Fundamentals progress: %d/%d (written=%d, skipped=%d, failed=%d)",
                            i, len(tickers), written, skipped, failed)
            time.sleep(self.throttle)

        summary = {"attempted": attempted, "written": written,
                   "skipped": skipped, "failed": failed}
        logger.info("Fundamentals refresh complete: %s", summary)
        return summary
