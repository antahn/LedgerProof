"""Celery task definitions.

Contract: one task per queued webhook event. The unit of work (map -> post)
runs in a database transaction at SERIALIZABLE; on serialization failure
(SQLSTATE 40001) the WHOLE unit retries with bounded attempts (both live in
ledger/post.py — this module only wires the task to it). Tasks are idempotent
end-to-end: replaying a task for an already-posted event is a no-op by way of
the transactions table's unique constraints, so redelivery after a crash is
always safe (acks_late relies on exactly that).

Windows note: the dev worker runs with --pool=solo (prefork is not supported
on Windows); the PARTIAL_WRITE fault kills this process mid-transaction.

    uv run celery -A ledgerproof.worker.tasks worker --pool=solo -l info
"""

from __future__ import annotations

from celery import Celery

from ledgerproof.config import get_settings
from ledgerproof.ingest.queue import PROCESS_EVENT_TASK
from ledgerproof.worker import handlers

celery_app = Celery("ledgerproof", broker=get_settings().redis_url, backend=None)
celery_app.conf.update(
    task_ignore_result=True,  # no result backend by design
    task_acks_late=True,  # ack after the idempotent unit completes -> crash = redeliver
)


@celery_app.task(name=PROCESS_EVENT_TASK)
def process_event(event: dict) -> str:
    settings = get_settings()
    client = None
    if settings.stripe_secret_key:
        # Imported lazily so the worker module stays importable without egress
        # configuration (handlers only need the client on the fetch path).
        from ledgerproof.stripe_io.client import StripeEgressClient

        client = StripeEgressClient(settings.stripe_secret_key)
    return handlers.handle_event(event, db_url=settings.app_database_url, client=client)
