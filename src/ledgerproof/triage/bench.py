"""Harness-labeled fault benchmark (Phase 5).

The harness caused every scenario, so it knows the fault class, the events, and
what the ledger should look like — labels that eval work normally pays humans
for, generated exactly and for free. This module turns those artifacts into a
benchmark: a stratified split, per-task metrics, and a cost model.

**Metrics are per task, not one aggregate score.** A single number would hide
the thing that matters: an agent can name the fault correctly and still propose
a repair that balances the books into the wrong accounts.

- `accuracy@1` — the agent's first choice is the injected fault.
- `accuracy@3` — the fault appears in first choice plus two alternates. Scored
  separately because several faults are genuinely hard to separate, and a
  ranked answer is more useful on-call than a confident wrong one.
- `repair_restores_invariant` — the proposal is applied in a scratch database
  and money conservation is re-checked. Objective, and the only metric that
  measures whether the agent's output would actually fix anything.
- `false_repair` — the repair balances (so the database accepts it) but posts
  to the wrong accounts. This is the dangerous failure: it looks like success
  to every check except comparing against ground truth.
- `over_repair` — the agent proposes a repair for a scenario where nothing was
  wrong. On a healthy system that is the most likely way an agent does harm.
- latency p50/p95 and cost per scenario, per model.

Budget: every call's usage is appended to `artifacts/llm_usage.jsonl` and the
running total is checked against the cap before each request, so a sweep aborts
rather than overruns.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any

from ledgerproof.triage import prompts
from ledgerproof.triage.agent import HAIKU, OPUS, SONNET
from ledgerproof.triage.schema import Verdict, repair_is_balanced

REPO_ROOT = Path(__file__).resolve().parents[3]
ARTIFACTS = REPO_ROOT / "artifacts"
USAGE_LOG = ARTIFACTS / "llm_usage.jsonl"

# Published list prices, USD per million tokens (2026-08).
PRICING: dict[str, tuple[float, float]] = {
    OPUS: (5.00, 25.00),
    SONNET: (3.00, 15.00),  # a $2/$10 introductory rate runs through 2026-08-31
    HAIKU: (1.00, 5.00),
}
CACHE_READ_MULTIPLIER = 0.10  # cached prefix reads bill at ~10% of input
CACHE_WRITE_MULTIPLIER = 1.25  # 5-minute TTL
BATCH_MULTIPLIER = 0.50  # Batch API discount

# Minimum cacheable prefix, per model. The shared prefix must clear the LARGEST
# of these or the cheapest model silently caches nothing and its measured cost
# is wrong — which would corrupt the whole frontier comparison.
MIN_CACHEABLE_PREFIX = {OPUS: 512, SONNET: 1024, HAIKU: 4096}


# The seeded chart of accounts: name -> normal direction. Used to apply a
# proposed repair arithmetically, exactly as the account_balances view would.
ACCOUNT_NORMALS = {
    "stripe_balance": "debit",
    "bank": "debit",
    "processing_fees": "debit",
    "dispute_losses": "debit",
    "refunds_contra": "debit",
    "revenue": "credit",
    "customer_liability": "credit",
}


@dataclass(frozen=True)
class Case:
    """One benchmark item: evidence the agent sees, and the label it must not."""

    case_id: str
    evidence: dict[str, Any]
    label_fault: str
    label_kind: str
    money_was_missing: bool
    expected_delta: dict[str, int]
    observed_delta: dict[str, int] = field(default_factory=dict)
    # False for damage no balanced transaction can undo — see
    # tests/unit/test_repair_algebra.py. The correct answer on those is to say
    # so, not to propose entries.
    repairable: bool = True
    stratum: str = "classification"

    def as_prompt_case(self) -> dict[str, Any]:
        return self.evidence


def load_cases(artifact: Path) -> list[Case]:
    """Turn a harness JSONL artifact into benchmark cases.

    The label is read here and kept OUT of `evidence` — `prompts.build_case`
    does the extraction, so the split between what is shown and what is scored
    lives in exactly one place.
    """
    cases: list[Case] = []
    for line in artifact.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("error"):
            continue  # a scenario that crashed has no trustworthy evidence
        label = record.get("label") or {}
        cases.append(
            Case(
                case_id=record["scenario_id"],
                evidence=prompts.build_case(record),
                label_fault=record["fault"],
                label_kind=(record.get("kinds") or ["unknown"])[0],
                money_was_missing=not record.get("invariant_after", True)
                or bool(record.get("breaks")),
                expected_delta=dict(label.get("balances_delta") or {}),
            )
        )
    return cases


def load_repair_cases(artifact: Path) -> list[Case]:
    """Load the repair stratum: scenarios run against deliberately broken builds.

    These are the only cases where the books are actually wrong, because the
    shipped system produces none. Each carries what the ledger SHOULD show and
    what it does show, so a proposed repair can be scored objectively.
    """
    cases: list[Case] = []
    for line in artifact.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("error"):
            continue
        label = record["repair_label"]
        cases.append(
            Case(
                case_id=f"{label['mutation']}::{record['scenario_id']}",
                evidence=prompts.build_case(record),
                label_fault=record["fault"],
                label_kind=(record.get("kinds") or ["unknown"])[0],
                money_was_missing=True,
                expected_delta=dict(label["expected_balances_delta"]),
                observed_delta=dict(label["observed_balances_delta"]),
                repairable=bool(label["repairable_by_compensating_transaction"]),
                stratum=label["mutation"],
            )
        )
    return cases


def apply_repair(observed: dict[str, int], entries: list[Any]) -> dict[str, int]:
    """Balances after posting these entries, using the account_balances sign rule.

    A debit adds to a debit-normal account and subtracts from a credit-normal
    one; a credit does the inverse.
    """
    result = dict(observed)
    for entry in entries:
        normal = ACCOUNT_NORMALS.get(entry.account_name)
        if normal is None:
            # An account that does not exist: the database would reject this.
            result[entry.account_name] = result.get(entry.account_name, 0)
            continue
        signed = entry.amount_minor if entry.direction == normal else -entry.amount_minor
        result[entry.account_name] = result.get(entry.account_name, 0) + signed
    return {k: v for k, v in result.items() if v != 0}


def repair_restores_expected_balances(case: Case, verdict: Verdict) -> bool:
    """Does applying the proposal make the books match what the events imply?

    This — not `repair_restores_invariant` — is the metric that separates a
    good repair from a bad one on this system. Money conservation is preserved
    by EVERY balanced transaction (proved exhaustively in
    tests/unit/test_repair_algebra.py), so it is unchanged by any repair the
    database would accept: it cannot distinguish a correct repair from one that
    balances into entirely the wrong accounts. Agreement with ground truth can.
    """
    if not verdict.proposed_repair or not repair_is_balanced(verdict.proposed_repair):
        return False
    repaired = apply_repair(case.observed_delta, verdict.proposed_repair)
    expected = {k: v for k, v in case.expected_delta.items() if v != 0}
    return repaired == expected


def class_balance(cases: list[Case]) -> dict[str, int]:
    return dict(sorted(Counter(c.label_fault for c in cases).items()))


def stratified_split(
    cases: list[Case], *, test_fraction: float = 0.4, seed: int = 0
) -> tuple[list[Case], list[Case]]:
    """Split per fault class, so both halves carry every class.

    A random split would give some rare class zero test items and make its
    accuracy undefined — reported as 0 or 100 depending on which way the
    averaging fell.
    """
    rng = random.Random(seed)
    by_fault: dict[str, list[Case]] = {}
    for case in cases:
        by_fault.setdefault(case.label_fault, []).append(case)

    train: list[Case] = []
    test: list[Case] = []
    for fault in sorted(by_fault):
        items = sorted(by_fault[fault], key=lambda c: c.case_id)
        rng.shuffle(items)
        n_test = max(1, round(len(items) * test_fraction))
        test.extend(items[:n_test])
        train.extend(items[n_test:])
    return train, test


# ------------------------------------------------------------------ scoring --


@dataclass
class Scored:
    """One (case, verdict) pair, scored against ground truth."""

    case_id: str
    label_fault: str
    predicted: str | None
    ranked: list[str]
    correct_at_1: bool
    correct_at_3: bool
    confidence: float
    latency_s: float
    cost_usd: float
    proposed_repair: bool
    repair_balanced: bool
    over_repair: bool
    refused: bool
    # Repair stratum only; None on classification cases where nothing is wrong.
    repair_correct: bool | None = None
    false_repair: bool | None = None
    claimed_to_fix_the_unfixable: bool | None = None


def score(case: Case, verdict: Verdict | None, *, latency_s: float, cost_usd: float,
          refused: bool = False) -> Scored:
    if verdict is None:
        return Scored(
            case_id=case.case_id, label_fault=case.label_fault, predicted=None,
            ranked=[], correct_at_1=False, correct_at_3=False, confidence=0.0,
            latency_s=latency_s, cost_usd=cost_usd, proposed_repair=False,
            repair_balanced=False, over_repair=False, refused=refused,
        )
    ranked = [verdict.fault_class.value] + [f.value for f in verdict.alternate_fault_classes]
    proposed = bool(verdict.proposed_repair) and verdict.money_is_missing
    balanced = proposed and repair_is_balanced(verdict.proposed_repair)

    repair_correct: bool | None = None
    false_repair: bool | None = None
    unfixable_claim: bool | None = None
    if case.money_was_missing:
        repair_correct = repair_restores_expected_balances(case, verdict)
        # The dangerous failure: it balances, so the database accepts it and
        # every automated check passes, but it posts to the wrong accounts.
        false_repair = balanced and not repair_correct
        if not case.repairable:
            # No balanced transaction can close an unbalanced write's gap.
            # Proposing one anyway is a confident, checkable error.
            unfixable_claim = proposed
            repair_correct = False if proposed else None

    return Scored(
        case_id=case.case_id,
        label_fault=case.label_fault,
        predicted=verdict.fault_class.value,
        ranked=ranked,
        correct_at_1=ranked[0] == case.label_fault,
        correct_at_3=case.label_fault in ranked[:3],
        confidence=verdict.confidence,
        latency_s=latency_s,
        cost_usd=cost_usd,
        proposed_repair=proposed,
        repair_balanced=balanced,
        repair_correct=repair_correct,
        false_repair=false_repair,
        claimed_to_fix_the_unfixable=unfixable_claim,
        # The healthy-system failure mode: "fixing" books that were already right.
        over_repair=proposed and not case.money_was_missing,
        refused=refused,
    )


def aggregate(scored: list[Scored]) -> dict[str, Any]:
    """Roll per-case scores into the reported metrics."""
    if not scored:
        return {"n": 0}
    n = len(scored)
    latencies = sorted(s.latency_s for s in scored)
    answered = [s for s in scored if s.predicted is not None]
    return {
        "n": n,
        "answered": len(answered),
        "refused": sum(s.refused for s in scored),
        "accuracy@1": round(sum(s.correct_at_1 for s in scored) / n, 4),
        "accuracy@3": round(sum(s.correct_at_3 for s in scored) / n, 4),
        "over_repair_rate": round(sum(s.over_repair for s in scored) / n, 4),
        "repair_proposed": sum(s.proposed_repair for s in scored),
        "repair_balanced": sum(s.repair_balanced for s in scored),
        **_repair_metrics(scored),
        "mean_confidence": round(sum(s.confidence for s in answered) / len(answered), 4)
        if answered
        else 0.0,
        "latency_p50_s": round(median(latencies), 3),
        "latency_p95_s": round(latencies[min(len(latencies) - 1, int(0.95 * len(latencies)))], 3),
        "cost_usd_total": round(sum(s.cost_usd for s in scored), 4),
        "cost_usd_per_scenario": round(sum(s.cost_usd for s in scored) / n, 6),
        "accuracy_by_fault": {
            fault: round(
                sum(s.correct_at_1 for s in scored if s.label_fault == fault)
                / max(1, sum(1 for s in scored if s.label_fault == fault)),
                4,
            )
            for fault in sorted({s.label_fault for s in scored})
        },
        "confusion": _confusion(scored),
    }


def _repair_metrics(scored: list[Scored]) -> dict[str, Any]:
    """Repair-stratum metrics, reported only over cases where books were wrong."""
    repairable = [s for s in scored if s.repair_correct is not None or s.false_repair is not None]
    if not repairable:
        return {"repair_stratum_n": 0}
    fixable = [s for s in repairable if s.claimed_to_fix_the_unfixable is None]
    unfixable = [s for s in repairable if s.claimed_to_fix_the_unfixable is not None]
    out: dict[str, Any] = {
        "repair_stratum_n": len(repairable),
        "false_repair_rate": round(
            sum(bool(s.false_repair) for s in repairable) / len(repairable), 4
        ),
    }
    if fixable:
        out["repair_restores_expected_balances"] = round(
            sum(bool(s.repair_correct) for s in fixable) / len(fixable), 4
        )
        out["repair_fixable_n"] = len(fixable)
    if unfixable:
        # An unbalanced write cannot be undone by anything the database accepts.
        # Proposing a repair here is a confident error worth counting on its own.
        out["unfixable_n"] = len(unfixable)
        out["claimed_to_fix_the_unfixable_rate"] = round(
            sum(bool(s.claimed_to_fix_the_unfixable) for s in unfixable) / len(unfixable), 4
        )
    return out


def _confusion(scored: list[Scored]) -> dict[str, dict[str, int]]:
    """Which faults get mistaken for which — where a benchmark earns its keep."""
    matrix: dict[str, Counter] = {}
    for s in scored:
        if s.predicted is None:
            continue
        matrix.setdefault(s.label_fault, Counter())[s.predicted] += 1
    return {k: dict(sorted(v.items())) for k, v in sorted(matrix.items())}


# --------------------------------------------------------------------- cost --


def call_cost(model: str, usage: dict[str, int], *, batch: bool = False) -> float:
    """USD for one call, from its reported usage."""
    in_rate, out_rate = PRICING[model]
    uncached = usage.get("input_tokens", 0) * in_rate
    cache_read = usage.get("cache_read_input_tokens", 0) * in_rate * CACHE_READ_MULTIPLIER
    cache_write = (
        usage.get("cache_creation_input_tokens", 0) * in_rate * CACHE_WRITE_MULTIPLIER
    )
    output = usage.get("output_tokens", 0) * out_rate
    total = (uncached + cache_read + cache_write + output) / 1_000_000
    return total * (BATCH_MULTIPLIER if batch else 1.0)


@dataclass
class BudgetGuard:
    """Hard spend cap. Checked BEFORE each call, not after the damage."""

    cap_usd: float
    spent_usd: float = 0.0
    calls: int = 0
    log_path: Path = field(default=USAGE_LOG)

    def check(self, projected_usd: float = 0.0) -> None:
        if self.spent_usd + projected_usd > self.cap_usd:
            raise BudgetExceeded(
                f"cap ${self.cap_usd:.2f} would be exceeded "
                f"(spent ${self.spent_usd:.4f}, next call ~${projected_usd:.4f})"
            )

    def record(self, model: str, usage: dict[str, int], *, batch: bool, **extra: Any) -> float:
        cost = call_cost(model, usage, batch=batch)
        self.spent_usd += cost
        self.calls += 1
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "model": model,
                        "batch": batch,
                        "usage": usage,
                        "cost_usd": round(cost, 6),
                        "running_total_usd": round(self.spent_usd, 6),
                        **extra,
                    }
                )
                + "\n"
            )
        return cost


class BudgetExceeded(RuntimeError):
    """Raised instead of spending past the cap."""
