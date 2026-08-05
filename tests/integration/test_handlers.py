"""Integration tests for worker handlers (LEDGERPROOF_BRIEF §5.3).

Order-independent, duplicate-tolerant, and they never invent state: a missing
fee is fetched from the (stubbed) Stripe API by id. Handlers run strictly
post-verification, so nothing here is signed.
"""

from __future__ import annotations

from types import SimpleNamespace

import psycopg

from ledgerproof.ledger import invariant
from ledgerproof.worker.handlers import handle_event

CREATED = 1_750_000_000  # any fixed tz-aware-able unix timestamp


class StubStripeClient:
    """Records .get calls and serves canned responses (handlers only .get)."""

    def __init__(self, responses: dict[str, dict] | None = None) -> None:
        self.responses = responses or {}
        self.requests: list[str] = []
        self.params: list[dict | None] = []

    def get(self, path: str, params: dict | None = None) -> dict:
        self.requests.append(path)
        self.params.append(params)
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


def test_null_balance_transaction_refetches_charge(db_url) -> None:
    # Observed live (2026-08-04): a real charge.succeeded delivered at
    # charge-creation time carries balance_transaction=None — no fee, not even
    # an id to fetch. The handler must re-fetch the CHARGE with the balance
    # transaction expanded rather than fail or invent a fee.
    event = charge_succeeded_event(event_id="evt_ch_003", charge_id="ch_003", expanded=True)
    event["data"]["object"]["balance_transaction"] = None
    stub = StubStripeClient(
        {
            "/v1/charges/ch_003": {
                "id": "ch_003",
                "object": "charge",
                "amount": 1000,
                "currency": "usd",
                "balance_transaction": {
                    "id": "txn_bt_003",
                    "object": "balance_transaction",
                    "amount": 1000,
                    "fee": 59,
                    "net": 941,
                    "currency": "usd",
                },
            }
        }
    )
    assert handle_event(event, db_url=db_url, client=stub) == "posted"
    assert stub.requests == ["/v1/charges/ch_003"]
    assert stub.params == [{"expand": ["balance_transaction"]}]
    b = balances(db_url)
    assert b["stripe_balance"] == 941
    assert b["processing_fees"] == 59
    assert b["revenue"] == 1000
    assert invariant.check(db_url).ok


def test_unsettled_charge_still_missing_fee_raises_for_retry(db_url) -> None:
    # If the re-fetched charge STILL has balance_transaction=None (charge not
    # yet settled into the balance), the handler must raise so the worker's
    # backoff retry waits out settlement — never guess a fee.
    import pytest

    from ledgerproof.stripe_io.mapping import MissingFeeData

    event = charge_succeeded_event(event_id="evt_ch_004", charge_id="ch_004", expanded=True)
    event["data"]["object"]["balance_transaction"] = None
    stub = StubStripeClient(
        {
            "/v1/charges/ch_004": {
                "id": "ch_004",
                "object": "charge",
                "amount": 1000,
                "currency": "usd",
                "balance_transaction": None,
            }
        }
    )
    with pytest.raises(MissingFeeData):
        handle_event(event, db_url=db_url, client=stub)
    assert txn_count(db_url) == 0


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


def test_charge_and_payment_intent_pair_posts_exactly_once(db_url) -> None:
    # A normal PaymentIntents payment emits BOTH charge.succeeded and
    # payment_intent.succeeded for ONE money movement, keyed by different
    # object ids (ch_... vs pi_...) — the demonstrated double-count. The
    # dispatch table handles only charge.succeeded; the PI event is 'ignored'.
    charge = charge_succeeded_event(
        event_id="evt_pair_ch_1", charge_id="ch_1", amount=1000, fee=59
    )
    pi = wrap(
        "evt_pair_pi_1",
        "payment_intent.succeeded",
        {
            "id": "pi_1",
            "object": "payment_intent",
            "amount": 1000,
            "amount_received": 1000,
            "currency": "usd",
            # Real PI payloads carry latest_charge as a STRING id, unexpanded.
            "latest_charge": "ch_1",
        },
    )
    assert handle_event(charge, db_url=db_url) == "posted"
    assert handle_event(pi, db_url=db_url) == "ignored"
    assert txn_count(db_url) == 1
    b = balances(db_url)
    assert b["revenue"] == 1000  # gross once, not 2000
    assert b["stripe_balance"] == 941
    assert b["processing_fees"] == 59
    assert invariant.check(db_url).ok
    assert event_status(db_url, "evt_pair_pi_1") == "no_money_moved"


def charge_refunded_event(
    event_id: str, refunds_data: list[dict], amount_refunded: int
) -> dict:
    return wrap(
        event_id,
        "charge.refunded",
        {
            "id": "ch_multi_refund",
            "object": "charge",
            "amount": 10000,
            "amount_refunded": amount_refunded,
            "refunded": amount_refunded >= 10000,
            "currency": "usd",
            # Stripe lists refunds newest-first: data[0] is THIS event's refund.
            "refunds": {"object": "list", "data": refunds_data, "has_more": False},
        },
    )


def test_two_partial_refunds_post_two_transactions(db_url) -> None:
    re_1 = {"id": "re_1", "object": "refund", "amount": 5000, "currency": "usd"}
    re_2 = {"id": "re_2", "object": "refund", "amount": 3000, "currency": "usd"}
    first = charge_refunded_event("evt_re_multi_1", [re_1], amount_refunded=5000)
    second = charge_refunded_event("evt_re_multi_2", [re_2, re_1], amount_refunded=8000)

    assert handle_event(first, db_url=db_url) == "posted"
    # Keyed by refund id, the second partial refund is a NEW money movement,
    # not a dedupe collision on (charge.refunded, ch_multi_refund).
    assert handle_event(second, db_url=db_url) == "posted"
    assert txn_count(db_url) == 2
    b = balances(db_url)
    assert b["stripe_balance"] == -8000
    assert b["refunds_contra"] == 8000
    assert invariant.check(db_url).ok

    # Replaying the same refund event is still a duplicate and moves nothing.
    before = balances(db_url)
    assert handle_event(second, db_url=db_url) == "duplicate"
    assert balances(db_url) == before
    assert txn_count(db_url) == 2
    assert invariant.check(db_url).ok


def test_task_retries_then_marks_failed_and_reraises(db_url, monkeypatch) -> None:
    # Finding 3: a task exception must not be acked into the void. Eager
    # .apply() exercises the real retry loop (Celery re-applies the task's
    # signature synchronously, ignoring countdown) with handle_event stubbed
    # to fail like a down database.
    from ledgerproof.worker import tasks as tasks_mod

    attempts: list[int] = []

    def always_down(event: dict, *, db_url: str, client=None) -> str:
        attempts.append(1)
        raise ConnectionError("database unreachable")

    monkeypatch.setattr(tasks_mod.handlers, "handle_event", always_down)
    monkeypatch.setattr(
        tasks_mod,
        "get_settings",
        lambda: SimpleNamespace(app_database_url=db_url, stripe_secret_key=""),
    )

    event = wrap(
        "evt_retry_001", "charge.succeeded", {"id": "ch_retry", "object": "charge"}
    )
    result = tasks_mod.process_event.apply(args=(event,))

    assert result.failed()
    assert isinstance(result.result, ConnectionError)
    assert len(attempts) == 6  # the first attempt + max_retries=5 retries
    assert event_status(db_url, "evt_retry_001") == "failed"
