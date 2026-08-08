"""Tests for stripe_io.signature — the eight required rejection cases (a)–(h).

Secret is a fixed fake ("whsec_" + 32 hex chars). Never a real one.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import random
import time

import pytest
import stripe

from ledgerproof.stripe_io.signature import (
    DEFAULT_TOLERANCE_SECONDS,
    SignatureVerificationError,
    sign,
    verify,
)

SECRET = "whsec_9f86d081884c7d659a2feaa0c55ad015"
OTHER_SECRET = "whsec_2c26b46b68ffc68ff99b453c1d304134"
NOW = 1_754_300_000  # fixed unix time, paired with the injectable now=

PAYLOAD = b'{"id":"evt_1","object":"event","type":"charge.succeeded"}'


def expected_v1(payload: bytes, secret: str, ts: int) -> str:
    """Independent digest computation — deliberately not the module's helper."""
    msg = f"{ts}.".encode() + payload
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------- (a) valid


def test_valid_signature_passes() -> None:
    header = sign(PAYLOAD, SECRET, timestamp=NOW)
    assert verify(PAYLOAD, header, SECRET, now=NOW) is None


def test_sign_emits_documented_header_shape() -> None:
    header = sign(PAYLOAD, SECRET, timestamp=NOW)
    assert header == f"t={NOW},v1={expected_v1(PAYLOAD, SECRET, NOW)}"


# ------------------------------------------------------------- (b) tampered


def test_tampered_body_fails() -> None:
    header = sign(PAYLOAD, SECRET, timestamp=NOW)
    tampered = bytearray(PAYLOAD)
    tampered[12] ^= 0x01  # flip one bit of one byte after signing
    assert bytes(tampered) != PAYLOAD
    with pytest.raises(SignatureVerificationError):
        verify(bytes(tampered), header, SECRET, now=NOW)


# ------------------------------------------------------------ (c) truncated


def test_truncated_body_fails() -> None:
    header = sign(PAYLOAD, SECRET, timestamp=NOW)
    with pytest.raises(SignatureVerificationError):
        verify(PAYLOAD[:-1], header, SECRET, now=NOW)


# ------------------------------------------- (d) v0 downgrade never accepted


def test_v0_only_header_fails_even_with_correct_hmac() -> None:
    header = sign(PAYLOAD, SECRET, timestamp=NOW, only_schemes=["v0"])
    # Prove the v0 digest itself is correct: rejection is about the scheme
    # label, not a wrong HMAC.
    assert header == f"t={NOW},v0={expected_v1(PAYLOAD, SECRET, NOW)}"
    with pytest.raises(SignatureVerificationError):
        verify(PAYLOAD, header, SECRET, now=NOW)


def test_v0_alongside_valid_v1_is_ignored_and_passes() -> None:
    header = sign(PAYLOAD, SECRET, timestamp=NOW, include_v0=True)
    assert ",v0=" in header
    assert verify(PAYLOAD, header, SECRET, now=NOW) is None


def test_unknown_scheme_is_ignored_not_accepted() -> None:
    good = expected_v1(PAYLOAD, SECRET, NOW)
    # unknown scheme next to a valid v1: ignored, still passes
    assert verify(PAYLOAD, f"t={NOW},v7=deadbeef,v1={good}", SECRET, now=NOW) is None
    # unknown scheme carrying the CORRECT digest but no v1: fails
    with pytest.raises(SignatureVerificationError):
        verify(PAYLOAD, f"t={NOW},v7={good}", SECRET, now=NOW)


# ------------------------------------------------------- (e) stale timestamp


def test_timestamp_older_than_tolerance_fails() -> None:
    stale = NOW - (DEFAULT_TOLERANCE_SECONDS + 1)
    header = sign(PAYLOAD, SECRET, timestamp=stale)
    with pytest.raises(SignatureVerificationError):
        verify(PAYLOAD, header, SECRET, now=NOW)


def test_timestamp_in_future_beyond_tolerance_fails() -> None:
    future = NOW + DEFAULT_TOLERANCE_SECONDS + 1
    header = sign(PAYLOAD, SECRET, timestamp=future)
    with pytest.raises(SignatureVerificationError):
        verify(PAYLOAD, header, SECRET, now=NOW)


def test_timestamp_exactly_at_tolerance_passes() -> None:
    edge = NOW - DEFAULT_TOLERANCE_SECONDS
    header = sign(PAYLOAD, SECRET, timestamp=edge)
    assert verify(PAYLOAD, header, SECRET, now=NOW) is None


def test_default_tolerance_is_300() -> None:
    assert DEFAULT_TOLERANCE_SECONDS == 300


def test_zero_or_negative_tolerance_is_a_programming_error() -> None:
    # A valid header must NOT slip through: ValueError fires before any check.
    header = sign(PAYLOAD, SECRET, timestamp=NOW)
    with pytest.raises(ValueError):
        verify(PAYLOAD, header, SECRET, tolerance=0, now=NOW)
    with pytest.raises(ValueError):
        verify(PAYLOAD, header, SECRET, tolerance=-5, now=NOW)


# ------------------------------------------------ (f) secret rotation (2 v1)


def test_two_valid_v1_signatures_pass() -> None:
    # Rotation: header signed with old + new secret; endpoint holds one of them.
    header = sign(PAYLOAD, OTHER_SECRET, timestamp=NOW, extra_v1_secrets=[SECRET])
    assert header.count("v1=") == 2
    assert verify(PAYLOAD, header, SECRET, now=NOW) is None
    # Match in first position too — order must not matter.
    header2 = sign(PAYLOAD, SECRET, timestamp=NOW, extra_v1_secrets=[OTHER_SECRET])
    assert verify(PAYLOAD, header2, SECRET, now=NOW) is None


