import json
from pathlib import Path
from typing import Optional

import yaml

from storage.repository import SecuritiesRepository
from utils.logger import get_logger

logger = get_logger(__name__)


_SUB_SECTORS_PATH = Path(__file__).parent.parent / "config" / "sub_sectors.yaml"


def reconcile(securities_repo: SecuritiesRepository, hkex_records: list[dict],
              watchlist: dict) -> dict:
    """Sync HKEX records and watchlist YAML into the `securities` table.

    Order matters:
      1. Upsert all HKEX rows (creates/updates name, listing_category, lot_size,
         force-sets is_active=1).
      2. Clear watchlist flags on every row (so removals from YAML take effect).
      3. Walk watchlist YAML and set is_watchlist + watchlist_sector + aliases_json.
         If a watchlist ticker is not in HKEX, insert it as a manual override row
         and log a warning.
      4. Deactivate any active row whose ticker isn't in the current HKEX list or
         the YAML watchlist (i.e. delisted equities that the upserts didn't touch).
         Skipped when hkex_records is empty (offline-fallback path) so we don't
         wrongly deactivate everything. Scoped to market='HK' so US rows are
         left alone.

    Returns a summary dict for the CLI to print.
    """
    for rec in hkex_records:
        securities_repo.upsert_security(
            ticker=rec["ticker"],
            hkex_code=rec["hkex_code"],
            name=rec["name"],
            listing_category=rec["listing_category"],
            lot_size=rec["lot_size"],
            market="HK",
        )
    logger.info("Upserted %d HKEX rows into securities", len(hkex_records))

    # Watchlist flags are cleared then re-applied — but ONLY for HK rows.
    # The US-side watchlist (if/when added) clears+re-applies via reconcile_us.
    securities_repo.clear_watchlist_flags_for_market("HK")

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

    deactivated = 0
    if hkex_records:
        yaml_tickers = {entry["ticker"]
                        for entries in watchlist.get("sectors", {}).values()
                        for entry in entries}
        current_set = hkex_tickers | yaml_tickers
        deactivated = securities_repo.deactivate_missing(current_set, market="HK")
        if deactivated:
            logger.warning("Deactivated %d delisted HK ticker(s) no longer in HKEX list", deactivated)

    # Sub-sector resolution pass — runs over every active row in securities
    # and writes sub_sector + effective_sector based on the config layered
    # over yfinance's industry classification. Idempotent, ~1s.
    sub_sector_summary = _reconcile_sub_sectors(securities_repo, watchlist)

    summary = {
        "total": securities_repo.count_all(),
        "watchlist": securities_repo.count_watchlist(),
        "hkex_ingested": len(hkex_records),
        "watchlist_in_yaml": watchlist_count,
        "missing_from_hkex": missing_from_hkex,
        "deactivated": deactivated,
        **sub_sector_summary,
    }
    logger.info("Reconcile summary: %s", summary)
    return summary


def reconcile_us(securities_repo: SecuritiesRepository, us_records: list[dict],
                  watchlist_us: Optional[dict] = None) -> dict:
    """Sync US records (Russell 3000 from iShares, or Wikipedia fallback) into
    the `securities` table with market='US'.

    Mirrors reconcile() but adapted to US conventions:
      * `hkex_code` left empty (the schema now allows NULL on this column)
      * `lot_size` = 1
      * `listing_category` = 'Equity'
      * Watchlist application is optional — pass `watchlist_us={}` if there
        isn't one yet. When provided, same priority as HK: watchlist takes
        precedence over universe data for is_watchlist + aliases.
      * Deactivation is scoped to market='US' so HK rows are never touched.
      * Sub-sector resolution runs the same global mapping (yfinance industry
        strings are global), so no separate config needed.

    Returns a summary dict suitable for the CLI to print.
    """
    if watchlist_us is None:
        watchlist_us = {}

    for rec in us_records:
        securities_repo.upsert_security(
            ticker=rec["ticker"],
            hkex_code=rec.get("hkex_code") or "",
            name=rec["name"],
            listing_category=rec.get("listing_category") or "Equity",
            lot_size=rec.get("lot_size") or 1,
            market="US",
        )
    logger.info("Upserted %d US rows into securities", len(us_records))

    securities_repo.clear_watchlist_flags_for_market("US")

    us_tickers = {r["ticker"] for r in us_records}
    missing_from_universe: list[str] = []
    watchlist_count = 0
    for sector, entries in (watchlist_us.get("sectors") or {}).items():
        for entry in entries:
            ticker = entry["ticker"]
            primary_name = entry.get("name") or ticker
            raw_aliases = entry.get("aliases", []) or []
            terms = [primary_name] + [a for a in raw_aliases if a != primary_name]
            aliases_json = json.dumps(terms)
            if ticker not in us_tickers:
                missing_from_universe.append(ticker)
                securities_repo.set_watchlist(
                    ticker=ticker, sector=sector, aliases_json=aliases_json,
                    hkex_code="", name=entry.get("name", ticker),
                )
                # The set_watchlist insert path doesn't set market — patch it.
                securities_repo.set_market(ticker, "US")
            else:
                securities_repo.set_watchlist(
                    ticker=ticker, sector=sector, aliases_json=aliases_json,
                )
            watchlist_count += 1

    if missing_from_universe:
        logger.warning("US watchlist tickers not in the universe list "
                       "(inserted as overrides): %s", missing_from_universe)

    deactivated = 0
    if us_records:
        wl_tickers = {entry["ticker"]
                       for entries in (watchlist_us.get("sectors") or {}).values()
                       for entry in entries}
        current_set = us_tickers | wl_tickers
        deactivated = securities_repo.deactivate_missing(current_set, market="US")
        if deactivated:
            logger.warning("Deactivated %d US ticker(s) no longer in the universe",
                            deactivated)

    sub_sector_summary = _reconcile_sub_sectors(securities_repo, watchlist_us)

    summary = {
        "total_us": securities_repo.count_active_for_market("US"),
        "us_ingested": len(us_records),
        "watchlist_us": watchlist_count,
        "missing_from_universe": missing_from_universe,
        "deactivated": deactivated,
        **sub_sector_summary,
    }
    logger.info("US reconcile summary: %s", summary)
    return summary


