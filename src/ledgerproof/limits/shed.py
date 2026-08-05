"""Tiered load shedding (Phase 4).

Contract: shed in this order — test-mode traffic, then GETs, then POSTs, then
critical writes — reserving fleet capacity for the critical write path. Shed
and recover GRADUALLY or the system flaps. Ships a dark-launch mode that
records what each tier WOULD have blocked without enforcing, and a kill switch
that disables enforcement entirely.
"""
