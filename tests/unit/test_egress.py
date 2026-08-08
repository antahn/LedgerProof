"""Egress retry-matrix tests against a scripted mini-Stripe.

Every test uses httpx.MockTransport as the fake Stripe and injects sleep/rng so
the backoff schedule is observable and reproducible. No network, no DB.
"""

from __future__ import annotations

import random
import urllib.parse
import uuid
from collections.abc import Callable

import httpx
import pytest

from ledgerproof.stripe_io.client import StripeEgressClient, StripeError


def make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    rng: random.Random | None = None,
    max_retries: int = 4,
) -> tuple[StripeEgressClient, list[float]]:
    sleeps: list[float] = []
    client = StripeEgressClient(
        "sk_test_dummy",
        transport=httpx.MockTransport(handler),
        max_retries=max_retries,
        sleep=sleeps.append,
        rng=rng,
    )
    return client, sleeps


# --- (a) 5xx then 200: same key, identical bodies -------------------------------------


def test_5xx_then_200_same_key_and_identical_body() -> None:
    calls: list[tuple[str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.headers["Idempotency-Key"], request.content))
        if len(calls) == 1:
            return httpx.Response(502, json={"error": {"message": "bad gateway"}})
        return httpx.Response(200, json={"id": "ch_1", "status": "succeeded"})

    client, sleeps = make_client(handler, rng=random.Random(0))
    result = client.post(
        "/v1/charges",
        {
            "amount": 1234,
            "currency": "usd",
            "metadata": {"order_id": "o_42"},
            "expand": ["balance_transaction"],
        },
    )

    assert result == {"id": "ch_1", "status": "succeeded"}
    assert len(calls) == 2
    (key_a, body_a), (key_b, body_b) = calls
    assert key_a == key_b
    assert uuid.UUID(key_a).version == 4  # minted key is a real UUID4
    assert body_a == body_b  # byte-identical retry body
    pairs = urllib.parse.parse_qsl(body_a.decode())
    assert ("amount", "1234") in pairs
    assert ("metadata[order_id]", "o_42") in pairs
    assert ("expand[0]", "balance_transaction") in pairs
    assert len(sleeps) == 1  # backed off once before the retry


# --- (b) network error then 200: same key, same body ----------------------------------


def test_network_error_then_200_same_key_same_body() -> None:
    calls: list[tuple[str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.headers["Idempotency-Key"], request.content))
        if len(calls) == 1:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json={"id": "ch_net"})

    client, sleeps = make_client(handler, rng=random.Random(0))
    result = client.post("/v1/charges", {"amount": 500, "currency": "usd"})

    assert result == {"id": "ch_net"}
    assert len(calls) == 2
    assert calls[0] == calls[1]  # same key AND same body after the transport error
    assert len(sleeps) == 1


# --- (c) backoff: exponential growth, jitter, seed-reproducible -----------------------


def run_two_500s_then_200(seed: int) -> list[float]:
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        if count <= 2:
            return httpx.Response(500, json={"error": {"message": "boom"}})
        return httpx.Response(200, json={"ok": True})

    client, sleeps = make_client(handler, rng=random.Random(seed))
    assert client.post("/v1/charges", {"amount": 100}) == {"ok": True}
    assert count == 3
    return sleeps


def test_backoff_is_exponential_with_full_jitter_and_reproducible() -> None:
    sleeps = run_two_500s_then_200(7)
    assert len(sleeps) == 2

    # Exact schedule: base doubles (0.5 then 1.0), each multiplied by the next
    # draw from the injected rng (full jitter).
    expected_rng = random.Random(7)
    assert sleeps == [0.5 * expected_rng.random(), 1.0 * expected_rng.random()]
    assert 0.0 <= sleeps[0] < 0.5
    assert 0.0 <= sleeps[1] < 1.0

    # Same seed -> reproducible; different seed -> different delay sequence.
    assert run_two_500s_then_200(7) == sleeps
    assert run_two_500s_then_200(8) != sleeps


