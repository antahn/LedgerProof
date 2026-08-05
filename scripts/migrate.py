"""Apply plain numbered SQL migrations, in order, exactly once each.

No ORM migration magic: migrations/ holds NNN_name.sql files; this runner
records applied filenames in schema_migrations and applies the rest in
lexicographic order. Each file is expected to manage its own BEGIN/COMMIT.

Usage:
    uv run python scripts/migrate.py            # apply to DATABASE_URL
    uv run python scripts/migrate.py --test     # apply to TEST_DATABASE_URL
    uv run python scripts/migrate.py --url postgresql://...
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

DEFAULT_URL = "postgresql://postgres:ledgerproof@localhost:5432/ledgerproof"
DEFAULT_TEST_URL = "postgresql://postgres:ledgerproof@localhost:5432/ledgerproof_test"


def migrate(url: str) -> list[str]:
    """Apply pending migrations; return the filenames applied."""
    applied: list[str] = []
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "  filename TEXT PRIMARY KEY,"
            "  applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        done = {r[0] for r in conn.execute("SELECT filename FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            sql = path.read_text(encoding="utf-8")
            conn.execute(sql)  # file contains its own BEGIN/COMMIT
            conn.execute(
                "INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,)
            )
            applied.append(path.name)
    return applied


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="apply to TEST_DATABASE_URL")
    ap.add_argument("--url", help="explicit database URL (overrides env)")
    args = ap.parse_args()

    if args.url:
        url = args.url
    elif args.test:
        url = os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_URL)
    else:
        url = os.environ.get("DATABASE_URL", DEFAULT_URL)

    applied = migrate(url)
    db = url.rsplit("/", 1)[-1]
    if applied:
        print(f"[{db}] applied: {', '.join(applied)}")
    else:
        print(f"[{db}] up to date")


if __name__ == "__main__":
    sys.exit(main())
