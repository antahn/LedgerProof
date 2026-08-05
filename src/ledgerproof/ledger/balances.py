"""Read account balances from the derived view.

Contract: SELECTs from account_balances only. There is no stored balance column
anywhere in this system; if this module ever writes, that is a bug by
definition.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg


@contextmanager
def _connection(db_url_or_conn: str | psycopg.Connection) -> Iterator[psycopg.Connection]:
    """Yield a connection; open (and close) one only if given a URL."""
    if isinstance(db_url_or_conn, str):
        with psycopg.connect(db_url_or_conn) as conn:
            yield conn
    else:
        yield db_url_or_conn


def all_balances(db_url_or_conn: str | psycopg.Connection) -> dict[str, int]:
    with _connection(db_url_or_conn) as conn:
        rows = conn.execute("SELECT name, balance_minor FROM account_balances").fetchall()
    # SUM(bigint) comes back as numeric/Decimal; balances are integer minor units.
    return {name: int(bal) for name, bal in rows}


def balance(db_url_or_conn: str | psycopg.Connection, name: str) -> int:
    with _connection(db_url_or_conn) as conn:
        row = conn.execute(
            "SELECT balance_minor FROM account_balances WHERE name = %s", (name,)
        ).fetchone()
    if row is None:
        raise KeyError(name)
    return int(row[0])
