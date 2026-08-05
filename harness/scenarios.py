"""Labeled scenario generator (Phase 2/5).

Contract: produces (fault, params, event fixture) combinations with a recorded
label. Because the harness CAUSED every break, it knows the exact fault class,
the events involved, and the repair that restores the invariant — free, exact
ground truth that eval work usually pays humans for. Scenario ids and labels
are written only to the artifacts log; the Phase 5 agent must never see them.

Two properties this module owes the rest of the harness:

- FRESH IDS. Every scenario draws its fixtures from one monotonic seq counter,
  so no two scenarios in a run can collide on a dedupe key. A collision would
  make a later scenario's "duplicate" answer a fact about an earlier scenario,
  which is indistinguishable from the bug the harness is hunting.
- RECORDED SKIPS. Some fault x kind combinations cannot fail: with a non-money
  event there is no transaction for a double-post or a lost post to show up in,
  so the scenario would pass no matter how broken the system is. Those combos
  are skipped and REPORTED (`ScenarioSet.skipped`, `skipped_combos()`), never
  silently dropped — silent truncation reads as "covered everything" (§1.8).
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from harness.events import ALL_KINDS, EventFixture, make
from harness.faults import ALL_FAULTS, Expectation, Fault, plan

# Labels are built from a plan, and a plan's Expectation does not depend on the
# signing secret — only its (unused here) signature bytes do. The runner
# re-plans every scenario with the stack's real secret before anything reaches
# the wire, so this placeholder never signs a delivery.
_LABEL_SECRET = "whsec_" + "0" * 32

DEFAULT_FAULTS: tuple[Fault, ...] = (Fault.NONE, *ALL_FAULTS)

# Stripe's US card fee (2.9% + 30c) and dispute fee ($15.00), in minor units.
_DISPUTE_FEE = 1500
_MIN_AMOUNT = 100  # keeps net = amount - fee positive, as every real charge is
_MAX_AMOUNT = 100_000

# Faults whose ground truth is "exactly one transaction". Paired with an event
# that moves no money there is no transaction to double, lose, or half-write,
# so the scenario cannot fail and its green result would be a lie.
_NEEDS_MONEY: dict[Fault, str] = {
    Fault.DUPLICATE: "dedupe is measured as exactly one transaction; a non-money event posts none",
    Fault.DUPLICATE_OBJECT: (
        "the second dedupe key is measured as exactly one transaction; a non-money event posts none"
    ),
    Fault.CONCURRENT_DUPLICATE: (
        "the race is over a ledger write that a non-money event never makes"
    ),
    Fault.REORDER: "order independence is measured by money posting regardless of arrival order",
    Fault.RESPOND_500: "retry safety is measured as exactly one transaction",
    Fault.PARTIAL_WRITE: "there is no ledger transaction to kill mid-write",
    Fault.DROP: (
        "a dropped non-money event is CORRECTLY invisible to the reconciler (no transaction was "
        "ever due), so the fault's expectation that a break is reported would be false"
    ),
}

# REORDER is the only fault whose meaning needs more than one event.
# DUPLICATE_OBJECT mints its own twin envelope inside plan().
_FIXTURE_COUNT: dict[Fault, int] = {Fault.REORDER: 2}


@dataclass(frozen=True)
class Scenario:
    """One fault injected into one set of events, plus its ground-truth label."""

    scenario_id: str
    fault: Fault
    params: dict
    fixtures: tuple[EventFixture, ...]
    label: dict

    @property
    def event_ids(self) -> tuple[str, ...]:
        return tuple(fx.event_id for fx in self.fixtures)


@dataclass(frozen=True)
class Skip:
    """A fault x kind combination deliberately not generated, and why."""

    fault: Fault
    kind: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"fault": self.fault.value, "kind": self.kind, "reason": self.reason}


class ScenarioSet(list[Scenario]):
    """Generated scenarios that also carry what was deliberately NOT generated."""

    def __init__(self, scenarios: Iterable[Scenario] = (), skipped: Sequence[Skip] = ()) -> None:
        super().__init__(scenarios)
        self.skipped: tuple[Skip, ...] = tuple(skipped)


def skip_reason(fault: Fault, kind: str) -> str | None:
    """Why (fault, kind) is not worth generating, or None if it is."""
    if kind == "invoice_payment_failed":
        return _NEEDS_MONEY.get(fault)
    return None


def skipped_combos(
    *, faults: Sequence[Fault] | None = None, kinds: Sequence[str] | None = None
) -> tuple[Skip, ...]:
    """Every combination `generate()` would skip, without generating anything."""
    return tuple(
        Skip(fault, kind, reason)
        for fault in (DEFAULT_FAULTS if faults is None else faults)
        for kind in (ALL_KINDS if kinds is None else kinds)
        if (reason := skip_reason(fault, kind)) is not None
    )


def _fee(amount: int) -> int:
    """Stripe's 2.9% + 30c card fee, in minor units."""
    return 30 + (amount * 29) // 1000


