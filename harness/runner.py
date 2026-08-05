"""Scenario runner (Phase 2).

Contract: for every scenario — snapshot invariant, inject, wait for
quiescence, re-check invariant, and emit one JSONL record to artifacts/:

  {"scenario_id": ..., "fault": ..., "params": {...}, "events": [...],
   "invariant_before": true, "invariant_after": ..., "ledger_diff": {...},
   "duration_ms": ...}

Artifacts are raw run output, never hand-edited; every number quoted anywhere
in this repo traces back to one of these records.
"""
