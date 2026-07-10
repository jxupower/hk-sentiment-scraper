-- ============================================================================
-- Rollback for scripts/supabase_rls_setup.sql
-- ============================================================================
--
-- Run this BEFORE dropping the app_backend role so you don't strand a
-- policy that references it. After this script runs, the DB is back to
-- the pre-RLS state:
--   • RLS disabled on every public table.
--   • All *_backend_all policies dropped.
--   • The app_backend role dropped (after re-assigning its owned objects,
--     though it doesn't own any in this design).
--
-- Before rolling back, revert SUPABASE_DB_URL in .env to the `postgres`
-- credential so the app has a working connection. Then restart the app.
-- Then run this script in Supabase SQL Editor as `postgres`.
-- ============================================================================


-- 1. Drop policies + disable RLS on every public table.
DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT tablename FROM pg_tables WHERE schemaname = 'public'
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I',
                       r.tablename || '_backend_all',
                       r.tablename);
        EXECUTE format('ALTER TABLE public.%I NO FORCE ROW LEVEL SECURITY',
                       r.tablename);
        EXECUTE format('ALTER TABLE public.%I DISABLE ROW LEVEL SECURITY',
                       r.tablename);
        RAISE NOTICE 'RLS disabled on public.%', r.tablename;
    END LOOP;
END $$;


-- 2. Revoke the grants we gave app_backend.
-- REVOKE is safe even if the role no longer holds the privilege.
REVOKE ALL PRIVILEGES ON ALL TABLES    IN SCHEMA public FROM app_backend;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM app_backend;
REVOKE USAGE    ON SCHEMA public FROM app_backend;
REVOKE CONNECT  ON DATABASE postgres FROM app_backend;

-- 3. Undo the default-privileges rule so future tables don't try to
--    auto-grant to a role that's about to be dropped.
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM app_backend;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    REVOKE USAGE, SELECT ON SEQUENCES FROM app_backend;

-- 4. Drop the role. `IF EXISTS` in case someone already removed it.
DROP ROLE IF EXISTS app_backend;

-- 5. Sanity check — should return zero rows.
SELECT tablename, rowsecurity FROM pg_tables
WHERE schemaname = 'public' AND rowsecurity = true;
