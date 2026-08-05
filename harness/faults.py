"""Fault taxonomy + plan builder (Phase 2). All 13, no exceptions.

DUPLICATE             same event N times, serially      -> event.id dedupe works
DUPLICATE_OBJECT      2 event.ids, same (type,obj.id)   -> second dedupe key is real
CONCURRENT_DUPLICATE  same event on N parallel conns    -> THE RACE FINDER
REORDER               release out of generation order   -> order-independent handlers
DELAY                 latency past tolerance window     -> timeouts; stale sigs rejected
DROP                  never deliver                     -> reconciler catches it
RESPOND_500           force ingest to 500               -> retry path; retry is safe
TAMPER_BODY           flip a byte after signing         -> verification fails closed
TRUNCATE_BODY         cut the payload                   -> verification fails closed
STALE_TIMESTAMP       back-date t                       -> replay protection
DOWNGRADE_SCHEME      send only v0                      -> no scheme downgrade
PARTIAL_WRITE         SIGKILL worker mid-transaction    -> atomicity; no half-post
SLOW_LORIS            hold the connection open          -> ingest doesn't wedge

`plan()` turns a fault plus fixtures into (a) the exact bytes to deliver and
(b) an `Expectation` — the ground truth for what the ledger should look like
afterwards. The harness caused the break, so it knows precisely what should
have happened; every downstream metric is measured against these expectations,
which makes a wrong Expectation worse than a missing one.

Two invariants hold across the whole taxonomy:
- `invariant_after` is ALWAYS True. No fault may create or destroy money; a
  dropped or rejected event leaves the books smaller but still balanced.
- The signature-attacking faults (TAMPER_BODY, TRUNCATE_BODY, STALE_TIMESTAMP,
  DOWNGRADE_SCHEME) are the ONLY ones that touch the signature. The rest
  deliver perfectly valid, verifiable requests: they attack semantics, so a
  4xx from any of them is a finding, not the expected outcome.
"""

from __future__ import annotations

import copy
import enum
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from harness.events import EventFixture, serialize
from ledgerproof.stripe_io.signature import DEFAULT_TOLERANCE_SECONDS, sign

# The error body ingest returns when HMAC verification refuses a delivery, as
# distinct from the body-size cap, the JSON parser, or the envelope check. A
# signature fault that is refused by any OTHER layer has proved nothing about
# the signature, so the expected refuser is named and checked.
INVALID_SIGNATURE = "invalid signature"

# DELAY compresses a real >300s network delay into a back-dated signature: the
# delivery is already this far past the tolerance window when the proxy sends
# it, so the scenario runs in seconds instead of stalling a suite for five
# minutes. The observable behavior at ingest is identical.
_STALE_MARGIN_SECONDS = 60


class Fault(str, enum.Enum):
    NONE = "NONE"
    DUPLICATE = "DUPLICATE"
    DUPLICATE_OBJECT = "DUPLICATE_OBJECT"
    CONCURRENT_DUPLICATE = "CONCURRENT_DUPLICATE"
    REORDER = "REORDER"
    DELAY = "DELAY"
    DROP = "DROP"
    RESPOND_500 = "RESPOND_500"
    TAMPER_BODY = "TAMPER_BODY"
    TRUNCATE_BODY = "TRUNCATE_BODY"
    STALE_TIMESTAMP = "STALE_TIMESTAMP"
    DOWNGRADE_SCHEME = "DOWNGRADE_SCHEME"
    PARTIAL_WRITE = "PARTIAL_WRITE"
    SLOW_LORIS = "SLOW_LORIS"


ALL_FAULTS: tuple[Fault, ...] = tuple(f for f in Fault if f is not Fault.NONE)


@dataclass(frozen=True)
class Delivery:
    """One HTTP POST the proxy will make, byte-for-byte.

    See INVALID_SIGNATURE for why a rejection carries its expected reason.
    """

    body: bytes
    sig_header: str
    event_id: str
    delay_before_s: float = 0.0
    slow_loris_s: float = 0.0
    expect_rejected: bool = False
    # WHICH check must refuse it, matched against ingest's error body. A
    # rejection alone is not proof: with HMAC verification entirely deleted,
    # TRUNCATE_BODY still read green because half a JSON body is unparseable,
    # so the 400 came from json.loads and the fault proved nothing about the
    # signature. Naming the expected refuser closes that hole.
    expect_rejection_reason: str | None = None


