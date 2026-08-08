"""Phase 5 pilot: a small live run on all three models, under a hard cap.

Purpose is measurement, not results: verify the pipeline end to end, confirm the
shared prefix actually caches on every model, and record real cost per scenario
so the full sweep can be projected from data instead of assumptions.

Live (non-batch) calls, deliberately — the pilot needs latency figures and fast
feedback; the full sweep uses the Batch API for its 50% discount.

    uv run python scripts/run_pilot.py --per-model 10 --cap 15
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import UTC, datetime
from pathlib import Path

import anthropic

from ledgerproof.config import get_settings
from ledgerproof.triage import bench
from ledgerproof.triage.agent import FRONTIER_MODELS, triage

REPO = Path(__file__).resolve().parent.parent
ARTIFACTS = REPO / "artifacts"


def pick(cases: list[bench.Case], n: int, seed: int) -> list[bench.Case]:
    """A spread across fault classes rather than the first n of one class."""
    rng = random.Random(seed)
    by_fault: dict[str, list[bench.Case]] = {}
    for case in cases:
        by_fault.setdefault(case.label_fault, []).append(case)
    for items in by_fault.values():
        rng.shuffle(items)

    picked: list[bench.Case] = []
    faults = sorted(by_fault)
    while len(picked) < n and any(by_fault[f] for f in faults):
        for fault in faults:
            if by_fault[fault] and len(picked) < n:
                picked.append(by_fault[fault].pop())
    return picked


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-model", type=int, default=10)
    ap.add_argument("--repair-cases", type=int, default=4)
    ap.add_argument("--cap", type=float, default=15.0, help="hard USD cap")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    # Newest sweep: filenames carry a sortable UTC stamp.
    classification = bench.load_cases(max(ARTIFACTS.glob("chaos_*.jsonl")))
    _, test = bench.stratified_split(classification, test_fraction=0.4, seed=0)
    selected = pick(test, args.per_model, args.seed)

    repair_path = ARTIFACTS / "repair_stratum.jsonl"
    if repair_path.exists() and args.repair_cases:
        repair_cases = bench.load_repair_cases(repair_path)
        fixable = [c for c in repair_cases if c.repairable]
        unfixable = [c for c in repair_cases if not c.repairable]
        rng = random.Random(args.seed)
        rng.shuffle(fixable)
        rng.shuffle(unfixable)
        half = max(1, args.repair_cases // 2)
        selected += fixable[:half] + unfixable[: args.repair_cases - half]

    client = anthropic.Anthropic(api_key=get_settings().anthropic_api_key)
    guard = bench.BudgetGuard(cap_usd=args.cap)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = ARTIFACTS / f"pilot_{stamp}.jsonl"

    per_model: dict[str, list[bench.Scored]] = {}
    with out.open("w", encoding="utf-8") as fh:
        for model in FRONTIER_MODELS:
            scored: list[bench.Scored] = []
            for case in selected:
                guard.check(projected_usd=0.05)  # generous per-call headroom
                result = triage(client, case.as_prompt_case(), model=model)
                if result.error:
                    print(f"  ! {model} {case.case_id}: {result.error[:120]}")
                    continue
                cost = guard.record(
                    model, result.usage, batch=False,
                    case_id=case.case_id, stage="pilot",
                )
                s = bench.score(
                    case, result.verdict, latency_s=result.latency_s,
                    cost_usd=cost, refused=result.refused,
                )
                scored.append(s)
                fh.write(json.dumps({
                    "model": model, "case_id": case.case_id,
                    "label_fault": case.label_fault, "stratum": case.stratum,
                    "predicted": s.predicted, "ranked": s.ranked,
                    "correct@1": s.correct_at_1, "correct@3": s.correct_at_3,
                    "confidence": s.confidence, "latency_s": round(s.latency_s, 3),
                    "cost_usd": round(cost, 6), "cache_read": result.usage.get(
                        "cache_read_input_tokens", 0),
                    "repair_correct": s.repair_correct, "false_repair": s.false_repair,
                    "claimed_to_fix_the_unfixable": s.claimed_to_fix_the_unfixable,
                    "usage": result.usage,
                }) + "\n")
                fh.flush()
            per_model[model] = scored
            agg = bench.aggregate(scored)
            print(f"{model:18} acc@1={agg['accuracy@1']:.2f} acc@3={agg['accuracy@3']:.2f} "
                  f"p50={agg['latency_p50_s']}s ${agg['cost_usd_per_scenario']:.5f}/case")

    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "cases_per_model": len(selected),
        "cap_usd": args.cap,
        "spent_usd": round(guard.spent_usd, 4),
        "calls": guard.calls,
        "per_model": {m: bench.aggregate(s) for m, s in per_model.items()},
        "artifact": out.name,
    }
    summary_path = ARTIFACTS / f"pilot_{stamp}_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "per_model"}, indent=2))


if __name__ == "__main__":
    main()
