"""Backfill `securities.yf_sector` + `securities.yf_industry` for US
tickers via yfinance `.info`. **Sector + industry fields only** — does NOT
touch any of the financial-ratio fields (market_cap, trailing_pe, etc.)
which have data-quality issues. Sector + industry are reliable.

Once these two columns are populated, re-running
`python main.py universe-us seed` triggers the reconciler's
`_reconcile_sub_sectors` which derives `sub_sector` and `effective_sector`
via the existing global `industry_to_subsector` map — no other code paths
need to change.

Run: `python main.py universe-us refresh-sectors [--throttle 0.5]
                                                 [--force-all]`
"""
from __future__ import annotations

import time
from typing import Optional

import yfinance as yf

from utils.logger import get_logger

logger = get_logger(__name__)


def _fetch_sector_industry(ticker: str) -> tuple[Optional[str], Optional[str]]:
    """Return (yf_sector, yf_industry) or (None, None) on miss/error."""
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception:
        return None, None
    sector = info.get("sector")
    industry = info.get("industry")
    if isinstance(sector, str) and not sector.strip():
        sector = None
    if isinstance(industry, str) and not industry.strip():
        industry = None
    return sector, industry


def fetch_many(tickers: list[str], securities_repo,
                throttle_seconds: float = 0.5,
                progress_every: int = 25) -> dict:
    """Loop over US tickers; write `(yf_sector, yf_industry)` to each row.

    Returns summary dict: `{attempted, tagged, both_null, errors}`.
    """
    attempted = 0
    tagged = 0
    both_null = 0
    errors = 0

    for i, ticker in enumerate(tickers, start=1):
        attempted += 1
        try:
            sector, industry = _fetch_sector_industry(ticker)
        except Exception as e:  # noqa: BLE001 — never let one ticker kill the run
            logger.warning("sector fetch crashed for %s: %s", ticker, e)
            errors += 1
            time.sleep(throttle_seconds)
            continue

        if sector is None and industry is None:
            both_null += 1
        else:
            try:
                securities_repo.set_yf_classification(ticker, sector, industry)
                tagged += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("yf classification write failed for %s: %s", ticker, e)
                errors += 1

        if i % progress_every == 0:
            logger.info(
                "us-sector backfill: %d/%d (tagged=%d, both_null=%d, errors=%d)",
                i, len(tickers), tagged, both_null, errors,
            )
        time.sleep(throttle_seconds)

    summary = {
        "attempted": attempted,
        "tagged": tagged,
        "both_null": both_null,
        "errors": errors,
    }
    logger.info("us-sector backfill complete: %s", summary)
    return summary
