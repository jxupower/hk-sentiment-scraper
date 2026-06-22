"""Seed `securities_meta` (Chinese + English name) per ticker.

Reuses akshare's per-ticker fundamentals endpoints (which return
`SECURITY_NAME_ABBR` as a freebie) so we don't depend on the
bulk-spot endpoints (`stock_hk_spot_em`, `stock_us_spot_em`) which
disconnect frequently. English names come from the existing
`securities.name` column populated at universe ingest.

For tickers without a Chinese name in akshare (US Financials sector,
odd share classes, micro caps), the row gets `chinese_name=NULL` and
the lookup falls back to English at read time. Run the seed
periodically — re-runs only update changed rows thanks to the COALESCE
upsert clause.
"""
from __future__ import annotations

import time
from typing import Optional

import akshare as ak

from utils.logger import get_logger

logger = get_logger(__name__)


def _hk_ak_symbol(ticker: str) -> str:
    """0700.HK → 00700 (akshare HK form)."""
    code = ticker.split(".")[0]
    try:
        return f"{int(code):05d}"
    except (ValueError, TypeError):
        return code


def _us_ak_symbol(ticker: str) -> str:
    """AAPL → AAPL; BRK-B → BRK_B (akshare US uses underscore for share classes)."""
    return (ticker or "").strip().upper().replace("-", "_")


def _fetch_chinese_name_hk(ticker: str) -> Optional[str]:
    try:
        df = ak.stock_financial_hk_analysis_indicator_em(
            symbol=_hk_ak_symbol(ticker), indicator="年度",
        )
    except Exception:
        return None
    if df is None or df.empty or "SECURITY_NAME_ABBR" not in df.columns:
        return None
    name = df.iloc[0].get("SECURITY_NAME_ABBR")
    if name is None:
        return None
    s = str(name).strip()
    return s if s else None


def _fetch_chinese_name_us(ticker: str) -> Optional[str]:
    try:
        df = ak.stock_financial_us_analysis_indicator_em(
            symbol=_us_ak_symbol(ticker), indicator="年报",
        )
    except Exception:
        return None
    if df is None or df.empty or "SECURITY_NAME_ABBR" not in df.columns:
        return None
    name = df.iloc[0].get("SECURITY_NAME_ABBR")
    if name is None:
        return None
    s = str(name).strip()
    return s if s else None


def seed_names_for_market(securities_repo, names_repo,
                            market: str,
                            throttle_seconds: float = 0.3,
                            skip_already_seeded: bool = True) -> dict:
    """Iterate active tickers in the given market, fetch Chinese names
    from akshare, upsert into `securities_meta`. English names come from
    the existing `securities.name` column.

    `skip_already_seeded=True` (default) skips tickers that already have
    BOTH english + chinese names recorded — re-runs become near-instant
    after the first pass. Set False to force a refresh.

    Returns summary dict for the CLI to print."""
    market = (market or "HK").upper()
    rows = securities_repo.get_all_active(market=market)
    targets = [(r["ticker"], r["name"]) for r in rows]

    # Skip already-seeded if requested
    if skip_already_seeded:
        from storage.database import Database
        # Need the underlying connection to ask which tickers already have both names.
        db = names_repo.db
        with db.get_connection() as conn:
            existing = {
                r[0] for r in conn.execute(
                    "SELECT ticker FROM securities_meta "
                    "WHERE english_name IS NOT NULL AND chinese_name IS NOT NULL"
                ).fetchall()
            }
        targets = [(t, n) for (t, n) in targets if t not in existing]

    fetch = _fetch_chinese_name_us if market == "US" else _fetch_chinese_name_hk

    attempted = 0
    with_chinese = 0
    failed = 0
    batch: list[dict] = []
    BATCH_SIZE = 100

    for i, (ticker, english) in enumerate(targets, start=1):
        attempted += 1
        try:
            cn = fetch(ticker)
        except Exception as e:  # noqa: BLE001 — never let one ticker kill the seed
            logger.warning("name fetch crashed for %s: %s", ticker, e)
            cn = None
            failed += 1
        if cn:
            with_chinese += 1
        batch.append({"ticker": ticker, "english_name": english, "chinese_name": cn})
        if len(batch) >= BATCH_SIZE:
            names_repo.bulk_upsert(batch)
            batch.clear()
        if i % 25 == 0:
            logger.info(
                "names seed [%s] %d/%d (chinese=%d, failed=%d)",
                market, i, len(targets), with_chinese, failed,
            )
        time.sleep(throttle_seconds)

    if batch:
        names_repo.bulk_upsert(batch)

    summary = {
        "market": market,
        "attempted": attempted,
        "with_chinese": with_chinese,
        "without_chinese": attempted - with_chinese - failed,
        "failed": failed,
        "skipped_already_seeded": (len(rows) - attempted) if skip_already_seeded else 0,
    }
    logger.info("names seed [%s] complete: %s", market, summary)
    return summary
