"""Per-event-type handlers — order-independent by contract.

Stripe guarantees neither ordering nor exactly-once delivery. Handlers
therefore never assume a prior event has been seen: when a later event arrives
first and references an object we lack, the handler FETCHES the missing object
from the Stripe API using the IDs on hand rather than inventing state or
failing. Duplicate deliveries are no-ops. No handler mutates ledger rows;
corrections are new compensating transactions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import psycopg
from psycopg.types.json import Json

from ledgerproof.ledger.post import PostOutcome, post_transaction
from ledgerproof.stripe_io.mapping import (
    MissingFeeData,
    build_transaction,
    money_movement_object_id,
)

if TYPE_CHECKING:
    from ledgerproof.stripe_io.client import StripeEgressClient

# The subscribed-event source of truth (brief §5.3: subscribe only to what you
# handle). payment_intent.succeeded is deliberately ABSENT: a PaymentIntents
# payment emits it alongside charge.succeeded for the SAME money movement, and
# handling both double-posts the payment — charge.succeeded carries the money.
# A PI event reaching the worker returns 'ignored' like any unhandled type.
HANDLED_EVENT_TYPES = frozenset(
    {
        "charge.succeeded",
        "charge.refunded",
        "charge.dispute.created",
        "payout.paid",
        "invoice.payment_failed",
    }
)


def handle_event(
    event: dict, *, db_url: str, client: StripeEgressClient | None = None
) -> str:
    """Process one queued webhook event; returns what happened.

    Returns 'posted' | 'duplicate' | 'no_money_moved' | 'ignored'. Idempotent
    end-to-end: replaying any event lands on the transactions table's unique
    constraints and comes back 'duplicate' without moving money again.
    """
    event_type = event.get("type", "")
    if event_type not in HANDLED_EVENT_TYPES:
        # 'ignored' is not a stripe_events status; 'no_money_moved' is the
        # closest truth — this event produced no ledger transaction.
        _mark_status(db_url, event, "no_money_moved")
        return "ignored"

    try:
        txn = build_transaction(event)
    except MissingFeeData as exc:
        # Order-independence: the payload lacks the fee, so fetch the
        # balance_transaction by the id on hand — never invent state.
        if client is None or exc.balance_transaction_id is None:
            raise
        fetched = client.get(f"/v1/balance_transactions/{exc.balance_transaction_id}")
        txn = build_transaction(event, fee_minor=fetched["fee"])

    if txn is None:
        # Event moves no money (e.g. invoice.payment_failed): an event-log
        # status, never a transaction — a zero-money transaction is itself a
        # way to create money from nothing.
        _mark_status(db_url, event, "no_money_moved")
        return "no_money_moved"

    try:
        outcome = post_transaction(db_url, txn)
    except psycopg.errors.UniqueViolation as exc:
        # mapping derives txn.id deterministically (uuid5 of event.id), so a
        # replay collides on the transactions PRIMARY KEY before either dedupe
        # constraint — which is the only pair post_transaction maps to
        # DUPLICATE. A pkey hit on a deterministic id can only be the same
        # event again: the same idempotent no-op.
        if exc.diag.constraint_name != "transactions_pkey":
            raise
        outcome = PostOutcome.DUPLICATE
    _mark_status(db_url, event, "processed")
    return "posted" if outcome is PostOutcome.POSTED else "duplicate"


def _mark_status(db_url: str, event: dict, status: str) -> None:
    """Advance the stripe_events row; upsert it if ingest never recorded one.

    Under chaos the worker can receive an event with no event-log row (e.g.
    the row insert lost a race the ledger constraints later resolve). Posting
    already happened safely — the ledger protects correctness — so here we
    just make the event log reflect reality.
    """
    with psycopg.connect(db_url) as conn:
        cur = conn.execute(
            "UPDATE stripe_events SET status = %s WHERE id = %s",
            (status, event["id"]),
        )
        if cur.rowcount:
            return
        try:
            conn.execute(
                "INSERT INTO stripe_events (id, event_type, object_id, payload, status)"
                " VALUES (%s, %s, %s, %s, %s)"
                " ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status",
                (
                    event["id"],
                    event.get("type"),
                    money_movement_object_id(event),
                    Json(event),
                    status,
                ),
            )
        except psycopg.errors.UniqueViolation:
            # A different event.id already holds this (event_type, object_id);
            # that row tracks its own delivery. Nothing more to record.
            conn.rollback()
