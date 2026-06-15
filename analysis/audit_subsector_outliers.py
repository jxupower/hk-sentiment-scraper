"""One-shot audit of sub-sector outliers.

Walks every sub-sector with >= MIN_SECTOR_SIZE viable tickers. For each one,
computes the Value / Quality / Growth composite signals using the same code
path that the Discovery percentile engine uses, then identifies the top and
bottom tail (default 5%). For each outlier ticker, suggests a possible
better-fit sub-sector based on the yf_industry -> sub_sector mapping in
config/sub_sectors.yaml (with ticker_overrides taking precedence).

Output: a single markdown report per run. Re-run after taxonomy edits to see
which candidates resolved and which remain.
"""
import math
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import yaml

from analysis.data_loader import get_universe_fundamentals
from analysis.factor_scores import FactorScoringEngine, MIN_SECTOR_SIZE
from storage.database import Database


def run_audit(db_path: str, sub_sectors_yaml: str,
               output_md_path: str, tail_pct: float = 0.05,
               watchlist_yaml: str = "config/watchlist.yaml") -> str:
    """Walk every viable sub-sector bucket, identify V/Q/G tail outliers,
    write a markdown report. Returns the path written."""
    engine = FactorScoringEngine(db_path)
    fund_rows = get_universe_fundamentals(Database(db_path))

    # Apply the same viability gate FactorScoringEngine.compute() uses so the
    # bucket counts in the report match what the live engine actually ranks.
    viable = [f for f in fund_rows if not engine._viability_check(f)]
    by_sub: dict[str, list[dict]] = defaultdict(list)
    for f in viable:
        sub = f.get("sub_sector")
        if sub:
            by_sub[sub].append(f)

    # Recommendation tables. Sub_sectors.yaml's ticker_overrides AND
    # watchlist.yaml's per-ticker `sub_sector:` field are both editorial
    # endorsements of a specific bucket; when they MATCH the current sub_sector
    # we treat the ticker as "user-endorsed" and suppress the yf_industry
    # mapping recommendation (which would otherwise be a false-positive
    # suggesting to move e.g. Alibaba out of Platforms & Cloud back to
    # Internet Retail, or NIO out of Auto Tech back to Auto Manufacturers).
    with open(sub_sectors_yaml, encoding="utf-8") as fp:
        cfg = yaml.safe_load(fp) or {}
    industry_to_sub = cfg.get("industry_to_subsector", {}) or {}
    ticker_overrides = cfg.get("ticker_overrides", {}) or {}

    # Watchlist YAML per-ticker sub_sector endorsements (highest priority in
    # universe/reconciler.py:_resolve_sub_sector, so user has literally chosen
    # that bucket for the ticker — never suggest moving it).
    watchlist_endorsed: dict[str, str] = {}
    try:
        with open(watchlist_yaml, encoding="utf-8") as fp:
            wcfg = yaml.safe_load(fp) or {}
        for entries in (wcfg.get("sectors") or {}).values():
            for entry in entries or []:
                t = entry.get("ticker")
                sub = entry.get("sub_sector")
                if t and sub:
                    watchlist_endorsed[t] = sub
    except FileNotFoundError:
        pass

    sections: list[str] = []
    total_outliers = 0
    for sub, bucket in sorted(by_sub.items()):
        if len(bucket) < MIN_SECTOR_SIZE:
            continue

        industries = Counter(f.get("yf_industry") or "—" for f in bucket)
        modal_industry, _ = industries.most_common(1)[0]

        # ticker -> {factor: "up"/"down"} accumulating tail membership
        outlier_tags: dict[str, dict[str, str]] = defaultdict(dict)
        for factor in ("value", "quality", "growth"):
            _, signal_map = engine._factor_signal_breakdown(factor, None, bucket)
            if len(signal_map) < MIN_SECTOR_SIZE:
                continue
            sorted_items = sorted(signal_map.items(), key=lambda kv: kv[1])
            n = len(sorted_items)
            tail = max(1, math.ceil(n * tail_pct))
            for t, _ in sorted_items[:tail]:
                outlier_tags[t][factor] = "down"
            for t, _ in sorted_items[-tail:]:
                outlier_tags[t][factor] = "up"

        if not outlier_tags:
            continue

        by_ticker_row = {f["ticker"]: f for f in bucket}
        rows = []
        for tick in sorted(outlier_tags.keys()):
            row = by_ticker_row.get(tick, {})
            yf_industry = row.get("yf_industry") or "—"
            tags = ", ".join(
                f"{factor[0].upper()}{'⬆' if d == 'up' else '⬇'}"
                for factor, d in outlier_tags[tick].items()
            )
            rec = _recommend(tick, yf_industry, sub, modal_industry,
                              ticker_overrides, industry_to_sub,
                              watchlist_endorsed)
            rows.append((tick, (row.get("name") or tick)[:30],
                         yf_industry, tags, rec))

        section = [
            f"## {sub} "
            f"({len(bucket)} viable tickers · modal industry: {modal_industry})",
            "",
            "| Ticker | Name | yf_industry | Outliers | Recommendation |",
            "|---|---|---|---|---|",
        ]
        for tick, name, ind, tags, rec in rows:
            section.append(f"| {tick} | {name} | {ind} | {tags} | {rec} |")
        section.append("")
        sections.append("\n".join(section))
        total_outliers += len(rows)

    today = date.today().isoformat()
    header = [
        f"# Sub-Sector Outlier Audit — {today}",
        "",
        f"**Outlier rule**: top {int(tail_pct * 100)}% or bottom "
        f"{int(tail_pct * 100)}% of any Value / Quality / Growth composite "
        "signal within the sub-sector bucket.",
        "",
        "**Tags**: V/Q/G followed by ⬆ (top tail — high signal = cheap / "
        "profitable / fast-growing) or ⬇ (bottom tail — low signal).",
        "",
        "**Recommendation priority**: config/sub_sectors.yaml `ticker_overrides` "
        "→ `industry_to_subsector[yf_industry]` → modal-industry comparison.",
        "",
        f"**Totals**: {len(sections)} sub-sectors with outliers · "
        f"{total_outliers} ticker rows.",
        "",
        "---",
        "",
    ]
    Path(output_md_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_md_path, "w", encoding="utf-8") as fp:
        fp.write("\n".join(header) + "\n".join(sections))
    return output_md_path


