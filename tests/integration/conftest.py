"""Integration-test fixtures: a private scratch database, reset per test.

These tests use their own database (ledgerproof_test_ingest) so they never
collide with ledgerproof_test, which other suites use concurrently. Each test
function gets a freshly migrated schema: DROP SCHEMA public CASCADE, recreate
it, and re-run every migration — indistinguishable from a brand-new database.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import psycopg
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ADMIN_URL = "postgresql://postgres:ledgerproof@localhost:5432/postgres"
SCRATCH_DB = "ledgerproof_test_ingest"
SCRATCH_URL = f"postgresql://postgres:ledgerproof@localhost:5432/{SCRATCH_DB}"


def _load_migrate():
    """Import scripts/migrate.py's migrate() by path (scripts/ is not a package)."""
    path = REPO_ROOT / "scripts" / "migrate.py"
    spec = importlib.util.spec_from_file_location("_ledgerproof_migrate", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.migrate


_migrate = _load_migrate()


@pytest.fixture(scope="session")
def _scratch_database() -> str:
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {SCRATCH_DB}")
    return SCRATCH_URL


@pytest.fixture()
def db_url(_scratch_database: str) -> str:
    """URL of the scratch database with all migrations freshly applied."""
    with psycopg.connect(_scratch_database, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    _migrate(_scratch_database)
    return _scratch_database
