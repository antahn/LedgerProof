"""FastAPI webhook ingress — adversarial by assumption.

Contract: the endpoint does exactly these things, in order: cap the body size
(before any HMAC work), read the RAW body (request.body(), never
request.json() before verification), verify the signature, reject non-event
JSON, dedupe, enqueue — then return 200 IMMEDIATELY. Anything that could time
out happens in the worker; a start-of-month renewal spike must not overwhelm
this endpoint. Invalid signature -> 400. Duplicate -> 200 (Stripe should not
retry what we already have). The route is exempt from CSRF middleware and
never follows redirects. Subscribes to only the event types the worker
handles.

Transactional-outbox shape: the stripe_events row is durable with status
'received' BEFORE the enqueue; only a successful enqueue flips it to 'queued'.
Enqueue failure -> explicit 500, so Stripe retries; the retry finds the
'received' row and recovers it by re-enqueueing the stored payload. A lost
enqueue can therefore never hide behind a 200 'duplicate'.
"""

from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ledgerproof.ingest.dedupe import Deduper
from ledgerproof.ingest.queue import EnqueueFn
from ledgerproof.stripe_io.signature import SignatureVerificationError, verify

# Stripe events are a few KiB; anything larger is not a webhook we subscribed
# to. Rejected BEFORE signature verification so unauthenticated megabyte POSTs
# get no free HMAC work.
MAX_BODY_BYTES = 512 * 1024

# Statuses that do NOT prove the event was processed. A redelivery of a row in
# one of these must re-enqueue rather than answer 'duplicate': 'received' means
# the enqueue never happened, and 'queued' only means the queue was told —
# an unclean worker death leaves the row right there (harness finding H1).
# 'processed' / 'no_money_moved' are done; 'failed' is the exhausted-retries
# dead-letter the reconciler acts on, and auto-redriving it would erase that.
NON_TERMINAL_STATUSES = frozenset({"received", "queued"})


def create_app(*, webhook_secret: str, deduper: Deduper, enqueue: EnqueueFn) -> FastAPI:
    app = FastAPI()

    @app.post("/webhook")
    async def webhook(request: Request) -> JSONResponse:
        # RAW bytes — any re-serialization before verification breaks the HMAC
        # and would silently accept tampered bodies. Parse only AFTER verify.
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            return JSONResponse({"error": "body too large"}, status_code=400)

        sig_header = request.headers.get("Stripe-Signature", "")
        try:
            verify(raw, sig_header, webhook_secret)
        except SignatureVerificationError:
            return JSONResponse({"error": "invalid signature"}, status_code=400)

        try:
            event = json.loads(raw)
        except ValueError:
            # Correctly signed but unparseable: fail closed, don't 500.
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

        if (
            not isinstance(event, dict)
            or not isinstance(event.get("id"), str)
            or not isinstance(event.get("type"), str)
        ):
            # Correctly signed JSON that is not a Stripe event envelope:
            # fail closed, don't 500.
            return JSONResponse({"error": "not an event"}, status_code=400)

        result = deduper.check_and_record(event)
        if not result.new:
            if (
                result.existing_status in NON_TERMINAL_STATUSES
                and result.existing_id is not None
                and result.existing_payload is not None
            ):
                # Outbox recovery: the row exists but nothing proves the event
                # was ever processed. Re-enqueue the STORED payload — that is
                # the row the worker will advance — then flip it to 'queued'.
                # Safe: the worker is idempotent end-to-end.
                try:
                    enqueue(result.existing_payload)
                except Exception:  # noqa: BLE001 — any backend failure must become a 500
                    return JSONResponse({"error": "enqueue failed"}, status_code=500)
                deduper.mark_queued(result.existing_id)
                return JSONResponse({"status": "queued"}, status_code=200)
            return JSONResponse({"status": "duplicate"}, status_code=200)

        try:
            enqueue(event)
        except Exception:  # noqa: BLE001 — any backend failure must become a 500
            # The row stays 'received'; Stripe retries this 500 and the retry
            # takes the recovery path above. Never answer 200 for an event
            # that did not reach the queue.
            return JSONResponse({"error": "enqueue failed"}, status_code=500)
        deduper.mark_queued(event["id"])
        # 200 before any heavy work: no ledger writes, no Stripe calls, no
        # mapping. The worker owns everything that could time out.
        return JSONResponse({"status": "queued"}, status_code=200)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
