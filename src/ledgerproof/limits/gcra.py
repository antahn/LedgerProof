"""GCRA rate limiter (Phase 4).

Contract: leaky-bucket variant that lazily computes a theoretical arrival time
(TAT) instead of simulating a leak — a few stored values per key. Three
adaptations, all required: (1) a Redis lock guards the TAT read-modify-write
(otherwise the limiter has the same race the ledger does); (2) float
timestamps; (3) Redis server TIME as the clock source, because app server
clocks drift.

2 req/sec is NOT interchangeable with 120 req/min — the coarser window admits
a full burst followed by a long idle stretch.

Failure policy: limiter errors FAIL OPEN (allow, log, alert) — a limiter bug
must never take down the API. The lock acquisition FAILS CLOSED (deny) — a
lock timeout means a concurrent writer is mid-update, so allowing would race.
These point in opposite directions on purpose.
"""
