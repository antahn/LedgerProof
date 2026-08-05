"""Structured verdict schema for the triage agent (Phase 5).

Contract: Pydantic models for the verdict (fault_class, confidence,
root_cause, affected_accounts, proposed_repair) used with
client.messages.parse(..., output_format=...) — never hand-parsed JSON. The
proposed repair is a compensating transaction as data: new balancing entries,
never a mutation.
"""
