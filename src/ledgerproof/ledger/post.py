"""Post balanced transactions to the append-only ledger.

Contract: the ONLY write path into transactions/entries. Accepts a transaction
(id, stripe ids, occurred_at, memo) plus a list of entries (account, direction,
amount in minor units, currency); inserts them in a single database transaction
at SERIALIZABLE. Balance is NOT checked here — the deferred constraint trigger
enforces it at COMMIT; this module's job is to hand the database something to
judge, retry on serialization failure (SQLSTATE 40001) with bounded attempts,
and surface unique-violation on the dedupe keys as an idempotent no-op signal.
Never UPDATEs, never DELETEs.
"""

from __future__ import annotations

import enum
import random
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import psycopg

Direction = Literal["debit", "credit"]

# Backoff: first retry is quick, then exponential; multiplied by a random
# jitter factor in [0.5, 1.5) so many colliding writers do not realign.
_BACKOFF_BASE_SECONDS = 0.05
_BACKOFF_CAP_SECONDS = 2.0

_DEDUPE_CONSTRAINTS = frozenset({"dedupe_event", "dedupe_object"})


@dataclass(frozen=True)
class Entry:
    account_name: str
    dir: Direction
    amount_minor: int  # > 0, integer minor units; the DB CHECKs this too
    currency: str = "USD"


@dataclass(frozen=True)
class LedgerTransaction:
    id: uuid.UUID
    occurred_at: datetime  # tz-aware
    entries: tuple[Entry, ...]
    stripe_event_id: str | None = None
    stripe_object_id: str | None = None
    event_type: str | None = None
    memo: str | None = None


class PostOutcome(enum.Enum):
    POSTED = "posted"
    DUPLICATE = "duplicate"


def post_transaction(
    db_url: str,
    txn: LedgerTransaction,
    *,
    max_attempts: int = 5,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> PostOutcome:
    """Insert txn + entries atomically; POSTED on success, DUPLICATE on replay.

    Retries the whole unit on SerializationFailure (40001) up to max_attempts,
    sleeping an exponentially growing, jittered delay between attempts. Unknown
    account names raise ValueError. Any other database error (including the
    balance/min-entries triggers rejecting the commit) propagates.
    """
    if rng is None:
        rng = random.Random()
    attempt = 0
    while True:
        attempt += 1
        try:
            return _attempt(db_url, txn)
        except psycopg.errors.SerializationFailure:
            if attempt >= max_attempts:
                raise
            delay = min(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), _BACKOFF_CAP_SECONDS)
            sleep(delay * (0.5 + rng.random()))


def _attempt(db_url: str, txn: LedgerTransaction) -> PostOutcome:
    # No `with` block: the connection object may be substituted in tests and
    # cleanup must be explicit anyway (rollback on any failure path).
    conn = psycopg.connect(db_url)
    try:
        conn.isolation_level = psycopg.IsolationLevel.SERIALIZABLE
        try:
            account_ids: dict[str, int] = {}
            for entry in txn.entries:
                if entry.account_name in account_ids:
                    continue
                row = conn.execute(
                    "SELECT id FROM accounts WHERE name = %s", (entry.account_name,)
                ).fetchone()
                if row is None:
                    raise ValueError(f"unknown account: {entry.account_name!r}")
                account_ids[entry.account_name] = row[0]

            conn.execute(
                "INSERT INTO transactions"
                " (id, stripe_event_id, stripe_object_id, event_type, occurred_at, memo)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    txn.id,
                    txn.stripe_event_id,
                    txn.stripe_object_id,
                    txn.event_type,
                    txn.occurred_at,
                    txn.memo,
                ),
            )
            for entry in txn.entries:
                conn.execute(
                    "INSERT INTO entries"
                    " (transaction_id, account_id, dir, amount_minor, currency)"
                    " VALUES (%s, %s, %s, %s, %s)",
                    (
                        txn.id,
                        account_ids[entry.account_name],
                        entry.dir,
                        entry.amount_minor,
                        entry.currency,
                    ),
                )
            conn.commit()  # deferred triggers judge balance/min-entries here
            return PostOutcome.POSTED
        except psycopg.errors.UniqueViolation as exc:
            if exc.diag.constraint_name in _DEDUPE_CONSTRAINTS:
                conn.rollback()
                return PostOutcome.DUPLICATE
            raise
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()
