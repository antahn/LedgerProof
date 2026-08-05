"""Idempotent Stripe egress client.

Contract:
- Idempotency-Key (UUID v4) on EVERY POST; never on GET/DELETE.
- Network error / timeout -> retry with the SAME key and SAME params.
- 5xx -> retry with the SAME key: the outcome is indeterminate, not failed;
  Stripe caches the 500 body, and the original may have had side effects, so a
  new key is never minted after a 5xx.
- 4xx -> fix the request and mint a NEW key: a 400 is cached against the old
  key for 24h and reusing it returns the same 400 forever.
- Honor Stripe-Should-Retry: true -> retry (after backoff), false -> stop,
  absent -> decide from the status code.
- Count Idempotent-Replayed: true responses as a replay metric.
- Backoff: one quick retry, then exponential with random jitter — without
  jitter, simultaneous failures align their retries into a thundering herd.
- Attach a local identifier via metadata on resource creation for later
  reconciliation.
- Stripe prunes idempotency keys after >=24h; a reused key past that window is
  a brand-new request, so keys must never be persisted for reuse across days.
"""

from __future__ import annotations

import random
import time
import urllib.parse
import uuid
from collections.abc import Callable

import httpx

# First retry is quick; each further retry doubles the base, capped, then the
# whole base is multiplied by rng.random() (full jitter: delay in [0, base)).
_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_CAP_SECONDS = 8.0


class StripeError(Exception):
    """A Stripe request failed for good (non-retryable, or retries exhausted)."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.request_id = request_id


def _flatten_params(params: dict, prefix: str = "") -> list[tuple[str, str]]:
    """Flatten to Stripe's bracket notation: metadata[order_id]=x, line_items[0][price]=y."""
    pairs: list[tuple[str, str]] = []
    for name, value in params.items():
        key = f"{prefix}[{name}]" if prefix else str(name)
        pairs.extend(_flatten_value(key, value))
    return pairs


def _flatten_value(key: str, value: object) -> list[tuple[str, str]]:
    if isinstance(value, dict):
        return _flatten_params(value, prefix=key)
    if isinstance(value, (list, tuple)):
        pairs: list[tuple[str, str]] = []
        for index, item in enumerate(value):
            pairs.extend(_flatten_value(f"{key}[{index}]", item))
        return pairs
    if isinstance(value, bool):  # before int/str: bool is an int subclass
        return [(key, "true" if value else "false")]
    if value is None:
        return [(key, "")]
    return [(key, str(value))]


class StripeEgressClient:
    """httpx-based Stripe client implementing the brief §5.2 retry matrix.

    ``max_retries`` bounds retries after the initial attempt, so a request is
    sent at most ``max_retries + 1`` times; exhausting retries raises a
    StripeError describing the last failure. ``sleep`` and ``rng`` are
    injectable so tests can observe and reproduce the backoff schedule.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.stripe.com",
        transport: httpx.BaseTransport | None = None,
        max_retries: int = 4,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._api_key = api_key
        self._max_retries = max_retries
        self._sleep = sleep
        self._rng = rng if rng is not None else random.Random()
        self.replayed_count: int = 0
        self._client = httpx.Client(
            base_url=base_url,
            transport=transport,
            timeout=httpx.Timeout(30.0),
        )

    def post(self, path: str, params: dict, *, idempotency_key: str | None = None) -> dict:
        """POST with a fresh UUID4 Idempotency-Key minted for THIS call.

        Retries within this call reuse the same key and byte-identical body.
        A new post() call always mints a new key — that is how "4xx -> fix the
        request and use a NEW key" falls out.
        """
        key = idempotency_key if idempotency_key is not None else str(uuid.uuid4())
        body = urllib.parse.urlencode(_flatten_params(params)).encode("ascii")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Idempotency-Key": key,
        }
        return self._request("POST", path, headers=headers, content=body)

    def get(self, path: str, params: dict | None = None) -> dict:
        # GET is idempotent by definition: no Idempotency-Key, ever.
        headers = {"Authorization": f"Bearer {self._api_key}"}
        query = _flatten_params(params) if params else None
        return self._request("GET", path, headers=headers, params=query)

    def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        content: bytes | None = None,
        params: list[tuple[str, str]] | None = None,
    ) -> dict:
        max_attempts = self._max_retries + 1
        for attempt in range(max_attempts):
            last_attempt = attempt + 1 >= max_attempts
            try:
                response = self._client.request(
                    method, path, headers=headers, content=content, params=params
                )
            except httpx.TransportError as exc:
                # Network error or timeout: outcome unknown -> same key, same body.
                if last_attempt:
                    raise StripeError(
                        f"network error talking to Stripe after {max_attempts} attempts: {exc}"
                    ) from exc
                self._sleep(self._backoff_delay(attempt))
                continue

            if response.is_success:
                if response.headers.get("Idempotent-Replayed", "").strip().lower() == "true":
                    self.replayed_count += 1
                return response.json()

            error = self._error_from_response(response)
            if not self._retryable(response) or last_attempt:
                raise error
            self._sleep(self._backoff_delay(attempt))

        raise AssertionError("unreachable: loop either returns or raises")

    @staticmethod
    def _retryable(response: httpx.Response) -> bool:
        # Stripe-Should-Retry outranks the status code; absent -> 5xx retries,
        # 4xx does not (the fix must arrive under a NEW key via a new post()).
        header = response.headers.get("Stripe-Should-Retry")
        if header is not None:
            normalized = header.strip().lower()
            if normalized == "true":
                return True
            if normalized == "false":
                return False
        return response.status_code >= 500

    @staticmethod
    def _error_from_response(response: httpx.Response) -> StripeError:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        error = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(error, dict):
            error = {}
        message = error.get("message") or f"Stripe returned HTTP {response.status_code}"
        return StripeError(
            message,
            status_code=response.status_code,
            code=error.get("code"),
            request_id=response.headers.get("Request-Id"),
        )

    def _backoff_delay(self, retry_index: int) -> float:
        base = min(_BACKOFF_BASE_SECONDS * (2**retry_index), _BACKOFF_CAP_SECONDS)
        return base * self._rng.random()