def _fixture(kind: str, seq: int, rng: random.Random) -> EventFixture:
    """One fixture of `kind` with seeded amounts and ids derived from `seq`."""
    amount = rng.randrange(_MIN_AMOUNT, _MAX_AMOUNT)
    if kind == "charge_succeeded":
        return make(kind, seq=seq, amount=amount, fee=_fee(amount))
    if kind == "charge_refunded":
        return make(kind, seq=seq, amount=amount, refunded=rng.randrange(1, amount + 1))
    if kind == "dispute_created":
        return make(kind, seq=seq, amount=amount, fee=_DISPUTE_FEE)
    return make(kind, seq=seq, amount=amount)


def _params(fault: Fault, index: int) -> dict:
    """Per-repeat parameter variation. `index` is the per_combo repeat number."""
    if fault is Fault.DUPLICATE:
        return {"n": 2 + index % 3}
    if fault is Fault.CONCURRENT_DUPLICATE:
        # The widest fan-out first: the race finder is most likely to find a
        # race at the largest n, and per_combo=1 runs must get that one.
        return {"n": 8 if index % 2 == 0 else 4}
    if fault is Fault.DELAY:
        # Repeat 0 is a benign in-tolerance delay; odd repeats land outside the
        # tolerance window and must be refused.
        return {"seconds": 0.25, "past_tolerance": index % 2 == 1}
    if fault is Fault.STALE_TIMESTAMP:
        return {"age_s": 600 + 300 * (index % 2)}
    if fault is Fault.PARTIAL_WRITE:
        # Repeat 0 is the documented default; the tighter values sweep the kill
        # toward the worker's write window. Whether any of them actually landed
        # inside it is measured per scenario, not assumed — see the runner's
        # `killed_with_task_in_flight`.
        return {"kill_after_s": (0.15, 0.10, 0.05)[index % 3]}
    if fault is Fault.SLOW_LORIS:
        # 2.0s rather than the 3.0s default: long enough that ingest must serve
        # other requests mid-body, short enough to keep a full sweep bounded.
        return {"hold_s": 2.0}
    return {}


def _label(
    fault: Fault, params: dict, fixtures: Sequence[EventFixture], exp: Expectation
) -> dict[str, Any]:
    """The ground truth for one scenario — Phase 5's free label."""
    return {
        "fault": fault.value,
        "params": params,
        "event_ids": [fx.event_id for fx in fixtures],
        "posted_event_ids": sorted(exp.posted_event_ids),
        "rejected_event_ids": sorted(exp.rejected_event_ids),
        "balances_delta": dict(exp.balances_delta),
        "kinds": [fx.kind for fx in fixtures],
        "reconciler_should_report_break": exp.reconciler_should_report_break,
        "notes": exp.notes,
    }


def generate(
    *,
    seed: int = 0,
    faults: Sequence[Fault] | None = None,
    kinds: Sequence[str] | None = None,
    per_combo: int = 1,
    start_seq: int = 0,
) -> ScenarioSet:
    """Cross faults x event kinds x per_combo into labeled scenarios.

    Deterministic given `seed`: the same seed produces the same ids, the same
    amounts, and the same labels, so a run is reproducible from its summary
    alone. Every fixture draws a fresh `seq`, so no two scenarios in a set can
    collide on a dedupe key.
    """
    if per_combo < 1:
        raise ValueError(f"per_combo must be >= 1, got {per_combo}")
    chosen_faults = tuple(Fault(f) for f in (DEFAULT_FAULTS if faults is None else faults))
    chosen_kinds = tuple(ALL_KINDS if kinds is None else kinds)
    unknown = [k for k in chosen_kinds if k not in ALL_KINDS]
    if unknown:
        raise ValueError(f"unknown event kinds: {unknown}; expected one of {ALL_KINDS}")

    rng = random.Random(seed)
    seq = start_seq
    scenarios: list[Scenario] = []
    skipped: list[Skip] = []

    for fault in chosen_faults:
        for kind in chosen_kinds:
            reason = skip_reason(fault, kind)
            if reason is not None:
                skipped.append(Skip(fault, kind, reason))
                continue
            count = _FIXTURE_COUNT.get(fault, 1)
            for index in range(per_combo):
                fixtures = tuple(_fixture(kind, seq + offset, rng) for offset in range(count))
                seq += count
                built = plan(fault, fixtures, _LABEL_SECRET, params=_params(fault, index))
                scenarios.append(
                    Scenario(
                        scenario_id=f"{fault.value.lower()}-{kind}-{index:04d}",
                        fault=fault,
                        # The EFFECTIVE params (defaults merged with the
                        # variation, plus whatever plan() derived, e.g. the
                        # twin event id) — what was actually injected.
                        params=built.params,
                        fixtures=fixtures,
                        label=_label(fault, built.params, fixtures, built.expectation),
                    )
                )

    return ScenarioSet(scenarios, skipped)