def test_retries_exhausted_raise_last_failure_and_backoff_caps() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, json={"error": {"message": "overloaded"}})

    client, sleeps = make_client(handler, rng=random.Random(3), max_retries=6)
    with pytest.raises(StripeError) as exc_info:
        client.post("/v1/charges", {"amount": 1})

    assert exc_info.value.status_code == 503
    assert exc_info.value.message == "overloaded"
    assert attempts == 7  # initial attempt + max_retries retries

    # Bases double from 0.5 and cap at 8.0; full jitter multiplies each.
    expected_rng = random.Random(3)
    bases = [0.5, 1.0, 2.0, 4.0, 8.0, 8.0]
    assert sleeps == [base * expected_rng.random() for base in bases]


# --- (d) 4xx: raise immediately; the NEXT post() mints a different key ----------------


def test_4xx_raises_immediately_and_next_post_mints_new_key() -> None:
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers["Idempotency-Key"])
        if len(keys) == 1:
            return httpx.Response(
                402,
                json={"error": {"message": "Your card was declined.", "code": "card_declined"}},
                headers={"Request-Id": "req_123"},
            )
        return httpx.Response(200, json={"id": "ch_2"})

    client, sleeps = make_client(handler)
    with pytest.raises(StripeError) as exc_info:
        client.post("/v1/charges", {"amount": 100, "source": "tok_declined"})

    err = exc_info.value
    assert err.status_code == 402
    assert err.code == "card_declined"
    assert err.request_id == "req_123"
    assert len(keys) == 1  # exactly one request sent, no retry
    assert sleeps == []  # and no backoff

    # The "fixed" request is a new post() call: it must carry a DIFFERENT key.
    assert client.post("/v1/charges", {"amount": 100, "source": "tok_visa"}) == {"id": "ch_2"}
    assert len(keys) == 2
    assert keys[1] != keys[0]
    assert all(uuid.UUID(key).version == 4 for key in keys)


# --- (e) mini-Stripe idempotency cache: replay vs. param mismatch ---------------------


class MiniStripe:
    """Stripe's idempotency cache in miniature: the first (key, body) pair wins.

    Same key + different body -> Stripe's idempotency_error 400 shape.
    Same key + same body -> the cached response with Idempotent-Replayed: true.
    """

    def __init__(self) -> None:
        self.seen: dict[str, tuple[bytes, dict]] = {}
        self.created = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        key = request.headers["Idempotency-Key"]
        if key in self.seen:
            first_body, cached = self.seen[key]
            if request.content != first_body:
                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "type": "idempotency_error",
                            "message": (
                                "Keys for idempotent requests can only be used with the same "
                                "parameters they were first used with."
                            ),
                        }
                    },
                )
            return httpx.Response(200, json=cached, headers={"Idempotent-Replayed": "true"})
        self.created += 1
        cached = {"id": f"ch_{self.created}"}
        self.seen[key] = (request.content, cached)
        return httpx.Response(200, json=cached)


def test_idempotency_cache_replays_and_rejects_param_mismatch() -> None:
    stripe = MiniStripe()
    client, _ = make_client(stripe.handler)

    first = client.post("/v1/charges", {"amount": 100}, idempotency_key="key-1")
    assert first == {"id": "ch_1"}
    assert client.replayed_count == 0

    # Same key, different params: Stripe's idempotency_error surfaces as StripeError.
    with pytest.raises(StripeError) as exc_info:
        client.post("/v1/charges", {"amount": 999}, idempotency_key="key-1")
    err = exc_info.value
    assert err.status_code == 400
    assert "same parameters" in err.message

    # Same key, same params: cached response replayed, metric increments.
    replay = client.post("/v1/charges", {"amount": 100}, idempotency_key="key-1")
    assert replay == first
    assert client.replayed_count == 1
    assert stripe.created == 1  # the charge was only ever created once


