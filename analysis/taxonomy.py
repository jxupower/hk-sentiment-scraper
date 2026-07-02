"""Compiled sector/sub-sector taxonomy — source-of-truth → DB → in-memory.

THREE LAYERS:

1. SOURCE — config/sub_sectors.yaml, config/us_sectors.yaml,
   config/us_size_splits.yaml. Edited by humans, reviewed via PR. The rules
   for industry → sub-sector mapping, ticker overrides, market-cap splits,
   and bilingual display labels all live here.

2. COMPILED — `sector_taxonomy` + `taxonomy_meta` DB tables (local SQLite +
   Supabase mirror). Written by `compile_taxonomy(write_to_db=True)` after
   Pydantic validates the source YAMLs. Validation catches the entire class
   of dangling-reference / drift bugs at edit time rather than at render
   time. Re-running compile is idempotent.

3. RUNTIME — `get_taxonomy()` returns a process-wide `Taxonomy` singleton
   loaded from the DB once. All lookups (label, parent_of, children_of)
   are O(1) dict reads. The singleton TTL-checks `taxonomy_meta.version`
   every 5 min so a separate `taxonomy compile` invocation eventually
   propagates without a restart. Use `refresh_taxonomy()` for immediate
   invalidation in the same process.

PHASE A SCOPE (this file): Pydantic schema + Taxonomy dataclass + compile
+ runtime singleton. The reconciler continues to read source YAMLs
directly for the rule layer (industry_to_subsector / ticker_overrides) —
Phase B migrates the reconciler to use this module as well.

The Pydantic schema validates referential integrity across all yamls:
  - every sub-sector referenced anywhere must exist in sub_sector_to_parent
  - every parent must exist in parent_sectors_zh
  - every canonical name must have both EN and ZH translations
  - ticker_overrides + us_sectors.yaml + us_size_splits must reference
    canonical names only
A typo or dangling reference aborts the compile with the offending key.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Optional

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

from storage.database import Database

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).parent.parent / "config"
_SUB_SECTORS_PATH    = _CONFIG_DIR / "sub_sectors.yaml"
_US_SECTORS_PATH     = _CONFIG_DIR / "us_sectors.yaml"
_US_SIZE_SPLITS_PATH = _CONFIG_DIR / "us_size_splits.yaml"

_DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "sentiment.db"

# Singleton state for the runtime accessor. Refresh interval is generous
# (5 min) because taxonomy edits are rare; refresh_taxonomy() forces an
# immediate reload in the same process when needed.
_LOCK = RLock()
_CACHED: Optional["Taxonomy"] = None
_CACHED_VERSION: Optional[str] = None
_CACHED_AT: float = 0.0
_REFRESH_INTERVAL_SECONDS: float = 5 * 60


# ============================================================================
# Pydantic source-YAML schemas + cross-file validation
# ============================================================================

class _TickerOverride(BaseModel):
    """Single entry under config/sub_sectors.yaml ticker_overrides."""
    model_config = ConfigDict(extra="forbid")
    parent_sector: Optional[str] = None
    sub_sector:    Optional[str] = None


class _SizeSplitTier(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sub_sector:         str
    min_market_cap_usd: float


class _SourceTaxonomy(BaseModel):
    """All source YAMLs unioned + validated for referential integrity."""
    model_config = ConfigDict(extra="ignore")
    industry_to_subsector: dict[str, str]
    sub_sector_to_parent:  dict[str, str]
    parent_sectors_zh:     dict[str, str]
    sub_sectors_zh:        dict[str, str]
    ticker_overrides:      dict[str, _TickerOverride] = {}
    us_overrides:          dict[str, dict]            = {}
    us_size_splits:        dict[str, list[_SizeSplitTier]] = {}
    watchlist_subs:        dict[str, str] = {}    # ticker -> sub_sector from any watchlist YAML

    @model_validator(mode="after")
    def _check_integrity(self) -> "_SourceTaxonomy":
        canonical_subs    = set(self.sub_sector_to_parent.keys())
        canonical_parents = set(self.parent_sectors_zh.keys())

        # 1. industry_to_subsector targets must be canonical subs
        dangling = set(self.industry_to_subsector.values()) - canonical_subs
        if dangling:
            raise ValueError(
                f"industry_to_subsector references unknown sub-sectors: {sorted(dangling)}"
            )

        # 2. sub_sector_to_parent values must be canonical parents
        missing_parents = set(self.sub_sector_to_parent.values()) - canonical_parents
        if missing_parents:
            raise ValueError(
                f"sub_sector_to_parent references unknown parents: {sorted(missing_parents)}"
            )

        # 3. Every canonical sub must have a zh translation
        missing_zh_subs = canonical_subs - set(self.sub_sectors_zh.keys())
        if missing_zh_subs:
            raise ValueError(
                f"sub-sectors missing zh translation: {sorted(missing_zh_subs)}"
            )

        # 4. sub_sectors_zh keys must be canonical (no orphan translations)
        orphan_zh = set(self.sub_sectors_zh.keys()) - canonical_subs
        if orphan_zh:
            raise ValueError(
                f"sub_sectors_zh has translations for unknown sub-sectors: {sorted(orphan_zh)}"
            )

        # 5. ticker_overrides targets must be canonical (when set)
        for ticker, ov in self.ticker_overrides.items():
            if ov.sub_sector and ov.sub_sector not in canonical_subs:
                raise ValueError(
                    f"ticker_overrides[{ticker}].sub_sector = {ov.sub_sector!r}"
                    f" is not a canonical sub-sector"
                )
            if ov.parent_sector and ov.parent_sector not in canonical_parents:
                raise ValueError(
                    f"ticker_overrides[{ticker}].parent_sector = {ov.parent_sector!r}"
                    f" is not a canonical parent sector"
                )

        # 6. us_overrides (from us_sectors.yaml) must reference canonical
        for ticker, ov in self.us_overrides.items():
            sub = ov.get("sub_sector")
            par = ov.get("parent_sector")
            if sub and sub not in canonical_subs:
                raise ValueError(
                    f"us_sectors.yaml[{ticker}].sub_sector = {sub!r} not canonical"
                )
            if par and par not in canonical_parents:
                raise ValueError(
                    f"us_sectors.yaml[{ticker}].parent_sector = {par!r} not canonical"
                )

        # 7. us_size_splits tiers must reference canonical sub-sectors
        for source_bucket, tiers in self.us_size_splits.items():
            if source_bucket not in canonical_subs:
                raise ValueError(
                    f"us_size_splits source bucket {source_bucket!r} not canonical"
                )
            for tier in tiers:
                if tier.sub_sector not in canonical_subs:
                    raise ValueError(
                        f"us_size_splits[{source_bucket}] tier {tier.sub_sector!r} not canonical"
                    )

        # 8. No name appears as both a parent and a sub-sector
        overlap = canonical_subs & canonical_parents
        if overlap:
            raise ValueError(
                f"Names appearing as BOTH parent and sub-sector: {sorted(overlap)}"
            )

        # 9. Watchlist YAMLs' explicit sub_sector field must be canonical.
        #    Catches stale references like 1088.HK: sub_sector="Thermal Coal"
        #    after Thermal Coal was merged into Coal.
        stale_in_watchlist = {
            t: sub for t, sub in self.watchlist_subs.items()
            if sub not in canonical_subs
        }
        if stale_in_watchlist:
            raise ValueError(
                f"watchlist YAMLs reference unknown sub_sectors: {stale_in_watchlist}"
            )

        return self


# ============================================================================
# Compiled in-memory representation
# ============================================================================

@dataclass(frozen=True)
class TaxonomyNode:
    canonical_name: str
    kind:           str           # 'parent' | 'sub'
    parent_name:    Optional[str]  # None for kind='parent'
    label_en:       str
    label_zh:       str
    display_order:  int


@dataclass(frozen=True)
class Taxonomy:
    """Process-wide immutable taxonomy snapshot. Returned by get_taxonomy().

    All lookups are O(1) dict reads. Unknown names fall back to themselves
    (so render code degrades gracefully if it sees an obsolete sub-sector
    label that's been removed from YAML)."""
    nodes:              dict[str, TaxonomyNode]
    children_by_parent: dict[str, tuple[str, ...]]
    version:            str

    def label(self, name: Optional[str], lang: str = "en") -> str:
        if not name:
            return ""
        node = self.nodes.get(name)
        if node is None:
            return name
        return node.label_zh if lang == "zh" else node.label_en

    def parent_of(self, sub_sector: Optional[str]) -> Optional[str]:
        if not sub_sector:
            return None
        node = self.nodes.get(sub_sector)
        return node.parent_name if node else None

    def children_of(self, parent: str) -> tuple[str, ...]:
        return self.children_by_parent.get(parent, ())

    def is_canonical(self, name: str) -> bool:
        return name in self.nodes

    def all_parents(self) -> tuple[str, ...]:
        return tuple(n.canonical_name for n in self.nodes.values() if n.kind == "parent")

    def all_subs(self) -> tuple[str, ...]:
        return tuple(n.canonical_name for n in self.nodes.values() if n.kind == "sub")


# ============================================================================
# YAML loading + compile
# ============================================================================

def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


_WATCHLIST_HK_PATH = _CONFIG_DIR / "watchlist.yaml"
_WATCHLIST_US_PATH = _CONFIG_DIR / "watchlist_us.yaml"


def _collect_watchlist_subs(*paths: Path) -> dict[str, str]:
    """Walk every watchlist YAML and pull out per-entry `sub_sector` fields.
    Returns {ticker: sub_sector}. Used by the validator to catch stale
    references after a sub-sector rename/merge."""
    result: dict[str, str] = {}
    for p in paths:
        data = _load_yaml(p)
        for entries in (data.get("sectors") or {}).values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if isinstance(entry, dict) and entry.get("sub_sector"):
                    result[str(entry.get("ticker") or "")] = entry["sub_sector"]
    return result


def _load_source_yamls() -> dict:
    """Read all source YAMLs and shape them into _SourceTaxonomy input."""
    sub_sectors_data = _load_yaml(_SUB_SECTORS_PATH)
    us_sectors_data  = _load_yaml(_US_SECTORS_PATH)
    size_splits_data = _load_yaml(_US_SIZE_SPLITS_PATH)
    watchlist_subs   = _collect_watchlist_subs(_WATCHLIST_HK_PATH, _WATCHLIST_US_PATH)

    # us_size_splits.yaml has the shape {splits: {Banks: {tiers: [...]}, ...}};
    # flatten to {Banks: [tier, tier, ...], ...} for the validator.
    raw_splits = (size_splits_data.get("splits") or {})
    flat_splits = {
        source: (cfg.get("tiers") or [])
        for source, cfg in raw_splits.items()
    }

    # YAML 1.1 boolean coercion turns ticker symbols like ON, Y, NO, OFF into
    # Python True/False. Coerce all ticker-keyed dicts back to string keys.
    def _strkey(d: dict) -> dict:
        return {str(k): v for k, v in (d or {}).items()}

    return {
        "industry_to_subsector": sub_sectors_data.get("industry_to_subsector") or {},
        "sub_sector_to_parent":  sub_sectors_data.get("sub_sector_to_parent")  or {},
        "parent_sectors_zh":     sub_sectors_data.get("parent_sectors_zh")     or {},
        "sub_sectors_zh":        sub_sectors_data.get("sub_sectors_zh")        or {},
        "ticker_overrides":      _strkey(sub_sectors_data.get("ticker_overrides")),
        "us_overrides":          _strkey(us_sectors_data.get("overrides")),
        "us_size_splits":        flat_splits,
        "watchlist_subs":        watchlist_subs,
    }


def _compute_version_hash(raw: dict) -> str:
    """SHA-256 over canonical-JSON serialised inputs. Stable across runs."""
    # default=str to handle Pydantic models if present; sort_keys for stability.
    payload = json.dumps(raw, sort_keys=True, default=str, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _build_nodes(schema: _SourceTaxonomy) -> tuple[dict[str, TaxonomyNode], dict[str, tuple[str, ...]]]:
    """Build the in-memory node + children maps from a validated schema."""
    nodes: dict[str, TaxonomyNode] = {}

    # Parents first. display_order = stable alphabetical for now (1-indexed).
    for i, parent in enumerate(sorted(schema.parent_sectors_zh.keys()), start=1):
        nodes[parent] = TaxonomyNode(
            canonical_name=parent,
            kind="parent",
            parent_name=None,
            label_en=parent,
            label_zh=schema.parent_sectors_zh[parent],
            display_order=i,
        )

    # Sub-sectors: order alphabetically within parent.
    subs_by_parent: dict[str, list[str]] = {}
    for sub, par in schema.sub_sector_to_parent.items():
        subs_by_parent.setdefault(par, []).append(sub)

    for parent, subs in subs_by_parent.items():
        for i, sub in enumerate(sorted(subs), start=1):
            nodes[sub] = TaxonomyNode(
                canonical_name=sub,
                kind="sub",
                parent_name=parent,
                label_en=sub,
                label_zh=schema.sub_sectors_zh[sub],
                display_order=i,
            )

    children_by_parent = {
        parent: tuple(sorted(subs))
        for parent, subs in subs_by_parent.items()
    }
    return nodes, children_by_parent


def compile_taxonomy(write_to_db: bool = True,
                     db_path: Optional[Path] = None) -> Taxonomy:
    """Load source YAMLs, validate, return Taxonomy. With write_to_db=True
    also persists to sector_taxonomy + bumps taxonomy_meta.version.
    Idempotent — re-running with no YAML changes produces the same version
    hash and zero row changes."""
    raw = _load_source_yamls()
    schema = _SourceTaxonomy(**raw)   # raises on validation failure
    nodes, children = _build_nodes(schema)
    version = _compute_version_hash(raw)
    taxonomy = Taxonomy(nodes=nodes, children_by_parent=children, version=version)

    if write_to_db:
        db = Database(str(db_path or _DEFAULT_DB_PATH))
        _persist_to_db(db, nodes, version)
        refresh_taxonomy()  # next get_taxonomy() in this process reloads
    return taxonomy


def _persist_to_db(db: Database, nodes: dict[str, TaxonomyNode], version: str) -> None:
    """Upsert sector_taxonomy rows + bump taxonomy_meta.version. Local SQLite
    only; Supabase mirror is a separate step (Phase D ops convenience)."""
    with db.get_connection() as conn:
        # Replace-all is fine: ~100 rows, single transaction. Keeps the
        # table in lockstep with the latest YAML state — old labels that
        # disappear from YAML also disappear from the table.
        conn.execute("DELETE FROM sector_taxonomy")
        conn.executemany("""
            INSERT INTO sector_taxonomy
              (canonical_name, kind, parent_name, label_en, label_zh,
               display_order, is_active, compiled_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
        """, [
            (n.canonical_name, n.kind, n.parent_name,
             n.label_en, n.label_zh, n.display_order)
            for n in nodes.values()
        ])
        # Single-row upsert for version meta.
        conn.execute("""
            INSERT INTO taxonomy_meta (key, value, updated_at)
            VALUES ('version', ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP
        """, (version,))
    logger.info("Compiled taxonomy: %d nodes, version=%s", len(nodes), version)


# ============================================================================
# Runtime accessor — process-wide singleton
# ============================================================================

def _load_from_db(db_path: Optional[Path] = None) -> tuple[Taxonomy, str]:
    """Read sector_taxonomy + taxonomy_meta into a Taxonomy snapshot."""
    db = Database(str(db_path or _DEFAULT_DB_PATH))
    with db.get_connection() as conn:
        rows = conn.execute("""
            SELECT canonical_name, kind, parent_name, label_en, label_zh,
                   display_order, is_active
            FROM sector_taxonomy
        """).fetchall()
        version_row = conn.execute(
            "SELECT value FROM taxonomy_meta WHERE key = 'version'"
        ).fetchone()
    version = version_row[0] if version_row else "uncompiled"

    nodes: dict[str, TaxonomyNode] = {}
    children_by_parent: dict[str, list[str]] = {}
    for name, kind, parent, en, zh, order, active in rows:
        if not active:
            continue
        nodes[name] = TaxonomyNode(
            canonical_name=name, kind=kind, parent_name=parent,
            label_en=en, label_zh=zh, display_order=order,
        )
        if kind == "sub" and parent:
            children_by_parent.setdefault(parent, []).append(name)
    children = {p: tuple(sorted(s)) for p, s in children_by_parent.items()}
    return Taxonomy(nodes=nodes, children_by_parent=children, version=version), version


def _current_db_version(db_path: Optional[Path] = None) -> Optional[str]:
    """Cheap version check — single SELECT, no row hydration."""
    db = Database(str(db_path or _DEFAULT_DB_PATH))
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT value FROM taxonomy_meta WHERE key = 'version'"
        ).fetchone()
    return row[0] if row else None


def get_taxonomy(db_path: Optional[Path] = None) -> Taxonomy:
    """Process-wide singleton.

    First call: full SELECT (~5ms). Subsequent calls within
    _REFRESH_INTERVAL_SECONDS: returns the cached snapshot immediately.
    After the interval: cheap version check; only re-hydrate if version
    changed in the DB. Thread-safe under the module RLock.

    For uncompiled databases (fresh install, before
    `python main.py taxonomy compile`), returns an empty Taxonomy so
    callers degrade gracefully via the fallback paths in `label()` etc.
    """
    global _CACHED, _CACHED_VERSION, _CACHED_AT
    now = time.time()

    with _LOCK:
        if _CACHED is not None and (now - _CACHED_AT) < _REFRESH_INTERVAL_SECONDS:
            return _CACHED

        # TTL expired or first call. Check if DB version moved.
        db_version = _current_db_version(db_path)
        if (_CACHED is not None
                and db_version == _CACHED_VERSION
                and _CACHED_VERSION is not None):
            # Same version: bump the TTL marker, return cached.
            _CACHED_AT = now
            return _CACHED

        # Need to reload.
        try:
            tax, version = _load_from_db(db_path)
        except Exception as e:
            logger.warning("Failed to load taxonomy from DB; returning empty: %s", e)
            tax = Taxonomy(nodes={}, children_by_parent={}, version="uncompiled")
            version = "uncompiled"
        _CACHED = tax
        _CACHED_VERSION = version
        _CACHED_AT = now
        return tax


def refresh_taxonomy() -> None:
    """Drop the in-process cache. Next get_taxonomy() will reload from DB.
    Call this right after a `compile_taxonomy(write_to_db=True)` invocation
    in the same process to make the new version visible immediately."""
    global _CACHED, _CACHED_VERSION, _CACHED_AT
    with _LOCK:
        _CACHED = None
        _CACHED_VERSION = None
        _CACHED_AT = 0.0
