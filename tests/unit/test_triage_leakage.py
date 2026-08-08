"""The agent must not be able to read the answer off its own prompt.

Leakage is the standard way a self-labelled benchmark silently becomes
meaningless: the harness knows the fault, the harness builds the prompt, and one
careless field turns "diagnose this" into "repeat this back". These tests assert
over the FULLY RENDERED request — system prefix plus user turn plus the schema
the model is constrained to — because that is what the model actually sees.
"""

from __future__ import annotations

import json

import pytest

from ledgerproof.triage import agent, prompts
from ledgerproof.triage.schema import Verdict

# A record shaped exactly like the runner's JSONL output, with distinctive
# sentinel values in every field the agent must never see.
LEAKY_RECORD = {
    "scenario_id": "SENTINEL-SCENARIO-ID-9f3a",
    "fault": "CONCURRENT_DUPLICATE",
    "params": {"n": 8, "sentinel_param": "SENTINEL-PARAM-7b21"},
    "kinds": ["charge_succeeded"],
    "events": ["evt_h000042"],
    "invariant_before": True,
    "invariant_after": True,
    "ledger_diff": {"stripe_balance": 941, "processing_fees": 59, "revenue": 1000},
    "deliveries": [
        {"event_id": "evt_h000042", "status_code": 200, "body": '{"status":"queued"}',
         "error": None, "duration_ms": 64},
        {"event_id": "evt_h000042", "status_code": 200, "body": '{"status":"duplicate"}',
         "error": None, "duration_ms": 71},
    ],
    "event_log": [
        {"id": "evt_h000042", "event_type": "charge.succeeded",
         "object_id": "ch_h000042", "status": "processed"}
    ],
    "expected": {"posted": ["evt_h000042"], "rejected": [],
                 "balances_delta": {"revenue": 1000}},
    "actual": {"posted": ["evt_h000042"], "transactions": 1},
    "breaks": [{"kind": "SENTINEL-BREAK-KIND", "detail": "SENTINEL-BREAK-DETAIL"}],
    "quiescent": True,
    "recon": {"reported": False, "checked": 1},
    "label": {"fault": "CONCURRENT_DUPLICATE", "sentinel_label": "SENTINEL-LABEL-4c88"},
    "killed_with_task_in_flight": False,
    "worker_restarted": False,
    "ledger_reset": False,
}

# Values that must not appear anywhere in the rendered prompt.
SENTINELS = (
    "SENTINEL-SCENARIO-ID-9f3a",
    "SENTINEL-PARAM-7b21",
    "SENTINEL-LABEL-4c88",
    "SENTINEL-BREAK-KIND",
    "SENTINEL-BREAK-DETAIL",
)


def rendered_request(record: dict) -> str:
    """Everything the model receives, as one searchable string."""
    case = prompts.build_case(record)
    params = agent.request_params(agent.OPUS, case)
    return json.dumps(
        {
            "system": params["system"],
            "messages": params["messages"],
            "schema": Verdict.model_json_schema(),
        },
        sort_keys=True,
    )


def test_no_sentinel_value_reaches_the_prompt() -> None:
    prompt = rendered_request(LEAKY_RECORD)
    for sentinel in SENTINELS:
        assert sentinel not in prompt, f"{sentinel} leaked into the prompt"


def test_the_fault_name_never_appears_as_an_answer() -> None:
    """The taxonomy names every fault — that is not leakage; naming THIS one is.

    The injected fault here is CONCURRENT_DUPLICATE. It legitimately appears in
    the taxonomy table (the agent must know its options) and in the response
    schema's enum. What must not appear is the fault attached to this case's
    evidence, so the check is on the user turn, where the evidence lives.
    """
    case = prompts.build_case(LEAKY_RECORD)
    user_turn = prompts.render_case(case)
    assert "CONCURRENT_DUPLICATE" not in user_turn
    assert LEAKY_RECORD["fault"] not in user_turn
    # ...while the system prefix does list it as one option among all of them.
    taxonomy = "".join(block["text"] for block in prompts.system_prefix())
    for fault in ("DUPLICATE", "DROP", "TAMPER_BODY", "CONCURRENT_DUPLICATE"):
        assert fault in taxonomy, "the agent must be told what its options are"


@pytest.mark.parametrize("forbidden", prompts.FORBIDDEN_FIELDS)
def test_every_forbidden_field_is_withheld(forbidden: str) -> None:
    """Each named field is excluded — enumerated so adding one is a decision."""
    case = prompts.build_case(LEAKY_RECORD)
    assert forbidden not in case, f"{forbidden} survived into the case"


def test_case_building_is_an_allowlist_not_a_denylist() -> None:
    """A field the runner adds tomorrow must not leak by default.

    This is the structural guarantee: build_case copies named fields out,
    rather than copying the record in and deleting known-bad keys. A denylist
    would silently pass through anything nobody remembered to add to it.
    """
    record = dict(LEAKY_RECORD)
    record["a_field_invented_after_this_test_was_written"] = "SENTINEL-FUTURE-FIELD"
    record["label_v2"] = {"fault": "SENTINEL-FUTURE-LABEL"}

    case = prompts.build_case(record)
    assert set(case) <= set(prompts.OBSERVABLE_FIELDS) | {"invariant_holds"}

    prompt = rendered_request(record)
    assert "SENTINEL-FUTURE-FIELD" not in prompt
    assert "SENTINEL-FUTURE-LABEL" not in prompt


def test_evidence_actually_survives() -> None:
    """The mirror image: a prompt that leaks nothing but shows nothing is useless."""
    case = prompts.build_case(LEAKY_RECORD)
    user_turn = prompts.render_case(case)
    assert "evt_h000042" in user_turn
    assert "HTTP 200" in user_turn
    assert "duplicate" in user_turn  # the response body is real evidence
    assert "revenue: +1000" in user_turn
    assert "processed" in user_turn


def test_dropped_event_renders_as_absence_not_emptiness() -> None:
    """A DROP case has no deliveries and no rows — the prompt must say so."""
    record = dict(LEAKY_RECORD, deliveries=[], event_log=[], ledger_diff={})
    user_turn = prompts.render_case(prompts.build_case(record))
    assert "no HTTP request reached ingest" in user_turn
    assert "ingest recorded no row" in user_turn
    assert "no balances changed" in user_turn


def test_system_prefix_is_byte_identical_across_calls() -> None:
    """Caching is a prefix match: one varying byte and nothing ever caches."""
    first = json.dumps(prompts.system_prefix(), sort_keys=True)
    second = json.dumps(prompts.system_prefix(), sort_keys=True)
    assert first == second

    # And it must not contain anything scenario- or run-specific.
    for volatile in ("evt_h", "scenario", "claude-", "2026-", "SENTINEL"):
        assert volatile not in first, f"volatile value {volatile!r} in the cached prefix"


def test_cache_breakpoint_is_on_the_last_system_block_only() -> None:
    blocks = prompts.system_prefix()
    assert "cache_control" in blocks[-1]
    assert all("cache_control" not in b for b in blocks[:-1])
