"""Chart-of-accounts access.

Contract: read-only lookup of accounts by name/id; exposes account kind and
normal direction so posting code can compute entry signs. Accounts are seeded
by migration; this module never creates or mutates them at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg


@dataclass(frozen=True)
class Account:
    id: int
    name: str
    kind: str
    normal: str
    currency: str


_SELECT = "SELECT id, name, kind, normal, currency FROM accounts"


def all_accounts(conn: psycopg.Connection) -> dict[str, Account]:
    rows = conn.execute(_SELECT + " ORDER BY id").fetchall()
    return {r[1]: Account(id=r[0], name=r[1], kind=r[2], normal=r[3], currency=r[4]) for r in rows}


def get_account(conn: psycopg.Connection, name: str) -> Account:
    row = conn.execute(_SELECT + " WHERE name = %s", (name,)).fetchone()
    if row is None:
        raise KeyError(name)
    return Account(id=row[0], name=row[1], kind=row[2], normal=row[3], currency=row[4])