def test_replayed_count_increments_on_cached_error_replay() -> None:
    # Stripe replays cached 4xx bodies with the same Idempotent-Replayed
    # header; the replay metric must count those, not only 2xx replays.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            headers={"Idempotent-Replayed": "true"},
            json={"error": {"message": "cached decline", "code": "card_declined"}},
        )

    client, sleeps = make_client(handler)
    with pytest.raises(StripeError) as exc_info:
        client.post("/v1/charges", {"amount": 100}, idempotency_key="key-cached-400")

    assert exc_info.value.status_code == 400
    assert client.replayed_count == 1
    assert sleeps == []  # 4xx still does not retry


# --- (f) Stripe-Should-Retry outranks the status code ---------------------------------


def test_should_retry_false_on_500_means_exactly_one_attempt() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            500,
            headers={"Stripe-Should-Retry": "false"},
            json={"error": {"message": "do not retry this"}},
        )

    client, sleeps = make_client(handler)
    with pytest.raises(StripeError) as exc_info:
        client.post("/v1/charges", {"amount": 1})

    assert exc_info.value.status_code == 500
    assert attempts == 1
    assert sleeps == []


def test_should_retry_true_on_400_retries_with_same_key() -> None:
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers["Idempotency-Key"])
        if len(keys) == 1:
            return httpx.Response(
                400,
                headers={"Stripe-Should-Retry": "true"},
                json={"error": {"message": "lock timeout", "code": "lock_timeout"}},
            )
        return httpx.Response(200, json={"id": "ch_ok"})

    client, sleeps = make_client(handler, rng=random.Random(1))
    assert client.post("/v1/charges", {"amount": 1}) == {"id": "ch_ok"}
    assert len(keys) == 2
    assert keys[0] == keys[1]  # header-directed retry reuses the key
    assert len(sleeps) == 1  # and still backs off first


# --- (g) GET never carries an Idempotency-Key -----------------------------------------


def test_get_carries_no_idempotency_key() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["has_key"] = "Idempotency-Key" in request.headers
        seen["params"] = dict(request.url.params)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"object": "list", "data": []})

    client, _ = make_client(handler)
    result = client.get("/v1/charges", {"limit": 3})

    assert result == {"object": "list", "data": []}
    assert seen["method"] == "GET"
    assert seen["has_key"] is False
    assert seen["params"] == {"limit": "3"}
    assert seen["auth"] == "Bearer sk_test_dummy"


# --- malformed 2xx body surfaces as StripeError, not a raw decode error ---------------


def test_non_json_200_raises_stripe_error_with_status_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    client, _ = make_client(handler)
    with pytest.raises(StripeError) as exc_info:
        client.post("/v1/charges", {"amount": 1})

    err = exc_info.value
    assert err.status_code == 200
    assert err.message == "malformed success response body"


# --- close() / context manager releases the underlying httpx client -------------------


def test_context_manager_closes_underlying_httpx_client() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "ch_ctx"})

    client, _ = make_client(handler)
    with client as ctx:
        assert ctx is client
        assert ctx.post("/v1/charges", {"amount": 1}) == {"id": "ch_ctx"}
        assert not client._client.is_closed
    assert client._client.is_closed


# --- form encoding: Stripe bracket notation -------------------------------------------


def test_form_encoding_uses_stripe_bracket_notation() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
        return httpx.Response(200, json={})

    client, _ = make_client(handler)
    client.post(
        "/v1/checkout/sessions",
        {
            "mode": "payment",
            "line_items": [{"price": "price_1", "quantity": 2}],
            "metadata": {"order_id": "o9", "flagged": True},
            "customer": None,
        },
    )

    pairs = urllib.parse.parse_qsl(bodies[0].decode(), keep_blank_values=True)
    assert pairs == [
        ("mode", "payment"),
        ("line_items[0][price]", "price_1"),
        ("line_items[0][quantity]", "2"),
        ("metadata[order_id]", "o9"),
        ("metadata[flagged]", "true"),
        ("customer", ""),
    ]
