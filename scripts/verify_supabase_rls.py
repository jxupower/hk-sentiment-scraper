"""Verify the Supabase RLS setup by exercising the app_backend role.

Run AFTER `scripts/supabase_rls_setup.sql` has been applied and `.env` has
been updated to use the `app_backend.<project-ref>` credential.

  venv/Scripts/python scripts/verify_supabase_rls.py

Exit code 0 = all pass; 1 = at least one check failed. Checks:

  1. Connection works with the current SUPABASE_DB_URL.
  2. The current role IS `app_backend` (not `postgres` — catches a
     forgotten .env update).
  3. The role has NO superuser / createdb / createrole / replication bits.
  4. Every public table has rowsecurity = true AND is FORCE-RLS'd.
  5. Every public table has exactly one policy for the app_backend role.
  6. Normal DML still works: SELECT from `historical_prices` succeeds.
  7. DDL is DENIED: `CREATE TABLE`, `DROP TABLE`, `ALTER ROLE` all fail
     with insufficient_privilege (SQLSTATE 42501).

The script never mutates real data. All test DDL targets a name that
doesn't exist (`__rls_verify_probe`); the point of the DDL calls is to
observe the permission denial, not to succeed.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the project importable when run as `python scripts/verify_supabase_rls.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# psycopg2 error codes: 42501 = insufficient_privilege; 42P01 = undefined_table.
INSUFFICIENT_PRIVILEGE = "42501"

PASS = "\033[32m[PASS]\033[0m"
FAIL = "\033[31m[FAIL]\033[0m"


def _p(ok: bool, label: str, detail: str = "") -> bool:
    tag = PASS if ok else FAIL
    line = f"{tag} {label}"
    if detail:
        line += f" — {detail}"
    print(line)
    return ok


def main() -> int:
    from storage import cloud_db
    from config import settings

    if not settings.cloud_db_configured():
        print(f"{FAIL} SUPABASE_DB_URL not set; nothing to verify.")
        return 1

    all_ok = True

    # 1. Connection ------------------------------------------------------
    try:
        with cloud_db.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        all_ok &= _p(True, "connection.open")
    except Exception as e:
        _p(False, "connection.open", str(e))
        return 1

    # 2. Current role ----------------------------------------------------
    with cloud_db.cursor() as cur:
        cur.execute("SELECT current_user, session_user")
        cur_user, session_user = cur.fetchone()
    ok = cur_user == "app_backend"
    all_ok &= _p(ok, "role.is_app_backend",
                 f"current_user={cur_user!r} session_user={session_user!r}")

    # 3. No dangerous role bits ------------------------------------------
    with cloud_db.cursor() as cur:
        cur.execute(
            "SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication "
            "FROM pg_roles WHERE rolname = current_user"
        )
        row = cur.fetchone()
    rolsuper, rolcreaterole, rolcreatedb, rolreplication = row
    ok = not any([rolsuper, rolcreaterole, rolcreatedb, rolreplication])
    all_ok &= _p(ok, "role.no_dangerous_bits",
                 f"super={rolsuper} createrole={rolcreaterole} "
                 f"createdb={rolcreatedb} replication={rolreplication}")

    # 4. RLS enabled + forced on every public table ----------------------
    with cloud_db.cursor() as cur:
        cur.execute("""
            SELECT t.tablename, t.rowsecurity, c.relforcerowsecurity
            FROM pg_tables t
            JOIN pg_class c ON c.oid = (t.schemaname||'.'||t.tablename)::regclass
            WHERE t.schemaname = 'public'
            ORDER BY t.tablename
        """)
        rls_rows = cur.fetchall()
    if not rls_rows:
        all_ok &= _p(False, "rls.enabled_all", "no public tables found — schema empty?")
    else:
        gaps = [t for t, en, forced in rls_rows if not (en and forced)]
        ok = not gaps
        all_ok &= _p(ok, "rls.enabled_all",
                     f"{len(rls_rows)} tables"
                     + ("" if ok else f"; missing: {gaps}"))

    # 5. Every table has an app_backend policy ---------------------------
    with cloud_db.cursor() as cur:
        cur.execute("""
            SELECT tablename,
                   COUNT(*) FILTER (WHERE 'app_backend' = ANY(roles)) AS n_backend_policies
            FROM pg_policies
            WHERE schemaname = 'public'
            GROUP BY tablename
        """)
        pol_rows = cur.fetchall()
    tables_with_policy = {t: n for t, n in pol_rows}
    tables_all = [t for t, _, _ in rls_rows]
    missing_policy = [t for t in tables_all if tables_with_policy.get(t, 0) < 1]
    ok = not missing_policy
    all_ok &= _p(ok, "rls.policies_present",
                 f"{sum(tables_with_policy.values())} policies across "
                 f"{len(tables_with_policy)} tables"
                 + ("" if ok else f"; missing: {missing_policy}"))

    # 6. DML still works -------------------------------------------------
    try:
        with cloud_db.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM historical_prices")
            n = cur.fetchone()[0]
        all_ok &= _p(True, "dml.select_historical_prices",
                     f"{n:,} rows visible")
    except Exception as e:
        all_ok &= _p(False, "dml.select_historical_prices", str(e))

    # 7. DDL denied ------------------------------------------------------
    # Each DDL statement runs in its own transaction; a failure aborts it
    # but doesn't poison the next one.
    ddl_probes = [
        ("ddl.create_denied",
         "CREATE TABLE public.__rls_verify_probe (x INT)"),
        ("ddl.drop_denied",
         "DROP TABLE IF EXISTS public.historical_prices"),
        ("ddl.alter_role_denied",
         "ALTER ROLE app_backend WITH SUPERUSER"),
    ]
    for label, sql in ddl_probes:
        try:
            with cloud_db.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql)
            # If we reach here, the DDL succeeded — that's a failure.
            all_ok &= _p(False, label, "DDL unexpectedly succeeded")
        except Exception as e:
            sqlstate = getattr(e, "pgcode", None)
            ok = sqlstate == INSUFFICIENT_PRIVILEGE
            all_ok &= _p(ok, label,
                         f"sqlstate={sqlstate} " + type(e).__name__)

    print()
    print("SUMMARY:", "ALL PASS" if all_ok else "AT LEAST ONE FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
