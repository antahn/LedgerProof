"""Unit tests for stripe_io.mapping — pure event -> balanced transaction.

No DB, no network: fixtures are minimal realistic Stripe event envelopes
({"id": "evt_...", "type": ..., "created": unix, "data": {"object": {...}}}).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from ledgerproof.ledger.post import Entry, LedgerTransaction
from ledgerproof.stripe_io.mapping import MissingFeeData, build_transaction

CREATED = 1754300000  # 2026-08-04T09:33:20Z


def envelope(event_id: str, event_type: str, obj: dict, created: int = CREATED) -> dict:
    return {
        "id": event_id,
        "object": "event",
        "type": event_type,
        "created": created,
        "data": {"object": obj},
    }


def charge_succeeded_event(**overrides) -> dict:
    obj = {
        "id": "ch_3QwXyz0001",
        "object": "charge",
        "amount": 5000,
        "currency": "usd",
        "balance_transaction": {
            "id": "txn_1QwXyzBt01",
            "object": "balance_transaction",
            "amount": 5000,
            "fee": 175,
            "net": 4825,
        },
    }
    obj.update(overrides)
    return envelope("evt_charge_ok_01", "charge.succeeded", obj)


def debit_total(txn: LedgerTransaction) -> int:
    return sum(e.amount_minor for e in txn.entries if e.dir == "debit")


def credit_total(txn: LedgerTransaction) -> int:
    return sum(e.amount_minor for e in txn.entries if e.dir == "credit")


def entry_for(txn: LedgerTransaction, account: str) -> Entry:
    matches = [e for e in txn.entries if e.account_name == account]
    assert len(matches) == 1, f"expected exactly one entry for {account}, got {matches}"
    return matches[0]


def assert_balanced(txn: LedgerTransaction) -> None:
    assert debit_total(txn) == credit_total(txn)
    assert len(txn.entries) >= 2
    assert all(e.amount_minor > 0 for e in txn.entries)


# --- charge.succeeded / payment_intent.succeeded -----------------------------


def test_charge_succeeded_expanded_balance_transaction():
    txn = build_transaction(charge_succeeded_event())
    assert txn is not None
    assert_balanced(txn)
    assert len(txn.entries) == 3

    net = entry_for(txn, "stripe_balance")
    assert (net.dir, net.amount_minor) == ("debit", 4825)
    fee = entry_for(txn, "processing_fees")
    assert (fee.dir, fee.amount_minor) == ("debit", 175)
    gross = entry_for(txn, "revenue")
    assert (gross.dir, gross.amount_minor) == ("credit", 5000)

    assert txn.stripe_event_id == "evt_charge_ok_01"
    assert txn.stripe_object_id == "ch_3QwXyz0001"
    assert txn.event_type == "charge.succeeded"
    assert txn.occurred_at == datetime.fromtimestamp(CREATED, tz=UTC)
    assert txn.occurred_at.tzinfo is not None


def test_payment_intent_succeeded_uses_amount_received_and_fee_param():
    obj = {
        "id": "pi_3QwXyz0002",
        "object": "payment_intent",
        "amount": 12000,
        "amount_received": 12000,
        "currency": "usd",
        "latest_charge": "ch_3QwXyz0002",
    }
    event = envelope("evt_pi_ok_01", "payment_intent.succeeded", obj)
    txn = build_transaction(event, fee_minor=378)
    assert txn is not None
    assert_balanced(txn)
    assert entry_for(txn, "stripe_balance").amount_minor == 12000 - 378
    assert entry_for(txn, "processing_fees").amount_minor == 378
    assert entry_for(txn, "revenue").amount_minor == 12000
    assert txn.stripe_object_id == "pi_3QwXyz0002"


def test_payment_intent_succeeded_fee_from_expanded_latest_charge():
    obj = {
        "id": "pi_3QwXyz0003",
        "object": "payment_intent",
        "amount": 8000,
        "amount_received": 8000,
        "currency": "usd",
        "latest_charge": {
            "id": "ch_3QwXyz0003",
            "object": "charge",
            "balance_transaction": {"id": "txn_bt03", "fee": 262},
        },
    }
    txn = build_transaction(envelope("evt_pi_ok_02", "payment_intent.succeeded", obj))
    assert txn is not None
    assert_balanced(txn)
    assert entry_for(txn, "stripe_balance").amount_minor == 8000 - 262
    assert entry_for(txn, "processing_fees").amount_minor == 262


def test_expanded_balance_transaction_wins_over_fee_param():
    txn = build_transaction(charge_succeeded_event(), fee_minor=999)
    assert txn is not None
    assert entry_for(txn, "processing_fees").amount_minor == 175


def test_charge_fee_via_fee_minor_when_unexpanded():
    event = charge_succeeded_event(balance_transaction="txn_1QwXyzBt01")
    txn = build_transaction(event, fee_minor=175)
    assert txn is not None
    assert_balanced(txn)
    assert entry_for(txn, "processing_fees").amount_minor == 175
    assert entry_for(txn, "stripe_balance").amount_minor == 4825


def test_missing_fee_data_carries_balance_transaction_id():
    event = charge_succeeded_event(balance_transaction="txn_1QwXyzBt01")
    with pytest.raises(MissingFeeData) as excinfo:
        build_transaction(event)
    assert excinfo.value.balance_transaction_id == "txn_1QwXyzBt01"


def test_missing_fee_data_with_no_balance_transaction_at_all():
    event = charge_succeeded_event(balance_transaction=None)
    with pytest.raises(MissingFeeData) as excinfo:
        build_transaction(event)
    assert excinfo.value.balance_transaction_id is None


def test_zero_fee_charge_maps_to_two_entries_and_balances():
    event = charge_succeeded_event(
        balance_transaction={"id": "txn_free", "fee": 0, "amount": 5000, "net": 5000}
    )
    txn = build_transaction(event)
    assert txn is not None
    assert_balanced(txn)
    assert len(txn.entries) == 2
    assert all(e.account_name != "processing_fees" for e in txn.entries)
    assert entry_for(txn, "stripe_balance").amount_minor == 5000
    assert entry_for(txn, "revenue").amount_minor == 5000


# --- charge.refunded ---------------------------------------------------------


def test_charge_refunded():
    obj = {
        "id": "ch_3QwXyz0004",
        "object": "charge",
        "amount": 5000,
        "amount_refunded": 5000,
        "currency": "usd",
        "refunded": True,
    }
    txn = build_transaction(envelope("evt_refund_01", "charge.refunded", obj))
    assert txn is not None
    assert_balanced(txn)
    assert len(txn.entries) == 2
    assert (entry_for(txn, "refunds_contra").dir, entry_for(txn, "refunds_contra").amount_minor) == (
        "debit",
        5000,
    )
    assert (entry_for(txn, "stripe_balance").dir, entry_for(txn, "stripe_balance").amount_minor) == (
        "credit",
        5000,
    )


def test_partial_refund_uses_amount_refunded_not_amount():
    obj = {
        "id": "ch_3QwXyz0005",
        "object": "charge",
        "amount": 5000,
        "amount_refunded": 1200,
        "currency": "usd",
        "refunded": False,
    }
    txn = build_transaction(envelope("evt_refund_02", "charge.refunded", obj))
    assert txn is not None
    assert_balanced(txn)
    assert entry_for(txn, "refunds_contra").amount_minor == 1200
    assert entry_for(txn, "stripe_balance").amount_minor == 1200


# --- charge.dispute.created --------------------------------------------------


def dispute_event(**overrides) -> dict:
    obj = {
        "id": "dp_1QwXyz0006",
        "object": "dispute",
        "amount": 5000,
        "currency": "usd",
        "charge": "ch_3QwXyz0006",
        "reason": "fraudulent",
        "balance_transactions": [
            {"id": "txn_disp01", "object": "balance_transaction", "amount": -5000, "fee": 1500, "net": -6500}
        ],
    }
    obj.update(overrides)
    return envelope("evt_dispute_01", "charge.dispute.created", obj)


def test_dispute_created_fee_from_balance_transactions_list():
    txn = build_transaction(dispute_event())
    assert txn is not None
    assert_balanced(txn)
    assert len(txn.entries) == 3
    assert (entry_for(txn, "dispute_losses").dir, entry_for(txn, "dispute_losses").amount_minor) == (
        "debit",
        5000,
    )
    assert entry_for(txn, "processing_fees").amount_minor == 1500
    assert entry_for(txn, "processing_fees").dir == "debit"
    assert (entry_for(txn, "stripe_balance").dir, entry_for(txn, "stripe_balance").amount_minor) == (
        "credit",
        6500,
    )
    assert txn.stripe_object_id == "dp_1QwXyz0006"


def test_dispute_fee_sums_across_balance_transactions():
    event = dispute_event(
        balance_transactions=[
            {"id": "txn_disp01", "fee": 1500},
            {"id": "txn_disp02", "fee": 250},
        ]
    )
    txn = build_transaction(event)
    assert txn is not None
    assert_balanced(txn)
    assert entry_for(txn, "processing_fees").amount_minor == 1750
    assert entry_for(txn, "stripe_balance").amount_minor == 6750


def test_dispute_fee_via_fee_minor_when_unexpanded():
    event = dispute_event(balance_transactions=["txn_disp01"])
    txn = build_transaction(event, fee_minor=1500)
    assert txn is not None
    assert_balanced(txn)
    assert entry_for(txn, "processing_fees").amount_minor == 1500


def test_dispute_missing_fee_data_carries_first_string_id():
    event = dispute_event(balance_transactions=["txn_disp01"])
    with pytest.raises(MissingFeeData) as excinfo:
        build_transaction(event)
    assert excinfo.value.balance_transaction_id == "txn_disp01"


def test_dispute_missing_fee_data_empty_list():
    event = dispute_event(balance_transactions=[])
    with pytest.raises(MissingFeeData) as excinfo:
        build_transaction(event)
    assert excinfo.value.balance_transaction_id is None


# --- payout.paid -------------------------------------------------------------


def test_payout_paid():
    obj = {
        "id": "po_1QwXyz0007",
        "object": "payout",
        "amount": 250000,
        "currency": "usd",
        "status": "paid",
    }
    txn = build_transaction(envelope("evt_payout_01", "payout.paid", obj))
    assert txn is not None
    assert_balanced(txn)
    assert len(txn.entries) == 2
    assert (entry_for(txn, "bank").dir, entry_for(txn, "bank").amount_minor) == ("debit", 250000)
    assert (entry_for(txn, "stripe_balance").dir, entry_for(txn, "stripe_balance").amount_minor) == (
        "credit",
        250000,
    )


# --- non-money and unhandled events ------------------------------------------


def test_invoice_payment_failed_returns_none():
    obj = {
        "id": "in_1QwXyz0008",
        "object": "invoice",
        "amount_due": 999,
        "currency": "usd",
        "status": "open",
    }
    assert build_transaction(envelope("evt_invfail_01", "invoice.payment_failed", obj)) is None


def test_unhandled_event_type_raises_value_error():
    obj = {"id": "cus_1QwXyz0009", "object": "customer"}
    with pytest.raises(ValueError, match="unhandled event type"):
        build_transaction(envelope("evt_customer_01", "customer.created", obj))


# --- id determinism, currency ------------------------------------------------


def test_transaction_id_is_deterministic_uuid5():
    txn_a = build_transaction(charge_succeeded_event())
    txn_b = build_transaction(charge_succeeded_event())
    assert txn_a is not None and txn_b is not None
    assert txn_a.id == txn_b.id
    assert txn_a.id == uuid.uuid5(uuid.NAMESPACE_URL, "ledgerproof:evt_charge_ok_01")


def test_distinct_events_get_distinct_ids():
    event_b = charge_succeeded_event()
    event_b["id"] = "evt_charge_ok_02"
    txn_a = build_transaction(charge_succeeded_event())
    txn_b = build_transaction(event_b)
    assert txn_a is not None and txn_b is not None
    assert txn_a.id != txn_b.id


def test_currency_is_uppercased_on_every_entry():
    txn = build_transaction(charge_succeeded_event())
    assert txn is not None
    assert all(e.currency == "USD" for e in txn.entries)

    obj = {
        "id": "po_1QwXyz0010",
        "object": "payout",
        "amount": 100,
        "currency": "eur",
        "status": "paid",
    }
    txn_eur = build_transaction(envelope("evt_payout_eur_01", "payout.paid", obj))
    assert txn_eur is not None
    assert all(e.currency == "EUR" for e in txn_eur.entries)
