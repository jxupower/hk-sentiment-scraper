import json

from storage.repository import SecuritiesRepository
from utils.logger import get_logger

logger = get_logger(__name__)


def reconcile(securities_repo: SecuritiesRepository, hkex_records: list[dict],
              watchlist: dict) -> dict:
    """Sync HKEX records and watchlist YAML into the `securities` table.

    Order matters:
      1. Upsert all HKEX rows (creates/updates name, listing_category, lot_size).
      2. Clear watchlist flags on every row (so removals from YAML take effect).
      3. Walk watchlist YAML and set is_watchlist + watchlist_sector + aliases_json.
         If a watchlist ticker is not in HKEX, insert it as a manual override row
         and log a warning.

    Returns a summary dict for the CLI to print.
    """
    for rec in hkex_records:
        securities_repo.upsert_security(
            ticker=rec["ticker"],
            hkex_code=rec["hkex_code"],
            name=rec["name"],
            listing_category=rec["listing_category"],
            lot_size=rec["lot_size"],
        )
    logger.info("Upserted %d HKEX rows into securities", len(hkex_records))

    securities_repo.clear_watchlist_flags()

    hkex_tickers = {r["ticker"] for r in hkex_records}
    missing_from_hkex: list[str] = []
    watchlist_count = 0
    for sector, entries in watchlist.get("sectors", {}).items():
        for entry in entries:
            ticker = entry["ticker"]
            primary_name = entry.get("name") or ticker
            raw_aliases = entry.get("aliases", []) or []
            # Prepend the canonical YAML name so the matcher always picks it up
            # (YAML's "Alibaba" is more useful for matching than HKEX's "BABA-W").
            terms = [primary_name] + [a for a in raw_aliases if a != primary_name]
            aliases_json = json.dumps(terms)
            if ticker not in hkex_tickers:
                missing_from_hkex.append(ticker)
                # Insert as a manual override so existing pipeline keeps working
                hkex_code = ticker.split(".")[0] if "." in ticker else ticker
                securities_repo.set_watchlist(
                    ticker=ticker, sector=sector, aliases_json=aliases_json,
                    hkex_code=hkex_code, name=entry.get("name", ticker),
                )
            else:
                securities_repo.set_watchlist(
                    ticker=ticker, sector=sector, aliases_json=aliases_json,
                )
            watchlist_count += 1

    if missing_from_hkex:
        logger.warning("Watchlist tickers not present in HKEX list (inserted as overrides): %s",
                       missing_from_hkex)

    summary = {
        "total": securities_repo.count_all(),
        "watchlist": securities_repo.count_watchlist(),
        "hkex_ingested": len(hkex_records),
        "watchlist_in_yaml": watchlist_count,
        "missing_from_hkex": missing_from_hkex,
    }
    logger.info("Reconcile summary: %s", summary)
    return summary
