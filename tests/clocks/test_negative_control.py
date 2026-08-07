"""A test that cannot fail proves nothing (brief §6 Phase 3).

The lifecycle suites assert against a real Stripe sandbox over simulated
months. If a broken handler still produced green suites, all that green would
mean is "the API responded." These controls deliberately break the money path
and require the same assertions the suites use to go red.

They run against the sandbox but need no clock advance, so they are cheap.
"""

from __future__ import annotations

import psycopg
import pytest
import stripe

from ledgerproof.ledger import invariant
from ledgerproof.stripe_io import mapping
from ledgerproof.stripe_io.mapping import build_transaction
from tests.clocks import clockkit

pytestmark = pytest.mark.clocks

MONTHLY_MINOR = 1_900


@pytest.fixture()
def charged_session(session: clockkit.ClockSession) -> clockkit.ClockSession:
    """A session whose subscription has produced exactly one real charge."""
    price_id = clockkit.create_monthly_price(MONTHLY_MINOR, "LedgerProof control monthly")
    subscription = stripe.Subscription.create(
        customer=session.customer_id, items=[{"price": price_id}]
    )
    session.subscription_id = subscription.id
    session.track(subscription.id, subscription.latest_invoice)
    session.settle_invoices()
    return session


def test_suite_goes_red_when_the_handler_drops_the_fee(
    charged_session: clockkit.ClockSession, monkeypatch
) -> None:
    """Debit the full gross instead of gross − fee: the books stop balancing.

    The database's deferred constraint trigger must refuse the write, so the
    break surfaces as an exception rather than as silently wrong balances.
    """
    real_build = build_transaction

    def broken(event: dict, *, fee_minor: int | None = None):
        txn = real_build(event, fee_minor=fee_minor)
        if txn is None or event["type"] != "charge.succeeded":
            return txn
        gross = event["data"]["object"]["amount"]
        entries = tuple(
            e if e.account_name != "stripe_balance" else type(e)(
                account_name=e.account_name,
                dir=e.dir,
                amount_minor=gross,  # the fee silently vanishes
                currency=e.currency,
            )
            for e in txn.entries
        )
        return type(txn)(
            id=txn.id,
            occurred_at=txn.occurred_at,
            entries=entries,
            stripe_event_id=txn.stripe_event_id,
            stripe_object_id=txn.stripe_object_id,
            event_type=txn.event_type,
            memo=txn.memo,
        )

    monkeypatch.setattr(mapping, "build_transaction", broken)
    monkeypatch.setattr("ledgerproof.worker.handlers.build_transaction", broken)

    with pytest.raises(psycopg.errors.RaiseException, match="unbalanced"):
        charged_session.drain()

    # Nothing half-written survived the refusal.
    with psycopg.connect(charged_session.db_url) as conn:
        assert conn.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
    assert invariant.check(charged_session.db_url).ok


def test_suite_goes_red_when_the_handler_posts_nothing(
    charged_session: clockkit.ClockSession, monkeypatch
) -> None:
    """Silently drop money events, then run the suite's OWN assertion.

    This calls clockkit.assert_cycles_posted — the exact function the renewal
    suite passes with — and requires it to raise. Hand-writing a doomed assert
    here would prove nothing about the suite.
    """
    monkeypatch.setattr(
        "ledgerproof.worker.handlers.build_transaction", lambda event, **kw: None
    )
    charged_session.drain()

    assert clockkit.transaction_count(charged_session.db_url) == 0, (
        "control setup failed: the mutation did not take effect"
    )
    with pytest.raises(AssertionError, match="expected exactly 1 transaction"):
        clockkit.assert_cycles_posted(charged_session, cycles=1, unit_minor=MONTHLY_MINOR)

    # Conservation stays GREEN while a whole cycle is missing: it proves the
    # books are internally consistent, not that they match reality — the same
    # blind spot harness finding H1 exposed. Only the explicit per-cycle count
    # catches this, which is why the suites assert it.
    assert invariant.check(charged_session.db_url).ok
