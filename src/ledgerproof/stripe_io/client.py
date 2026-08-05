"""Idempotent Stripe egress client.

Contract:
- Idempotency-Key (UUID v4) on EVERY POST; never on GET/DELETE.
- Network error / timeout -> retry with the SAME key and SAME params.
- 5xx -> retry with the SAME key: the outcome is indeterminate, not failed;
  Stripe caches the 500 body, and the original may have had side effects, so a
  new key is never minted after a 5xx.
- 4xx -> fix the request and mint a NEW key: a 400 is cached against the old
  key for 24h and reusing it returns the same 400 forever.
- Honor Stripe-Should-Retry: true -> retry (after backoff), false -> stop,
  absent -> decide from the status code.
- Count Idempotent-Replayed: true responses as a replay metric.
- Backoff: one quick retry, then exponential with random jitter — without
  jitter, simultaneous failures align their retries into a thundering herd.
- Attach a local identifier via metadata on resource creation for later
  reconciliation.
- Stripe prunes idempotency keys after >=24h; a reused key past that window is
  a brand-new request, so keys must never be persisted for reuse across days.
"""
