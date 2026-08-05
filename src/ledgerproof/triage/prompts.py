"""Prompt assembly for the triage agent (Phase 5).

Contract: the stable prefix (rubric, ledger schema, fault taxonomy, output
schema) lives in system blocks with cache_control on the last one; the
volatile per-scenario payload goes in the user turn AFTER it. Never
interpolate a timestamp, UUID, or scenario id into the system prompt — prompt
caching is a prefix match and a single byte invalidates everything after it.
"""
