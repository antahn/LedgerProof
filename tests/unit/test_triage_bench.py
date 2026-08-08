"""Benchmark mechanics: per-model request shapes, split, scoring, budget, gate."""

from __future__ import annotations

import pytest

from ledgerproof.triage import bench
from ledgerproof.triage.agent import (
    EFFORT_MODELS,
    HAIKU,
    OPUS,
    SONNET,
    RepairApproval,
    RepairRejected,
    approve_and_apply,
    request_params,
)
from ledgerproof.triage.schema import FaultClass, RepairEntry, Verdict, repair_is_balanced

CASE = {"ledger_diff": {}, "invariant_holds": True, "event_log": [], "deliveries": []}


def make_case(case_id: str, fault: str, *, missing: bool = False) -> bench.Case:
    return bench.Case(
        case_id=case_id,
        evidence=CASE,
        label_fault=fault,
        label_kind="charge_succeeded",
        money_was_missing=missing,
        expected_delta={},
    )


# ------------------------------------------------------- per-model shapes --


def test_haiku_gets_neither_effort_nor_adaptive_thinking() -> None:
    """Measured against the Models API: Haiku 4.5 supports neither.

    Sending either returns a 400, so a single request shape across the three
    models would fail on the cheapest one.
    """
    params = request_params(HAIKU, CASE, effort="high")
    assert "output_config" not in params
    assert params["thinking"] == {"type": "enabled", "budget_tokens": 2000}
    assert params["thinking"]["budget_tokens"] < params["max_tokens"]


@pytest.mark.parametrize("model", EFFORT_MODELS)
def test_opus_and_sonnet_get_adaptive_and_effort(model: str) -> None:
    params = request_params(model, CASE, effort="low")
    assert params["thinking"] == {"type": "adaptive"}
    assert params["output_config"] == {"effort": "low"}
    assert "budget_tokens" not in params["thinking"]


def test_effort_is_omitted_when_not_requested() -> None:
    assert "output_config" not in request_params(OPUS, CASE)


def test_every_model_gets_the_identical_cached_prefix() -> None:
    """A per-model prefix would mean per-model caches and incomparable costs."""
    prefixes = {m: request_params(m, CASE)["system"] for m in (OPUS, SONNET, HAIKU)}
    assert prefixes[OPUS] == prefixes[SONNET] == prefixes[HAIKU]


# ------------------------------------------------------------------ split --


def test_split_is_stratified_and_covers_every_class() -> None:
    cases = [make_case(f"c{i}", f"F{i % 5}") for i in range(100)]
    train, test = bench.stratified_split(cases, test_fraction=0.4, seed=0)

    assert len(train) + len(test) == len(cases)
    assert set(bench.class_balance(train)) == set(bench.class_balance(test))
    for fault, n in bench.class_balance(cases).items():
        assert bench.class_balance(test)[fault] == round(n * 0.4)


def test_a_rare_class_still_reaches_the_test_split() -> None:
    """A random split can give a rare class zero test items, making its
    accuracy undefined and silently reported as 0 or 100."""
    cases = [make_case(f"c{i}", "COMMON") for i in range(50)] + [make_case("rare", "RARE")]
    _, test = bench.stratified_split(cases, test_fraction=0.2, seed=0)
    assert "RARE" in bench.class_balance(test)


def test_split_is_deterministic_and_disjoint() -> None:
    cases = [make_case(f"c{i}", f"F{i % 4}") for i in range(40)]
    a_train, a_test = bench.stratified_split(cases, seed=7)
    _, b_test = bench.stratified_split(cases, seed=7)
    assert [c.case_id for c in a_test] == [c.case_id for c in b_test]
    assert not {c.case_id for c in a_train} & {c.case_id for c in a_test}


# ---------------------------------------------------------------- scoring --


def verdict(primary: str, alternates: tuple[str, ...] = (), *, missing: bool = False,
            repair: list[RepairEntry] | None = None) -> Verdict:
    return Verdict(
        fault_class=FaultClass(primary),
        alternate_fault_classes=[FaultClass(a) for a in alternates],
        confidence=0.8,
        root_cause="because",
        money_is_missing=missing,
        proposed_repair=repair or [],
    )


def test_accuracy_at_1_and_3_are_scored_separately() -> None:
    case = make_case("c1", "TAMPER_BODY")
    near = bench.score(case, verdict("TRUNCATE_BODY", ("STALE_TIMESTAMP", "TAMPER_BODY")),
                       latency_s=1.0, cost_usd=0.0)
    assert near.correct_at_1 is False
    assert near.correct_at_3 is True

    exact = bench.score(case, verdict("TAMPER_BODY"), latency_s=1.0, cost_usd=0.0)
    assert exact.correct_at_1 and exact.correct_at_3


def test_over_repair_is_counted_on_a_healthy_case() -> None:
    """Proposing a fix for books that were already right is the way an agent
    does harm on a healthy system."""
    case = make_case("c1", "DUPLICATE", missing=False)
    entries = [
        RepairEntry(account_name="revenue", direction="debit", amount_minor=100),
        RepairEntry(account_name="stripe_balance", direction="credit", amount_minor=100),
    ]
    scored = bench.score(case, verdict("DUPLICATE", missing=True, repair=entries),
                         latency_s=1.0, cost_usd=0.0)
    assert scored.over_repair is True
    assert scored.repair_balanced is True  # balanced, and still wrong to propose


