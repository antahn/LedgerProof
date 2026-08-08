"""Produce the G4 design report: class balance, split, token sizes, cost projection.

Spends nothing on inference. `count_tokens` is free, so the projection below is
measured against the real rendered prompts rather than estimated from guesses
about their size.

    uv run python scripts/bench_design_report.py artifacts/chaos_<ts>.jsonl
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import anthropic

from ledgerproof.config import get_settings
from ledgerproof.triage import prompts
from ledgerproof.triage.agent import EFFORT_LEVELS, FRONTIER_MODELS, HAIKU, request_params
from ledgerproof.triage.bench import (
    BATCH_MULTIPLIER,
    CACHE_READ_MULTIPLIER,
    MIN_CACHEABLE_PREFIX,
    PRICING,
    class_balance,
    load_cases,
    stratified_split,
)

OUT = Path(__file__).resolve().parent.parent / "artifacts" / "bench_design.json"

# Measured from the pilot in a first run; used only to project OUTPUT cost,
# which count_tokens cannot tell us in advance.
ASSUMED_OUTPUT_TOKENS = 1200


def main() -> None:
    artifact = Path(sys.argv[1])
    client = anthropic.Anthropic(api_key=get_settings().anthropic_api_key)

    cases = load_cases(artifact)
    train, test = stratified_split(cases, test_fraction=0.4, seed=0)

    # --- measure the real prompts ------------------------------------------
    sample = test[0]
    prefix_only = client.messages.count_tokens(
        model="claude-opus-5",
        system=prompts.system_prefix(),
        messages=[{"role": "user", "content": "x"}],
    ).input_tokens

    per_case = []
    for case in test[:25]:
        params = request_params("claude-opus-5", case.as_prompt_case())
        per_case.append(
            client.messages.count_tokens(
                model="claude-opus-5", system=params["system"], messages=params["messages"]
            ).input_tokens
        )
    mean_total = sum(per_case) / len(per_case)
    mean_case_only = mean_total - prefix_only

    # --- project cost ------------------------------------------------------
    projection = {}
    for model in FRONTIER_MODELS:
        in_rate, out_rate = PRICING[model]
        cached = prefix_only * in_rate * CACHE_READ_MULTIPLIER
        fresh = mean_case_only * in_rate
        out = ASSUMED_OUTPUT_TOKENS * out_rate
        per_scenario = (cached + fresh + out) / 1_000_000 * BATCH_MULTIPLIER
        projection[model] = {
            "usd_per_scenario_batched": round(per_scenario, 6),
            "usd_for_test_split": round(per_scenario * len(test), 4),
            "prefix_caches": prefix_only >= MIN_CACHEABLE_PREFIX[model],
            "min_cacheable_prefix": MIN_CACHEABLE_PREFIX[model],
        }

    frontier_total = sum(p["usd_for_test_split"] for p in projection.values())
    effort_model = "claude-sonnet-5"
    effort_total = projection[effort_model]["usd_for_test_split"] * len(EFFORT_LEVELS)

    report = {
        "artifact": artifact.name,
        "labeled_set": {
            "total_cases": len(cases),
            "class_balance": class_balance(cases),
            "distinct_classes": len(class_balance(cases)),
        },
        "split": {
            "test_fraction": 0.4,
            "train_n": len(train),
            "test_n": len(test),
            "train_balance": class_balance(train),
            "test_balance": class_balance(test),
            "every_class_in_test": set(class_balance(cases)) == set(class_balance(test)),
        },
        "prompt_tokens": {
            "cached_system_prefix": prefix_only,
            "mean_case_evidence": round(mean_case_only, 1),
            "mean_total_input": round(mean_total, 1),
            "min_cacheable_prefix_by_model": MIN_CACHEABLE_PREFIX,
            "prefix_clears_every_model": all(
                prefix_only >= v for v in MIN_CACHEABLE_PREFIX.values()
            ),
        },
        "cost_projection_usd": {
            "assumptions": {
                "output_tokens_per_call": ASSUMED_OUTPUT_TOKENS,
                "batch_discount": BATCH_MULTIPLIER,
                "cache_read_multiplier": CACHE_READ_MULTIPLIER,
                "note": "list prices; Sonnet 5 has a lower introductory rate through 2026-08-31",
            },
            "per_model": projection,
            "frontier_sweep_total": round(frontier_total, 2),
            "effort_sweep_total": round(effort_total, 2),
            "grand_total": round(frontier_total + effort_total, 2),
            "pilot_30_cases": round(
                sum(p["usd_per_scenario_batched"] for p in projection.values()) * 30, 2
            ),
        },
        "model_capabilities_measured": {
            m: {
                "effort_supported": m != HAIKU,
                "thinking": "adaptive" if m != HAIKU else "enabled+budget_tokens",
            }
            for m in FRONTIER_MODELS
        },
        "sample_rendered_prompt": prompts.render_case(sample.as_prompt_case()),
        "sample_hidden_label": sample.label_fault,
    }

    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "sample_rendered_prompt"}, indent=2))


if __name__ == "__main__":
    main()