def _recommend(ticker: str, yf_industry: str, current_sub: str,
                modal_industry: str, ticker_overrides: dict,
                industry_to_sub: dict,
                watchlist_endorsed: dict) -> str:
    """Pick the strongest taxonomy signal pointing to a different sub-sector.
    Priority:
      1. Watchlist or ticker_override explicitly disagrees with current_sub
         → recommend the move (this would mean the reconciler got stale).
      2. Watchlist or ticker_override matches current_sub → user-endorsed;
         suppress further suggestions even if yf_industry would map elsewhere.
      3. industry_to_subsector mapping disagrees with current_sub → suggest move.
      4. Modal-industry mismatch → review flag.
      5. Otherwise → keep.
    """
    wl_endorsed = watchlist_endorsed.get(ticker)
    override = ticker_overrides.get(ticker) or {}
    ov_sub = override.get("sub_sector")

    # 1. Editorial disagreement with reality (sanity check — should be rare)
    if wl_endorsed and wl_endorsed != current_sub:
        return f"**move → {wl_endorsed}** (watchlist YAML)"
    if ov_sub and ov_sub != current_sub:
        return f"**move → {ov_sub}** (sub_sectors override)"

    # 2. Editorial endorsement of the current bucket — user has explicitly
    # chosen this sub-sector, so don't second-guess based on yf_industry.
    if wl_endorsed == current_sub or ov_sub == current_sub:
        return "keep — user-endorsed (editorial)"

    # 3. yfinance industry mapping points elsewhere
    mapped = industry_to_sub.get(yf_industry)
    if mapped and mapped != current_sub:
        return f"**move → {mapped}** (yf_industry mapping)"

    # 4. Modal-industry heuristic
    if yf_industry and yf_industry != modal_industry and yf_industry != "—":
        return (f"review — yf_industry '{yf_industry}' atypical "
                 f"(bucket modal: '{modal_industry}')")

    # 5. Nothing actionable
    return "keep — no taxonomy alternative"


if __name__ == "__main__":
    today = date.today().isoformat()
    out = run_audit(
        db_path="data/sentiment.db",
        sub_sectors_yaml="config/sub_sectors.yaml",
        output_md_path=f"data/audit_subsector_outliers_{today}.md",
    )
    print(f"Audit written to {out}")
