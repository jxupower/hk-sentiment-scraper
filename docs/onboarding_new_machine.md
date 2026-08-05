# Onboarding: cloning this repo onto a new machine

This document is written for **two readers**:

1. **A human operator** setting the project up on a fresh laptop (macOS or Windows).
2. **A fresh Claude Code session** running in VSCode on that new laptop, which needs to understand what state the project is in and how to help without breaking invariants.

Sections marked **[HUMAN]** are the click-here / type-this bits. Sections marked **[CLAUDE]** hand context to the AI assistant so it doesn't have to re-derive it. Anyone can read either.

The end state we're targeting: fresh clone → dashboard live at `http://localhost:8050` in **~35-50 min**, with the same functionality as the origin machine.

---

## 0. What this project is (short version)

Croissant Stock Analyser — a Dash-based HK + US stock dashboard combining sentiment (RSS / Yahoo / Reddit), fundamentals (akshare / yfinance), Modern Portfolio Theory optimisation, GJR-GARCH risk forecasting, and a Claude-powered Stock Research tab.

Storage is **hybrid**:
- Local SQLite (`data/sentiment.db`) for everything except prices and fundamentals.
- Supabase Postgres for `fundamentals_snapshots`, `securities_reference`, `financial_statements`, `portfolios`, and `historical_prices` (the last one is the seed source; primary read path for prices is local Parquet — see §7).

The full architecture lives in [CLAUDE.md](../CLAUDE.md) at the repo root. Claude Code loads that automatically on session start; a human should skim it once.

---

## 1. [HUMAN] Prerequisites

### macOS

Open **Terminal** and run:

```bash
# Homebrew (skip if already installed)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Python 3.11 — pyarrow + akshare do not ship 3.14 wheels yet
brew install python@3.11

# Verify git (macOS usually preinstalls it, but Xcode CLT is required for pip builds)
git --version
xcode-select --install    # no-op if already installed
```

### Windows

