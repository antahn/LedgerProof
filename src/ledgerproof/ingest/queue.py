"""Enqueue verified, deduped events for the worker.

Contract: hands the raw event payload to Celery/Redis and returns fast. No
business logic; delivery to the queue is the ingress endpoint's only side
effect besides the dedupe record.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from celery import Celery

# The ingress endpoint depends only on this shape; tests substitute a
# recording fake, production uses make_celery_enqueue.
EnqueueFn = Callable[[dict], None]

# Task name shared with worker/tasks.py (a string, so neither module has to
# import the other at definition time).
PROCESS_EVENT_TASK = "ledgerproof.process_event"


def make_celery_enqueue(celery_app: Celery) -> EnqueueFn:
    """An EnqueueFn that .delay()s the worker's process_event task."""

    def enqueue(event: dict) -> None:
        celery_app.tasks[PROCESS_EVENT_TASK].delay(event)

    return enqueue
