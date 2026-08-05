"""FastAPI webhook ingress — adversarial by assumption.

Contract: the endpoint does exactly four things, in order: read the RAW body
(request.body(), never request.json() before verification), verify the
signature, dedupe, enqueue — then return 200 IMMEDIATELY. Anything that could
time out happens in the worker; a start-of-month renewal spike must not
overwhelm this endpoint. Invalid signature -> 400. Duplicate -> 200 (Stripe
should not retry what we already have). The route is exempt from CSRF
middleware and never follows redirects. Subscribes to only the event types the
worker handles.
"""

from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ledgerproof.ingest.dedupe import Deduper
from ledgerproof.ingest.queue import EnqueueFn
from ledgerproof.stripe_io.signature import SignatureVerificationError, verify


def create_app(*, webhook_secret: str, deduper: Deduper, enqueue: EnqueueFn) -> FastAPI:
    app = FastAPI()

    @app.post("/webhook")
    async def webhook(request: Request) -> JSONResponse:
        # RAW bytes — any re-serialization before verification breaks the HMAC
        # and would silently accept tampered bodies. Parse only AFTER verify.
        raw = await request.body()
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

        if not deduper.check_and_record(event):
            return JSONResponse({"status": "duplicate"}, status_code=200)

        enqueue(event)
        # 200 before any heavy work: no ledger writes, no Stripe calls, no
        # mapping. The worker owns everything that could time out.
        return JSONResponse({"status": "queued"}, status_code=200)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
