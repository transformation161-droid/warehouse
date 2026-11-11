"""One-time, controlled PR40 SQL acceptance on an INDEPENDENT LOCAL PG17 restore.

Owner-side tool, never executed by the production application.
PowerShell entrypoint checks restored-container identity and mounts this script and
a pinned SQL file. This script refuses non-local databases and never prints secrets.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from psycopg.conninfo import conninfo_to_dict

# The audited application modules come from the pinned Docker image.
sys.path.insert(0, "/app")
from server.pg_admin import fingerprints, validate_schema
from server.pg_transport import connect


def acceptance(sql_path: str) -> dict:
    dsn = os.environ.get("KSU_PG_ADMIN_DSN", "")
    if not dsn or os.environ.get("KSU_PG_TRANSPORT_PROFILE") != "verify-full":
        raise RuntimeError("Isolated local admin profile required")
    try:
        info = conninfo_to_dict(dsn)
    except Exception:
        raise RuntimeError("Invalid local test DSN") from None
    if not (
        info.get("host") == "localhost"
        and info.get("port") == "5432"
        and info.get("dbname") == "ksu_restore"
        and info.get("user") == "ksu_admin"
        and info.get("sslmode") == "verify-full"
        and info.get("sslrootcert") == "/config/cert.pem"
    ):
        raise RuntimeError("Refusing non-local or unexpected database identity")
    source = Path(sql_path)
    if source != Path("/input/inventory.sql") or not source.is_file():
        raise RuntimeError("Approved SQL mount is required")
    migration = source.read_text(encoding="utf-8-sig")
    if "CREATE OR REPLACE FUNCTION record_guard()" not in migration or "inventory_count_version()" not in migration:
        raise RuntimeError("Unexpected inventory migration source")

    with connect(dsn, connect_timeout=10) as con:
        validate_schema(con, 4)
        if con.info.server_version < 170000 or con.info.server_version >= 180000:
            raise RuntimeError("PostgreSQL 17 required")
        if con.execute("select to_regprocedure('ksu.inventory_count_version()')").fetchone()[0] is not None:
            raise RuntimeError("Inventory migration already installed; refusing to reapply")
        if con.execute("select count(*) from ksu.sessions").fetchone()[0]:
            raise RuntimeError("Test restore must contain no active sessions")
        if con.execute("select current_database()").fetchone()[0] != "ksu_restore":
            raise RuntimeError("Unexpected database")
        before = fingerprints(con, postgres=True)

    # Migration SQL is transactional, with an advisory lock. Errors cause rollback.
    # This is the disposable restored COPY, never the real database.
    with connect(dsn, connect_timeout=10) as con:
        con.execute(migration)

    with connect(dsn, connect_timeout=10) as con:
        validate_schema(con, 4)
        after = fingerprints(con, postgres=True)
        if after != before:
            raise RuntimeError("Business table fingerprints changed; rejecting migration")
        if con.execute("select ksu.inventory_count_version()").fetchone()[0] != 1:
            raise RuntimeError("Inventory feature marker missing or invalid")
        if not con.execute(
            "select has_function_privilege('ksu_runtime',"
            "'ksu.inventory_count_version()','EXECUTE')"
        ).fetchone()[0]:
            raise RuntimeError("Runtime privilege missing")
        policy = con.execute(
            "select qual from pg_policies where schemaname='ksu' "
            "and tablename='records' and policyname='records_update'"
        ).fetchone()
        if policy is None or "inventoryLines" not in policy[0]:
            raise RuntimeError("Cross-author inventory line RLS rule missing")
        if not con.execute(
            "select exists(select 1 from pg_trigger where tgrelid='ksu.records'::regclass "
            "and tgname='records_guard' and not tgisinternal)"
        ).fetchone()[0]:
            raise RuntimeError("Records write guard trigger missing")
    return {
        "verified": True,
        "database": "ksu_restore",
        "schema_version": 4,
        "migration": "005_inventory_count",
        "business_data_unchanged": True,
        "table_count": len(after),
        "total_rows": sum(entry["count"] for entry in after.values()),
        "runtime_permission": True,
        "production_connected": False,
    }


if __name__ == "__main__":
    try:
        result = acceptance(sys.argv[1])
        print("LOCAL INVENTORY MIGRATION PASS")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    except Exception as error:
        # Never echo connection strings or libpq failures, which can contain secrets.
        print("LOCAL INVENTORY MIGRATION FAILED: " + (
            str(error) if isinstance(error, RuntimeError) else
            "private local logs required"
        ), file=sys.stderr)
        sys.exit(1)