def test_one_valid_plus_one_garbage_v1_passes() -> None:
    good = expected_v1(PAYLOAD, SECRET, NOW)
    header = f"t={NOW},v1=deadbeefdeadbeef,v1={good}"
    assert verify(PAYLOAD, header, SECRET, now=NOW) is None


def test_garbage_only_v1_fails() -> None:
    with pytest.raises(SignatureVerificationError):
        verify(PAYLOAD, f"t={NOW},v1=deadbeefdeadbeef", SECRET, now=NOW)


# ------------------------------------- (g) re-serialized body must not verify


def test_reserialized_body_fails_raw_body_matters() -> None:
    raw = b'{"b":2,"a":1}'  # compact, insertion order b,a
    header = sign(raw, SECRET, timestamp=NOW)
    assert verify(raw, header, SECRET, now=NOW) is None

    # Same JSON value, different whitespace AND key order after a round-trip.
    reserialized = json.dumps(json.loads(raw), sort_keys=True).encode("utf-8")
    assert reserialized != raw
    assert json.loads(reserialized) == json.loads(raw)
    with pytest.raises(SignatureVerificationError):
        verify(reserialized, header, SECRET, now=NOW)


# --------------------------------------------- fail-closed parsing (§5.1)


@pytest.mark.parametrize(
    "bad_header",
    [
        "",  # empty
        "garbage",  # no k=v structure at all
        f"t=notanint,v1={'0' * 64}",  # non-integer t
        f"v1={'0' * 64}",  # missing t
        f"t={NOW}",  # missing v1
        f"t={NOW},t={NOW + 1},v1={'0' * 64}",  # ambiguous duplicate t
        f"t=,v1={'0' * 64}",  # empty t value
        f"t={NOW},v1=",  # empty v1 value
        f"t={NOW},v1={'0' * 64},loneitem",  # item without '='
    ],
    ids=[
        "empty-header",
        "no-structure",
        "non-integer-t",
        "missing-t",
        "missing-v1",
        "duplicate-t",
        "empty-t-value",
        "empty-v1-value",
        "item-without-equals",
    ],
)
def test_malformed_header_fails_closed(bad_header: str) -> None:
    with pytest.raises(SignatureVerificationError):
        verify(PAYLOAD, bad_header, SECRET, now=NOW)


def test_empty_body_fails() -> None:
    header = sign(b"", SECRET, timestamp=NOW)
    with pytest.raises(SignatureVerificationError):
        verify(b"", header, SECRET, now=NOW)


def test_str_payload_is_rejected_raw_bytes_only() -> None:
    header = sign(PAYLOAD, SECRET, timestamp=NOW)
    with pytest.raises(SignatureVerificationError):
        verify(PAYLOAD.decode("utf-8"), header, SECRET, now=NOW)  # type: ignore[arg-type]


def test_error_carries_reason() -> None:
    with pytest.raises(SignatureVerificationError) as exc_info:
        verify(PAYLOAD, f"t={NOW}", SECRET, now=NOW)
    assert isinstance(exc_info.value.reason, str)
    assert exc_info.value.reason


# ------------------------------------- (h) SDK cross-check on 100 payloads


def _generated_payload(rng: random.Random, i: int) -> bytes:
    """Vary content, length, unicode, separators; always valid UTF-8 JSON."""
    words = ["café", "naïve", "日本語のテキスト", "emoji 🎉🔥", "plain ascii", "Ω≈ç√∫", ""]
    obj = {
        "id": f"evt_{i:04d}",
        "object": "event",
        "type": rng.choice(
            ["charge.succeeded", "charge.refunded", "payout.paid", "invoice.payment_failed"]
        ),
        "data": {
            "object": {
                "id": f"ch_{rng.randrange(10**12):012d}",
                "amount": rng.randint(0, 10**9),
                "description": rng.choice(words) + "x" * rng.randint(0, 300),
            }
        },
        "extras": [rng.choice(words) for _ in range(rng.randint(0, 6))],
    }
    separators = rng.choice([(",", ":"), (", ", ": ")])
    return json.dumps(obj, ensure_ascii=False, separators=separators).encode("utf-8")


def _flip_ascii_letter(payload: bytes, rng: random.Random) -> bytes:
    """Tamper one byte while keeping valid UTF-8 (case-flip an ASCII letter),
    so the SDK path fails on the SIGNATURE, not on a UnicodeDecodeError."""
    letter_positions = [
        i for i, b in enumerate(payload) if 0x41 <= b <= 0x5A or 0x61 <= b <= 0x7A
    ]
    pos = rng.choice(letter_positions)
    tampered = bytearray(payload)
    tampered[pos] ^= 0x20
    return bytes(tampered)


def test_sdk_cross_check_agrees_on_100_payloads() -> None:
    rng = random.Random(20260804)
    for i in range(100):
        payload = _generated_payload(rng, i)
        # SDK's tolerance check uses the real wall clock — sign with real time.
        ts = int(time.time())
        header = sign(payload, SECRET, timestamp=ts)

        # Both accept the valid pair.
        assert verify(payload, header, SECRET, now=ts) is None
        assert (
            stripe.WebhookSignature.verify_header(
                payload.decode("utf-8"), header, SECRET, tolerance=300
            )
            is True
        )

        # Both reject the tampered variant.
        tampered = _flip_ascii_letter(payload, rng)
        assert tampered != payload
        with pytest.raises(SignatureVerificationError):
            verify(tampered, header, SECRET, now=ts)
        with pytest.raises(stripe.SignatureVerificationError):
            stripe.WebhookSignature.verify_header(
                tampered.decode("utf-8"), header, SECRET, tolerance=300
            )
