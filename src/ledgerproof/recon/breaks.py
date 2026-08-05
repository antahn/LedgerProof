"""Break taxonomy and records.

Contract: typed representation of every reconciliation discrepancy —
missing_in_ledger, missing_in_stripe, amount_mismatch, fee_mismatch — with the
Stripe object ids and ledger transaction ids needed to reproduce. Serializes
to the JSONL shape harness runs record in artifacts/.

A Break is an observation, never an instruction: nothing here repairs anything.
Repairs are new compensating transactions and only ever pass through the human
gate.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any


class BreakKind(str, enum.Enum):
    MISSING_IN_LEDGER = "missing_in_ledger"
    MISSING_IN_SOURCE = "missing_in_source"
    AMOUNT_MISMATCH = "amount_mismatch"
    FEE_MISMATCH = "fee_mismatch"
    INVARIANT_VIOLATION = "invariant_violation"


@dataclass(frozen=True)
class Break:
    """One discrepancy between a source of truth and the ledger.

    expected_minor is the source's number, actual_minor the ledger's; either
    may be None when the concept does not apply (nothing expected, nothing
    posted). Amounts are signed minor units, in the same sign convention as
    account_balances: positive means the account moved in its normal direction.
    """

    kind: BreakKind
    object_id: str | None = None
    event_id: str | None = None
    expected_minor: int | None = None
    actual_minor: int | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "object_id": self.object_id,
            "event_id": self.event_id,
            "expected_minor": self.expected_minor,
            "actual_minor": self.actual_minor,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ReconResult:
    """The outcome of one reconciliation pass.

    checked counts source records actually compared, so a run that found
    nothing to compare reports checked == 0 rather than a green ok on an empty
    diff — silence and agreement must stay distinguishable.
    """

    breaks: tuple[Break, ...]
    checked: int
    invariant_ok: bool

    @property
    def ok(self) -> bool:
        return not self.breaks and self.invariant_ok

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked": self.checked,
            "invariant_ok": self.invariant_ok,
            "breaks": [b.as_dict() for b in self.breaks],
        }
