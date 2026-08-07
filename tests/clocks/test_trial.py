"""Trial lifecycle on a test clock: warning event, then conversion.

Brief §6 Phase 3 suite 1: advance to trial_end − 3d and assert
`customer.subscription.trial_will_end`; advance past trial_end and assert the
subscription converts and the resulting charge lands in the ledger.
"""

from __future__ import annotations

import pytest
import stripe

from tests.clocks import clockkit

TRIAL_DAYS = 14
MONTHLY_MINOR = 1_500

pytestmark = pytest.mark.clocks


def test_trial_warns_then_converts(session: clockkit.ClockSession, clock_metrics) -> None:
    price_id = clockkit.create_monthly_price(MONTHLY_MINOR, "LedgerProof trial monthly")
    subscription = stripe.Subscription.create(
        customer=session.customer_id,
        items=[{"price": price_id}],
        trial_period_days=TRIAL_DAYS,
    )
    session.subscription_id = subscription.id
    session.track(subscription.id, subscription.latest_invoice)
    assert subscription.status == "trialing"

    # The scoping trap: an unscoped list cannot see this customer at all.
    clockkit.assert_listing_is_scoped(session.clock_id, session.customer_id)

    session.drain()  # events from setup; a trial start moves no money
    assert session.balances() == dict.fromkeys(session.balances(), 0) or not any(
        session.balances().values()
    ), "a trial that has not charged anything must not have moved money"

    # --- three days before the trial ends -----------------------------------
    session.advance_to(subscription.trial_end - 3 * clockkit.DAY)
    warned = [e for e in session.drain() if e["type"] == "customer.subscription.trial_will_end"]
    assert warned, "expected customer.subscription.trial_will_end three days before trial_end"
    assert warned[0]["data"]["object"]["id"] == subscription.id

    # --- past the trial end: conversion and the first real charge -----------
    session.advance_to(subscription.trial_end + clockkit.HOUR)
    session.settle_invoices()  # draft invoices sit ~1h before finalizing
    events = session.drain()

    converted = stripe.Subscription.retrieve(subscription.id)
    assert converted.status == "active", f"expected conversion, got {converted.status}"

    charges = [e for e in events if e["type"] == "charge.succeeded"]
    assert charges, f"expected a charge after conversion; saw {sorted({e['type'] for e in events})}"

    balances = session.balances()
    assert balances["revenue"] == MONTHLY_MINOR, balances
    assert balances["stripe_balance"] + balances["processing_fees"] == MONTHLY_MINOR, balances
    session.assert_invariant()

    clock_metrics["suites"].append(
        {
            "suite": "trial",
            "simulated_span_s": session.simulated_span_s,
            "revenue_minor": balances["revenue"],
        }
    )
