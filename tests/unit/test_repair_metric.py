"""Scoring a proposed repair against ground truth.

Exercised on the real repair stratum where it exists, so the metric is
validated against ledgers the harness actually damaged rather than fixtures.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ledgerproof.triage import bench
from ledgerproof.triage.schema import FaultClass, RepairEntry, Verdict

STRATUM = Path(__file__).resolve().parents[2] / "artifacts" / "repair_stratum.jsonl"

# A doubled charge: expected 941/59/1000, observed exactly twice that.
DOUBLED = bench.Case(
    case_id="m5::x",
    evidence={},
    label_fault="DUPLICATE",
    label_kind="charge_succeeded",
    money_was_missing=True,
    expected_delta={"stripe_balance": 941, "processing_fees": 59, "revenue": 1000},
    observed_delta={"stripe_balance": 1882, "processing_fees": 118, "revenue": 2000},
    repairable=True,
    stratum="m5_double_post",
)

# An unbalanced write: stripe_balance debited the full gross instead of net.
UNBALANCED = bench.Case(
    case_id="m7::x",
    evidence={},
    label_fault="NONE",
    label_kind="charge_succeeded",
    money_was_missing=True,
    expected_delta={"stripe_balance": 941, "processing_fees": 59, "revenue": 1000},
    observed_delta={"stripe_balance": 1000, "processing_fees": 59, "revenue": 1000},
    repairable=False,
    stratum="m7_unbalanced",
)


def verdict(entries: list[RepairEntry], *, missing: bool = True) -> Verdict:
    return Verdict(
        fault_class=FaultClass.DUPLICATE,
        confidence=0.9,
        root_cause="reasoning",
        money_is_missing=missing,
        proposed_repair=entries,
    )


CORRECT_REVERSAL = [
    RepairEntry(account_name="revenue", direction="debit", amount_minor=1000),
    RepairEntry(account_name="stripe_balance", direction="credit", amount_minor=941),
    RepairEntry(account_name="processing_fees", direction="credit", amount_minor=59),
]


def test_a_correct_reversal_restores_the_expected_balances() -> None:
    assert bench.repair_restores_expected_balances(DOUBLED, verdict(CORRECT_REVERSAL))


def test_a_balanced_repair_into_the_wrong_accounts_is_a_false_repair() -> None:
    """The dangerous failure: the database accepts it and the books stay wrong."""
    wrong_account = [
        RepairEntry(account_name="revenue", direction="debit", amount_minor=1000),
        RepairEntry(account_name="bank", direction="credit", amount_minor=941),
        RepairEntry(account_name="processing_fees", direction="credit", amount_minor=59),
    ]
    v = verdict(wrong_account)
    from ledgerproof.triage.schema import repair_is_balanced

    assert repair_is_balanced(wrong_account), "it balances, so the DB would take it"
    assert not bench.repair_restores_expected_balances(DOUBLED, v)

    scored = bench.score(DOUBLED, v, latency_s=1.0, cost_usd=0.0)
    assert scored.false_repair is True
    assert scored.repair_balanced is True


def test_proposing_nothing_scores_as_no_repair_not_as_success() -> None:
    scored = bench.score(DOUBLED, verdict([], missing=False), latency_s=1.0, cost_usd=0.0)
    assert scored.repair_correct is False
    assert scored.proposed_repair is False


def test_claiming_to_fix_an_unbalanced_write_is_counted_as_an_error() -> None:
    """No balanced transaction can close that gap — so any proposal is wrong."""
    scored = bench.score(UNBALANCED, verdict(CORRECT_REVERSAL), latency_s=1.0, cost_usd=0.0)
    assert scored.claimed_to_fix_the_unfixable is True

    honest = bench.score(
        UNBALANCED, verdict([], missing=True), latency_s=1.0, cost_usd=0.0
    )
    assert honest.claimed_to_fix_the_unfixable is False


def test_aggregate_separates_fixable_from_unfixable() -> None:
    scored = [
        bench.score(DOUBLED, verdict(CORRECT_REVERSAL), latency_s=1.0, cost_usd=0.0),
        bench.score(UNBALANCED, verdict(CORRECT_REVERSAL), latency_s=1.0, cost_usd=0.0),
    ]
    agg = bench.aggregate(scored)
    assert agg["repair_stratum_n"] == 2
    assert agg["repair_fixable_n"] == 1
    assert agg["unfixable_n"] == 1
    assert agg["repair_restores_expected_balances"] == 1.0
    assert agg["claimed_to_fix_the_unfixable_rate"] == 1.0


@pytest.mark.skipif(not STRATUM.exists(), reason="repair stratum not generated")
def test_the_real_stratum_loads_and_is_genuinely_damaged() -> None:
    cases = bench.load_repair_cases(STRATUM)
    assert cases, "stratum is empty"
    assert all(c.money_was_missing for c in cases)
    # Every case's books must actually disagree with what the events implied,
    # or it is not a repair case at all.
    assert all(
        {k: v for k, v in c.observed_delta.items() if v}
        != {k: v for k, v in c.expected_delta.items() if v}
        for c in cases
    )
    strata = {c.stratum for c in cases}
    assert strata == {"m5_double_post", "m7_unbalanced"}
    assert any(c.repairable for c in cases)
    assert any(not c.repairable for c in cases)


@pytest.mark.skipif(not STRATUM.exists(), reason="repair stratum not generated")
def test_the_known_reversal_repairs_every_real_double_post() -> None:
    """Ground truth is constructible: for each M5 case, the mirror of the
    surplus copy restores the expected balances exactly."""
    cases = [c for c in bench.load_repair_cases(STRATUM) if c.stratum == "m5_double_post"]
    assert cases

    for case in cases:
        surplus = {
            account: case.observed_delta.get(account, 0) - case.expected_delta.get(account, 0)
            for account in set(case.observed_delta) | set(case.expected_delta)
        }
        entries = []
        for account, delta in surplus.items():
            if delta == 0:
                continue
            normal = bench.ACCOUNT_NORMALS[account]
            # Reverse the surplus: post the direction opposite to its sign.
            reversing = "credit" if (delta > 0) == (normal == "debit") else "debit"
            entries.append(
                RepairEntry(
                    account_name=account, direction=reversing, amount_minor=abs(delta)
                )
            )
        assert bench.repair_restores_expected_balances(case, verdict(entries)), case.case_id
