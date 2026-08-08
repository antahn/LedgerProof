"""Stripe event fixtures with ground-truth ledger deltas (Phase 2).

Every fixture carries `expected_delta`: the signed, minor-unit change each
account SHOULD show once this event is posted. That is the harness's ground
truth — the free-labels insight the benchmark rests on — so it is derived from
the double-entry mapping table independently of `stripe_io.mapping`, and
`tests/chaos/test_faults.py` asserts the two agree.
If they ever disagree, one of them is wrong and the disagreement is the bug.

Sign convention (identical to the `account_balances` view): a debit entry ADDS
to a debit-normal account and SUBTRACTS from a credit-normal one. Accounts
whose net change is zero are omitted, because the mapping skips zero-amount
entries (the database requires amount_minor > 0).

Payload realism matters: these fixtures stand in for real deliveries, so they
carry the shapes real Stripe objects have — expanded balance transactions,
newest-first refund lists, `source` back-references on balance transactions.
charge.succeeded deliberately carries an EXPANDED balance_transaction so the
worker resolves the fee from the payload and the harness's core loop needs no
live Stripe call. (Real deliveries carry `balance_transaction: null` until the
charge settles — FINDINGS R6 — which is a handler concern, not a chaos one.)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

# Mirrors migrations/002_seed_accounts.sql. Duplicated here on purpose: the
# harness must be able to state what the ledger SHOULD hold without querying
# the ledger it is auditing.
ACCOUNT_NORMALS: dict[str, str] = {
    "stripe_balance": "debit",
    "bank": "debit",
    "processing_fees": "debit",
    "dispute_losses": "debit",
    "refunds_contra": "debit",
    "revenue": "credit",
    "customer_liability": "credit",
}

ALL_KINDS: tuple[str, ...] = (
    "charge_succeeded",
    "charge_refunded",
    "dispute_created",
    "payout_paid",
    "invoice_payment_failed",
)

# Fixture api_version is cosmetic: nothing in this system dispatches on it.
_API_VERSION = "2025-06-30.basil"


@dataclass(frozen=True)
class EventFixture:
    """A Stripe event envelope plus what posting it should do to the ledger."""

    event: dict
    kind: str
    expected_delta: dict[str, int]

    @property
    def event_id(self) -> str:
        return self.event["id"]


def serialize(event: dict) -> bytes:
    """Canonical raw body bytes for an event.

    The signature covers these exact bytes; ingest verifies against the raw
    body, so the harness must sign and send one identical byte string and
    never re-serialize between the two.
    """
    return json.dumps(event, separators=(",", ":"), sort_keys=False).encode()


def signed_delta(account: str, dir_: str, amount_minor: int) -> int:
    """Signed balance change of one entry, per the account's normal side."""
    return amount_minor if dir_ == ACCOUNT_NORMALS[account] else -amount_minor


