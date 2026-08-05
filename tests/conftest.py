"""Shared DB fixtures.

The ledger is append-only by design, so per-test cleanup cannot DELETE rows —
the only sanctioned reset is dropping the schema and re-running migrations.
`ledger_db` does exactly that and yields the test database URL; integration
tests can reuse it (or the `ledger_conn` convenience wrapper) as-is.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Callable, Iterator
from pathlib import Path

import psycopg
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://postgres:ledgerproof@localhost:5432/ledgerproof_test",
)


def _load_migrate() -> Callable[[str], list[str]]:
    # scripts/ is not a package; import migrate.py by path.
    path = REPO_ROOT / "scripts" / "migrate.py"
    spec = importlib.util.spec_from_file_location("_ledgerproof_migrate", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.migrate


_migrate = _load_migrate()


@pytest.fixture()
def ledger_db() -> Iterator[str]:
    """Fresh, fully migrated test database; yields its URL."""
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    _migrate(TEST_DATABASE_URL)
    yield TEST_DATABASE_URL


@pytest.fixture()
def ledger_conn(ledger_db: str) -> Iterator[psycopg.Connection]:
    """An open superuser connection to the fresh test database."""
    with psycopg.connect(ledger_db) as conn:
        yield conn


@pytest.fixture()
def app_db_url(ledger_db: str) -> str:
    """The fresh test database's URL as the least-privilege ledger_app role.

    'ledger_app' password is a local-dev container credential, not a secret
    (see migrations/001_ledger.sql).
    """
    params = psycopg.conninfo.conninfo_to_dict(ledger_db)
    host = params.get("host") or "localhost"
    port = params.get("port") or 5432
    return f"postgresql://ledger_app:ledger_app@{host}:{port}/{params['dbname']}"
