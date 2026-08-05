"""The chaos proxy (Phase 2) — the adversary.

Contract: sits between `stripe listen` (or a replayer) and ingest. Forwards
webhook deliveries while injecting faults from harness/faults.py, and logs
exactly which fault it injected for every delivery — that log is the debugging
aid and, later, free labeled ground truth for the Phase 5 benchmark. The proxy
is the only component allowed to lie; everything downstream must survive it.
"""
