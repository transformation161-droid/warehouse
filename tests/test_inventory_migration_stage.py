"""Stage checker must import successfully and refuse any non-local connection.

Regression: importing validate_schema from pg_admin raised ImportError before
the local SQL acceptance could even start.
"""
from unittest.mock import patch
import pytest

from tools import inventory_migration_stage as stage


def test_checker_imports_schema_validation_from_actual_backup_module():
    from server.pg_backup import validate_schema
    assert stage.validate_schema is validate_schema


def test_checker_refuses_missing_or_nonlocal_credentials_before_network(monkeypatch):
    monkeypatch.setenv("KSU_PG_TRANSPORT_PROFILE", "verify-full")
    monkeypatch.delenv("KSU_PG_ADMIN_DSN", raising=False)
    with patch.object(stage, "connect", side_effect=AssertionError("must not connect")):
        with pytest.raises(RuntimeError, match="Isolated local admin profile"):
            stage.acceptance("/input/inventory.sql")

    monkeypatch.setenv(
        "KSU_PG_ADMIN_DSN",
        "host=example.invalid port=5432 dbname=ksu_restore user=ksu_admin "
        "password=fake-not-a-secret sslmode=verify-full sslrootcert=/config/cert.pem",
    )
    with patch.object(stage, "connect", side_effect=AssertionError("must not connect")):
        with pytest.raises(RuntimeError, match="non-local"):
            stage.acceptance("/input/inventory.sql")


def test_checker_refuses_wrong_database_before_network(monkeypatch):
    monkeypatch.setenv("KSU_PG_TRANSPORT_PROFILE", "verify-full")
    monkeypatch.setenv(
        "KSU_PG_ADMIN_DSN",
        "host=localhost port=5432 dbname=production user=ksu_admin "
        "password=fake-not-a-secret sslmode=verify-full sslrootcert=/config/cert.pem",
    )
    with patch.object(stage, "connect", side_effect=AssertionError("must not connect")):
        with pytest.raises(RuntimeError, match="non-local"):
            stage.acceptance("/input/inventory.sql")
