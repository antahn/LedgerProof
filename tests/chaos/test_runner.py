"""Scenario generator + runner.

Two layers, deliberately separated:

- Pure tests over `generate()` — determinism, id uniqueness across a whole
  sweep, fault coverage, and the recorded skips. No processes, no database.
- A SMOKE RUN of the real thing: a live stack (uvicorn + celery + Postgres +
  Redis), three scenarios driven end to end through the chaos proxy, and the
  artifact they produce. Nothing about the runner's central claim — that it can
  tell a healthy ledger from a broken one — is observable without real
  processes, so the smoke run uses them.

The label test is the one that protects Phase 5: break detection must be
computable from the plan and the observation alone. `detect_breaks` never
receives a Scenario, and the smoke run re-derives every record's verdict from a
label-stripped copy to prove the label was never load-bearing.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    # harness/ is not an installed package (the wheel ships src/ledgerproof
    # only), and pytest puts the test's own directory on sys.path, not rootdir.
    sys.path.insert(0, str(REPO_ROOT))

from harness.events import ALL_KINDS
from harness.faults import ALL_FAULTS, Fault, FaultPlan, plan
from harness.runner import (
    Observation,
    _HealthProbe,
    detect_breaks,
    observation_from_record,
    run_all,
    summarize,
    task_in_flight,
)
from harness.scenarios import (
    DEFAULT_FAULTS,
    Scenario,
    generate,
    skip_reason,
    skipped_combos,
)
from harness.stack import Stack

PINNED_KEYS = {
    "scenario_id",
    "fault",
    "params",
    "events",
    "invariant_before",
    "invariant_after",
    "ledger_diff",
    "duration_ms",
    "deliveries",
    "expected",
    "actual",
    "breaks",
    "quiescent",
    "label",
}

SMOKE_PORT = 8103  # not 8100: tests/chaos/test_stack.py owns that one


# --------------------------------------------------------------- generator --


def test_generate_is_deterministic_for_a_seed() -> None:
    first = generate(seed=11, per_combo=2)
    second = generate(seed=11, per_combo=2)

    assert [s.scenario_id for s in first] == [s.scenario_id for s in second]
    assert [s.label for s in first] == [s.label for s in second]
    assert [[fx.event for fx in s.fixtures] for s in first] == [
        [fx.event for fx in s.fixtures] for s in second
    ]
    # A different seed must actually move the amounts, or "seeded" is decorative.
    assert [s.label["balances_delta"] for s in generate(seed=12, per_combo=2)] != [
        s.label["balances_delta"] for s in first
    ]


def test_event_ids_are_unique_across_the_whole_sweep() -> None:
    scenarios = generate(seed=3, per_combo=3)
    event_ids = [fx.event_id for s in scenarios for fx in s.fixtures]
    object_ids = [s.fixtures[0].event["data"]["object"]["id"] for s in scenarios if s.fixtures]

    assert len(event_ids) == len(set(event_ids))
    # Object ids matter as much as event ids: (event_type, object_id) is the
    # second dedupe key, so a repeat there would make one scenario's duplicate
    # answer a fact about another scenario.
    assert len(object_ids) == len(set(object_ids))
    assert all(eid.startswith("evt_h") for eid in event_ids)


def test_start_seq_keeps_two_generations_from_colliding() -> None:
    first = generate(seed=0, kinds=("charge_succeeded",))
    used = sum(len(s.fixtures) for s in first)
    second = generate(seed=0, kinds=("charge_succeeded",), start_seq=used)

    ids = {fx.event_id for s in first for fx in s.fixtures}
    assert ids.isdisjoint({fx.event_id for s in second for fx in s.fixtures})


def test_covers_every_fault() -> None:
    scenarios = generate(seed=0)
    covered = {s.fault for s in scenarios}

    assert set(ALL_FAULTS) <= covered, "all 13 faults must be generated"
    assert Fault.NONE in covered, "the baseline is what proves the harness can see 'correct'"
    assert covered <= set(DEFAULT_FAULTS)


def test_every_kind_is_exercised() -> None:
    scenarios = generate(seed=0)
    kinds = {fx.kind for s in scenarios for fx in s.fixtures}
    assert kinds == set(ALL_KINDS)


def test_skipped_combos_are_reported_not_silently_dropped() -> None:
    scenarios = generate(seed=0)
    skipped = scenarios.skipped

    assert skipped, "some combinations are meaningless; saying so is the point"
    assert skipped == skipped_combos()
    assert all(s.reason for s in skipped)
    # Nothing skipped was also generated.
    generated = {(s.fault, fx.kind) for s in scenarios for fx in s.fixtures}
    assert not any((s.fault, s.kind) in generated for s in skipped)
    # The sharpest one: a dropped non-money event is CORRECTLY invisible to the
    # reconciler, so DROP's expectation would be a lie for that pairing.
    assert skip_reason(Fault.DROP, "invoice_payment_failed") is not None
    assert skip_reason(Fault.DROP, "charge_succeeded") is None
    # Signature attacks are event-agnostic, so they keep the non-money kind.
    assert skip_reason(Fault.TAMPER_BODY, "invoice_payment_failed") is None


def test_reorder_gets_two_events_and_they_are_distinct() -> None:
    scenarios = generate(seed=5, faults=(Fault.REORDER,), kinds=("charge_succeeded",))
    (scenario,) = scenarios

    assert len(scenario.fixtures) == 2
    assert scenario.fixtures[0].event_id != scenario.fixtures[1].event_id
    assert len(scenario.label["posted_event_ids"]) == 2


def test_label_is_the_ground_truth() -> None:
    for scenario in generate(seed=9):
        label = scenario.label
        assert set(label) >= {
            "fault",
            "params",
            "event_ids",
            "posted_event_ids",
            "rejected_event_ids",
            "balances_delta",
        }
        assert label["fault"] == scenario.fault.value
        assert label["event_ids"] == [fx.event_id for fx in scenario.fixtures]
        assert not set(label["posted_event_ids"]) & set(label["rejected_event_ids"])

        # balances_delta must be the sum of the posted fixtures' own deltas,
        # recomputed here rather than trusted: a wrong label is worse than none.
        want: dict[str, int] = {}
        for fixture in scenario.fixtures:
            if fixture.event_id in set(label["posted_event_ids"]):
                for account, amount in fixture.expected_delta.items():
                    want[account] = want.get(account, 0) + amount
        assert label["balances_delta"] == {k: v for k, v in want.items() if v != 0}


def test_generate_rejects_unknown_kinds() -> None:
    with pytest.raises(ValueError, match="unknown event kinds"):
        generate(kinds=("charge_suceeded",))


# ---------------------------------------------------------- break detection --


def _plan_for(scenario: Scenario) -> FaultPlan:
    return plan(scenario.fault, scenario.fixtures, "whsec_" + "t" * 32, params=scenario.params)


def test_signature_fault_refused_by_the_wrong_layer_is_a_break() -> None:
    # The blind spot a mutation study found: with HMAC verification entirely
    # deleted, TRUNCATE_BODY still read green, because half a JSON body is
    # unparseable and ingest answered 400 from json.loads. A rejection now has
    # to come from the layer the fault is actually attacking.
    (scenario,) = generate(seed=1, faults=(Fault.TRUNCATE_BODY,), kinds=("charge_succeeded",))
    fault_plan = _plan_for(scenario)
    (delivery,) = fault_plan.deliveries
    assert delivery.expect_rejection_reason == "invalid signature"

    refused_by_signature = Observation(
        ledger_diff={}, invariant_after=True, posted=(), transactions=0, quiescent=True
    )
    assert detect_breaks(fault_plan, refused_by_signature) == []

    parser_refusal = (
        f"{delivery.event_id}: expected refusal by 'invalid signature', "
        'got HTTP 400 \'{"error":"invalid JSON body"}\''
    )
    refused_by_json_parser = dataclasses.replace(
        refused_by_signature, wrong_rejection_reason=(parser_refusal,)
    )
    kinds = [b["kind"] for b in detect_breaks(fault_plan, refused_by_json_parser)]
    assert kinds == ["WRONG_REJECTION_REASON"]


def test_detect_breaks_cannot_see_the_label() -> None:
    (scenario,) = generate(seed=1, faults=(Fault.NONE,), kinds=("charge_succeeded",))
    fault_plan = _plan_for(scenario)
    stripped = dataclasses.replace(scenario, label={})
    obs = Observation(
        ledger_diff=dict(fault_plan.expectation.balances_delta),
        invariant_after=True,
        posted=tuple(sorted(fault_plan.expectation.posted_event_ids)),
        transactions=1,
        quiescent=True,
    )

    assert detect_breaks(fault_plan, obs) == []
    assert detect_breaks(_plan_for(stripped), obs) == detect_breaks(fault_plan, obs)


def test_detect_breaks_names_every_failure_mode() -> None:
    (scenario,) = generate(seed=2, faults=(Fault.NONE,), kinds=("charge_succeeded",))
    fault_plan = _plan_for(scenario)
    event_id = scenario.fixtures[0].event_id

    lost = detect_breaks(
        fault_plan,
        Observation(
            ledger_diff={}, invariant_after=True, posted=(), transactions=0, quiescent=True
        ),
    )
    kinds = [b["kind"] for b in lost]
    assert "LOST_EVENT" in kinds and "LEDGER_DIFF_MISMATCH" in kinds
    assert any(event_id in b["detail"] for b in lost)

    doubled = detect_breaks(
        fault_plan,
        Observation(
            ledger_diff={k: v * 2 for k, v in fault_plan.expectation.balances_delta.items()},
            invariant_after=False,
            posted=(event_id, f"{event_id}_dup"),
            transactions=2,
            quiescent=False,
        ),
    )
    assert [b["kind"] for b in doubled][:2] == ["INVARIANT_VIOLATION", "LEDGER_DIFF_MISMATCH"]
    assert {"DOUBLE_POST", "NOT_QUIESCENT"} <= {b["kind"] for b in doubled}


def test_detect_breaks_flags_an_accepted_bad_delivery() -> None:
    (scenario,) = generate(seed=4, faults=(Fault.TAMPER_BODY,), kinds=("charge_succeeded",))
    fault_plan = _plan_for(scenario)
    event_id = scenario.fixtures[0].event_id

    # Nothing posted, but ingest answered 200 to a tampered body: the ledger is
    # clean by luck, and that must still be a break.
    breaks = detect_breaks(
        fault_plan,
        Observation(
            ledger_diff={},
            invariant_after=True,
            posted=(),
            transactions=0,
            quiescent=True,
            accepted_rejected=(event_id,),
        ),
    )
    assert [b["kind"] for b in breaks] == ["ACCEPTED_BAD_DELIVERY"]

    assert (
        detect_breaks(
            fault_plan,
            Observation(
                ledger_diff={}, invariant_after=True, posted=(), transactions=0, quiescent=True
            ),
        )
        == []
    )


def test_detect_breaks_flags_a_drop_the_reconciler_missed() -> None:
    (scenario,) = generate(seed=6, faults=(Fault.DROP,), kinds=("payout_paid",))
    fault_plan = _plan_for(scenario)
    clean = Observation(
        ledger_diff={}, invariant_after=True, posted=(), transactions=0, quiescent=True
    )

    assert [b["kind"] for b in detect_breaks(fault_plan, clean)] == ["RECONCILER_MISSED_DROP"]
    assert detect_breaks(fault_plan, dataclasses.replace(clean, recon_reported_break=True)) == []


def test_health_probe_samples_and_stops() -> None:
    """The SLOW_LORIS responsiveness probe must sample, and must join cleanly.

    Regression: the probe's stop flag was originally named `_stop`, shadowing
    threading.Thread._stop — a real method CPython calls from join() — so every
    SLOW_LORIS scenario died with "'Event' object is not callable" *after* the
    delivery had already been made. Found by the first real run.
    """

    class _FakeStack:
        def __init__(self) -> None:
            self.calls = 0

        def ingest_healthy(self, *, timeout: float = 2.0) -> bool:
            self.calls += 1
            return True

    fake = _FakeStack()
    probe = _HealthProbe(fake, interval=0.05)  # type: ignore[arg-type]
    assert probe.responsive is None, "nothing sampled yet is not the same as healthy"
    probe.start()
    time.sleep(0.3)
    probe.stop()

    assert not probe.is_alive()
    assert fake.calls >= 2
    assert probe.responsive is True

    unhealthy = _HealthProbe(fake, interval=0.05)  # type: ignore[arg-type]
    unhealthy.samples = [True, False, True]
    assert unhealthy.responsive is False


def test_task_in_flight_reads_the_worker_log() -> None:
    """PARTIAL_WRITE must report whether the kill actually caught a write."""
    both = (
        "[INFO] Task ledgerproof.process_event[aaaa-1111] received\n"
        "[INFO] Task ledgerproof.process_event[aaaa-1111] succeeded in 0.06s: 'posted'\n"
    )
    assert task_in_flight(both) is False
    assert task_in_flight(both + "[INFO] Task ledgerproof.process_event[bbbb-2222] received\n")
    assert task_in_flight("") is False


# ----------------------------------------------------------------- summary --


def test_summarize_counts_faults_breaks_and_skips() -> None:
    records = [
        {"fault": "NONE", "breaks": []},
        {"fault": "DUPLICATE", "breaks": [{"kind": "LOST_EVENT", "detail": "x"}]},
        {"fault": "DUPLICATE", "breaks": [{"kind": "LOST_EVENT", "detail": "y"}], "error": "boom"},
        {"fault": "PARTIAL_WRITE", "breaks": [], "killed_with_task_in_flight": False},
    ]
    summary = summarize(
        records,
        argv=["runner.py", "--seed", "0"],
        skipped=skipped_combos(),
        generated=5,
        wall_clock_s=1.234,
        started_at="2026-08-05T00:00:00+00:00",
        artifact="artifacts/chaos_x.jsonl",
    )

    assert summary["counts_by_fault"] == {"DUPLICATE": 2, "NONE": 1, "PARTIAL_WRITE": 1}
    assert summary["breaks_by_kind"] == {"LOST_EVENT": 2}
    assert summary["scenarios_with_breaks"] == 2
    assert summary["scenarios_errored"] == 1
    assert summary["scenarios_generated"] == 5 and summary["scenarios_run"] == 4
    # Coverage must never read as a result: one kill, none of them mid-write.
    assert summary["kill_scenarios"] == 1
    assert summary["kills_with_task_in_flight"] == 0
    assert summary["skipped_combos"] and summary["argv"][0].endswith("runner.py")


# --------------------------------------------------------------- smoke run --


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    live = Stack(port=SMOKE_PORT)
    live.create_database()
    with live:
        yield live


@pytest.mark.chaos
def test_smoke_run_end_to_end(stack: Stack, tmp_path: Path) -> None:
    """Three scenarios through the real system: baseline, dedupe, forgery."""
    scenarios = generate(
        seed=42,
        faults=(Fault.NONE, Fault.DUPLICATE, Fault.TAMPER_BODY),
        kinds=("charge_succeeded",),
    )
    assert len(scenarios) == 3

    out = tmp_path / "chaos_smoke.jsonl"
    started = time.perf_counter()
    records = run_all(stack, scenarios, out)
    elapsed = time.perf_counter() - started

    assert len(records) == 3
    assert elapsed < 120, f"smoke run took {elapsed:.1f}s"

    for record in records:
        assert PINNED_KEYS <= set(record), sorted(PINNED_KEYS - set(record))
        assert record["error"] is None, record["error"]
        assert record["invariant_before"] is True
        assert record["invariant_after"] is True, "no fault may create or destroy money"
        assert isinstance(record["duration_ms"], int)
        assert all(
            {"event_id", "status_code", "error", "duration_ms"} <= set(d)
            for d in record["deliveries"]
        )
        # Self-consistency: a ledger that moved differently than promised must
        # produce the break that says so.
        if record["ledger_diff"] != record["expected"]["balances_delta"]:
            assert any(b["kind"] == "LEDGER_DIFF_MISMATCH" for b in record["breaks"])
        # And the verdict must be reproducible from the artifact alone, with
        # the label removed — Phase 5 leakage protection, checked on real data.
        scenario = next(s for s in scenarios if s.scenario_id == record["scenario_id"])
        stripped = dataclasses.replace(scenario, label={})
        replay = plan(
            stripped.fault, stripped.fixtures, stack.webhook_secret, params=stripped.params
        )
        assert detect_breaks(replay, observation_from_record(record)) == record["breaks"]

    by_fault = {r["fault"]: r for r in records}

    baseline = by_fault["NONE"]
    assert baseline["breaks"] == [], baseline["breaks"]
    assert baseline["actual"]["transactions"] == 1
    assert baseline["ledger_diff"] == baseline["expected"]["balances_delta"]
    assert baseline["quiescent"] is True

    duplicate = by_fault["DUPLICATE"]
    assert len(duplicate["deliveries"]) == duplicate["params"]["n"] >= 2
    assert duplicate["actual"]["transactions"] == 1, "N deliveries, one transaction"
    assert duplicate["breaks"] == [], duplicate["breaks"]

    tampered = by_fault["TAMPER_BODY"]
    assert tampered["expected"]["rejected"] == tampered["events"]
    assert tampered["actual"]["transactions"] == 0
    assert tampered["ledger_diff"] == {}
    assert tampered["breaks"] == [], tampered["breaks"]

    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert [json.loads(line)["scenario_id"] for line in lines] == [
        r["scenario_id"] for r in records
    ]


@pytest.mark.chaos
def test_a_ledger_that_does_not_move_is_reported(stack: Stack, tmp_path: Path) -> None:
    """The harness must be able to go red against the real system.

    63 green scenarios mean nothing unless a red one is reachable, so this runs
    the SAME scenario twice. The second run's Expectation says the event posts
    a transaction; the system correctly answers `duplicate` and the ledger does
    not move. The expectation is the thing that is wrong here — deliberately —
    and the runner must say so rather than report another quiet success.
    """
    scenarios = generate(
        seed=1234, faults=(Fault.NONE,), kinds=("charge_succeeded",), start_seq=50_000
    )
    out = tmp_path / "chaos_falsify.jsonl"

    (first,) = run_all(stack, scenarios, out)
    assert first["breaks"] == [], first["breaks"]
    assert first["ledger_diff"] == first["expected"]["balances_delta"] != {}

    (second,) = run_all(stack, scenarios, tmp_path / "chaos_falsify_2.jsonl")
    assert second["ledger_diff"] == {}, "the second delivery must be a no-op duplicate"
    assert [b["kind"] for b in second["breaks"]] == ["LEDGER_DIFF_MISMATCH"]
    assert second["expected"]["balances_delta"] == first["ledger_diff"]
    # Money conservation is untouched by the harness being wrong about it.
    assert second["invariant_after"] is True


@pytest.mark.chaos
def test_smoke_run_artifact_carries_the_label(stack: Stack, tmp_path: Path) -> None:
    """The label rides in the artifact (Phase 5 reads it) but never in a verdict."""
    scenarios = generate(seed=77, faults=(Fault.NONE,), kinds=("payout_paid",), start_seq=9000)
    out = tmp_path / "chaos_label.jsonl"
    (record,) = run_all(stack, scenarios, out)

    label = record["label"]
    assert label["fault"] == "NONE"
    assert label["event_ids"] == record["events"]
    assert label["balances_delta"] == record["ledger_diff"] == record["expected"]["balances_delta"]
    assert record["actual"]["posted"] == label["posted_event_ids"]
