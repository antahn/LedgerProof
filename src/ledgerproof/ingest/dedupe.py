"""Webhook dedupe — both documented keys.

Contract: an event is a duplicate if EITHER key has been seen:
  1. event.id
  2. (event.type, data.object.id)
Stripe documents that two distinct Event objects can be generated for the same
underlying state change, so event.id alone is insufficient. This layer is
best-effort fast-path; the database's unique constraints on transactions are
the authoritative last line of defense (the CONCURRENT_DUPLICATE fault attacks
exactly the gap between the two).
"""

from __future__ import annotations

import psycopg
from psycopg.types.json import Json


def object_id_of(event: dict) -> str | None:
    """data.object.id, or None when the payload lacks one."""
    data_obj = event.get("data", {}).get("object")
    if isinstance(data_obj, dict):
        oid = data_obj.get("id")
        return oid if isinstance(oid, str) else None
    return None


class Deduper:
    """Fast-path dedupe backed by the stripe_events table."""

    def __init__(self, db_url: str) -> None:
        self._db_url = db_url

    def check_and_record(self, event: dict) -> bool:
        """Record the event as queued; True = new, False = duplicate on either key.

        A single INSERT lets Postgres judge both keys atomically: a unique
        violation on the primary key (event.id) or on the
        (event_type, object_id) constraint means this state change was already
        recorded. Rows with a NULL object_id never collide on the second key
        (SQL NULLs are distinct), which is the desired behavior for events
        whose payload carries no object id.
        """
        try:
            with psycopg.connect(self._db_url) as conn:
                conn.execute(
                    "INSERT INTO stripe_events (id, event_type, object_id, payload, status)"
                    " VALUES (%s, %s, %s, %s, 'queued')",
                    (event["id"], event["type"], object_id_of(event), Json(event)),
                )
        except psycopg.errors.UniqueViolation:
            return False
        return True
