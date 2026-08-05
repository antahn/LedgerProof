"""Integration tests for worker handlers (LEDGERPROOF_BRIEF §5.3).

Order-independent, duplicate-tolerant, and they never invent state: a missing
fee is fetched from the (stubbed) Stripe API by id. Handlers run strictly
post-verification, so nothing here is signed.
"""

from __future__ import annotations

import psycopg

from ledgerproof.ledger import invariant
from ledgerproof.worker.handlers import handle_event

CREATED = 1_750_000_000  # any fixed tz-aware-able unix timestamp


class StubStripeClient:
    """Records .get calls and serves canned responses (handlers only .get)."""

    def __init__(self, responses: dict[str, dict] | None = None) -> None:
        self.responses = responses or {}
        self.requests: list[str] = []

    def get(self, path: str, params: dict | None = None) -> dict:
        self.requests.append(path)
        return self.responses[path]


def balances(db_url: str) -> dict[str, int]:
    with psycopg.connect(db_url) as conn:
        return dict(conn.execute("SELECT name, balance_minor FROM account_balances"))


def txn_count(db_url: str) -> int:
    with psycopg.connect(db_url) as conn:
        return conn.execute("SELECT count(*) FROM transactions").fetchone()[0]


def event_status(db_url: str, event_id: str) -> str | None:
    with psycopg.connect(db_url) as conn:
        row = conn.execute(
            "SELECT status FROM stripe_events WHERE id = %s", (event_id,)
        ).fetchone()
        return row[0] if row else None


def wrap(event_id: str, event_type: str, obj: dict) -> dict:
    return {
        "id": event_id,
        "object": "event",
        "type": event_type,
        "created": CREATED,
        "data": {"object": obj},
    }


def charge_succeeded_event(
    event_id: str = "evt_ch_001",
    charge_id: str = "ch_001",
    amount: int = 1000,
    fee: int = 59,
    *,
    expanded: bool = True,
    bt_id: str = "txn_bt_001",
) -> dict:
    bt: dict | str
    if expanded:
        bt = {
            "id": bt_id,
            "object": "balance_transaction",
            "amount": amount,
            "fee": fee,
            "net": amount - fee,
            "currency": "usd",
        }
    else:
        bt = bt_id
    return wrap(
        event_id,
        "charge.succeeded",
        {
            "id": charge_id,
            "object": "charge",
            "amount": amount,
            "currency": "usd",
            "balance_transaction": bt,
        },
    )


def test_charge_succeeded_with_expanded_fee_posts(db_url) -> None:
    result = handle_event(charge_succeeded_event(), db_url=db_url)
    assert result == "posted"
    b = balances(db_url)
    assert b["stripe_balance"] == 941
    assert b["processing_fees"] == 59
    assert b["revenue"] == 1000
    assert invariant.check(db_url).ok
    assert event_status(db_url, "evt_ch_001") == "processed"


def test_same_event_twice_is_duplicate_and_moves_no_money(db_url) -> None:
    event = charge_succeeded_event()
    assert handle_event(event, db_url=db_url) == "posted"
    before = balances(db_url)
    assert handle_event(event, db_url=db_url) == "duplicate"
    assert balances(db_url) == before
    assert txn_count(db_url) == 1
    assert invariant.check(db_url).ok


def test_missing_fee_is_fetched_from_stripe(db_url) -> None:
    event = charge_succeeded_event(event_id="evt_ch_002", charge_id="ch_002", expanded=False)
    stub = StubStripeClient(
        {
            "/v1/balance_transactions/txn_bt_001": {
                "id": "txn_bt_001",
                "object": "balance_transaction",
                "amount": 1000,
                "fee": 59,
                "net": 941,
                "currency": "usd",
            }
        }
    )
    assert handle_event(event, db_url=db_url, client=stub) == "posted"
    assert stub.requests == ["/v1/balance_transactions/txn_bt_001"]
    b = balances(db_url)
    assert b["stripe_balance"] == 941
    assert b["processing_fees"] == 59
    assert b["revenue"] == 1000
    assert invariant.check(db_url).ok


def test_refund_posts_without_prior_charge(db_url) -> None:
    # ORDER-INDEPENDENCE: the charge.succeeded for this charge was never
    # processed. The refund must still post from the payload alone; a negative
    # stripe_balance is fine — conservation is the invariant, not
    # non-negativity.
    event = wrap(
        "evt_re_001",
        "charge.refunded",
        {
            "id": "ch_orphan",
            "object": "charge",
            "amount": 1500,
            "amount_refunded": 1500,
            "refunded": True,
            "currency": "usd",
        },
    )
    assert handle_event(event, db_url=db_url) == "posted"
    b = balances(db_url)
    assert b["refunds_contra"] == 1500
    assert b["stripe_balance"] == -1500
    assert invariant.check(db_url).ok


def test_dispute_posts_amount_and_fee(db_url) -> None:
    event = wrap(
        "evt_dp_001",
        "charge.dispute.created",
        {
            "id": "dp_001",
            "object": "dispute",
            "amount": 2000,
            "currency": "usd",
            "charge": "ch_disputed",
            "balance_transactions": [
                {
                    "id": "txn_dp_001",
                    "object": "balance_transaction",
                    "amount": -2000,
                    "fee": 1500,
                    "net": -3500,
                    "currency": "usd",
                }
            ],
        },
    )
    assert handle_event(event, db_url=db_url) == "posted"
    b = balances(db_url)
    assert b["dispute_losses"] == 2000
    assert b["processing_fees"] == 1500
    assert b["stripe_balance"] == -3500
    assert invariant.check(db_url).ok


def test_payout_paid_moves_stripe_balance_to_bank(db_url) -> None:
    event = wrap(
        "evt_po_001",
        "payout.paid",
        {"id": "po_001", "object": "payout", "amount": 5000, "currency": "usd"},
    )
    assert handle_event(event, db_url=db_url) == "posted"
    b = balances(db_url)
    assert b["bank"] == 5000
    assert b["stripe_balance"] == -5000
    assert invariant.check(db_url).ok


def test_invoice_payment_failed_moves_no_money(db_url) -> None:
    event = wrap(
        "evt_in_001",
        "invoice.payment_failed",
        {"id": "in_001", "object": "invoice", "amount_due": 3000, "currency": "usd"},
    )
    assert handle_event(event, db_url=db_url) == "no_money_moved"
    assert txn_count(db_url) == 0
    assert event_status(db_url, "evt_in_001") == "no_money_moved"
    assert invariant.check(db_url).ok


def test_unhandled_event_type_is_ignored(db_url) -> None:
    event = wrap(
        "evt_cu_001", "customer.created", {"id": "cus_001", "object": "customer"}
    )
    assert handle_event(event, db_url=db_url) == "ignored"
    assert txn_count(db_url) == 0
    assert invariant.check(db_url).ok
