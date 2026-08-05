"""Manual Stripe webhook signature verification (deliberately not the SDK).

The SDK's Webhook.construct_event is what production code should use; here the
HMAC check is implemented by hand so the security reasoning is legible, and a
test cross-checks it against the SDK on generated payloads. This module is what
the chaos proxy's TAMPER_BODY / TRUNCATE_BODY / STALE_TIMESTAMP /
DOWNGRADE_SCHEME faults attack.

Contract (each line has a dedicated test):
- Parses `Stripe-Signature: t=<unix>,v1=<hex>,v0=<hex>`.
- Signed payload is f"{t}.{raw_body}"; HMAC-SHA256 keyed with endpoint secret.
- Operates on the RAW request body bytes — never re-serialized JSON.
- Ignores every scheme that is not v1 (v0 acceptance is a downgrade attack).
- Constant-time comparison (hmac.compare_digest), all candidates checked.
- Timestamp tolerance 300 seconds — never 0, which disables recency entirely.
- Accepts multiple valid v1 signatures (secret rotation yields two for 24h).
- Fails CLOSED: any parse error, missing scheme, or mismatch is a rejection.
"""
