"""Production wiring for the ingress app.

Builds the FastAPI app from Settings: real Deduper on the least-privilege
APP_DATABASE_URL, real Celery enqueue (send_task by name, so this process
never imports the worker). Run:

    uv run uvicorn --factory ledgerproof.ingest.server:build_app --port 8000
"""

from __future__ import annotations

from celery import Celery
from fastapi import FastAPI

from ledgerproof.config import get_settings
from ledgerproof.ingest.app import create_app
from ledgerproof.ingest.dedupe import Deduper
from ledgerproof.ingest.queue import make_celery_enqueue


def build_app() -> FastAPI:
    settings = get_settings()
    if not settings.stripe_webhook_secret:
        raise RuntimeError(
            "STRIPE_WEBHOOK_SECRET is empty; run `stripe listen --print-secret` "
            "and put the whsec_... value in .env"
        )
    celery_app = Celery("ledgerproof", broker=settings.redis_url, backend=None)
    return create_app(
        webhook_secret=settings.stripe_webhook_secret,
        deduper=Deduper(settings.app_database_url),
        enqueue=make_celery_enqueue(celery_app),
    )
