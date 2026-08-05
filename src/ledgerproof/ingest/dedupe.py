"""Webhook dedupe — both documented keys, transactional-outbox statuses.

Contract: an event is a duplicate if EITHER key has been seen:
  1. event.id
  2. (event.type, money_movement_object_id) — the id of the object that MOVED
     MONEY, not merely data.object.id (two partial refunds of one charge are
     two distinct money movements and must not collide).
Stripe documents that two distinct Event objects can be generated for the same
underlying state change, so event.id alone is insufficient. This layer is
best-effort fast-path; the database's unique constraints on transactions are
the authoritative last line of defense (the CONCURRENT_DUPLICATE fault attacks
exactly the gap between the two).

Outbox shape: check_and_record inserts with status 'received'; only a
successful enqueue advances the row to 'queued' (mark_queued). A duplicate
therefore reports the blocking row so the caller can recover a
recorded-but-never-enqueued ('received') event instead of telling Stripe to
stop retrying it.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg
from psycopg.types.json import Json

from ledgerproof.stripe_io.mapping import money_movement_object_id


@dataclass(frozen=True)
class DedupeResult:
    """Outcome of Deduper.check_and_record.

    new=True: the event was durably recorded with status 'received'; the
    caller must enqueue it and then mark_queued. new=False: the existing_*
    fields describe the row that blocked the insert (matched by event id or
    by the (event_type, object_id) key) so the caller can distinguish a real
    duplicate from a 'received' row that never reached the queue.
    """

    new: bool
    existing_id: str | None = None
    existing_status: str | None = None
    existing_payload: dict | None = None


class Deduper:
    """Fast-path dedupe backed by the stripe_events table."""

    def __init__(self, db_url: str) -> None:
        self._db_url = db_url

    def check_and_record(self, event: dict) -> DedupeResult:
        """Record the event as 'received'; report the blocking row on duplicate.

        A single INSERT lets Postgres judge both keys atomically: a unique
        violation on the primary key (event.id) or on the
        (event_type, object_id) constraint means this state change was already
        recorded. Rows with a NULL object_id never collide on the second key
        (SQL NULLs are distinct), which is the desired behavior for events
        whose payload carries no object id.

        Status is 'received', NOT 'queued': the row is durable before the
        enqueue runs, and only a successful enqueue (mark_queued) may claim
        the event reached the queue.
        """
        object_id = money_movement_object_id(event)
        with psycopg.connect(self._db_url) as conn:
            try:
                conn.execute(
                    "INSERT INTO stripe_events (id, event_type, object_id, payload, status)"
                    " VALUES (%s, %s, %s, %s, 'received')",
                    (event["id"], event["type"], object_id, Json(event)),
                )
            except psycopg.errors.UniqueViolation:
                conn.rollback()
                row = conn.execute(
                    "SELECT id, status, payload FROM stripe_events"
                    " WHERE id = %(id)s"
                    "    OR (event_type = %(event_type)s AND object_id = %(object_id)s)"
                    " ORDER BY (id = %(id)s) DESC"
                    " LIMIT 1",
                    {
                        "id": event["id"],
                        "event_type": event["type"],
                        "object_id": object_id,
                    },
                ).fetchone()
                if row is None:  # pragma: no cover — nothing deletes stripe_events rows
                    return DedupeResult(new=False)
                return DedupeResult(
                    new=False,
                    existing_id=row[0],
                    existing_status=row[1],
                    existing_payload=row[2],
                )
        return DedupeResult(new=True)

    def mark_queued(self, event_id: str) -> None:
        """Advance 'received' -> 'queued' after a successful enqueue.

        Guarded on status = 'received' so a late or racing ingress request can
        never clobber a worker-written status ('processed', 'no_money_moved',
        'failed').
        """
        with psycopg.connect(self._db_url) as conn:
            conn.execute(
                "UPDATE stripe_events SET status = 'queued'"
                " WHERE id = %s AND status = 'received'",
                (event_id,),
            )
