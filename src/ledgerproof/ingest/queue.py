"""Enqueue verified, deduped events for the worker.

Contract: hands the raw event payload to Celery/Redis and returns fast. No
business logic; delivery to the queue is the ingress endpoint's only side
effect besides the dedupe record.
"""
