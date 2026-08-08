"""Map Stripe events to balanced ledger transactions.

Contract (each row balances):
- charge.succeeded / payment_intent.succeeded:
    DR stripe_balance (net), DR processing_fees (fee) / CR revenue (gross)
    The PAIR is ONE money movement — dispatch subscribes to charge.succeeded
    only (see build_transaction's docstring).
- charge.refunded:  DR refunds_contra / CR stripe_balance
    Amount and object id come from the NEWEST refund (obj.refunds.data[0])
    when the refunds list is present: each charge.refunded event posts ITS
    refund's delta, so two partial refunds are two balanced transactions.
- charge.dispute.created:
    DR dispute_losses (amount), DR processing_fees (dispute fee)
    / CR stripe_balance (amount + fee)
- payout.paid:      DR bank / CR stripe_balance
- invoice.payment_failed: NO money moved — an event-log row, NOT a transaction.
  Recording a transaction for a non-money event is itself a way to create
  money from nothing.

Amounts are Stripe minor units (int). Output is data (transaction + entries);
this module performs no I/O and never decides idempotency — that lives in
dedupe and in the database's unique constraints.

Additional behavior:
- build_transaction returns None ONLY for invoice.payment_failed; any other
  unrecognized event type raises ValueError so the worker's dispatch layer —
  not this module — decides what is ignorable.
- Transaction ids are deterministic: uuid5(NAMESPACE_URL, "ledgerproof:" +
  event.id), so re-mapping the same event yields the same id.
- Fee resolution (charge.succeeded / payment_intent.succeeded /
  charge.dispute.created): an expanded balance_transaction(s) in the payload
  wins; otherwise the fee_minor parameter; otherwise MissingFeeData carrying
  the unexpanded balance transaction id so the handler can fetch and retry.
- Zero-amount entries are skipped (the DB requires amount_minor > 0); a
  zero-fee charge maps to a two-entry transaction that still balances.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from ledgerproof.ledger.post import Direction, Entry, LedgerTransaction


def money_movement_object_id(event: dict) -> str | None:
    """The id of the OBJECT THAT MOVED MONEY, not merely data.object.id.

    For charge.refunded this is the newest refund's id (obj.refunds.data[0].id)
    when present — two partial refunds of one charge are two distinct money
    movements and must not collide on the (event_type, object_id) dedupe key.
    Falls back to data.object.id; None when the payload lacks an id.
    """
    data_obj = event.get("data", {}).get("object")
    if not isinstance(data_obj, dict):
        return None
    if event.get("type") == "charge.refunded":
        refund = _newest_refund(data_obj)
        if refund is not None:
            rid = refund.get("id")
            if isinstance(rid, str):
                return rid
    oid = data_obj.get("id")
    return oid if isinstance(oid, str) else None


def _newest_refund(obj: dict) -> dict | None:
    """obj.refunds.data[0] (Stripe lists refunds newest-first), else None."""
    refunds = obj.get("refunds")
    if not isinstance(refunds, dict):
        return None
    data = refunds.get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return None


class MissingFeeData(Exception):
    """The payload alone cannot resolve the fee.

    balance_transaction_id is the unexpanded balance transaction id if the
    payload carried one (else None); the handler should fetch it from Stripe
    and call build_transaction again with fee_minor.
    """

    def __init__(self, balance_transaction_id: str | None = None) -> None:
        super().__init__(
            "fee not present in payload; fetch balance transaction "
            f"{balance_transaction_id!r} and retry with fee_minor"
        )
        self.balance_transaction_id = balance_transaction_id


def _charge_fee(obj: dict, fee_minor: int | None) -> int:
    """Fee for a charge or payment_intent object, per the pinned order."""
    bt = obj.get("balance_transaction")
    if bt is None:
        # payment_intent payloads carry the balance transaction on the
        # expanded latest_charge, not at top level.
        latest_charge = obj.get("latest_charge")
        if isinstance(latest_charge, dict):
            bt = latest_charge.get("balance_transaction")
    if isinstance(bt, dict):
        return int(bt["fee"])
    if fee_minor is not None:
        return fee_minor
    raise MissingFeeData(bt if isinstance(bt, str) else None)


def _dispute_fee(obj: dict, fee_minor: int | None) -> int:
    """Dispute fee: sum of fee fields across the balance_transactions list."""
    bts = obj.get("balance_transactions") or []
    if bts and all(isinstance(bt, dict) for bt in bts):
        return sum(int(bt["fee"]) for bt in bts)
    if fee_minor is not None:
        return fee_minor
    bt_id = next((bt for bt in bts if isinstance(bt, str)), None)
    raise MissingFeeData(bt_id)


def build_transaction(event: dict, *, fee_minor: int | None = None) -> LedgerTransaction | None:
    """Build a balanced LedgerTransaction from a Stripe event envelope.

    Returns None iff the event moves no money (invoice.payment_failed).
    Raises ValueError for any event type this mapping does not handle, and
    MissingFeeData when a required fee cannot be resolved from the payload
    or the fee_minor parameter.

    payment_intent.succeeded is a documented ALTERNATE mapping of the same
    money movement as charge.succeeded, unsubscribed by default: a normal
    PaymentIntents payment emits BOTH events for ONE payment, keyed by
    different object ids (pi_... vs ch_...), so subscribing to both without
    normalization double-posts the payment. That is why the worker's dispatch
    (handlers.HANDLED_EVENT_TYPES) handles only charge.succeeded — the charge
    carries the money. Real PI payloads also carry latest_charge as a STRING
    id, not an expanded object, so the fee usually needs a fetch anyway.
    """
    etype: str = event["type"]
    obj: dict = event["data"]["object"]

    if etype == "invoice.payment_failed":
        return None

    handled = {
        "charge.succeeded",
        "payment_intent.succeeded",
        "charge.refunded",
        "charge.dispute.created",
        "payout.paid",
    }
    if etype not in handled:
        raise ValueError(f"unhandled event type: {etype}")

    currency = obj["currency"].upper()
    entries: list[Entry] = []

    def add(account: str, dir_: Direction, amount: int) -> None:
        if amount != 0:
            entries.append(
                Entry(account_name=account, dir=dir_, amount_minor=amount, currency=currency)
            )

    if etype in ("charge.succeeded", "payment_intent.succeeded"):
        gross = obj["amount_received"] if etype == "payment_intent.succeeded" else obj["amount"]
        fee = _charge_fee(obj, fee_minor)
        add("stripe_balance", "debit", gross - fee)
        add("processing_fees", "debit", fee)
        add("revenue", "credit", gross)
    elif etype == "charge.refunded":
        # Each charge.refunded event posts ITS refund's delta — the newest
        # refund's amount — never the cumulative amount_refunded, so a second
        # partial refund is a second balanced transaction instead of a dropped
        # duplicate. Fall back to amount_refunded when the refunds list is
        # absent (then it equals the single refund's amount).
        refund = _newest_refund(obj)
        if refund is not None and "amount" in refund:
            amount = refund["amount"]
        else:
            amount = obj["amount_refunded"]
        add("refunds_contra", "debit", amount)
        add("stripe_balance", "credit", amount)
    elif etype == "charge.dispute.created":
        amount = obj["amount"]
        fee = _dispute_fee(obj, fee_minor)
        add("dispute_losses", "debit", amount)
        add("processing_fees", "debit", fee)
        add("stripe_balance", "credit", amount + fee)
    else:  # payout.paid
        amount = obj["amount"]
        add("bank", "debit", amount)
        add("stripe_balance", "credit", amount)

    return LedgerTransaction(
        id=uuid.uuid5(uuid.NAMESPACE_URL, "ledgerproof:" + event["id"]),
        occurred_at=datetime.fromtimestamp(event["created"], tz=UTC),
        entries=tuple(entries),
        stripe_event_id=event["id"],
        stripe_object_id=money_movement_object_id(event),
        event_type=etype,
    )
