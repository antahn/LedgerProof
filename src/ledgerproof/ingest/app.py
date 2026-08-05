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