@dataclass(frozen=True)
class Expectation:
    """Ground truth for a scenario: what the ledger must look like after.

    `balances_delta` lists only accounts whose expected net change is NON-ZERO
    (the mapping never posts a zero-amount entry), so compare it against the
    non-zero part of an observed before/after diff.
    """

    posted_event_ids: frozenset[str]
    rejected_event_ids: frozenset[str]
    balances_delta: dict[str, int]
    invariant_after: bool = True
    reconciler_should_report_break: bool = False
    notes: str = ""


@dataclass(frozen=True)
class FaultPlan:
    """Everything the proxy needs to inject one fault, plus its ground truth."""

    fault: Fault
    params: dict
    deliveries: tuple[Delivery, ...]
    concurrent: bool = False
    force_500: bool = False
    kill_worker_after_s: float | None = None
    redeliver_after_kill: bool = True
    expectation: Expectation = field(
        default_factory=lambda: Expectation(frozenset(), frozenset(), {})
    )


_DEFAULT_PARAMS: dict[Fault, dict] = {
    Fault.NONE: {},
    Fault.DUPLICATE: {"n": 3},
    Fault.DUPLICATE_OBJECT: {},
    Fault.CONCURRENT_DUPLICATE: {"n": 8},
    Fault.REORDER: {"order": "reverse"},
    Fault.DELAY: {"seconds": 1.0, "past_tolerance": False},
    Fault.DROP: {},
    Fault.RESPOND_500: {},
    Fault.TAMPER_BODY: {},
    Fault.TRUNCATE_BODY: {},
    Fault.STALE_TIMESTAMP: {"age_s": 600},
    Fault.DOWNGRADE_SCHEME: {},
    Fault.PARTIAL_WRITE: {"kill_after_s": 0.15},
    Fault.SLOW_LORIS: {"hold_s": 3.0},
}

_DIGITS = frozenset(b"0123456789")


def _flip_one_byte(body: bytes) -> tuple[bytes, int]:
    """Change one digit inside the event's data region; keep the JSON VALID.

    Valid JSON matters: a parse failure would be rejected by ingest for the
    wrong reason, and the fault must prove that verification — not the JSON
    parser — fails closed. Digits sit inside amounts and object ids, so the
    flip is also semantically material: it redirects or re-prices money.
    """
    start = max(body.find(b'"data":'), 0)
    for index in range(start, len(body)):
        if body[index] in _DIGITS:
            digit = body[index] - ord("0")
            flipped = bytearray(body)
            flipped[index] = ord("0") + (digit + 1) % 10
            return bytes(flipped), index
    raise ValueError("no digit to flip; body is not a Stripe event body")


def _money(fixtures: Sequence[EventFixture]) -> tuple[EventFixture, ...]:
    """Fixtures that SHOULD move money (invoice.payment_failed does not)."""
    return tuple(fx for fx in fixtures if fx.expected_delta)


def _sum_delta(
    fixtures: Sequence[EventFixture], posted: frozenset[str]
) -> dict[str, int]:
    totals: dict[str, int] = {}
    for fx in fixtures:
        if fx.event_id in posted:
            for account, amount in fx.expected_delta.items():
                totals[account] = totals.get(account, 0) + amount
    return {account: amount for account, amount in totals.items() if amount != 0}


def _clean(fixture: EventFixture, secret: str, timestamp: int, **kwargs) -> Delivery:
    """A correct, verifiable delivery of `fixture` — no signature tampering."""
    body = serialize(fixture.event)
    return Delivery(
        body=body,
        sig_header=sign(body, secret, timestamp=timestamp),
        event_id=fixture.event_id,
        **kwargs,
    )


def _rejected_plan(
    fault: Fault,
    fixture: EventFixture,
    params: dict,
    *,
    body: bytes,
    sig_header: str,
    notes: str,
) -> FaultPlan:
    """One delivery ingest must refuse: nothing posts, the books do not move."""
    return FaultPlan(
        fault=fault,
        params=params,
        deliveries=(
            Delivery(
                body=body,
                sig_header=sig_header,
                event_id=fixture.event_id,
                expect_rejected=True,
                # Every caller here attacks the signature, so the HMAC check —
                # not the body-size cap, the JSON parser, or the envelope
                # check — is the layer that must do the refusing.
                expect_rejection_reason=INVALID_SIGNATURE,
            ),
        ),
        expectation=Expectation(
            posted_event_ids=frozenset(),
            rejected_event_ids=frozenset({fixture.event_id}),
            balances_delta={},
            notes=notes,
        ),
    )


