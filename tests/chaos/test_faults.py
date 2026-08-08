"""The harness's ground truth, checked without a database or a network.

Two things are being proven here. First, that every fault in the taxonomy
produces a coherent plan for every event kind. Second — the load-bearing one —
that each fixture's `expected_delta` agrees with what `stripe_io.mapping`
actually posts. Those two are derived independently from the same double-entry
mapping, so agreement is evidence and disagreement is a bug in one of them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    # harness/ is not part of the installed package (src layout), so the test
    # puts the repo root on the path itself rather than depending on how
    # pytest was invoked.
    sys.path.insert(0, str(REPO_ROOT))

from harness.events import (
    ALL_KINDS,
    EventFixture,
    make,
    serialize,
    signed_delta,
)
from harness.faults import ALL_FAULTS, Fault, FaultPlan, plan
from ledgerproof.stripe_io.mapping import (
    build_transaction,
    money_movement_object_id,
)
from ledgerproof.stripe_io.signature import (
    DEFAULT_TOLERANCE_SECONDS,
    SignatureVerificationError,
    verify,
)

SECRET = "whsec_" + "c" * 32
NOW = 1_780_000_000  # pinned: signatures and tolerance checks must be reproducible

SIGNATURE_ATTACKS = (
    Fault.TAMPER_BODY,
    Fault.TRUNCATE_BODY,
    Fault.STALE_TIMESTAMP,
    Fault.DOWNGRADE_SCHEME,
)
SEMANTIC_FAULTS = (
    Fault.NONE,
    Fault.DUPLICATE,
    Fault.DUPLICATE_OBJECT,
    Fault.CONCURRENT_DUPLICATE,
    Fault.REORDER,
    Fault.RESPOND_500,
    Fault.PARTIAL_WRITE,
    Fault.SLOW_LORIS,
)
MONEY_KINDS = tuple(k for k in ALL_KINDS if k != "invoice_payment_failed")


def pair(kind: str, *, first: int = 1, second: int = 2) -> list[EventFixture]:
    """Two fixtures of one kind — enough for REORDER and DUPLICATE_OBJECT."""
    return [make(kind, seq=first, created=NOW), make(kind, seq=second, created=NOW)]


def expected_sum(fixtures: list[EventFixture], posted: frozenset[str]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for fixture in fixtures:
        if fixture.event_id in posted:
            for account, amount in fixture.expected_delta.items():
                totals[account] = totals.get(account, 0) + amount
    return {a: v for a, v in totals.items() if v != 0}


def observed_delta(event: dict) -> dict[str, int]:
    """What the production mapping would actually do to the balances."""
    txn = build_transaction(event)
    if txn is None:
        return {}
    totals: dict[str, int] = {}
    for entry in txn.entries:
        totals[entry.account_name] = totals.get(entry.account_name, 0) + signed_delta(
            entry.account_name, entry.dir, entry.amount_minor
        )
    return {a: v for a, v in totals.items() if v != 0}


# --- the taxonomy is complete and stable ------------------------------------


def test_thirteen_faults_plus_none() -> None:
    assert len(ALL_FAULTS) == 13
    assert Fault.NONE not in ALL_FAULTS
    assert len(set(Fault)) == 14


def test_fault_values_equal_their_names() -> None:
    for fault in Fault:
        assert fault.value == fault.name
        assert fault == fault.name  # str mixin: usable as a JSONL label


# --- ground truth: fixtures agree with the production mapping ---------------


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_fixture_expected_delta_matches_mapping(kind: str) -> None:
    fixture = make(kind, seq=7, created=NOW)
    assert observed_delta(fixture.event) == fixture.expected_delta


@pytest.mark.parametrize("kind", MONEY_KINDS)
def test_money_fixtures_map_to_balanced_transactions(kind: str) -> None:
    fixture = make(kind, seq=8, created=NOW)
    txn = build_transaction(fixture.event)
    assert txn is not None
    debits = sum(e.amount_minor for e in txn.entries if e.dir == "debit")
    credits = sum(e.amount_minor for e in txn.entries if e.dir == "credit")
    assert debits == credits
    assert len(txn.entries) >= 2
    assert all(e.amount_minor > 0 for e in txn.entries)
    assert txn.stripe_event_id == fixture.event_id
    assert fixture.expected_delta != {}


def test_invoice_payment_failed_moves_no_money() -> None:
    fixture = make("invoice_payment_failed", seq=9, created=NOW)
    assert fixture.expected_delta == {}
    assert build_transaction(fixture.event) is None


def test_expected_deltas_match_the_mapping_table() -> None:
    # Spelled out literally so a mapping change cannot quietly redefine what
    # "correct" means on both sides at once.
    charge = make("charge_succeeded", seq=10, created=NOW, amount=1000, fee=59)
    assert charge.expected_delta == {
        "stripe_balance": 941,
        "processing_fees": 59,
        "revenue": 1000,
    }
    refund = make("charge_refunded", seq=11, created=NOW, amount=1000, refunded=400)
    assert refund.expected_delta == {"refunds_contra": 400, "stripe_balance": -400}
    dispute = make("dispute_created", seq=12, created=NOW, amount=2000, fee=1500)
    assert dispute.expected_delta == {
        "dispute_losses": 2000,
        "processing_fees": 1500,
        "stripe_balance": -3500,
    }
    payout = make("payout_paid", seq=13, created=NOW, amount=5000)
    assert payout.expected_delta == {"bank": 5000, "stripe_balance": -5000}


def test_zero_fee_charge_omits_the_fee_account_on_both_sides() -> None:
    fixture = make("charge_succeeded", seq=14, created=NOW, amount=2500, fee=0)
    assert fixture.expected_delta == {"stripe_balance": 2500, "revenue": 2500}
    assert observed_delta(fixture.event) == fixture.expected_delta


def test_refund_fixture_is_keyed_by_its_refund_id() -> None:
    fixture = make("charge_refunded", seq=15, created=NOW)
    assert money_movement_object_id(fixture.event) == "re_h000015"
    txn = build_transaction(fixture.event)
    assert txn is not None
    assert txn.stripe_object_id == "re_h000015"


def test_charge_fixture_needs_no_stripe_call_for_its_fee() -> None:
    # An expanded balance_transaction is what keeps the harness's core loop
    # hermetic: build_transaction must resolve the fee with no fee_minor.
    fixture = make("charge_succeeded", seq=16, created=NOW)
    bt = fixture.event["data"]["object"]["balance_transaction"]
    assert isinstance(bt, dict) and bt["source"] == "ch_h000016"
    assert build_transaction(fixture.event) is not None


# --- fixture identity -------------------------------------------------------


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_make_ids_are_stable_for_a_seq_and_unique_across_seqs(kind: str) -> None:
    again = make(kind, seq=42, created=NOW)
    assert make(kind, seq=42, created=NOW).event == again.event
    ids = {make(kind, seq=s, created=NOW).event_id for s in range(1, 51)}
    assert len(ids) == 50
    object_ids = {
        money_movement_object_id(make(kind, seq=s, created=NOW).event) for s in range(1, 51)
    }
    assert len(object_ids) == 50


def test_make_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unknown fixture kind"):
        make("charge_exploded", seq=1)


def test_serialize_is_the_exact_bytes_that_parse_back() -> None:
    fixture = make("charge_succeeded", seq=17, created=NOW)
    body = serialize(fixture.event)
    assert isinstance(body, bytes)
    assert b", " not in body and b": " not in body  # compact separators
    assert json.loads(body) == fixture.event
    assert json.loads(body)["object"] == "event"


# --- every fault plans, for every kind --------------------------------------


@pytest.mark.parametrize("fault", (Fault.NONE, *ALL_FAULTS))
@pytest.mark.parametrize("kind", ALL_KINDS)
def test_every_fault_plans_for_every_kind(fault: Fault, kind: str) -> None:
    fixtures = pair(kind)
    result = plan(fault, fixtures, SECRET, now=NOW)

    assert isinstance(result, FaultPlan)
    assert result.fault is fault
    # No fault may create or destroy money — that is the whole thesis.
    assert result.expectation.invariant_after is True

    ids = {fixture.event_id for fixture in fixtures}
    money_ids = {fixture.event_id for fixture in fixtures if fixture.expected_delta}
    posted = result.expectation.posted_event_ids
    assert posted <= money_ids
    assert result.expectation.rejected_event_ids <= ids
    assert not (posted & result.expectation.rejected_event_ids)
    assert result.expectation.balances_delta == expected_sum(fixtures, posted)
    if kind == "invoice_payment_failed":
        assert posted == frozenset()
        assert result.expectation.balances_delta == {}

    # Delivery-level rejection flags and the scenario-level expectation are two
    # views of one fact; they must not drift.
    assert result.expectation.rejected_event_ids == {
        d.event_id for d in result.deliveries if d.expect_rejected
    }
    for delivery in result.deliveries:
        assert isinstance(delivery.body, bytes) and delivery.body
        assert delivery.sig_header.startswith("t=")
        assert delivery.delay_before_s >= 0.0
        assert delivery.slow_loris_s >= 0.0


def test_plan_requires_a_fixture() -> None:
    with pytest.raises(ValueError, match="at least one fixture"):
        plan(Fault.NONE, [], SECRET, now=NOW)


# --- signature-attacking faults must be rejected ----------------------------


@pytest.mark.parametrize("fault", SIGNATURE_ATTACKS)
@pytest.mark.parametrize("kind", ALL_KINDS)
def test_signature_attacks_are_rejected_by_verify(fault: Fault, kind: str) -> None:
    fixtures = pair(kind)
    result = plan(fault, fixtures, SECRET, now=NOW)

    assert len(result.deliveries) == 1
    delivery = result.deliveries[0]
    assert delivery.expect_rejected is True
    with pytest.raises(SignatureVerificationError):
        verify(delivery.body, delivery.sig_header, SECRET, now=NOW)

    assert result.expectation.posted_event_ids == frozenset()
    assert result.expectation.rejected_event_ids == {fixtures[0].event_id}
    assert result.expectation.balances_delta == {}


def test_tamper_changes_exactly_one_byte_and_keeps_the_json_valid() -> None:
    fixtures = pair("charge_succeeded")
    original = serialize(fixtures[0].event)
    tampered = plan(Fault.TAMPER_BODY, fixtures, SECRET, now=NOW).deliveries[0].body

    assert len(tampered) == len(original)
    differing = [i for i in range(len(original)) if original[i] != tampered[i]]
    assert len(differing) == 1
    # Valid JSON on purpose: a parse error would make ingest reject the body
    # for the wrong reason, proving nothing about signature verification.
    assert json.loads(tampered)["object"] == "event"


def test_truncate_cuts_the_body_in_half() -> None:
    fixtures = pair("charge_succeeded")
    original = serialize(fixtures[0].event)
    result = plan(Fault.TRUNCATE_BODY, fixtures, SECRET, now=NOW)
    assert result.deliveries[0].body == original[: len(original) // 2]
    assert result.params["kept_bytes"] == len(original) // 2


def test_stale_timestamp_signs_in_the_past_and_refuses_a_fresh_age() -> None:
    fixtures = pair("charge_succeeded")
    result = plan(Fault.STALE_TIMESTAMP, fixtures, SECRET, now=NOW)
    assert result.params["age_s"] == 600
    assert result.deliveries[0].sig_header.startswith(f"t={NOW - 600},")
    # The body itself is untouched: only the timestamp is the attack.
    assert result.deliveries[0].body == serialize(fixtures[0].event)

    with pytest.raises(ValueError, match="inside the"):
        plan(Fault.STALE_TIMESTAMP, fixtures, SECRET, params={"age_s": 10}, now=NOW)


def test_downgrade_scheme_sends_a_correct_v0_and_no_v1() -> None:
    fixtures = pair("charge_succeeded")
    header = plan(Fault.DOWNGRADE_SCHEME, fixtures, SECRET, now=NOW).deliveries[0].sig_header
    assert "v0=" in header
    assert "v1=" not in header


# --- semantic faults must NOT be rejected -----------------------------------


@pytest.mark.parametrize("fault", SEMANTIC_FAULTS)
@pytest.mark.parametrize("kind", ALL_KINDS)
def test_semantic_faults_deliver_verifiable_requests(fault: Fault, kind: str) -> None:
    # These attack semantics, not cryptography: every byte must verify, so a
    # 4xx during a run of one of them is a finding rather than the plan.
    result = plan(fault, pair(kind), SECRET, now=NOW)
    assert result.deliveries
    for delivery in result.deliveries:
        assert delivery.expect_rejected is False
        verify(delivery.body, delivery.sig_header, SECRET, now=NOW)


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_delay_inside_tolerance_verifies_on_arrival(kind: str) -> None:
    fixtures = pair(kind)
    result = plan(Fault.DELAY, fixtures, SECRET, params={"seconds": 2.0}, now=NOW)
    delivery = result.deliveries[0]
    assert delivery.delay_before_s == 2.0
    assert delivery.expect_rejected is False
    verify(delivery.body, delivery.sig_header, SECRET, now=NOW + 2)
    assert result.expectation.posted_event_ids == frozenset(
        f.event_id for f in fixtures[:1] if f.expected_delta
    )


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_delay_past_tolerance_is_stale_on_arrival(kind: str) -> None:
    fixtures = pair(kind)
    result = plan(
        Fault.DELAY,
        fixtures,
        SECRET,
        params={"seconds": 1.0, "past_tolerance": True},
        now=NOW,
    )
    delivery = result.deliveries[0]
    assert delivery.expect_rejected is True
    with pytest.raises(SignatureVerificationError, match="outside tolerance"):
        verify(delivery.body, delivery.sig_header, SECRET, now=NOW + 1)
    assert result.expectation.posted_event_ids == frozenset()
    assert result.expectation.rejected_event_ids == {fixtures[0].event_id}
    assert result.expectation.balances_delta == {}


def test_delay_backdating_clears_the_tolerance_window() -> None:
    fixtures = pair("charge_succeeded")
    result = plan(Fault.DELAY, fixtures, SECRET, params={"past_tolerance": True}, now=NOW)
    signed_at = int(result.deliveries[0].sig_header.split(",")[0].removeprefix("t="))
    assert NOW - signed_at > DEFAULT_TOLERANCE_SECONDS


# --- per-fault shapes -------------------------------------------------------


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_duplicate_sends_identical_bodies_and_posts_once(kind: str) -> None:
    fixtures = pair(kind)
    result = plan(Fault.DUPLICATE, fixtures, SECRET, now=NOW)
    assert result.params["n"] == 3
    assert len(result.deliveries) == 3
    assert result.concurrent is False
    bodies = {d.body for d in result.deliveries}
    headers = {d.sig_header for d in result.deliveries}
    assert len(bodies) == 1 and len(headers) == 1
    assert {d.event_id for d in result.deliveries} == {fixtures[0].event_id}
    assert result.expectation.posted_event_ids == frozenset(
        f.event_id for f in fixtures[:1] if f.expected_delta
    )
    assert result.expectation.balances_delta == fixtures[0].expected_delta


def test_duplicate_honors_n_and_rejects_a_degenerate_n() -> None:
    fixtures = pair("charge_succeeded")
    assert len(plan(Fault.DUPLICATE, fixtures, SECRET, params={"n": 5}, now=NOW).deliveries) == 5
    with pytest.raises(ValueError, match="n >= 2"):
        plan(Fault.DUPLICATE, fixtures, SECRET, params={"n": 1}, now=NOW)


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_concurrent_duplicate_is_parallel_and_posts_once(kind: str) -> None:
    fixtures = pair(kind)
    result = plan(Fault.CONCURRENT_DUPLICATE, fixtures, SECRET, now=NOW)
    assert result.concurrent is True
    assert result.params["n"] == 8
    assert len(result.deliveries) == 8
    assert len({d.body for d in result.deliveries}) == 1
    assert result.expectation.posted_event_ids == frozenset(
        f.event_id for f in fixtures[:1] if f.expected_delta
    )


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_duplicate_object_shares_the_money_movement_key(kind: str) -> None:
    fixtures = pair(kind)
    result = plan(Fault.DUPLICATE_OBJECT, fixtures, SECRET, now=NOW)
    assert len(result.deliveries) == 2

    first, second = (json.loads(d.body) for d in result.deliveries)
    assert first["id"] != second["id"]
    assert first["type"] == second["type"]
    assert money_movement_object_id(first) == money_movement_object_id(second)
    assert result.params["twin_event_id"] == second["id"]

    assert result.expectation.posted_event_ids == frozenset(
        f.event_id for f in fixtures[:1] if f.expected_delta
    )
    assert second["id"] not in result.expectation.posted_event_ids
    assert result.expectation.balances_delta == fixtures[0].expected_delta


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_reorder_delivers_in_reverse_and_posts_everything(kind: str) -> None:
    fixtures = [make(kind, seq=s, created=NOW) for s in (21, 22, 23)]
    result = plan(Fault.REORDER, fixtures, SECRET, now=NOW)
    assert result.params["order"] == "reverse"
    assert [d.event_id for d in result.deliveries] == [
        f.event_id for f in reversed(fixtures)
    ]
    assert result.expectation.posted_event_ids == frozenset(
        f.event_id for f in fixtures if f.expected_delta
    )
    assert result.expectation.balances_delta == expected_sum(
        fixtures, result.expectation.posted_event_ids
    )


def test_reorder_needs_two_events() -> None:
    with pytest.raises(ValueError, match="at least two fixtures"):
        plan(Fault.REORDER, [make("payout_paid", seq=24, created=NOW)], SECRET, now=NOW)
    with pytest.raises(ValueError, match="unsupported REORDER order"):
        plan(Fault.REORDER, pair("payout_paid"), SECRET, params={"order": "shuffle"}, now=NOW)


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_drop_delivers_nothing_and_only_the_reconciler_can_see_it(kind: str) -> None:
    fixtures = pair(kind)
    result = plan(Fault.DROP, fixtures, SECRET, now=NOW)
    assert result.deliveries == ()
    assert result.expectation.posted_event_ids == frozenset()
    assert result.expectation.rejected_event_ids == frozenset()
    assert result.expectation.balances_delta == {}
    assert result.expectation.reconciler_should_report_break is True
    assert result.expectation.invariant_after is True
    assert result.params["dropped_event_ids"] == [f.event_id for f in fixtures]


@pytest.mark.parametrize("fault", tuple(f for f in ALL_FAULTS if f is not Fault.DROP))
def test_only_drop_expects_a_reconciler_break(fault: Fault) -> None:
    result = plan(fault, pair("charge_succeeded"), SECRET, now=NOW)
    assert result.expectation.reconciler_should_report_break is False


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_respond_500_retries_the_same_body_once(kind: str) -> None:
    fixtures = pair(kind)
    result = plan(Fault.RESPOND_500, fixtures, SECRET, now=NOW)
    assert result.force_500 is True
    assert len(result.deliveries) == 2
    assert result.deliveries[0].body == result.deliveries[1].body
    assert result.deliveries[0].sig_header == result.deliveries[1].sig_header
    assert result.expectation.posted_event_ids == frozenset(
        f.event_id for f in fixtures[:1] if f.expected_delta
    )


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_partial_write_kills_the_worker_then_redelivers(kind: str) -> None:
    fixtures = pair(kind)
    result = plan(Fault.PARTIAL_WRITE, fixtures, SECRET, now=NOW)
    assert len(result.deliveries) == 1
    assert result.kill_worker_after_s == 0.15
    assert result.redeliver_after_kill is True
    assert result.expectation.posted_event_ids == frozenset(
        f.event_id for f in fixtures[:1] if f.expected_delta
    )

    tuned = plan(Fault.PARTIAL_WRITE, fixtures, SECRET, params={"kill_after_s": 0.5}, now=NOW)
    assert tuned.kill_worker_after_s == 0.5


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_slow_loris_dribbles_one_body(kind: str) -> None:
    fixtures = pair(kind)
    result = plan(Fault.SLOW_LORIS, fixtures, SECRET, now=NOW)
    assert len(result.deliveries) == 1
    assert result.deliveries[0].slow_loris_s == 3.0
    assert result.expectation.posted_event_ids == frozenset(
        f.event_id for f in fixtures[:1] if f.expected_delta
    )


def test_only_the_designated_faults_set_the_process_control_flags() -> None:
    for fault in (Fault.NONE, *ALL_FAULTS):
        result = plan(fault, pair("charge_succeeded"), SECRET, now=NOW)
        assert result.concurrent is (fault is Fault.CONCURRENT_DUPLICATE)
        assert result.force_500 is (fault is Fault.RESPOND_500)
        assert (result.kill_worker_after_s is not None) is (fault is Fault.PARTIAL_WRITE)


def test_none_delivers_each_fixture_once_in_order() -> None:
    fixtures = [make(k, seq=30 + i, created=NOW) for i, k in enumerate(ALL_KINDS)]
    result = plan(Fault.NONE, fixtures, SECRET, now=NOW)
    assert [d.event_id for d in result.deliveries] == [f.event_id for f in fixtures]
    assert result.expectation.posted_event_ids == frozenset(
        f.event_id for f in fixtures if f.expected_delta
    )
    # One of each money kind: the deltas add up across kinds, not just within.
    assert result.expectation.balances_delta == {
        "stripe_balance": 941 - 400 - 3500 - 5000,
        "processing_fees": 59 + 1500,
        "revenue": 1000,
        "refunds_contra": 400,
        "dispute_losses": 2000,
        "bank": 5000,
    }
