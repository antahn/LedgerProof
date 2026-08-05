"""Harness-labeled fault benchmark (Phase 5).

Contract: sweeps fault taxonomy x event types x parameters into >=300 labeled
scenarios with a stratified held-out split and recorded class balance. Runs
the three-model frontier via the Batch API (results keyed by custom_id, never
position) with a shared cached prefix. Metrics are per-task, not one
aggregate: accuracy@1/@3 on fault class, repair_restores_invariant rate
(apply proposal in a scratch DB, check the invariant), false_repair rate,
p50/p95 latency, cost per scenario. Logs every call's usage to
artifacts/llm_usage.jsonl and ABORTS at the budget cap.
"""
