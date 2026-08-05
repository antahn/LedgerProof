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

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Sequence

DEFAULT_TOLERANCE_SECONDS = 300


class SignatureVerificationError(Exception):
    """Raised on ANY verification failure. `.reason` says which check failed."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _v1_digest(payload: bytes, secret: str, timestamp: int) -> str:
    """Hex HMAC-SHA256 of f"{timestamp}.{payload}" keyed with the secret."""
    signed = f"{timestamp}.".encode() + payload
    return hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def sign(
    payload: bytes,
    secret: str,
    *,
    timestamp: int | None = None,
    extra_v1_secrets: Sequence[str] = (),
    include_v0: bool = False,
    only_schemes: Sequence[str] | None = None,
) -> str:
    """Test helper: build a full Stripe-Signature header value for `payload`.

    Returns "t=<unix>,v1=<hex>[,v1=<hex2>][,v0=<hex>]". Each entry in
    `extra_v1_secrets` adds another v1 signature (secret-rotation shape).
    `include_v0` appends a correctly computed v0 signature (same HMAC scheme,
    labeled v0). `only_schemes` filters which signature schemes are emitted —
    only_schemes=["v0"] produces the v0-only header for the downgrade test
    (the t= item is always present; it is the timestamp, not a scheme).
    """
    ts = int(time.time()) if timestamp is None else timestamp
    schemes: list[tuple[str, str]] = [
        ("v1", _v1_digest(payload, s, ts)) for s in (secret, *extra_v1_secrets)
    ]
    wants_v0 = include_v0 or (only_schemes is not None and "v0" in only_schemes)
    if wants_v0:
        # v0 carries a *correct* HMAC so the downgrade test proves rejection
        # is about the scheme label, not a wrong digest.
        schemes.append(("v0", _v1_digest(payload, secret, ts)))
    if only_schemes is not None:
        allowed = set(only_schemes)
        schemes = [(k, v) for k, v in schemes if k in allowed]
    return ",".join([f"t={ts}", *(f"{k}={v}" for k, v in schemes)])


def verify(
    payload: bytes,
    sig_header: str,
    secret: str,
    *,
    tolerance: int = DEFAULT_TOLERANCE_SECONDS,
    now: int | None = None,
) -> None:
    """Verify a Stripe-Signature header against the raw request body.

    Returns None on success; raises SignatureVerificationError on ANY failure
    (fails closed). `now` is injectable for tests; defaults to wall clock.
    A tolerance <= 0 is a programming error (it would disable the recency
    check) and raises ValueError immediately, never a silent pass.
    """
    if tolerance <= 0:
        raise ValueError(
            "tolerance must be a positive number of seconds; "
            "0 or negative would disable replay protection"
        )
    if not isinstance(payload, (bytes, bytearray)):
        raise SignatureVerificationError(
            "payload must be the raw request body bytes"
        )
    if not payload:
        raise SignatureVerificationError("empty body")

    timestamps: list[int] = []
    v1_candidates: list[str] = []
    for item in sig_header.split(","):
        key, sep, value = item.partition("=")
        if not sep or not key or not value:
            raise SignatureVerificationError(f"malformed header item: {item!r}")
        if key == "t":
            try:
                timestamps.append(int(value))
            except ValueError:
                raise SignatureVerificationError(
                    f"non-integer timestamp: {value!r}"
                ) from None
        elif key == "v1":
            v1_candidates.append(value)
        # any other scheme (v0 included) is ignored, never accepted

    if len(timestamps) != 1:
        raise SignatureVerificationError(
            "header must carry exactly one t=<unix> item"
        )
    if not v1_candidates:
        raise SignatureVerificationError("no v1 signatures in header")

    ts = timestamps[0]
    now_s = int(time.time()) if now is None else now
    if abs(now_s - ts) > tolerance:
        raise SignatureVerificationError(
            f"timestamp {ts} outside tolerance ({tolerance}s) of {now_s}"
        )

    expected = _v1_digest(payload, secret, ts).encode("ascii")
    # Check EVERY candidate in constant time; accept if any matches. No early
    # exit, so match position leaks nothing.
    matched = False
    for candidate in v1_candidates:
        if hmac.compare_digest(expected, candidate.encode("utf-8")):
            matched = True
    if not matched:
        raise SignatureVerificationError("no v1 signature matched")
