---
name: run-hk-sentiment-scraper
description: Run, start, launch, smoke-test, or screenshot the Croissant Stock Analyser dashboard (HK/US sentiment + fundamentals Dash app); also run a CLI scrape cycle. Use when asked to boot the dashboard, verify it works, capture the UI, or exercise a scrape.
---

# Run: Croissant Stock Analyser (hk-sentiment-scraper)

Dash (Flask + React) web dashboard plus a CLI scraper. All paths below are
relative to the **repo root**. The agent path is the driver script — it
launches headless, proves the callback graph executes (fires a real i18n
callback and asserts Chinese labels come back), takes a screenshot via
Chrome DevTools Protocol, and shuts down cleanly. No browser needed for
smoke; Chrome/Edge (already on Windows) for pixels.

## Prerequisites

- Windows host with the project venv already at `venv/` (`venv\Scripts\python.exe`).
  If missing: `python -m venv venv && venv\Scripts\pip install -r requirements.txt`.
- `.env` is optional — without Supabase/Claude keys the app degrades to local
  SQLite (`USE_CLOUD_DB=false` path) and VADER-only sentiment.
- Chrome or Edge in a standard install location (screenshot only).

## Run (agent path) — the driver

```bash
# Full cycle: launch → smoke → screenshot → stop. Exit 0 = all pass.
venv/Scripts/python .claude/skills/run-hk-sentiment-scraper/driver.py
```

Individual phases (all accept `--port`, default **8051** so you never
collide with a user's manual dashboard on 8050):

```bash
venv/Scripts/python .claude/skills/run-hk-sentiment-scraper/driver.py launch
venv/Scripts/python .claude/skills/run-hk-sentiment-scraper/driver.py smoke
venv/Scripts/python .claude/skills/run-hk-sentiment-scraper/driver.py screenshot
venv/Scripts/python .claude/skills/run-hk-sentiment-scraper/driver.py stop
```

What each does:

- **launch** — spawns `main.py dashboard --port 8051` with
  `SKIP_DASHBOARD_PREWARM=true` (skips the Supabase pre-warm) and
  `PYTHONIOENCODING=utf-8`; polls `/_dash-layout` up to 90s (typical ready:
  8-17s); writes PID to `.driver.pid`, server log to `.driver.log` (both in
  the skill dir, gitignored).
- **smoke** — 3 checks: `GET /_dash-layout` is 200+JSON;
  `GET /_dash-dependencies` lists >50 callbacks (~100 registered); `POST
  /_dash-update-component` fires the largest server-side i18n callback with
  `user-language="zh"` and asserts CJK characters in the response. That last
  check proves callbacks *execute*, not just that the shell serves.
- **screenshot** — drives Chrome via CDP (stdlib WebSocket client inside the
  driver): waits for React hydration ("Croissant" in body text), clicks the
  first-visit Welcome modal's `startup-confirm-btn`, waits for the Market
  tab's "trading days" header (data loaded), captures to `shot.png` in the
  skill dir. Read the PNG to verify — a good capture shows the HSI chart,
  4 KPI cards, and the constituents table.
- **stop** — `taskkill /F /T` on the PID file, verifies the port is free.

## Direct invocation (no server)

Catches import/callback-registration breakage in ~5s — most PRs only need
this:

```bash
venv/Scripts/python -c "from dashboard.app import create_app; from config import settings; app = create_app('data/sentiment.db', settings); print(len(app.callback_map), 'callbacks')"
```

i18n checks without booting anything:

```bash
PYTHONIOENCODING=utf-8 venv/Scripts/python -c "from dashboard.i18n import T, EN, ZH; print(len(EN), len(ZH), T('tab.screener','zh'))"
```

## Run (human path)

```bash
venv/Scripts/python main.py dashboard        # http://localhost:8050, Ctrl+C to stop
```

First visit shows a Welcome modal (market + language picker). The dashboard
also starts the background scraper thread immediately — first scrape hits
the network within seconds of boot.

CLI scrape cycle (~2-4 min, prints a sector-signals table at the end):

```bash
PYTHONIOENCODING=utf-8 venv/Scripts/python main.py scrape --once --market HK
```

## Test

There is no test suite: `venv/Scripts/python -m pytest` collects **0 items**
(verified — the `scrapers/test_rss_scraper.py` mentioned in CLAUDE.md no
longer exists). The smoke driver above is the project's de-facto regression
check; the CI mirror lives in `.github/workflows/ci.yml` (`smoke` job).

## Gotchas

- **Port 8050 squatters.** Stale dashboard processes accumulate across
  sessions. `netstat -ano | grep :8050` → `taskkill //F //PID <pid>` (double
  slash in Git Bash). The driver avoids this class of problem by defaulting
  to 8051 + a pidfile.
- **`--debug` double-spawns.** Dash debug mode enables the Werkzeug reloader
  which forks a second process the pidfile doesn't know about. Never use it
  under the driver.
- **cp1252 vs Chinese.** Any Python that prints ZH strings crashes with
  `UnicodeEncodeError` on the default Windows console. Prefix with
  `PYTHONIOENCODING=utf-8` (the driver sets it for the server it spawns).
- **Dash JSON-escapes non-ASCII.** Callback responses contain `筛...`,
  not raw CJK — decode the JSON before grepping for Chinese.
- **Clientside callbacks 500 on POST.** Entries in `/_dash-dependencies`
  whose output contains `@<hash>` are browser-side; POSTing them to
  `_dash-update-component` returns "Callback function not found". Filter
  them out (the driver does).
- **One-shot headless screenshots don't work on this app.**
  `--screenshot --timeout=N` captures the pre-hydration "Loading..." shell;
  `--virtual-time-budget` hangs forever because page-load fires ~100 Dash
  callbacks and every pending XHR pauses virtual time. CDP with real-time
  readiness polling (what the driver does) is the only reliable path.
- **Headless needs `--user-data-dir`.** Without a throwaway profile dir,
  headless Edge/Chrome attaches to the user's running browser and hangs.
- **`create_app` signature** is `create_app(db_path: str, settings)` — both
  args required, first is a path string not a Database object.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `launch` says FAIL, log tail shows import error | Run the direct-invocation one-liner above for the full traceback |
| Port still serving after `stop` | Another process owns it: `netstat -ano \| grep :8051` → `taskkill //F //PID <pid>` |
| `UnicodeEncodeError ... charmap` | Prefix the command with `PYTHONIOENCODING=utf-8` |
| Screenshot is the Welcome modal | `startup-confirm-btn` id changed in `dashboard/layout.py` — update the driver |
| Screenshot KPI cards are skeleton dashes | The "trading days" readiness marker changed in the Market tab header — update `cmd_screenshot`'s poll expression |
