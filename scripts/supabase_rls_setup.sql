-- ============================================================================
-- Supabase Row-Level Security setup for HK Sentiment Scraper
-- ============================================================================
--
-- Goal: reduce the blast radius of a leaked SUPABASE_DB_URL. Today the app
-- connects as `postgres` (Supabase's built-in superuser role), which:
--   • bypasses RLS entirely (Postgres superusers do)
--   • has full DDL rights (DROP TABLE, DROP DATABASE, etc.)
--   • can create new users
-- Anyone with the URL owns the DB.
--
-- After this migration:
--   • The app connects as a new `app_backend` role with SELECT/INSERT/
--     UPDATE/DELETE on the public schema tables only — no DDL, no role
--     management, no ability to escape the public schema.
--   • RLS is ENABLED on every public table.
--   • Policies are `USING (true) WITH CHECK (true)` for `app_backend`
--     today (single-tenant app; every user sees the same data). The
--     policy names carry a `_backend_all` suffix so future per-user
--     tightening (`portfolios_backend_all` → `portfolios_owner_read`
--     + `portfolios_owner_write`) is a small delta, not an ALTER TABLE.
--
-- HOW TO RUN
-- ----------
-- 1. Generate a strong password for the new role:
--       python -c "import secrets; print(secrets.token_urlsafe(32))"
--    …or `openssl rand -base64 32`. Save it in your password manager.
--
-- 2. In this file, replace the single placeholder below with that value:
--       :'APP_BACKEND_PASSWORD'   →   'the-long-random-string-you-just-generated'
--    (Or run this script via `psql --variable=APP_BACKEND_PASSWORD=... -f`
--     which substitutes the placeholder without editing the file.)
--
-- 3. Open Supabase Dashboard → SQL Editor → paste this whole file → Run.
--    You must be authenticated as the project owner (i.e. the SQL Editor
--    runs as `postgres`) for the CREATE ROLE + GRANT statements to work.
--
-- 4. Update .env:
--       SUPABASE_DB_URL=postgresql://app_backend.<project-ref>:<PASSWORD>@aws-<region>.pooler.supabase.com:5432/postgres
--    The `app_backend.<project-ref>` format is required by Supabase's
--    pgbouncer session pooler (same shape as `postgres.<project-ref>`).
--
-- 5. Restart the app. Confirm normal reads/writes still work.
--    Run `python scripts/verify_supabase_rls.py` from repo root.
--
-- 6. ROTATE the `postgres` password in Supabase Dashboard → Project
--    Settings → Database → Reset database password. That credential is
--    no longer used at runtime, only by human operators in the SQL
--    Editor for schema migrations. Rotating it closes the F1 finding.
--
-- SAFETY
-- ------
-- • Idempotent: safe to re-run. CREATE ROLE guards against duplicates,
--   REVOKE + GRANT are declarative, ENABLE RLS is a no-op if already on.
-- • Reversible: see scripts/supabase_rls_rollback.sql.
-- • No data touched: this is a permissions + policy migration only.
-- ============================================================================


-- ============================================================================
-- 0. Fail fast on lock contention
-- ============================================================================
-- ALTER TABLE ... ENABLE ROW LEVEL SECURITY needs an ACCESS EXCLUSIVE lock.
-- If the app (or any other client) has open connections to the target
-- tables, the migration would wait forever. Cap the wait so we surface
-- the problem in seconds instead of hours. If this fires, either stop
-- the app first, or run `SELECT pg_terminate_backend(pid) FROM
-- pg_stat_activity WHERE application_name ILIKE '%psycopg%'` to evict
-- open connections, then re-run this script.
SET lock_timeout = '10s';
SET statement_timeout = '60s';


-- ============================================================================
-- 1. Create the app_backend role
-- ============================================================================
-- NOLOGIN would be safer as a template role, but the app needs LOGIN to
-- authenticate through the pooler. NOSUPERUSER + NOCREATEDB + NOCREATEROLE
-- explicitly denies each thing a compromised credential shouldn't be able
-- to do (INHERIT stays default so future grants to child roles flow).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_backend') THEN
        EXECUTE format(
            'CREATE ROLE app_backend WITH LOGIN NOSUPERUSER NOCREATEDB '
            'NOCREATEROLE NOREPLICATION PASSWORD %L',
            :'APP_BACKEND_PASSWORD'
        );
        RAISE NOTICE 'Role app_backend created.';
    ELSE
        -- Role exists (re-running). Just refresh the password so this
        -- script is a valid rotation vehicle too.
        EXECUTE format(
            'ALTER ROLE app_backend WITH LOGIN NOSUPERUSER NOCREATEDB '
            'NOCREATEROLE NOREPLICATION PASSWORD %L',
            :'APP_BACKEND_PASSWORD'
        );
        RAISE NOTICE 'Role app_backend already existed; password refreshed.';
    END IF;
END $$;


-- ============================================================================
-- 2. Schema-level grants
-- ============================================================================
-- CONNECT — allow the role to open a connection to this database.
GRANT CONNECT ON DATABASE postgres TO app_backend;

-- USAGE (but NOT CREATE) on the public schema — the role can reference
-- objects that already exist but can't add new tables/functions.
GRANT USAGE ON SCHEMA public TO app_backend;
REVOKE CREATE ON SCHEMA public FROM app_backend;

-- Belt-and-braces: remove any default PUBLIC grants on public so an
-- attacker who somehow got another role can't hop through PUBLIC.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO PUBLIC;  -- keep so Supabase internals still work


-- ============================================================================
-- 3. Table + sequence privileges
-- ============================================================================
-- DML on every existing table in public. This is broad by intent —
-- listing tables explicitly means a later `CREATE TABLE new_thing` would
-- silently be inaccessible to the app until someone remembers to grant.
GRANT SELECT, INSERT, UPDATE, DELETE
    ON ALL TABLES IN SCHEMA public
    TO app_backend;

-- Sequences: needed for BIGSERIAL PKs (e.g. ticker_taxonomy_history.id).
GRANT USAGE, SELECT
    ON ALL SEQUENCES IN SCHEMA public
    TO app_backend;

-- Default privileges — apply the same grants to any table/sequence
-- created LATER by the `postgres` role. Without this, adding a table
-- through the SQL Editor would leave the app unable to read it.
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_backend;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO app_backend;


-- ============================================================================
-- 4. Enable RLS on every public table + write policies
-- ============================================================================
-- One DO block that iterates over pg_tables so the migration stays
-- correct if the schema grows. Every table in public gets:
--   • ROW LEVEL SECURITY enabled
--   • A single permissive policy named `<table>_backend_all` scoped to
--     the app_backend role, `USING (true) WITH CHECK (true)`.
--
-- The policy name convention is deliberate: future per-user tightening
-- on portfolios becomes `DROP POLICY portfolios_backend_all ...; CREATE
-- POLICY portfolios_owner_rw ... USING (owner_email = current_setting(
-- 'request.email', true)) WITH CHECK (owner_email = current_setting(
-- 'request.email', true));`. No ALTER TABLE required.
DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = 'public'
    LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY',
                       r.tablename);
        -- FORCE RLS so the table OWNER (postgres) also has to obey
        -- policies — closes the "connect as postgres to bypass" hole
        -- for any future callers on the same DB.
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY',
                       r.tablename);

        -- Drop any prior version of the policy (idempotent re-run).
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I',
                       r.tablename || '_backend_all',
                       r.tablename);
        -- Permissive policy for the app_backend role.
        EXECUTE format(
            'CREATE POLICY %I ON public.%I '
            'FOR ALL TO app_backend '
            'USING (true) WITH CHECK (true)',
            r.tablename || '_backend_all',
            r.tablename
        );

        RAISE NOTICE 'RLS enabled on public.% with policy %',
                     r.tablename, r.tablename || '_backend_all';
    END LOOP;
