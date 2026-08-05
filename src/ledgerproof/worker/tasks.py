"""Celery task definitions.

Contract: one task per queued webhook event. The unit of work (map -> post)
runs in a database transaction at SERIALIZABLE; on serialization failure
(SQLSTATE 40001) the WHOLE unit retries with bounded attempts (both live in
ledger/post.py — this module only wires the task to it). Tasks are idempotent
end-to-end: replaying a task for an already-posted event is a no-op by way of
the transactions table's unique constraints, so redelivery after a crash is
always safe (acks_late relies on exactly that).

Failure handling: transient failures (DB down, Stripe fetch error) must NOT
be acked-and-forgotten — the task retries with exponential backoff and jitter
(max_retries=5, countdown capped at 60s); once retries are exhausted the
stripe_events row is marked 'failed' and the exception re-raised so the
failure is visible, not silent.

Windows note: the dev worker runs with --pool=solo (prefork is not supported
on Windows); the PARTIAL_WRITE fault kills this process mid-transaction.

    uv run celery -A ledgerproof.worker.tasks worker --pool=solo -l info
"""

from __future__ import annotations

import logging
import random

from celery import Celery

from ledgerproof.config import get_settings
from ledgerproof.ingest.queue import PROCESS_EVENT_TASK
from ledgerproof.worker import handlers

logger = logging.getLogger(__name__)

celery_app = Celery("ledgerproof", broker=get_settings().redis_url, backend=None)
celery_app.conf.update(
    task_ignore_result=True,  # no result backend by design
    task_acks_late=True,  # ack after the idempotent unit completes -> crash = redeliver
    # acks_late only promises redelivery *eventually*: with the Redis broker an
    # unacked message stays invisible for visibility_timeout, which defaults to
    # ONE HOUR. An unclean worker death would strand a money-moving event for
    # that long (harness finding H1), so shorten it to a minute. Must exceed the
    # longest plausible task duration, or a slow task gets redelivered while it
    # is still running; the unit is idempotent, so that costs a no-op, not money.
    broker_transport_options={"visibility_timeout": 60},
)


@celery_app.task(name=PROCESS_EVENT_TASK, bind=True, max_retries=5)
def process_event(self, event: dict) -> str:
    settings = get_settings()
    client = None
    if settings.stripe_secret_key:
        # Imported lazily so the worker module stays importable without egress
        # configuration (handlers only need the client on the fetch path).
        from ledgerproof.stripe_io.client import StripeEgressClient

        client = StripeEgressClient(settings.stripe_secret_key)
    try:
        return handlers.handle_event(
            event, db_url=settings.app_database_url, client=client
        )
    except Exception as exc:
        if self.request.retries < self.max_retries:
            # Transient failure (DB down, Stripe fetch error): retry with
            # exponential backoff, capped at 60s, jittered so many failing
            # tasks do not realign their retry schedules.
            raise self.retry(
                exc=exc,
                countdown=min(2**self.request.retries, 60) * (0.5 + random.random()),
            )
        # Retries exhausted: record the terminal failure in the event log,
        # then re-raise so the failure is visible instead of acked away.
        # handle_event is idempotent end-to-end, so a later manual replay of
        # a 'failed' event is always safe.
        try:
            handlers._mark_status(settings.app_database_url, event, "failed")
        except Exception:  # the DB being unreachable IS the terminal case
            logger.exception(
                "could not mark event %s as failed; re-raising the original error",
                event.get("id"),
            )
        raise