def _delta(entries: list[tuple[str, str, int]]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for account, dir_, amount in entries:
        if amount == 0:  # the mapping skips zero-amount entries; so do we
            continue
        totals[account] = totals.get(account, 0) + signed_delta(account, dir_, amount)
    return totals


def _now(created: int | None) -> int:
    return int(time.time()) if created is None else created


def _sibling_id(base: str, prefix: str) -> str:
    """`ch_h000001` + `txn` -> `txn_h000001`: related ids share a suffix."""
    suffix = base.split("_", 1)[1] if "_" in base else base
    return f"{prefix}_{suffix}"


def _envelope(event_id: str, event_type: str, obj: dict, created: int) -> dict:
    return {
        "id": event_id,
        "object": "event",
        "api_version": _API_VERSION,
        "created": created,
        "data": {"object": obj},
        "livemode": False,
        "pending_webhooks": 1,
        "request": {"id": None, "idempotency_key": None},
        "type": event_type,
    }


def _balance_transaction(
    bt_id: str,
    *,
    source: str,
    amount: int,
    fee: int,
    created: int,
    currency: str,
    reporting_category: str,
    bt_type: str,
) -> dict:
    return {
        "id": bt_id,
        "object": "balance_transaction",
        "amount": amount,
        "available_on": created + 172_800,
        "created": created,
        "currency": currency,
        "description": None,
        "exchange_rate": None,
        "fee": fee,
        "fee_details": [
            {
                "amount": fee,
                "application": None,
                "currency": currency,
                "description": "Stripe processing fees",
                "type": "stripe_fee",
            }
        ]
        if fee
        else [],
        "net": amount - fee,
        "reporting_category": reporting_category,
        # The reconciler's Stripe-side path joins balance transactions to the
        # ledger through this field.
        "source": source,
        "status": "available",
        "type": bt_type,
    }


def make_charge_succeeded(
    *,
    event_id: str,
    charge_id: str,
    amount: int = 1000,
    fee: int = 59,
    created: int | None = None,
    currency: str = "usd",
) -> EventFixture:
    """DR stripe_balance (net), DR processing_fees (fee) / CR revenue (gross)."""
    created = _now(created)
    obj = {
        "id": charge_id,
        "object": "charge",
        "amount": amount,
        "amount_captured": amount,
        "amount_refunded": 0,
        "application_fee_amount": None,
        "balance_transaction": _balance_transaction(
            _sibling_id(charge_id, "txn"),
            source=charge_id,
            amount=amount,
            fee=fee,
            created=created,
            currency=currency,
            reporting_category="charge",
            bt_type="charge",
        ),
        "captured": True,
        "created": created,
        "currency": currency,
        "customer": _sibling_id(charge_id, "cus"),
        "description": "LedgerProof chaos harness",
        "disputed": False,
        "failure_code": None,
        "failure_message": None,
        "livemode": False,
        "metadata": {},
        "paid": True,
        "payment_intent": _sibling_id(charge_id, "pi"),
        "payment_method": _sibling_id(charge_id, "pm"),
        "payment_method_details": {
            "card": {
                "brand": "visa",
                "country": "US",
                "exp_month": 12,
                "exp_year": 2030,
                "funding": "credit",
                "last4": "4242",
                "network": "visa",
            },
            "type": "card",
        },
        "refunded": False,
        "refunds": {
            "object": "list",
            "data": [],
            "has_more": False,
            "total_count": 0,
            "url": f"/v1/charges/{charge_id}/refunds",
        },
        "status": "succeeded",
    }
    return EventFixture(
        event=_envelope(event_id, "charge.succeeded", obj, created),
        kind="charge_succeeded",
        expected_delta=_delta(
            [
                ("stripe_balance", "debit", amount - fee),
                ("processing_fees", "debit", fee),
                ("revenue", "credit", amount),
            ]
        ),
    )


def make_charge_refunded(
    *,
    event_id: str,
    charge_id: str,
    refund_id: str,
    amount: int = 1000,
    refunded: int = 400,
    created: int | None = None,
    currency: str = "usd",
) -> EventFixture:
    """DR refunds_contra / CR stripe_balance, for THIS refund's amount.

    `data.object` is the CHARGE (that is what Stripe sends), and the money that
    moved is the newest refund — `refunds.data[0]`, which Stripe lists
    newest-first. `refunded` is this refund's own amount, not the cumulative
    `amount_refunded`; posting the cumulative figure double-counts earlier
    partial refunds (FINDINGS R3).
    """
    created = _now(created)
    refund = {
        "id": refund_id,
        "object": "refund",
        "amount": refunded,
        "balance_transaction": _sibling_id(refund_id, "txn"),
        "charge": charge_id,
        "created": created,
        "currency": currency,
        "metadata": {},
        "payment_intent": _sibling_id(charge_id, "pi"),
        "reason": None,
        "receipt_number": None,
        "status": "succeeded",
    }
    obj = {
        "id": charge_id,
        "object": "charge",
        "amount": amount,
        "amount_captured": amount,
        "amount_refunded": refunded,
        "captured": True,
        "created": created,
        "currency": currency,
        "customer": _sibling_id(charge_id, "cus"),
        "disputed": False,
        "livemode": False,
        "metadata": {},
        "paid": True,
        "payment_intent": _sibling_id(charge_id, "pi"),
        "refunded": refunded >= amount,
        "refunds": {
            "object": "list",
            "data": [refund],
            "has_more": False,
            "total_count": 1,
            "url": f"/v1/charges/{charge_id}/refunds",
        },
        "status": "succeeded",
    }
    return EventFixture(
        event=_envelope(event_id, "charge.refunded", obj, created),
        kind="charge_refunded",
        expected_delta=_delta(
            [
                ("refunds_contra", "debit", refunded),
                ("stripe_balance", "credit", refunded),
            ]
        ),
    )


def make_dispute_created(
    *,
    event_id: str,
    dispute_id: str,
    charge_id: str,
    amount: int = 2000,
    fee: int = 1500,
    created: int | None = None,
    currency: str = "usd",
) -> EventFixture:
    """DR dispute_losses (amount), DR processing_fees (fee) / CR stripe_balance."""
    created = _now(created)
    obj = {
        "id": dispute_id,
        "object": "dispute",
        "amount": amount,
        "balance_transactions": [
            _balance_transaction(
                _sibling_id(dispute_id, "txn"),
                source=dispute_id,
                amount=-amount,
                fee=fee,
                created=created,
                currency=currency,
                reporting_category="dispute",
                bt_type="adjustment",
            )
        ],
        "charge": charge_id,
        "created": created,
        "currency": currency,
        "evidence_details": {
            "due_by": created + 604_800,
            "has_evidence": False,
            "past_due": False,
            "submission_count": 0,
        },
        "is_charge_refundable": False,
        "livemode": False,
        "metadata": {},
        "payment_intent": _sibling_id(charge_id, "pi"),
        "reason": "fraudulent",
        "status": "needs_response",
    }
    return EventFixture(
        event=_envelope(event_id, "charge.dispute.created", obj, created),
        kind="dispute_created",
        expected_delta=_delta(
            [
                ("dispute_losses", "debit", amount),
                ("processing_fees", "debit", fee),
                ("stripe_balance", "credit", amount + fee),
            ]
        ),
    )


def make_payout_paid(
    *,
    event_id: str,
    payout_id: str,
    amount: int = 5000,
    created: int | None = None,
    currency: str = "usd",
) -> EventFixture:
    """DR bank / CR stripe_balance."""
    created = _now(created)
    obj = {
        "id": payout_id,
        "object": "payout",
        "amount": amount,
        "arrival_date": created + 172_800,
        "automatic": True,
        "balance_transaction": _sibling_id(payout_id, "txn"),
        "created": created,
        "currency": currency,
        "description": "STRIPE PAYOUT",
        "destination": _sibling_id(payout_id, "ba"),
        "failure_code": None,
        "failure_message": None,
        "livemode": False,
        "metadata": {},
        "method": "standard",
        "reconciliation_status": "completed",
        "source_type": "card",
        "status": "paid",
        "type": "bank_account",
    }
    return EventFixture(
        event=_envelope(event_id, "payout.paid", obj, created),
        kind="payout_paid",
        expected_delta=_delta(
            [
                ("bank", "debit", amount),
                ("stripe_balance", "credit", amount),
            ]
        ),
    )


def make_invoice_payment_failed(
    *,
    event_id: str,
    invoice_id: str,
    amount: int = 1000,
    created: int | None = None,
    currency: str = "usd",
) -> EventFixture:
    """NO money moved: an event-log row, never a transaction (§4.3).

    expected_delta is empty on purpose — recording a transaction for a
    non-money event is itself a way to create money from nothing, so "nothing
    changed" is the ground truth this fixture asserts.
    """
    created = _now(created)
    obj = {
        "id": invoice_id,
        "object": "invoice",
        "amount_due": amount,
        "amount_paid": 0,
        "amount_remaining": amount,
        "attempt_count": 1,
        "attempted": True,
        "auto_advance": True,
        "billing_reason": "subscription_cycle",
        "charge": None,
        "collection_method": "charge_automatically",
        "created": created,
        "currency": currency,
        "customer": _sibling_id(invoice_id, "cus"),
        "livemode": False,
        "metadata": {},
        "next_payment_attempt": created + 3600,
        "paid": False,
        "payment_intent": _sibling_id(invoice_id, "pi"),
        "status": "open",
        "subscription": _sibling_id(invoice_id, "sub"),
        "subtotal": amount,
        "total": amount,
    }
    return EventFixture(
        event=_envelope(event_id, "invoice.payment_failed", obj, created),
        kind="invoice_payment_failed",
        expected_delta={},
    )


_MAKERS = {
    "charge_succeeded": make_charge_succeeded,
    "charge_refunded": make_charge_refunded,
    "dispute_created": make_dispute_created,
    "payout_paid": make_payout_paid,
    "invoice_payment_failed": make_invoice_payment_failed,
}


def _ids_for(kind: str, seq: int) -> dict[str, str]:
    tag = f"h{seq:06d}"
    ids: dict[str, str] = {"event_id": f"evt_{tag}"}
    if kind == "charge_succeeded":
        ids["charge_id"] = f"ch_{tag}"
    elif kind == "charge_refunded":
        ids["charge_id"] = f"ch_{tag}"
        ids["refund_id"] = f"re_{tag}"
    elif kind == "dispute_created":
        ids["dispute_id"] = f"dp_{tag}"
        ids["charge_id"] = f"ch_{tag}"
    elif kind == "payout_paid":
        ids["payout_id"] = f"po_{tag}"
    else:
        ids["invoice_id"] = f"in_{tag}"
    return ids


def make(kind: str, *, seq: int, created: int | None = None, **overrides) -> EventFixture:
    """A fixture of `kind` whose ids are derived deterministically from `seq`.

    Ids are stable for a given seq and distinct across seqs, so a scenario can
    mint as many events as it likes without ever colliding on a dedupe key.
    Callers must therefore draw seqs from ONE counter for a whole run — two
    kinds built at the same seq share the event id `evt_h<seq>` by design, so
    that reusing a seq is a visible collision rather than a silent one.
    """
    if kind not in _MAKERS:
        raise ValueError(f"unknown fixture kind: {kind!r}; expected one of {ALL_KINDS}")
    kwargs = {**_ids_for(kind, seq), **overrides}
    return _MAKERS[kind](created=created, **kwargs)
