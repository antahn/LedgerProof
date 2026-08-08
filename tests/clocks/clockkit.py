"""Helpers for deterministic Stripe test-clock lifecycle tests.

A test clock gives a frozen, forward-only clock, so a subscription-year of
billing behaviour runs in seconds. The constraints below are not style choices;
each one is a documented rule that silently breaks a suite if ignored:

- Sandbox / test mode only. A customer must be CREATED with `test_clock` set —
  you cannot move an existing customer onto a clock, or detach one.
- Advancing is asynchronous and forward-only: poll `status` until it leaves
  `advancing`, and treat `internal_failure` as a hard error.
- Each advance may jump at most TWO service periods (monthly sub -> 2 months).
- Limits per clock: 3 customers, 3 subscriptions per customer.
- Deleting the clock deletes every object on it — that is the intended teardown.
- **List endpoints omit clock objects unless scoped.** An unscoped list sees
  nothing and a suite built on one passes vacuously. `assert_listing_is_scoped`
  exists to prove we did not fall into that; every helper here works from
  retrieved objects and event ids rather than unscoped lists.
- Draft subscription invoices sit in `draft` for ~1 hour, so crossing a period
  boundary is followed by a one-hour advance to finalize.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import psycopg
import stripe

from ledgerproof.ledger import invariant
from ledgerproof.ledger.balances import all_balances
from ledgerproof.stripe_io.mapping import MissingFeeData
from ledgerproof.worker.handlers import HANDLED_EVENT_TYPES, handle_event

DAY = 86_400
HOUR = 3_600

# An advance is asynchronous; these bound the wait rather than hang a suite.
ADVANCE_POLL_INTERVAL_S = 2.0
ADVANCE_TIMEOUT_S = 180.0

# A charge's balance_transaction is null until the charge settles into the
# Stripe balance (FINDINGS.md R6). The worker's answer is a backoff retry; a
# test needs the same patience or it reports a product bug that is really a
# race with settlement.
FEE_RETRIES = 6
FEE_RETRY_SLEEP_S = 2.0


class AdvanceFailed(RuntimeError):
    """The clock reported internal_failure, or never left `advancing`."""


@dataclass
class ClockSession:
    """One test clock plus the objects living on it, and the ledger they feed."""

    clock_id: str
    customer_id: str
    db_url: str
    subscription_id: str | None = None
    price_id: str | None = None
    started_at: int = 0
    _seen_event_ids: set[str] = field(default_factory=set)
    _object_ids: set[str] = field(default_factory=set)
    simulated_span_s: int = 0

    # ------------------------------------------------------------- clock --

    @property
    def frozen_time(self) -> int:
        return int(stripe.test_helpers.TestClock.retrieve(self.clock_id).frozen_time)

    def advance_to(self, frozen_time: int) -> int:
        """Advance and block until the clock is ready. Returns the new time.

        Forward-only: advancing to a time at or before now is a programming
        error, not a no-op, so it raises rather than silently doing nothing.
        """
        current = self.frozen_time
        if frozen_time <= current:
            raise AdvanceFailed(
                f"test clocks are forward-only: cannot advance to {frozen_time} from {current}"
            )
        stripe.test_helpers.TestClock.advance(self.clock_id, frozen_time=frozen_time)
        deadline = time.monotonic() + ADVANCE_TIMEOUT_S
        while True:
            clock = stripe.test_helpers.TestClock.retrieve(self.clock_id)
            if clock.status == "ready":
                self.simulated_span_s += frozen_time - current
                return int(clock.frozen_time)
            if clock.status == "internal_failure":
                raise AdvanceFailed(f"clock {self.clock_id} reported internal_failure")
            if time.monotonic() > deadline:
                raise AdvanceFailed(
                    f"clock {self.clock_id} still {clock.status} after {ADVANCE_TIMEOUT_S}s"
                )
            time.sleep(ADVANCE_POLL_INTERVAL_S)

    def advance_by(self, seconds: int) -> int:
        return self.advance_to(self.frozen_time + seconds)

    def settle_invoices(self) -> int:
        """Advance an hour so draft subscription invoices finalize and charge."""
        return self.advance_by(HOUR)

    # ------------------------------------------------------------ events --

    def track(self, *object_ids: str) -> None:
        """Register object ids whose events belong to this session."""
        self._object_ids.update(i for i in object_ids if i)

    def new_events(self) -> list[dict]:
        """Events generated for THIS session's objects since the last call.

        Scoped by object id, not by listing the account: two suites running
        against the same sandbox must never read each other's events. Returned
        oldest-first so handlers see them in generation order.
        """
        fresh: list[dict] = []
        for event in stripe.Event.list(limit=100, created={"gte": self.started_at}).auto_paging_iter():
            if event.id in self._seen_event_ids:
                continue
            # json.loads(str(event)) rather than the SDK's private
            # _to_dict_recursive: handlers must receive a PLAIN dict, exactly
            # the shape a webhook body parses into. A StripeObject would let a
            # test pass on attribute access a real payload never supports.
            payload = json.loads(str(event))
            if not self._belongs(payload):
                continue
            self._seen_event_ids.add(event.id)
            fresh.append(payload)
        fresh.sort(key=lambda e: (e["created"], e["id"]))
        return fresh

    def _belongs(self, event: dict) -> bool:
        obj = event.get("data", {}).get("object", {})
        if not isinstance(obj, dict):
            return False
        candidates = {
            obj.get("id"),
            obj.get("customer"),
            obj.get("subscription"),
            obj.get("charge"),
            obj.get("invoice"),
            obj.get("payment_intent"),
        }
        return bool(candidates & self._object_ids)

    def drain(self) -> list[dict]:
        """Pull new events and feed the handled ones into the ledger.

        Returns every new event (handled or not) so a suite can assert on the
        lifecycle events Stripe emitted, while the ledger only ever sees what
        the worker would actually process.
        """
        events = self.new_events()
        for event in events:
            self.track(event.get("data", {}).get("object", {}).get("id", ""))
            if event["type"] in HANDLED_EVENT_TYPES:
                _handle_with_settlement_retry(event, self.db_url)
        return events

    # ------------------------------------------------------------ ledger --

    def balances(self) -> dict[str, int]:
        return all_balances(self.db_url)

    def assert_invariant(self) -> None:
        result = invariant.check(self.db_url)
        assert result.ok, f"money conservation broke: {result.as_dict()}"


def transaction_count(db_url: str) -> int:
    with psycopg.connect(db_url) as conn:
        return conn.execute("SELECT count(*) FROM transactions").fetchone()[0]


def assert_cycles_posted(session: ClockSession, *, cycles: int, unit_minor: int) -> None:
    """The per-cycle billing assertion, in ONE place.

    Shared deliberately: the renewal suite calls it to pass, and the negative
    control calls it against a broken handler to prove it can fail. An
    assertion a control cannot exercise proves nothing about the suite.
    """
    posted = transaction_count(session.db_url)
    assert posted == cycles, f"expected exactly {cycles} transaction(s), found {posted}"

    balances = session.balances()
    expected = unit_minor * cycles
    assert balances.get("revenue", 0) == expected, (
        f"expected revenue {expected}, found {balances.get('revenue', 0)}: {balances}"
    )
    # Every cent of revenue is either settled balance or processing fee.
    settled = balances.get("stripe_balance", 0) + balances.get("processing_fees", 0)
    assert settled == expected, f"revenue {expected} unaccounted for: {balances}"
    session.assert_invariant()


def _handle_with_settlement_retry(event: dict, db_url: str) -> str:
    """handle_event, waiting out balance-transaction settlement like the worker."""
    for attempt in range(FEE_RETRIES):
        try:
            return handle_event(event, db_url=db_url, client=_egress_client())
        except MissingFeeData:
            if attempt == FEE_RETRIES - 1:
                raise
            time.sleep(FEE_RETRY_SLEEP_S)
    raise AssertionError("unreachable")


def _egress_client():
    from ledgerproof.config import get_settings
    from ledgerproof.stripe_io.client import StripeEgressClient

    return StripeEgressClient(get_settings().stripe_secret_key)


# ------------------------------------------------------------------ setup --


def create_clock(frozen_time: int, name: str) -> str:
    return stripe.test_helpers.TestClock.create(frozen_time=frozen_time, name=name).id


def create_customer_on_clock(clock_id: str, payment_method: str) -> tuple[str, str]:
    """Customer created ON the clock with a default payment method attached.

    `test_clock` must be set at creation — there is no way to attach one later.
    """
    customer = stripe.Customer.create(test_clock=clock_id, email="clocks@ledgerproof.test")
    pm = stripe.PaymentMethod.attach(payment_method, customer=customer.id)
    stripe.Customer.modify(
        customer.id, invoice_settings={"default_payment_method": pm.id}
    )
    return customer.id, pm.id


def create_monthly_price(amount_minor: int, label: str) -> str:
    return stripe.Price.create(
        unit_amount=amount_minor,
        currency="usd",
        recurring={"interval": "month"},
        product_data={"name": label},
    ).id


def delete_clock(clock_id: str) -> None:
    """Teardown: deletes the clock and every object on it."""
    try:
        stripe.test_helpers.TestClock.delete(clock_id)
    except stripe.error.InvalidRequestError:
        pass  # already gone


def assert_listing_is_scoped(clock_id: str, customer_id: str) -> None:
    """Prove the vacuous-pass trap is real and that we avoid it.

    An unscoped customer list omits test-clock customers entirely. A suite that
    asserted over an unscoped list would find nothing and pass without testing
    anything, so this guard is asserted explicitly rather than assumed.
    """
    scoped = [c.id for c in stripe.Customer.list(test_clock=clock_id, limit=100).data]
    assert customer_id in scoped, "scoped listing must see the clock's customer"
    unscoped = [c.id for c in stripe.Customer.list(limit=100).data]
    assert customer_id not in unscoped, (
        "unscoped listing unexpectedly returned a test-clock customer; "
        "the scoping rule this suite guards against may have changed"
    )
