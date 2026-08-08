"""LLM triage agent (Phase 5) — proposes, never applies.

The agent reads evidence and returns a `Verdict`. It has no database handle, no
write path, and no tools. Applying a repair requires a human to call
`approve_and_apply` with `approved_by` set — there is no code path from a model
response to a ledger write that does not pass through that function, and the
function refuses anything the database itself would refuse.

**The three models need three different request shapes** (measured against the
Models API, not assumed):

| Model | `effort` | thinking |
|---|---|---|
| `claude-opus-5` | all five levels | adaptive only |
| `claude-sonnet-5` | all five levels | adaptive only |
| `claude-haiku-4-5` | **unsupported** | `enabled` + `budget_tokens` only |

Sending `output_config.effort` or `thinking={"type": "adaptive"}` to Haiku 4.5
returns a 400, and sending `budget_tokens` to Opus 5 or Sonnet 5 does too. The
effort sweep therefore runs on a model that has effort at all.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import anthropic

from ledgerproof.ledger.post import Entry, LedgerTransaction, PostOutcome, post_transaction
from ledgerproof.triage import prompts
from ledgerproof.triage.schema import Verdict, repair_is_balanced

OPUS = "claude-opus-5"
SONNET = "claude-sonnet-5"
HAIKU = "claude-haiku-4-5"
FRONTIER_MODELS = (OPUS, SONNET, HAIKU)

# Models that accept output_config.effort. Haiku 4.5 does not; the effort sweep
# is meaningful only where the parameter exists.
EFFORT_MODELS = (OPUS, SONNET)
EFFORT_LEVELS = ("low", "medium", "high")

# max_tokens caps thinking PLUS response text on Opus 5, so this is sized with
# headroom rather than around the expected verdict length.
MAX_TOKENS = 8000
HAIKU_THINKING_BUDGET = 2000


class RepairRejected(Exception):
    """A proposed repair was refused before it could reach the ledger."""


@dataclass
class TriageResult:
    """One model call: the verdict plus everything the benchmark measures."""

    verdict: Verdict | None
    model: str
    effort: str | None
    latency_s: float
    usage: dict[str, int]
    stop_reason: str | None
    refused: bool = False
    error: str | None = None
    raw_text: str = ""

    @property
    def cache_hit(self) -> bool:
        return self.usage.get("cache_read_input_tokens", 0) > 0


def request_params(
    model: str, case: dict[str, Any], *, effort: str | None = None
) -> dict[str, Any]:
    """Build the per-model request body. Shared by the live and batch paths.

    Kept in one place so a sweep and a pilot cannot drift apart in request
    shape — which would make their measured costs incomparable.
    """
    params: dict[str, Any] = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": prompts.system_prefix(),
        "messages": prompts.build_messages(case),
    }
    if model in EFFORT_MODELS:
        params["thinking"] = {"type": "adaptive"}
        if effort is not None:
            params["output_config"] = {"effort": effort}
    else:
        # Haiku 4.5: no adaptive, no effort. budget_tokens must be < max_tokens.
        params["thinking"] = {"type": "enabled", "budget_tokens": HAIKU_THINKING_BUDGET}
    return params


def _usage_dict(usage: Any) -> dict[str, int]:
    if usage is None:
        return {}
    return {
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
    }


def triage(
    client: anthropic.Anthropic,
    case: dict[str, Any],
    *,
    model: str = OPUS,
    effort: str | None = None,
) -> TriageResult:
    """Diagnose one case. Never writes anything, anywhere."""
    import time

    params = request_params(model, case, effort=effort)
    started = time.perf_counter()
    try:
        response = client.messages.parse(output_format=Verdict, **params)
    except anthropic.APIError as exc:
        return TriageResult(
            verdict=None,
            model=model,
            effort=effort,
            latency_s=time.perf_counter() - started,
            usage={},
            stop_reason=None,
            error=str(exc),
        )
    latency = time.perf_counter() - started

    # A refusal is an HTTP 200 with empty or partial content: reading
    # content[0].text without this check raises IndexError, which would look
    # like a bug in the harness rather than a model outcome.
    if response.stop_reason == "refusal":
        return TriageResult(
            verdict=None,
            model=model,
            effort=effort,
            latency_s=latency,
            usage=_usage_dict(response.usage),
            stop_reason=response.stop_reason,
            refused=True,
        )

    return TriageResult(
        verdict=response.parsed_output,
        model=model,
        effort=effort,
        latency_s=latency,
        usage=_usage_dict(response.usage),
        stop_reason=response.stop_reason,
        raw_text="".join(b.text for b in response.content if b.type == "text"),
    )


# ---------------------------------------------------------------- the gate --


@dataclass
class RepairApproval:
    """Evidence that a human decided to apply a specific proposal."""

    approved_by: str
    scenario_id: str
    reason: str = ""
    approved_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


def approve_and_apply(
    db_url: str,
    verdict: Verdict,
    approval: RepairApproval,
    *,
    occurred_at: datetime | None = None,
) -> PostOutcome:
    """The ONLY path from a model proposal to a ledger write.

    Refuses before touching the database when: no human is named, the verdict
    claims no repair is needed, or the entries would not balance. The database
    would reject an unbalanced transaction anyway — this refuses earlier so the
    reviewer sees why, rather than a trigger error.
    """
    if not approval.approved_by.strip():
        raise RepairRejected(
            "no human approver recorded: a repair may only be applied by a named person"
        )
    if not verdict.money_is_missing:
        raise RepairRejected(
            "the verdict states no money is missing; there is nothing to repair"
        )
    if not verdict.proposed_repair:
        raise RepairRejected("the verdict proposes no entries")
    if not repair_is_balanced(verdict.proposed_repair):
        raise RepairRejected(
            "proposed repair does not balance per currency, or has fewer than two "
            "entries; the ledger's deferred constraint would reject it"
        )

    txn = LedgerTransaction(
        id=uuid.uuid4(),
        occurred_at=occurred_at or datetime.now(UTC),
        entries=tuple(
            Entry(
                account_name=e.account_name,
                dir=e.direction,  # type: ignore[arg-type]
                amount_minor=e.amount_minor,
                currency=e.currency,
            )
            for e in verdict.proposed_repair
        ),
        memo=(
            f"compensating transaction approved by {approval.approved_by} "
            f"for {approval.scenario_id}: {verdict.repair_memo}"
        )[:500],
    )
    return post_transaction(db_url, txn)
