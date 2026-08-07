"""Multi-attempt dunning on a test clock.

Brief §6 Phase 3 suite 3: a card that fails at CHARGE time (not at attach
time — `pm_card_chargeCustomerFail` attaches successfully, which is exactly why
it reaches the renewal path), then advance through Stripe's Smart Retries
schedule asserting each attempt, then the terminal state.

The ledger assertion is the one that matters: a failed payment moves no money.
`invoice.payment_failed` maps to an event-log row and never a transaction —
recording a transaction for a non-money event is itself a way to create money
from nothing.
"""

from __future__ import annotations

import psycopg
import pytest
import stripe

from tests.clocks import clockkit

MONTHLY_MINOR = 3_100
# Smart Retries spread attempts over roughly three weeks; step in day-scale
# jumps and stop as soon as a terminal state is reached.
MAX_RETRY_STEPS = 12
STEP_DAYS = 3

pytestmark = pytest.mark.clocks


def _transaction_count(db_url: str) -> int:
    with psycopg.connect(db_url) as conn:
        return conn.execute("SELECT count(*) FROM transactions").fetchone()[0]


def _terminal_state(subscription_id: str, invoice_id: str | None) -> str | None:
    """The documented ends of the dunning road, or None if still retrying."""
    sub = stripe.Subscription.retrieve(subscription_id)
    if sub.status in ("canceled", "unpaid"):
        return f"subscription.{sub.status}"
    if invoice_id:
        invoice = stripe.Invoice.retrieve(invoice_id)
        if invoice.status in ("uncollectible", "void", "paid"):
            return f"invoice.{invoice.status}"
    return None


def test_failed_renewal_retries_then_reaches_terminal_state(
    session: clockkit.ClockSession, clock_metrics
) -> None:
    price_id = clockkit.create_monthly_price(MONTHLY_MINOR, "LedgerProof dunning monthly")
    subscription = stripe.Subscription.create(
        customer=session.customer_id, items=[{"price": price_id}]
    )
    session.subscription_id = subscription.id
    session.track(subscription.id, subscription.latest_invoice)

    # Cycle 1 succeeds on a good card: dunning is about a card that goes bad,
    # and this also proves the failure below is not just a broken fixture.
    session.settle_invoices()
    session.drain()
    session.assert_invariant()
    assert _transaction_count(session.db_url) == 1
    good_cycle_revenue = session.balances()["revenue"]
    assert good_cycle_revenue == MONTHLY_MINOR

    # The card goes bad. pm_card_chargeCustomerFail ATTACHES cleanly and fails
    # only when charged, which is what lets it reach the renewal path at all.
    bad_pm = stripe.PaymentMethod.attach(
        "pm_card_chargeCustomerFail", customer=session.customer_id
    )
    stripe.Customer.modify(
        session.customer_id, invoice_settings={"default_payment_method": bad_pm.id}
    )
    session.track(bad_pm.id)

    period_end = stripe.Subscription.retrieve(subscription.id)["items"]["data"][0][
        "current_period_end"
    ]
    session.advance_to(period_end + clockkit.HOUR)
    session.settle_invoices()
    events = session.drain()

    failures = [e for e in events if e["type"] == "invoice.payment_failed"]
    assert failures, (
        "expected invoice.payment_failed at renewal; "
        f"saw {sorted({e['type'] for e in events})}"
    )
    failed_invoice_id = failures[0]["data"]["object"]["id"]
    session.track(failed_invoice_id)

    # --- walk the Smart Retries schedule ------------------------------------
    attempts = len(failures)
    terminal = _terminal_state(subscription.id, failed_invoice_id)
    steps = 0
    while terminal is None and steps < MAX_RETRY_STEPS:
        session.advance_by(STEP_DAYS * clockkit.DAY)
        events = session.drain()
        attempts += sum(1 for e in events if e["type"] == "invoice.payment_failed")
        terminal = _terminal_state(subscription.id, failed_invoice_id)
        steps += 1

    assert attempts >= 2, (
        f"expected multi-attempt dunning, saw {attempts} attempt(s) over "
        f"{steps * STEP_DAYS} simulated days"
    )
    assert terminal is not None, (
        f"no terminal state after {attempts} attempts across {steps * STEP_DAYS} "
        "simulated days; Smart Retries may be configured differently on this account"
    )
    assert terminal != "invoice.paid", "the dunning card must never succeed"

    # The ledger is the point: not one cent moved for any failed attempt.
    assert _transaction_count(session.db_url) == 1, (
        "failed payments must not post transactions; only the one good cycle should exist"
    )
    assert session.balances()["revenue"] == good_cycle_revenue
    session.assert_invariant()

    clock_metrics["suites"].append(
        {
            "suite": "dunning",
            "simulated_span_s": session.simulated_span_s,
            "payment_attempts": attempts,
            "terminal_state": terminal,
            "retry_steps": steps,
        }
    )
