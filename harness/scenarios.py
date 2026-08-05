"""Labeled scenario generator (Phase 2/5).

Contract: produces (fault, params, event fixture) combinations with a recorded
label. Because the harness CAUSED every break, it knows the exact fault class,
the events involved, and the repair that restores the invariant — free, exact
ground truth that eval work usually pays humans for. Scenario ids and labels
are written only to the artifacts log; the Phase 5 agent must never see them.
"""