def plan(
    fault: Fault,
    fixtures: Sequence[EventFixture],
    secret: str,
    *,
    params: dict | None = None,
    now: int | None = None,
) -> FaultPlan:
    """Build the deliveries for `fault` and the Expectation they must satisfy.

    `secret` is the harness's own webhook secret — the core loop signs its own
    payloads and never depends on live Stripe. `now` pins the signature
    timestamp (and, for the back-dating faults, the reference point it is
    measured from) so scenarios are reproducible.
    """
    fixtures = tuple(fixtures)
    if not fixtures:
        raise ValueError("plan() needs at least one fixture")
    effective = {**_DEFAULT_PARAMS[fault], **(params or {})}
    ts = int(time.time()) if now is None else now
    target = fixtures[0]
    posts_money = bool(target.expected_delta)
    target_posted = frozenset({target.event_id}) if posts_money else frozenset()

    if fault is Fault.NONE:
        posted = frozenset(fx.event_id for fx in _money(fixtures))
        return FaultPlan(
            fault=fault,
            params=effective,
            deliveries=tuple(_clean(fx, secret, ts) for fx in fixtures),
            expectation=Expectation(
                posted_event_ids=posted,
                rejected_event_ids=frozenset(),
                balances_delta=_sum_delta(fixtures, posted),
                notes="baseline: every money event posts exactly once",
            ),
        )

    if fault in (Fault.DUPLICATE, Fault.CONCURRENT_DUPLICATE):
        n = int(effective["n"])
        if n < 2:
            raise ValueError(f"{fault.value} needs n >= 2, got {n}")
        # One Delivery object reused: the n requests are byte-identical, which
        # is exactly what Stripe's at-least-once delivery produces.
        delivery = _clean(target, secret, ts)
        concurrent = fault is Fault.CONCURRENT_DUPLICATE
        notes = (
            f"{n} parallel connections carrying one event; the database's unique "
            "constraints, not the fast-path dedupe, are what must hold"
            if concurrent
            else f"{n} serial redeliveries of one event; event.id dedupe must absorb them"
        )
        return FaultPlan(
            fault=fault,
            params=effective,
            deliveries=(delivery,) * n,
            concurrent=concurrent,
            expectation=Expectation(
                posted_event_ids=target_posted,
                rejected_event_ids=frozenset(),
                balances_delta=_sum_delta(fixtures, target_posted),
                notes=notes,
            ),
        )

    if fault is Fault.DUPLICATE_OBJECT:
        twin_event = copy.deepcopy(target.event)
        twin_event["id"] = f"{target.event_id}_dup"
        twin = EventFixture(
            event=twin_event,
            kind=target.kind,
            expected_delta=dict(target.expected_delta),
        )
        effective = {**effective, "twin_event_id": twin.event_id}
        return FaultPlan(
            fault=fault,
            params=effective,
            deliveries=(_clean(target, secret, ts), _clean(twin, secret, ts)),
            expectation=Expectation(
                posted_event_ids=target_posted,
                rejected_event_ids=frozenset(),
                balances_delta=_sum_delta(fixtures, target_posted),
                notes=(
                    "two Event objects for one state change: distinct event ids, "
                    "identical (type, money-movement object id). The twin is a 200 "
                    "duplicate, never a second transaction"
                ),
            ),
        )

    if fault is Fault.REORDER:
        if len(fixtures) < 2:
            raise ValueError("REORDER needs at least two fixtures")
        if effective["order"] != "reverse":
            raise ValueError(f"unsupported REORDER order: {effective['order']!r}")
        posted = frozenset(fx.event_id for fx in _money(fixtures))
        return FaultPlan(
            fault=fault,
            params=effective,
            deliveries=tuple(_clean(fx, secret, ts) for fx in reversed(fixtures)),
            expectation=Expectation(
                posted_event_ids=posted,
                rejected_event_ids=frozenset(),
                balances_delta=_sum_delta(fixtures, posted),
                notes=(
                    "delivered in reverse generation order; Stripe guarantees no "
                    "ordering, so every event must still post"
                ),
            ),
        )

    if fault is Fault.DELAY:
        seconds = float(effective["seconds"])
        past_tolerance = bool(effective["past_tolerance"])
        stale_ts = ts - (DEFAULT_TOLERANCE_SECONDS + _STALE_MARGIN_SECONDS)
        delivery = _clean(
            target,
            secret,
            stale_ts if past_tolerance else ts,
            delay_before_s=seconds,
            expect_rejected=past_tolerance,
        )
        posted = frozenset() if past_tolerance else target_posted
        rejected = frozenset({target.event_id}) if past_tolerance else frozenset()
        return FaultPlan(
            fault=fault,
            params=effective,
            deliveries=(delivery,),
            expectation=Expectation(
                posted_event_ids=posted,
                rejected_event_ids=rejected,
                balances_delta=_sum_delta(fixtures, posted),
                notes=(
                    f"held {seconds}s and signed outside the "
                    f"{DEFAULT_TOLERANCE_SECONDS}s tolerance: must be refused"
                    if past_tolerance
                    else f"held {seconds}s inside the tolerance window: must still post"
                ),
            ),
        )

    if fault is Fault.DROP:
        effective = {
            **effective,
            "dropped_event_ids": [fx.event_id for fx in fixtures],
        }
        return FaultPlan(
            fault=fault,
            params=effective,
            deliveries=(),
            expectation=Expectation(
                posted_event_ids=frozenset(),
                rejected_event_ids=frozenset(),
                balances_delta={},
                reconciler_should_report_break=True,
                notes=(
                    "never delivered: the ledger stays balanced and looks healthy, "
                    "so only the reconciler can catch this one"
                ),
            ),
        )

    if fault is Fault.RESPOND_500:
        delivery = _clean(target, secret, ts)
        return FaultPlan(
            fault=fault,
            params=effective,
            # The 500 is what Stripe sees, so the event reaches ingest twice:
            # the original and Stripe's retry of the same body.
            deliveries=(delivery, delivery),
            force_500=True,
            expectation=Expectation(
                posted_event_ids=target_posted,
                rejected_event_ids=frozenset(),
                balances_delta=_sum_delta(fixtures, target_posted),
                notes="Stripe's retry after a 500 must not double-post",
            ),
        )

    if fault is Fault.TAMPER_BODY:
        body = serialize(target.event)
        sig_header = sign(body, secret, timestamp=ts)
        tampered, index = _flip_one_byte(body)
        return _rejected_plan(
            fault,
            target,
            {**effective, "byte_index": index},
            body=tampered,
            sig_header=sig_header,
            notes="one byte changed after signing; verification must fail closed",
        )

    if fault is Fault.TRUNCATE_BODY:
        body = serialize(target.event)
        sig_header = sign(body, secret, timestamp=ts)
        kept = len(body) // 2
        return _rejected_plan(
            fault,
            target,
            {**effective, "kept_bytes": kept},
            body=body[:kept],
            sig_header=sig_header,
            notes="body cut in half after signing; verification must fail closed",
        )

    if fault is Fault.STALE_TIMESTAMP:
        age_s = int(effective["age_s"])
        if age_s <= DEFAULT_TOLERANCE_SECONDS:
            raise ValueError(
                f"age_s={age_s} is inside the {DEFAULT_TOLERANCE_SECONDS}s tolerance, "
                "so the delivery would be accepted and the expectation would be wrong"
            )
        body = serialize(target.event)
        return _rejected_plan(
            fault,
            target,
            effective,
            body=body,
            sig_header=sign(body, secret, timestamp=ts - age_s),
            notes=f"signed {age_s}s in the past; replay protection must reject it",
        )

    if fault is Fault.DOWNGRADE_SCHEME:
        body = serialize(target.event)
        return _rejected_plan(
            fault,
            target,
            effective,
            body=body,
            # A CORRECT v0 HMAC: the rejection must be about the scheme label,
            # not a wrong digest.
            sig_header=sign(body, secret, timestamp=ts, only_schemes=["v0"]),
            notes="v0 is a test-only scheme; accepting it is a downgrade attack",
        )

    if fault is Fault.PARTIAL_WRITE:
        return FaultPlan(
            fault=fault,
            params=effective,
            deliveries=(_clean(target, secret, ts),),
            kill_worker_after_s=float(effective["kill_after_s"]),
            redeliver_after_kill=True,
            expectation=Expectation(
                posted_event_ids=target_posted,
                rejected_event_ids=frozenset(),
                balances_delta=_sum_delta(fixtures, target_posted),
                notes=(
                    "worker SIGKILLed mid-transaction, then the event is redelivered: "
                    "exactly one transaction, and never a half-posted one"
                ),
            ),
        )

    if fault is Fault.SLOW_LORIS:
        hold_s = float(effective["hold_s"])
        return FaultPlan(
            fault=fault,
            params=effective,
            deliveries=(_clean(target, secret, ts, slow_loris_s=hold_s),),
            expectation=Expectation(
                posted_event_ids=target_posted,
                rejected_event_ids=frozenset(),
                balances_delta=_sum_delta(fixtures, target_posted),
                notes=(
                    f"body dribbled over {hold_s}s: ingest must stay responsive, and "
                    "the event posts if the request completes"
                ),
            ),
        )

    raise ValueError(f"unhandled fault: {fault!r}")  # pragma: no cover
