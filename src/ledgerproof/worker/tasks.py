"""Celery task definitions.

Contract: one task per queued webhook event. The unit of work (map -> post)
runs in a database transaction at SERIALIZABLE; on serialization failure
(SQLSTATE 40001) the WHOLE unit retries with bounded attempts. Tasks are
idempotent end-to-end: replaying a task for an already-posted event is a no-op
by way of the transactions table's unique constraints.

Windows note: the dev worker runs with --pool=solo (prefork is not supported
on Windows); the PARTIAL_WRITE fault kills this process mid-transaction.
"""
