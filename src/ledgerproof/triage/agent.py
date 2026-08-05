"""LLM triage agent (Phase 5) — proposes, never applies.

Contract: given a ledger break, the agent sees ONLY the ledger diff, the event
log as ingest saw it, and the relevant source files. It must NEVER see the
injected-fault label, the proxy's injection log, or the scenario id (an
explicit leakage test asserts this). It returns a structured verdict —
fault_class, confidence, root_cause prose, affected_accounts, and a proposed
compensating transaction AS DATA. It executes no writes; every repair passes
through an explicit human approval gate, enforced in code.
"""