def test_refusal_scores_as_wrong_not_as_a_crash() -> None:
    scored = bench.score(make_case("c1", "DROP"), None, latency_s=0.5, cost_usd=0.0,
                         refused=True)
    assert scored.refused and not scored.correct_at_1 and scored.predicted is None


def test_aggregate_reports_per_fault_accuracy_and_confusion() -> None:
    cases = [make_case("a", "DROP"), make_case("b", "DROP"), make_case("c", "NONE")]
    scored = [
        bench.score(cases[0], verdict("DROP"), latency_s=1.0, cost_usd=0.01),
        bench.score(cases[1], verdict("NONE"), latency_s=2.0, cost_usd=0.01),
        bench.score(cases[2], verdict("NONE"), latency_s=3.0, cost_usd=0.01),
    ]
    agg = bench.aggregate(scored)
    assert agg["accuracy@1"] == pytest.approx(2 / 3, abs=1e-4)
    assert agg["accuracy_by_fault"]["DROP"] == 0.5
    assert agg["confusion"]["DROP"] == {"DROP": 1, "NONE": 1}
    assert agg["cost_usd_total"] == pytest.approx(0.03)


# ----------------------------------------------------------------- budget --


def test_budget_guard_refuses_before_spending_past_the_cap() -> None:
    guard = bench.BudgetGuard(cap_usd=0.10)
    guard.spent_usd = 0.09
    with pytest.raises(bench.BudgetExceeded):
        guard.check(projected_usd=0.05)
    guard.check(projected_usd=0.005)  # still under: allowed


def test_cost_model_applies_cache_and_batch_discounts() -> None:
    usage = {"input_tokens": 1_000_000, "output_tokens": 0,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    assert bench.call_cost(OPUS, usage) == pytest.approx(5.00)
    assert bench.call_cost(OPUS, usage, batch=True) == pytest.approx(2.50)

    cached = {"input_tokens": 0, "output_tokens": 0,
              "cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 0}
    assert bench.call_cost(OPUS, cached) == pytest.approx(0.50)  # 10% of input rate


# ------------------------------------------------------------- human gate --


def approval() -> RepairApproval:
    return RepairApproval(approved_by="anthony", scenario_id="s1", reason="reviewed")


def balanced_entries() -> list[RepairEntry]:
    return [
        RepairEntry(account_name="revenue", direction="debit", amount_minor=1000),
        RepairEntry(account_name="stripe_balance", direction="credit", amount_minor=941),
        RepairEntry(account_name="processing_fees", direction="credit", amount_minor=59),
    ]


def test_repair_balance_check_mirrors_the_database_trigger() -> None:
    assert repair_is_balanced(balanced_entries())
    assert not repair_is_balanced(balanced_entries()[:1])  # min two entries
    unbalanced = [
        RepairEntry(account_name="revenue", direction="debit", amount_minor=1000),
        RepairEntry(account_name="stripe_balance", direction="credit", amount_minor=900),
    ]
    assert not repair_is_balanced(unbalanced)


def test_gate_refuses_without_a_named_human() -> None:
    with pytest.raises(RepairRejected, match="no human approver"):
        approve_and_apply(
            "postgresql://unused",
            verdict("DUPLICATE", missing=True, repair=balanced_entries()),
            RepairApproval(approved_by="   ", scenario_id="s1"),
        )


def test_gate_refuses_when_the_verdict_says_nothing_is_wrong() -> None:
    with pytest.raises(RepairRejected, match="no money is missing"):
        approve_and_apply(
            "postgresql://unused",
            verdict("DUPLICATE", missing=False, repair=balanced_entries()),
            approval(),
        )


def test_gate_refuses_an_unbalanced_repair_before_touching_the_database() -> None:
    bad = [
        RepairEntry(account_name="revenue", direction="debit", amount_minor=1000),
        RepairEntry(account_name="stripe_balance", direction="credit", amount_minor=1),
    ]
    with pytest.raises(RepairRejected, match="does not balance"):
        approve_and_apply(
            "postgresql://unused",
            verdict("DUPLICATE", missing=True, repair=bad),
            approval(),
        )


def test_gate_refuses_an_empty_proposal() -> None:
    with pytest.raises(RepairRejected, match="proposes no entries"):
        approve_and_apply(
            "postgresql://unused", verdict("DUPLICATE", missing=True), approval()
        )


def test_the_agent_module_exposes_no_other_write_path() -> None:
    """Every ledger write in triage/ must go through the gate.

    A second import of post_transaction anywhere in the package would be a way
    around the human approval requirement.
    """
    from pathlib import Path

    triage_dir = Path(__file__).resolve().parents[2] / "src" / "ledgerproof" / "triage"
    writers = [
        path.name
        for path in triage_dir.glob("*.py")
        if "post_transaction" in path.read_text(encoding="utf-8")
    ]
    assert writers == ["agent.py"], f"unexpected ledger write path in {writers}"