Install via [winget](https://learn.microsoft.com/en-us/windows/package-manager/winget/) (built into Windows 10 21H1+ / 11):

```powershell
winget install --id Python.Python.3.11
winget install --id Git.Git
winget install --id Microsoft.VisualStudioCode
```

Then open a **new PowerShell** so PATH picks up the new binaries.

### Both platforms

Install **VSCode + Claude Code extension**:

1. VSCode from https://code.visualstudio.com (macOS: drag to `/Applications`; Windows: winget line above).
2. Launch VSCode → **Extensions** panel (Cmd/Ctrl+Shift+X) → search "Claude Code" (publisher: Anthropic) → **Install**.
3. Cmd/Ctrl+Shift+P → `Claude Code: Sign in` → OAuth in browser.

---

## 2. [HUMAN] Clone the repo

**Inside VSCode:** Cmd/Ctrl+Shift+P → `Git: Clone` → paste `https://github.com/jxupower/hk-sentiment-scraper.git` → pick a target folder (e.g. `~/Projects/`) → **Open** when prompted.

**Or terminal:**

```bash
# macOS
mkdir -p ~/Projects && cd ~/Projects
git clone https://github.com/jxupower/hk-sentiment-scraper.git
code hk-sentiment-scraper
```

```powershell
# Windows
mkdir $HOME\Projects -Force
cd $HOME\Projects
git clone https://github.com/jxupower/hk-sentiment-scraper.git
code hk-sentiment-scraper
```

Configure git identity on the new machine (once):

```bash
git config --global user.name  "Your Name"
git config --global user.email "you@example.com"
```

---

## 3. [HUMAN] Python venv + dependencies

Open the VSCode integrated terminal (Ctrl+backtick).

**macOS:**
```bash
python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

**Windows:**
```powershell
py -3.11 -m venv venv
venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

This compiles a few native packages (psycopg2, pyarrow, numpy, akshare) — takes 3-5 min. If psycopg2 fails on macOS: `brew install libpq && pip install psycopg2-binary` as a fallback. If pyarrow fails: check that Python is 3.11.

**Tell VSCode about the venv** so Pylance / linting works: Cmd/Ctrl+Shift+P → `Python: Select Interpreter` → pick `./venv/bin/python` (macOS) or `.\venv\Scripts\python.exe` (Windows).

---

## 4. [HUMAN] Configure secrets in `.env`

The `.env` file is **gitignored** — it never leaves the machine that created it. On a new machine, you have to reconstruct it.

```bash
cp .env.example .env     # macOS
copy .env.example .env   # Windows PowerShell
```

Then open `.env` (in VSCode: it's in the file tree) and fill in **at minimum**:

```bash
# Routing
USE_CLOUD_DB=true
USE_PARQUET_PRICES=true       # once §7 seeds data/prices/, factory auto-flips

# Supabase — copy from the origin machine's .env OR from
# Supabase Project -> Settings -> Database -> Session pooler URI
# Password must be URL-encoded (@ -> %40, + -> %2B)
SUPABASE_DB_URL=postgresql://app_backend:PWD@aws-...pooler.supabase.com:5432/postgres

# Anthropic — regenerate at https://console.anthropic.com/settings/keys
# The Stock Research tab's 4 AI sections need this
ANTHROPIC_API_KEY=sk-ant-...

# Optional: Reddit sentiment — skip if you don't want to bother
# REDDIT_CLIENT_ID=
# REDDIT_CLIENT_SECRET=

# Security headers ON in dev (CSP, HSTS, etc.) — safe to leave true
SECURITY_HEADERS_ENABLED=true
```

### Transferring secrets safely

Do **not** email the `.env` or paste it into chat. Options in decreasing preference:

1. **Password manager** (1Password / Bitwarden secure note field) — read from phone, type into new machine.
2. **iCloud Keychain / Windows Credential Manager** — sync across your own devices only.
3. **Regenerate** — Supabase: rotate the `app_backend` password; Anthropic: mint a new API key. New machine gets fresh values; old machine's `.env` is now stale (edit or delete).

The `SUPABASE_DB_URL` password is the single most sensitive value here — leaking it grants read/write to your shared cloud DB.

---

## 5. [HUMAN] Seed local Parquet price store

This is the long step (~15-30 min) but it's the one thing that makes the "clone from GitHub, be running quickly" story work at all. Prices live locally as Parquet files (~908 MB); Supabase is the seed source.

```bash
python scripts/seed_parquet_from_supabase.py
```

You'll see one line per ticker:

```
[    1/7062] &ADVERTISING_AGENCIES  full           +  7055 rows    2.0s  (rate  3.6/s ETA 32.0m)
...
[ 7062/7062] ^VIX                    full           +  2516 rows    1.6s  (rate 10.0/s ETA  0.0m)
```

**Safe to Ctrl+C and resume** — a checkpoint at `data/.parquet_seed_checkpoint.json` tracks completed tickers, and every re-run short-circuits skip-uptodate work in ~0.1s per already-done ticker. Ends with a summary and a directive to run the smoke test.

**Options:**

```bash
# Test on 20 tickers first
python scripts/seed_parquet_from_supabase.py --limit 20

# Lower concurrency if the pooler is slow / complains
python scripts/seed_parquet_from_supabase.py --workers 4

# Force-refresh specific tickers
python scripts/seed_parquet_from_supabase.py --force 0700.HK 6181.HK
```

**Expected disk usage after seed:** ~908 MB in `data/prices/` (snappy-compressed Parquet).

---

## 6. [HUMAN] Verify parity

```bash
python scripts/smoke_test_parquet_reads.py
```

Should end with `7 PASS  0 FAIL  0 WARN`. If any FAIL, don't launch the dashboard until you understand why — the checks cover ticker inventory, per-ticker row counts, latest dates, deep row-diff on 15 random tickers, factory routing, and live-caller smoke via `analysis.data_loader.get_or_fetch_prices`.

---

## 7. [HUMAN] Run the dashboard

```bash
python main.py dashboard
```

Open http://localhost:8050. Cold first render is ~5-10 s while caches prime. Click through **Stock Research** → `0700.HK` (Tencent) to sanity-check that price + fundamentals + AI cache round-trip works.

To stop: Ctrl+C in the terminal.

---

## 8. [HUMAN] Optional: Docker path

If Docker Desktop is running, this alternative bypasses steps 3 + 5 by baking the venv into a container image:

```bash
docker compose up --build
```

The `data/` directory is bind-mounted so the SQLite DB persists across container recycles. `data/prices/` is also on the host so the Parquet store survives too — but you'll still need to run the seed script (§5) at least once, either inside the container or from a host venv.

---

## 9. [CLAUDE] What you need to know on a fresh clone

If you are a Claude Code session opening this repo for the first time on this machine, read this before doing anything substantive.

### What's already true

- **CLAUDE.md at the repo root is the source of truth** for architecture. It loads automatically into your context. Do not re-derive facts already stated there.
- **The Parquet migration (P3.16) is complete on this repo but per-machine.** `data/prices/` is gitignored, so a fresh machine has an empty store. `storage/factory.py:get_prices_repo` falls back to `CloudHistoricalPricesRepository` until `data/prices/` contains ≥100 ticker dirs (`store_populated()` gate). §5 above populates it.
- **`historical_prices` remains present on Supabase by explicit user decision** (F2 skipped in the 2026-08-02 free-tier cleanup). Do not propose dropping it — it is the seed source that makes cross-machine onboarding possible.
- **Duplicate index `idx_hp_market_ticker_date` was dropped (F3, 2026-07-30)** — do not propose recreating it; the primary key + `idx_hp_ticker_date` cover every query pattern.

### What is NOT carried across machines

Memory in `~/.claude/projects/<hash>/memory/` is per-machine. A fresh Claude session on a new laptop starts blank on memory even though the code state is identical. You will need to rebuild memory over time as the user interacts. Do not assume the presence of memories created on the origin machine.

### Working rules (from CLAUDE.md — restated for emphasis)

- **Never add a third-party dependency** (`pip install`, new `requirements.txt` entry, etc.) **without explicit user approval in plan mode.** Prefer stdlib or already-installed packages; if impossible, enter plan mode and name package + version + rationale.
- **Never mock the database** in tests. Integration tests must hit real SQLite / Supabase.
- **Never commit `.env`** — enforced by `.gitignore`, but stay defensive.
- **Never run destructive git commands** (`reset --hard`, `push --force`, etc.) without explicit user approval.
- **Prefer editing existing files** to creating new ones. No unsolicited README or docs.
- **No comments explaining WHAT** — well-named identifiers already do that. Only WHY comments, when non-obvious.

### Open follow-ups (2026-08-02)

Recorded in the origin machine's todo list but likely dropped on new-machine memory reset. Confirm with the user before acting:

1. **`analysis/subsector_synth.py` naming fix** — currently generates tickers like `&US:AEROSPACE_AND_DEFENSE`. The `:` is illegal on Windows filesystem paths (Parquet write fails). One such orphan was already DELETEd from Supabase during the F2/F3 cleanup. Rename to `&US_X` before US composite regeneration is run again.
2. **`requirements.lock` regeneration** — blocked on Docker Desktop being running (pip-tools workflow in `docs/lockfile_workflow.md`).

### Cross-machine drift to watch for

- **`data/sentiment.db`** — created fresh by `Database.initialize()` on first dashboard boot. Schema is idempotent (`CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ADD COLUMN IF NOT EXISTS` guards). New machine's SQLite has no historical `articles`, `sentiment_scores`, `ticker_signals`, `research_notes` etc. — these repopulate on next scrape cycle.
- **AI statement cache** in `securities_reference` (Supabase) is shared across machines — a research report generated on the origin machine displays instantly on the new machine.
- **`optimized_parameters`** (SQLite only) — backtest walk-forward results don't transfer. Not urgent; re-run `python main.py backtest optimize --screen <name>` when needed.

---

## 10. [HUMAN] Common failure modes + fixes

| Symptom | Cause | Fix |
|---|---|---|
| `pip install` fails on psycopg2 | Missing libpq / SDK | macOS: `brew install libpq && pip install psycopg2-binary`. Windows: `pip install psycopg2-binary` (pre-built wheel). |
| `pip install` fails on pyarrow | Python 3.14 (no wheels yet) | Recreate venv with `python3.11 -m venv venv`. |
| Dashboard loads but Stock Research AI buttons do nothing | `ANTHROPIC_API_KEY` missing / no credits | Check `.env`; visit https://console.anthropic.com/settings/billing. |
| Dashboard loads but no price data anywhere | `data/prices/` empty AND cloud unreachable | Re-run §5 seed. Check `SUPABASE_DB_URL` in `.env`. |
| `psycopg2.OperationalError: could not translate host name` | `SUPABASE_DB_URL` malformed | Password must be URL-encoded. Verify against Supabase Project → Settings → Database → Session pooler URI. |
| Seed script errors on 1 ticker (`&US:AEROSPACE_AND_DEFENSE`) | Known Windows filesystem issue | Ignore — the smoke test's `IGNORE_ORPHANS` handles it. If on macOS/Linux, the `:` may work, but the ticker was DELETEd from Supabase during the origin's F2/F3 cleanup, so it should no longer be present. |
| `store_populated=False` even after seed | Fewer than 100 ticker dirs in `data/prices/` | Re-run seed; verify with `ls data/prices/ | wc -l` (macOS) or `(Get-ChildItem data/prices -Directory).Count` (Windows). |
| Import errors on modules that were fine on Windows | Case-sensitive filesystem on macOS/Linux vs Windows | Check import casing matches actual filename. |

---

## 11. [HUMAN + CLAUDE] After onboarding: keeping machines in sync

The **code** is git-managed — `git pull` / `git push` handle that.

The **cloud data** (`historical_prices`, `fundamentals_snapshots`, `securities_reference`, `financial_statements`, `portfolios`) is shared automatically via Supabase.

The **local data** (`data/sentiment.db`, `data/prices/`) is not shared. If both machines write concurrently:

- **`data/sentiment.db`** — mostly repopulates from scrape cycles; short of hand-edited `research_notes`, drift is low-impact.
- **`data/prices/`** — both machines pull the same EOD prices from yfinance, then `upsert_rows` merges deterministically per `(ticker, date)`. Convergence is automatic; there's no split-brain window worth worrying about.

If one machine gets far ahead and the other has been dormant, the simplest catch-up is: on the dormant one, re-run `python scripts/seed_parquet_from_supabase.py` — the incremental path will fetch only rows the cloud has that the local doesn't.

---

## 12. [HUMAN] Getting help

- **On this project** — inside VSCode, open Claude Code (Cmd/Ctrl+Esc). Because `CLAUDE.md` is loaded automatically, Claude answers with full architectural context from turn one.
- **Claude Code itself** — https://docs.claude.com/en/docs/claude-code
- **VSCode Python setup** — https://code.visualstudio.com/docs/python/python-tutorial
- **Supabase** — https://supabase.com/docs

Total wall-clock for a fresh Mac + fresh git checkout + you-have-the-secrets in a password manager: **~35-50 min**, of which ~20-30 min is the Parquet seed running unattended.
