"""Phase 5 full sweep: the model frontier and an effort sweep, via the Batch API.

Batched for the 50% discount. Results arrive in ANY order, so everything is
keyed by `custom_id` — never by position, which is the classic way a batch
benchmark silently mis-attributes every answer.

Cost is projected before submitting and checked against the cap; the cap is a
hard stop, not a warning.

    uv run python scripts/run_sweep.py --cap 20
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

from ledgerproof.config import get_settings
from ledgerproof.triage import batch as batchlib
from ledgerproof.triage import bench
from ledgerproof.triage.agent import EFFORT_LEVELS, FRONTIER_MODELS, SONNET, request_params
from ledgerproof.triage.schema import Verdict

REPO = Path(__file__).resolve().parent.parent
ARTIFACTS = REPO / "artifacts"
POLL_SECONDS = 20
MAX_WAIT_S = 3 * 3600


def build_request(case: bench.Case, model: str, effort: str | None) -> Request:
    params = request_params(model, case.as_prompt_case(), effort=effort)
    params["output_config"] = {
        **params.get("output_config", {}),
        **batchlib.output_config(Verdict),
    }
    tag = f"{model}|{effort or 'default'}|{case.case_id}"
    return Request(custom_id=tag[:64], params=MessageCreateParamsNonStreaming(**params))


def submit_and_wait(client: anthropic.Anthropic, requests: list[Request], label: str):
    created = client.messages.batches.create(requests=requests)
    print(f"  {label}: batch {created.id} submitted ({len(requests)} requests)")
    deadline = time.monotonic() + MAX_WAIT_S
    while True:
        status = client.messages.batches.retrieve(created.id)
        if status.processing_status == "ended":
            counts = status.request_counts
            print(f"  {label}: ended — ok={counts.succeeded} err={counts.errored} "
                  f"expired={counts.expired} canceled={counts.canceled}")
            return created.id
        if time.monotonic() > deadline:
            raise SystemExit(f"{label}: batch {created.id} still {status.processing_status}")
        time.sleep(POLL_SECONDS)


def collect(client, batch_id: str, by_tag: dict[str, bench.Case], guard: bench.BudgetGuard,
            fh) -> dict[tuple[str, str], list[bench.Scored]]:
    grouped: dict[tuple[str, str], list[bench.Scored]] = {}
    for result in client.messages.batches.results(batch_id):
        # Keyed by custom_id: batch results arrive in arbitrary order.
        tag = result.custom_id
        case = by_tag.get(tag)
        if case is None:
            continue
        model, effort, _ = tag.split("|", 2)
        if result.result.type != "succeeded":
            continue
        message = result.result.message
        usage = {
            "input_tokens": message.usage.input_tokens,
            "output_tokens": message.usage.output_tokens,
            "cache_read_input_tokens": getattr(message.usage, "cache_read_input_tokens", 0) or 0,
            "cache_creation_input_tokens": getattr(
                message.usage, "cache_creation_input_tokens", 0) or 0,
        }
        cost = guard.record(model, usage, batch=True, case_id=case.case_id,
                            stage="sweep", effort=effort)

        refused = message.stop_reason == "refusal"
        verdict = None
        parse_error = None
        if not refused:
            text = "".join(b.text for b in message.content if b.type == "text")
            verdict, parse_error = batchlib.parse_verdict(text, Verdict)

        scored = bench.score(case, verdict, latency_s=0.0, cost_usd=cost, refused=refused)
        grouped.setdefault((model, effort), []).append(scored)
        fh.write(json.dumps({
            "model": model, "effort": effort, "case_id": case.case_id,
            "stratum": case.stratum, "label_fault": case.label_fault,
            "predicted": scored.predicted, "ranked": scored.ranked,
            "correct@1": scored.correct_at_1, "correct@3": scored.correct_at_3,
            "confidence": scored.confidence, "cost_usd": round(cost, 6),
            "repair_correct": scored.repair_correct,
            "false_repair": scored.false_repair,
            "claimed_to_fix_the_unfixable": scored.claimed_to_fix_the_unfixable,
            "refused": refused, "parse_error": parse_error, "usage": usage,
        }) + "\n")
        fh.flush()
    return grouped


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap", type=float, default=20.0)
    ap.add_argument("--test-fraction", type=float, default=0.4)
    args = ap.parse_args()

    classification = bench.load_cases(max(ARTIFACTS.glob("chaos_*.jsonl")))
    _, test = bench.stratified_split(classification, test_fraction=args.test_fraction, seed=0)
    repair = bench.load_repair_cases(ARTIFACTS / "repair_stratum.jsonl")
    cases = test + repair
    print(f"cases: {len(test)} classification + {len(repair)} repair = {len(cases)}")

    client = anthropic.Anthropic(api_key=get_settings().anthropic_api_key)
    guard = bench.BudgetGuard(cap_usd=args.cap)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = ARTIFACTS / f"sweep_{stamp}.jsonl"

    # (model, effort) pairs: the frontier at default effort, plus an effort
    # sweep on one model that supports it (Haiku 4.5 does not).
    runs: list[tuple[str, str | None]] = [(m, None) for m in FRONTIER_MODELS]
    runs += [(SONNET, level) for level in EFFORT_LEVELS]

    results: dict[tuple[str, str], list[bench.Scored]] = {}
    with out.open("w", encoding="utf-8") as fh:
        for model, effort in runs:
            label = f"{model}@{effort or 'default'}"
            requests, by_tag = [], {}
            for case in cases:
                req = build_request(case, model, effort)
                requests.append(req)
                by_tag[req["custom_id"]] = case
            guard.check(projected_usd=0.02 * len(requests))
            batch_id = submit_and_wait(client, requests, label)
            grouped = collect(client, batch_id, by_tag, guard, fh)
            for key, scored in grouped.items():
                results.setdefault(key, []).extend(scored)
            agg = bench.aggregate(results.get((model, effort or "default"), []))
            print(f"  {label}: acc@1={agg.get('accuracy@1')} acc@3={agg.get('accuracy@3')} "
                  f"${agg.get('cost_usd_per_scenario')}/case  [spent ${guard.spent_usd:.3f}]")

    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "cases": len(cases),
        "classification_cases": len(test),
        "repair_cases": len(repair),
        "cap_usd": args.cap,
        "spent_usd": round(guard.spent_usd, 4),
        "calls": guard.calls,
        "runs": {f"{m}@{e}": bench.aggregate(s) for (m, e), s in sorted(results.items())},
        "artifact": out.name,
    }
    path = ARTIFACTS / f"sweep_{stamp}_summary.json"
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "runs"}, indent=2))


if __name__ == "__main__":
    main()
