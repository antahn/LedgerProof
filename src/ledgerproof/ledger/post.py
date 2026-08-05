"""Post balanced transactions to the append-only ledger.

Contract: the ONLY write path into transactions/entries. Accepts a transaction
(id, stripe ids, occurred_at, memo) plus a list of entries (account, direction,
amount in minor units, currency); inserts them in a single database transaction
at SERIALIZABLE. Balance is NOT checked here — the deferred constraint trigger
enforces it at COMMIT; this module's job is to hand the database something to
judge, retry on serialization failure (SQLSTATE 40001) with bounded attempts,
and surface unique-violation on the dedupe keys as an idempotent no-op signal.
Never UPDATEs, never DELETEs.
"""
