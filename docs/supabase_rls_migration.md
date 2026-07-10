# Supabase RLS + limited-role migration

This is a **defense-in-depth** change: after this runs, the Supabase URL
in your `.env` no longer belongs to a superuser. If it leaks:

- Attackers can still read + write app data (they have the DB creds)
- Attackers **cannot** drop tables, create new users, escalate to superuser,
  reach other schemas, or do anything the app can't already do

That's the meaningful reduction. Row-level policies today are permissive
(`USING (true) WITH CHECK (true)`) because the app is single-tenant — but
they're **structured** so per-user policies on `portfolios` become a small
`DROP POLICY / CREATE POLICY` delta later, no `ALTER TABLE` needed.

## Prerequisites

- Access to the Supabase Dashboard for this project (owner-tier — you need
  the SQL Editor to run as `postgres`).
- The current `postgres` password (needed one last time to run the setup
  script, then rotated at the end).
- Local repo with the venv active.

## The 8 steps

### 1. Generate the `app_backend` password

Any strong random string. Two easy options:

```bash
venv/Scripts/python -c "import secrets; print(secrets.token_urlsafe(32))"
```

or

```bash
openssl rand -base64 32
```

**Save it in your password manager immediately.** You'll paste it into the
SQL script (once) and into `.env` (once); you won't be shown it again.

### 2. Substitute the password into the setup SQL

`scripts/supabase_rls_setup.sql` uses `psql`-style variable substitution:
the placeholder `:'APP_BACKEND_PASSWORD'` must become the literal password
before Supabase's SQL Editor sees it (the Editor doesn't do `\set`).

Simplest: open the file, replace `:'APP_BACKEND_PASSWORD'` with
`'the-actual-password-you-generated'` (keep the single quotes). Do not
commit that edit — revert it after step 4.

### 3. Run the setup SQL in Supabase SQL Editor

- Supabase Dashboard → **SQL Editor** → **New query**
- Paste the whole (password-substituted) contents of
  `scripts/supabase_rls_setup.sql`
- Click **Run**
- The last four `SELECT` statements print verification tables. Every
  public table should show `rls_enabled = true` and `rls_forced = true`;
  every table should have a policy named `<table>_backend_all`; the last
  query (dangerous-role-bits check) should return **zero rows**.

If any check fails, do NOT proceed — read the error, fix, re-run (the
script is idempotent).

### 4. Revert the password edit in the SQL file

Change `'the-actual-password'` back to `:'APP_BACKEND_PASSWORD'` in
`scripts/supabase_rls_setup.sql` and stage that revert. The file should
match what's in git.

### 5. Update `.env`

Find your current line:

```
SUPABASE_DB_URL=postgresql://postgres.<project-ref>:<postgres-pw>@aws-<region>.pooler.supabase.com:5432/postgres
```

Change **role + password only**:

```
SUPABASE_DB_URL=postgresql://app_backend.<project-ref>:<app-backend-pw>@aws-<region>.pooler.supabase.com:5432/postgres
```

The `<project-ref>` stays the same (pooler needs it in `role.ref` format);
the host + port + database stay the same. **URL-encode** `@`, `+`, `%` if
they appear in the password (`@` → `%40`, `+` → `%2B`, `%` → `%25`).

### 6. Restart the app + verify

```bash
# Local dev:
venv/Scripts/python main.py dashboard --port 8050
# Or production (VM):
sudo docker compose -f /srv/dashboard/docker-compose.yml restart
```

Then run the automated verification:

```bash
venv/Scripts/python scripts/verify_supabase_rls.py
```

Expected output: **10 checks pass** (connection, role identity, no
dangerous role bits, RLS enabled + forced on every public table, policies
present, DML still works, three DDL operations denied).

If any DDL check unexpectedly succeeded, `.env` is still pointing at
`postgres`, not `app_backend` — re-check step 5.

### 7. Rotate the `postgres` password

This closes the F1 finding — the old `postgres` credential (still exposed
in this repo's history and in any `.driver.log` from when the app used it)
becomes worthless.

- Supabase Dashboard → **Project Settings** → **Database** → **Reset
  database password**
- Save the new value in your password manager. You'll use it whenever you
  need to run schema migrations from the SQL Editor.
- Do NOT paste it back into `.env` — the app now uses `app_backend`.

### 8. Verify one last time + document

Run `python scripts/verify_supabase_rls.py` a second time (belt-and-braces
that the app didn't silently reconnect as `postgres` from some cached
credential). Then update your team's runbook: `SUPABASE_DB_URL` is now the
`app_backend` role in every environment.

## What each artifact does

| File | Purpose |
|---|---|
| `scripts/supabase_rls_setup.sql` | The migration. Creates `app_backend`, grants DML, enables + forces RLS on every public table, writes a permissive backend policy per table. Idempotent. |
| `scripts/supabase_rls_rollback.sql` | Undo. Drops all policies, disables RLS, revokes grants, drops the role. Run only after reverting `.env` to `postgres`. |
| `scripts/verify_supabase_rls.py` | End-to-end check. Runs as whoever `SUPABASE_DB_URL` says; asserts identity, RLS state, DML success, DDL denial. Should be part of a pre-deploy checklist. |
| This file | The runbook. Point ops docs here. |

## Future: per-user portfolios

When you want per-user isolation on the `portfolios` table (each user sees
only their own), the delta is small because the policy scaffolding is
already in place:

1. `ALTER TABLE portfolios ADD COLUMN owner_email TEXT;`
2. Backfill: `UPDATE portfolios SET owner_email = 'legacy@you.com' WHERE
   owner_email IS NULL;` then `NOT NULL`.
3. In `storage/cloud_db.py` `connection()` context manager, add
   `SET LOCAL "request.email" = %s` where `%s` is `flask.g.user_email`
   (which is already captured by [dashboard/app.py](../dashboard/app.py)
   when `TRUST_CF_ACCESS_HEADER=true`).
4. Replace the `portfolios_backend_all` policy with:
   ```sql
   DROP POLICY portfolios_backend_all ON portfolios;
   CREATE POLICY portfolios_owner_rw ON portfolios
       FOR ALL TO app_backend
       USING      (owner_email = current_setting('request.email', true))
       WITH CHECK (owner_email = current_setting('request.email', true));
   ```

No other table needs per-user policies today — they're either global
(prices, fundamentals, taxonomy) or single-writer (financial_statements
cache).

## Rollback

If anything goes wrong post-migration:

1. Change `.env` back to the `postgres` connection string (use the new
   rotated password from step 7).
2. Restart the app. Confirm reads/writes work.
3. Run `scripts/supabase_rls_rollback.sql` in the SQL Editor.
4. Verify all `public.*` tables show `rowsecurity = false` (the rollback
   script's final `SELECT` should return zero rows).

The rollback is safe because RLS policies don't touch data — only who
can access it.
