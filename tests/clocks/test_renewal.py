"""Renewal cycles on a test clock: one balanced transaction per cycle.

Suite 2: advance past `current_period_end` repeatedly and
assert exactly one balanced transaction per cycle, with money conservation
holding after each.
"""

from __future__ import annotations

import pytest
import stripe

from tests.clocks import clockkit

MONTHLY_MINOR = 2_400
CYCLES = 2

pytestmark = pytest.mark.clocks


def test_each_renewal_posts_exactly_one_balanced_transaction(
    session: clockkit.ClockSession, clock_metrics
) -> None:
    price_id = clockkit.create_monthly_price(MONTHLY_MINOR, "LedgerProof renewal monthly")
    subscription = stripe.Subscription.create(
        customer=session.customer_id, items=[{"price": price_id}]
    )
    session.subscription_id = subscription.id
    session.track(subscription.id, subscription.latest_invoice)

    # Cycle 1 is the subscription's own first invoice.
    session.settle_invoices()
    session.drain()
    clockkit.assert_cycles_posted(session, cycles=1, unit_minor=MONTHLY_MINOR)

    period_end = stripe.Subscription.retrieve(subscription.id)["items"]["data"][0][
        "current_period_end"
    ]
    for cycle in range(2, CYCLES + 2):
        # One advance per cycle: never more than two service periods at a time.
        session.advance_to(period_end + clockkit.HOUR)
        session.settle_invoices()
        session.drain()

        clockkit.assert_cycles_posted(session, cycles=cycle, unit_minor=MONTHLY_MINOR)

        fresh = stripe.Subscription.retrieve(subscription.id)
        assert fresh.status == "active"
        period_end = fresh["items"]["data"][0]["current_period_end"]

    balances = session.balances()
    clock_metrics["suites"].append(
        {
            "suite": "renewal",
            "simulated_span_s": session.simulated_span_s,
            "cycles": CYCLES + 1,
            "revenue_minor": balances["revenue"],
        }
    )
