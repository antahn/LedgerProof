"""Enqueue verified, deduped events for the worker.

Contract: hands the raw event payload to Celery/Redis and returns fast. No
business logic; delivery to the queue is the ingress endpoint's only side
effect besides the dedupe record.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from ledgerproof.observability import inject_trace_context

if TYPE_CHECKING:
    from celery import Celery

# The ingress endpoint depends only on this shape; tests substitute a
# recording fake, production uses make_celery_enqueue.
EnqueueFn = Callable[[dict], None]

# Task name shared with worker/tasks.py (a string, so neither module has to
# import the other at definition time).
PROCESS_EVENT_TASK = "ledgerproof.process_event"


def make_celery_enqueue(celery_app: Celery) -> EnqueueFn:
    """An EnqueueFn that send_task()s the worker's process_event task.

    send_task publishes by NAME: the ingest process never imports the worker
    module, so the task is not in celery_app.tasks here — a registry lookup
    (celery_app.tasks[...]) KeyErrors in this process.
    """

    def enqueue(event: dict) -> None:
        # The trace context rides inside the payload: Redis carries no headers,
        # and without it the worker's spans are orphans in a different trace.
        # The worker strips it before any handler sees the event.
        celery_app.send_task(PROCESS_EVENT_TASK, args=[inject_trace_context(event)])

    return enqueue
