"""Structured verdict schema for the triage agent (Phase 5).

The agent returns DATA, never an action. A proposed repair is a compensating
transaction described as entries — the same shape `ledger/post.py` accepts —
and nothing in this module can apply one. Corrections to an append-only ledger
are new balancing transactions, never mutations, so a "repair" that edits
history is not expressible here by construction.

Used with `client.messages.parse(..., output_format=Verdict)`, so the model is
constrained to this schema rather than asked to emit JSON and hand-parsed.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class FaultClass(str, Enum):
    """What the delivery path experienced, from the ledger's point of view.

    These are the harness's fault names plus NONE. The agent is scored on
    naming the right one from evidence alone — it never sees the label.
    """

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


class RepairEntry(BaseModel):
    """One leg of a proposed compensating transaction."""

    account_name: str = Field(
        description="One of: stripe_balance, bank, processing_fees, dispute_losses, "
        "refunds_contra, revenue, customer_liability"
    )
    direction: str = Field(description="Either 'debit' or 'credit'")
    amount_minor: int = Field(gt=0, description="Positive integer, minor units (cents)")
    currency: str = Field(default="USD", description="ISO code, e.g. USD")


class Verdict(BaseModel):
    """The agent's complete answer for one scenario."""

    fault_class: FaultClass = Field(
        description="Most likely fault, judged from the evidence provided"
    )
    alternate_fault_classes: list[FaultClass] = Field(
        default_factory=list,
        max_length=2,
        description="Up to two next-most-likely faults, most likely first. Used for "
        "accuracy@3 — leave empty only if no other fault is plausible.",
    )
    confidence: float = Field(
        ge=0.0, le=1.0, description="Calibrated probability that fault_class is correct"
    )
    root_cause: str = Field(
        min_length=1,
        max_length=2000,
        description="Prose diagnosis: what the evidence shows and why it implies this "
        "fault. Cite specific event ids, statuses, and balance figures.",
    )
    affected_accounts: list[str] = Field(
        default_factory=list, description="Ledger accounts whose balances are wrong, if any"
    )
    money_is_missing: bool = Field(
        description="True only if the ledger is materially wrong and needs a correction. "
        "A correctly-refused delivery moves no money and needs no repair."
    )
    proposed_repair: list[RepairEntry] = Field(
        default_factory=list,
        description="A balanced compensating transaction that would correct the ledger. "
        "Empty when money_is_missing is false. Sum of debits must equal sum of credits "
        "per currency; the database rejects anything else.",
    )
    repair_memo: str = Field(
        default="", max_length=500, description="One line explaining what the repair corrects"
    )


def repair_is_balanced(entries: list[RepairEntry]) -> bool:
    """Would the database accept this repair? Checked before it is ever shown as applicable.

    Mirrors the deferred constraint trigger: per currency, debits must equal
    credits, and a transaction needs at least two entries.
    """
    if len(entries) < 2:
        return False
    per_currency: dict[str, int] = {}
    for entry in entries:
        if entry.direction not in ("debit", "credit"):
            return False
        sign = 1 if entry.direction == "debit" else -1
        per_currency[entry.currency] = per_currency.get(entry.currency, 0) + sign * entry.amount_minor
    return all(net == 0 for net in per_currency.values())