# ============================================================================
# Sub-sector resolution
# ============================================================================

def _load_sub_sectors_config() -> dict:
    """Load config/sub_sectors.yaml. Returns an empty dict (no overrides,
    no industry mapping) if the file is missing — keeps the reconciler
    backwards-compatible with deployments that haven't shipped the config yet."""
    if not _SUB_SECTORS_PATH.exists():
        logger.warning("config/sub_sectors.yaml missing — sub-sector "
                       "resolution will leave all rows NULL")
        return {}
    with open(_SUB_SECTORS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _build_watchlist_sub_sector_map(watchlist: dict) -> dict[str, str]:
    """Index the watchlist YAML by ticker → sub_sector. Used as the highest
    priority source in sub-sector resolution. Only includes entries that
    have a `sub_sector` field set explicitly."""
    out: dict[str, str] = {}
    for entries in (watchlist.get("sectors") or {}).values():
        for entry in entries:
            sub = entry.get("sub_sector")
            if sub:
                out[entry["ticker"]] = sub
    return out


def _resolve_sub_sector(ticker: str, yf_industry: Optional[str],
                         watchlist_sub_map: dict[str, str],
                         ticker_overrides: dict,
                         industry_map: dict) -> Optional[str]:
    """Resolution priority (first match wins):
      1. Watchlist YAML per-ticker `sub_sector`
      2. ticker_overrides[ticker].sub_sector
      3. industry_to_subsector[yf_industry]
      4. None
    """
    if ticker in watchlist_sub_map:
        return watchlist_sub_map[ticker]
    override = ticker_overrides.get(ticker) or {}
    if override.get("sub_sector"):
        return override["sub_sector"]
    if yf_industry and yf_industry in industry_map:
        return industry_map[yf_industry]
    return None


def _resolve_effective_sector(ticker: str, watchlist_sector: Optional[str],
                                yf_sector: Optional[str],
                                ticker_overrides: dict) -> Optional[str]:
    """Resolve the PARENT sector — what factor-scoring/analysis should treat
    as the broad bucket. Priority:
      1. ticker_overrides[ticker].parent_sector — explicit cross-sector
         promotion / demotion (e.g. BYD: Consumer Cyclical → Technology).
      2. yf_sector — yfinance's parent classification, the authoritative source.
      3. watchlist_sector — last-resort fallback for tickers without
         yfinance metadata. NOTE: watchlist_sector is now sub-sector-grained
         (e.g. "Platforms & Cloud Infrastructure"), so using it as a parent
         is a degradation but better than NULL.
    """
    override = ticker_overrides.get(ticker) or {}
    if override.get("parent_sector"):
        return override["parent_sector"]
    return yf_sector or watchlist_sector


def _reconcile_sub_sectors(securities_repo: SecuritiesRepository,
                             watchlist: dict) -> dict:
    """Walk every active security and write resolved (sub_sector,
    effective_sector). Returns a small counts dict for the summary log."""
    config = _load_sub_sectors_config()
    industry_map: dict = config.get("industry_to_subsector") or {}
    ticker_overrides: dict = config.get("ticker_overrides") or {}
    watchlist_sub_map = _build_watchlist_sub_sector_map(watchlist)

    active_rows = securities_repo.get_all_active()
    updates: list[tuple] = []
    n_sub_assigned = 0
    n_effective_changed = 0
    for row in active_rows:
        ticker = row["ticker"]
        sub_sector = _resolve_sub_sector(
            ticker, row.get("yf_industry"),
            watchlist_sub_map, ticker_overrides, industry_map,
        )
        effective_sector = _resolve_effective_sector(
            ticker, row.get("watchlist_sector"), row.get("yf_sector"),
            ticker_overrides,
        )
        # Only enqueue an update when something actually changed — keeps the
        # transaction small and the per-row work minimal.
        if (sub_sector != row.get("sub_sector")
                or effective_sector != row.get("effective_sector")):
            updates.append((ticker, sub_sector, effective_sector))
        if sub_sector:
            n_sub_assigned += 1
        if effective_sector and effective_sector != (row.get("watchlist_sector")
                                                       or row.get("yf_sector")):
            n_effective_changed += 1

    n_updated = securities_repo.bulk_set_sub_sector(updates)
    logger.info(
        "Sub-sector reconcile: %d active rows · %d sub_sector assigned · "
        "%d effective_sector overridden · %d rows updated",
        len(active_rows), n_sub_assigned, n_effective_changed, n_updated,
    )
    return {
        "sub_sector_assigned": n_sub_assigned,
        "effective_sector_overridden": n_effective_changed,
        "sub_sector_rows_updated": n_updated,
    }