END $$;


-- ============================================================================
-- 5. Verification queries (results shown in SQL Editor output)
-- ============================================================================
-- 5a. Every public table has RLS enabled AND forced.
SELECT
    tablename,
    rowsecurity     AS rls_enabled,
    (SELECT relforcerowsecurity FROM pg_class
      WHERE oid = (schemaname || '.' || tablename)::regclass) AS rls_forced
FROM pg_tables
WHERE schemaname = 'public'
ORDER BY tablename;

-- 5b. app_backend has exactly one permissive policy per table.
SELECT
    schemaname,
    tablename,
    policyname,
    roles,
    cmd,
    qual        AS using_expression,
    with_check
FROM pg_policies
WHERE schemaname = 'public'
ORDER BY tablename, policyname;

-- 5c. app_backend's granted privileges — should be exactly SELECT / INSERT /
--     UPDATE / DELETE on each table, USAGE + SELECT on sequences, no more.
SELECT
    table_schema,
    table_name,
    string_agg(privilege_type, ', ' ORDER BY privilege_type) AS privileges
FROM information_schema.table_privileges
WHERE grantee = 'app_backend' AND table_schema = 'public'
GROUP BY table_schema, table_name
ORDER BY table_name;

-- 5d. What app_backend CANNOT do (should return zero rows).
SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolreplication
FROM pg_roles
WHERE rolname = 'app_backend'
  AND (rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication);
