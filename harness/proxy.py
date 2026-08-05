"""The chaos proxy (Phase 2) — the adversary.

Contract: sits between `stripe listen` (or a replayer) and ingest. Forwards
webhook deliveries while injecting faults from harness/faults.py, and logs
exactly which fault it injected for every delivery — that log is the debugging
aid and, later, free labeled ground truth for the Phase 5 benchmark. The proxy
is the only component allowed to lie; everything downstream must survive it.

Two modes, one fault taxonomy:

- `ChaosProxy.execute(plan)` drives a pre-built `FaultPlan` at a target URL.
  The runner owns the stack; the proxy owns the wire. Every request is hard
  capped by a timeout and every failure becomes a `DeliveryResult` with
  `status_code=None` — a hung ingest must never hang the run.
- `make_proxy_app(target)` is the live-traffic path: a FastAPI app that accepts
  `stripe listen` deliveries, applies the configured fault, forwards, and
  appends one JSON line per delivery to the log.

The load-bearing rule in both modes: the body bytes and the Stripe-Signature
header are forwarded EXACTLY as received unless the fault under test says
otherwise. Any re-serialization would break the HMAC and turn every scenario
into a false positive — the ledger would look defended when it was only ever
handed garbage.
"""

from __future__ import annotations

import asyncio
import json
import math
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from harness.faults import Delivery, Fault, FaultPlan
from ledgerproof.stripe_io.signature import sign

USER_AGENT = "LedgerProof-ChaosProxy/1"
CONTENT_TYPE = "application/json"

# A slow-loris body is dribbled in this many chunks; more chunks means the
# connection is held open across more read() calls on the server side.
SLOW_LORIS_CHUNKS = 12

# Extra seconds allowed for the PARTIAL_WRITE kill timer to fire before
# execute() gives up waiting for it. Bounded so a wedged callback cannot hang
# the runner.
KILL_JOIN_SLACK_S = 10.0


@dataclass(frozen=True)
class DeliveryResult:
    """What actually happened on the wire for one delivery.

    `status_code is None` means the request never produced a response (timeout,
    connection refused, protocol error); `error` says which. Errors are values
    here, never exceptions — a scenario that cannot reach ingest is a result to
    record, not a crash.
    """

    event_id: str
    status_code: int | None
    body: str
    error: str | None
    duration_ms: int
    started_at: float


class _Once:
    """Calls `fn` on the first invocation only, from any thread."""

    def __init__(self, fn: Callable[[], None]) -> None:
        self._fn = fn
        self._lock = threading.Lock()
        self.fired = False

    def __call__(self) -> None:
        with self._lock:
            if self.fired:
                return
            self.fired = True
        self._fn()


def _split(body: bytes, chunks: int) -> list[bytes]:
    if not body:
        return [b""]
    size = max(1, math.ceil(len(body) / max(1, chunks)))
    return [body[i : i + size] for i in range(0, len(body), size)]


def _dribble(body: bytes, seconds: float, chunks: int = SLOW_LORIS_CHUNKS) -> Iterator[bytes]:
    """Yield the body slowly so the request occupies the server for `seconds`."""
    pieces = _split(body, chunks)
    interval = seconds / len(pieces)
    for piece in pieces:
        time.sleep(interval)
        yield piece


async def _adribble(
    body: bytes, seconds: float, chunks: int = SLOW_LORIS_CHUNKS
) -> AsyncIterator[bytes]:
    pieces = _split(body, chunks)
    interval = seconds / len(pieces)
    for piece in pieces:
        await asyncio.sleep(interval)
        yield piece


class ChaosProxy:
    """Drives a FaultPlan at `target_url` over real sockets."""

    def __init__(self, target_url: str, *, timeout: float = 30.0) -> None:
        self.target_url = target_url
        self.timeout = timeout

    def execute(
        self, plan: FaultPlan, *, on_kill: Callable[[], None] | None = None
    ) -> list[DeliveryResult]:
        """Send every delivery in `plan`, honoring its fault semantics.

        Results come back in `plan.deliveries` order regardless of send order.
        DROP plans carry no deliveries and return []. `force_500` needs nothing
        here: it describes what the live proxy answers UPSTREAM, and the retry
        it provokes is already a second delivery in the plan.

        `on_kill` (PARTIAL_WRITE) fires exactly once, `kill_worker_after_s`
        after the first delivery actually hits the wire — not after execute()
        is entered, so a plan that also delays cannot kill the worker before
        anything was sent. execute() waits for the kill before returning, so
        the caller can restart the worker knowing it is already down.
        """
        deliveries = tuple(plan.deliveries)
        if not deliveries:
            return []

        timer: threading.Timer | None = None
        first_send: _Once | None = None
        kill_after = plan.kill_worker_after_s
        if kill_after is not None and on_kill is not None:
            timer = threading.Timer(max(kill_after, 0.0), on_kill)
            first_send = _Once(timer.start)

        if plan.concurrent and len(deliveries) > 1:
            # One thread and one fresh connection per delivery, released
            # together by a barrier: CONCURRENT_DUPLICATE only finds races if
            # the requests genuinely overlap in flight.
            barrier = threading.Barrier(len(deliveries))
            with ThreadPoolExecutor(max_workers=len(deliveries)) as pool:
                futures = [
                    pool.submit(self._send, d, barrier=barrier, before_send=first_send)
                    for d in deliveries
                ]
                results = [f.result() for f in futures]
        else:
            results = [self._send(d, before_send=first_send) for d in deliveries]

        if timer is not None and first_send is not None:
            if first_send.fired:
                timer.join(timeout=max(kill_after or 0.0, 0.0) + KILL_JOIN_SLACK_S)
            else:
                timer.cancel()
        return results

    def send(self, delivery: Delivery) -> DeliveryResult:
        """Send one delivery.

        The PARTIAL_WRITE redelivery goes through here, because the runner may
        only issue it after it has restarted the worker it killed.
        """
        return self._send(delivery)

    def _timeout(self, delivery: Delivery) -> httpx.Timeout:
        if delivery.slow_loris_s > 0:
            # The dribble is deliberate, so the read/write budget must cover
            # it; connect and pool stay capped at the normal timeout.
            extended = self.timeout + delivery.slow_loris_s
            return httpx.Timeout(self.timeout, read=extended, write=extended)
        return httpx.Timeout(self.timeout)

    def _send(
        self,
        delivery: Delivery,
        *,
        barrier: threading.Barrier | None = None,
        before_send: Callable[[], None] | None = None,
    ) -> DeliveryResult:
        started_at = time.monotonic()
        started_perf = time.perf_counter()
        status: int | None = None
        body = ""
        error: str | None = None
        try:
            if delivery.delay_before_s > 0:
                time.sleep(delivery.delay_before_s)
            if barrier is not None:
                try:
                    barrier.wait(timeout=self.timeout)
                except threading.BrokenBarrierError:
                    pass  # a sibling failed; send anyway rather than lose the delivery
            if before_send is not None:
                before_send()
            # Two clocks on purpose: started_at is time.monotonic() because
            # callers compare it against their own monotonic readings, but on
            # Windows that clock is GetTickCount64 (15.6ms resolution), which
            # would quantize a localhost round trip to 0ms or 16ms. The
            # duration is measured with perf_counter.
            started_at = time.monotonic()
            started_perf = time.perf_counter()
            headers = {
                "Stripe-Signature": delivery.sig_header,
                "Content-Type": CONTENT_TYPE,
                "User-Agent": USER_AGENT,
            }
            content: Any = (
                _dribble(delivery.body, delivery.slow_loris_s)
                if delivery.slow_loris_s > 0
                else delivery.body
            )
            # A client per delivery guarantees a distinct connection: pooled
            # keep-alive could serialize what is supposed to race.
            with httpx.Client(timeout=self._timeout(delivery), follow_redirects=False) as client:
                response = client.post(self.target_url, content=content, headers=headers)
            status = response.status_code
            body = response.text
        except Exception as exc:  # noqa: BLE001 — every wire failure is a result, not a crash
            error = f"{type(exc).__name__}: {exc}"
        return DeliveryResult(
            event_id=delivery.event_id,
            status_code=status,
            body=body,
            error=error,
            duration_ms=int((time.perf_counter() - started_perf) * 1000),
            started_at=started_at,
        )


def _event_id(raw: bytes) -> str | None:
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    if isinstance(parsed, dict) and isinstance(parsed.get("id"), str):
        return parsed["id"]
    return None


def _flip_byte(body: bytes) -> bytes:
    """Flip one alphanumeric byte near the middle — inside a JSON value for any
    realistic event envelope, so the body stays parseable and the ONLY thing
    wrong with it is that it no longer matches the signature."""
    if not body:
        return b"x"
    buf = bytearray(body)
    order = list(range(len(buf) // 2, len(buf))) + list(range(len(buf) // 2))
    for i in order:
        char = chr(buf[i])
        if char.isalnum():
            buf[i] = ord("0") if char != "0" else ord("1")
            return bytes(buf)
    buf.append(ord(" "))
    return bytes(buf)


def _parse_sig(sig_header: str) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for item in sig_header.split(","):
        key, sep, value = item.partition("=")
        if sep:
            items.append((key.strip(), value))
    return items


def _downgraded(sig_header: str) -> tuple[str, bool]:
    """Relabel every v1 signature as v0, keeping the digests correct.

    Rejection must be about the scheme label, not a wrong HMAC — a bad digest
    would prove nothing about downgrade resistance.
    """
    items = _parse_sig(sig_header)
    if not any(k == "v1" for k, _ in items):
        return sig_header, False
    out = [f"{k}={v}" for k, v in items if k == "t"]
    out += [f"v0={v}" for k, v in items if k == "v1"]
    return ",".join(out), True


def _backdated(sig_header: str, body: bytes, age_s: float, secret: str | None) -> tuple[str, bool]:
    """Back-date `t`.

    With the endpoint secret the whole header is re-signed at the stale
    timestamp, so recency is the only thing wrong with it; without the secret
    only `t` moves, which the tolerance check rejects before it ever reaches
    the HMAC comparison.
    """
    stale = int(time.time() - age_s)
    if secret:
        return sign(body, secret, timestamp=stale), True
    items = _parse_sig(sig_header)
    if not any(k == "t" for k, _ in items):
        return sig_header, False
    return ",".join(f"t={stale}" if k == "t" else f"{k}={v}" for k, v in items), True


def _reissued(body: bytes, secret: str, event_id: str) -> tuple[bytes, str]:
    """Same money movement, a brand-new event id — the DUPLICATE_OBJECT shape.
    Requires the endpoint secret, because the envelope must be re-signed."""
    event = json.loads(body)
    event["id"] = event_id
    reissued = json.dumps(event, separators=(",", ":")).encode()
    return reissued, sign(reissued, secret)


@dataclass(frozen=True)
class _LivePlan:
    """What the live proxy will do with one inbound delivery."""

    deliveries: tuple[tuple[bytes, str], ...]  # (body, Stripe-Signature)
    delay_before_s: float = 0.0
    slow_loris_s: float = 0.0
    concurrent: bool = False
    force_500: bool = False
    applied: bool = True
    note: str = ""


def _live_plan(fault: Fault, params: dict, raw: bytes, sig: str) -> _LivePlan:
    once = ((raw, sig),)
    if fault is Fault.NONE:
        return _LivePlan(once)
    if fault is Fault.DUPLICATE:
        return _LivePlan(once * int(params.get("n", 3)))
    if fault is Fault.CONCURRENT_DUPLICATE:
        return _LivePlan(once * int(params.get("n", 8)), concurrent=True)
    if fault is Fault.DUPLICATE_OBJECT:
        secret = params.get("secret")
        if not secret:
            return _LivePlan(
                once,
                applied=False,
                note="DUPLICATE_OBJECT needs params['secret'] to re-sign the second envelope",
            )
        event_id = _event_id(raw) or "evt_unknown"
        return _LivePlan((*once, _reissued(raw, secret, f"{event_id}_dup")))
    if fault is Fault.REORDER:
        # The buffering itself lives in _Reorderer; this delivery is what it
        # will release, in reverse arrival order.
        return _LivePlan(once)
    if fault is Fault.DELAY:
        return _LivePlan(once, delay_before_s=float(params.get("seconds", 1.0)))
    if fault is Fault.DROP:
        return _LivePlan(())
    if fault is Fault.RESPOND_500:
        return _LivePlan(once, force_500=True)
    if fault is Fault.TAMPER_BODY:
        return _LivePlan(((_flip_byte(raw), sig),))
    if fault is Fault.TRUNCATE_BODY:
        cut = max(1, len(raw) // 2)
        return _LivePlan(((raw[:cut], sig),))
    if fault is Fault.STALE_TIMESTAMP:
        header, applied = _backdated(
            sig, raw, float(params.get("age_s", 600)), params.get("secret")
        )
        return _LivePlan(((raw, header),), applied=applied)
    if fault is Fault.DOWNGRADE_SCHEME:
        header, applied = _downgraded(sig)
        return _LivePlan(((raw, header),), applied=applied)
    if fault is Fault.SLOW_LORIS:
        return _LivePlan(once, slow_loris_s=float(params.get("hold_s", 3.0)))
    if fault is Fault.PARTIAL_WRITE:
        return _LivePlan(
            once,
            applied=False,
            note="PARTIAL_WRITE kills the worker process; the runner owns that, not the proxy",
        )
    return _LivePlan(once, applied=False, note=f"no live injector for {fault}")


def _loggable(params: dict) -> dict:
    """DUPLICATE_OBJECT and STALE_TIMESTAMP take a signing secret in params;
    the delivery log is an artifact that gets committed and shared."""
    return {k: ("<redacted>" if "secret" in k.lower() else v) for k, v in params.items()}


class _DeliveryLog:
    """Append-only JSONL of what the proxy did — the labeled ground truth."""

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: dict) -> None:
        if self.path is None:
            return
        line = json.dumps(record, separators=(",", ":"), default=str) + "\n"
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)


class _Pending:
    def __init__(self, plan: _LivePlan) -> None:
        self.plan = plan
        self.done = asyncio.Event()
        self.results: list[dict] = []


@dataclass
class _Reorderer:
    """Buffers deliveries and releases them in reverse arrival order.

    Stripe does not guarantee ordering, so handlers must not either. A buffered
    delivery is released anyway once `max_hold_s` elapses — a proxy that waited
    forever for a second event would be an outage, not a reorder.
    """

    n: int
    max_hold_s: float
    forward: Callable[[_LivePlan], Any]
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _buffer: list[_Pending] = field(default_factory=list)

    async def submit(self, plan: _LivePlan) -> list[dict]:
        pending = _Pending(plan)
        async with self._lock:
            self._buffer.append(pending)
            batch: list[_Pending] | None = None
            if len(self._buffer) >= self.n:
                batch = list(self._buffer)
                self._buffer.clear()
        if batch is not None:
            for held in reversed(batch):
                held.results = await self.forward(held.plan)
                held.done.set()
            return pending.results

        try:
            await asyncio.wait_for(pending.done.wait(), timeout=self.max_hold_s)
        except TimeoutError:
            async with self._lock:
                mine = pending in self._buffer
                if mine:
                    self._buffer.remove(pending)
            if mine:
                pending.results = await self.forward(pending.plan)
                pending.done.set()
            else:
                # A concurrent flush claimed it between the timeout and the
                # lock; it is on the wire right now.
                await asyncio.wait_for(pending.done.wait(), timeout=self.max_hold_s)
        return pending.results


def make_proxy_app(
    target_url: str,
    *,
    fault: Fault = Fault.NONE,
    params: dict | None = None,
    log_path: str | Path | None = None,
) -> FastAPI:
    """A FastAPI app for LIVE traffic: `stripe listen` -> proxy -> ingest.

    Forwards the raw body bytes and the Stripe-Signature header untouched
    unless the configured fault mutates them, and appends one JSON line per
    delivery (including dropped ones) to `log_path`.

    The upstream response is ingest's own status: Stripe's retry behavior is
    part of what the harness exercises, so lying about it would suppress the
    very retry a fault is meant to provoke. RESPOND_500 is the one exception,
    and provoking that retry is the entire point of the fault.
    """
    fault = Fault(fault)
    fault_params = dict(params or {})
    log = _DeliveryLog(log_path)
    app = FastAPI()

    async def forward_one(body: bytes, sig_header: str, slow_loris_s: float) -> dict:
        headers = {
            "Stripe-Signature": sig_header,
            "Content-Type": CONTENT_TYPE,
            "User-Agent": USER_AGENT,
        }
        started = time.perf_counter()
        status: int | None = None
        error: str | None = None
        timeout = httpx.Timeout(30.0, read=30.0 + slow_loris_s, write=30.0 + slow_loris_s)
        content: Any = _adribble(body, slow_loris_s) if slow_loris_s > 0 else body
        try:
            # A client per forward: pooled keep-alive would serialize the
            # deliveries CONCURRENT_DUPLICATE needs to actually race.
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                response = await client.post(target_url, content=content, headers=headers)
            status = response.status_code
        except Exception as exc:  # noqa: BLE001 — a dead target is a logged delivery, not a crash
            error = f"{type(exc).__name__}: {exc}"
        return {
            "status": status,
            "error": error,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "body_bytes": len(body),
        }

    async def forward_all(plan: _LivePlan) -> list[dict]:
        if plan.delay_before_s > 0:
            await asyncio.sleep(plan.delay_before_s)
        sends = [forward_one(body, sig, plan.slow_loris_s) for body, sig in plan.deliveries]
        if plan.concurrent and len(sends) > 1:
            return list(await asyncio.gather(*sends))
        return [await send for send in sends]

    reorderer = (
        _Reorderer(
            n=int(fault_params.get("n", 2)),
            max_hold_s=float(fault_params.get("max_hold_s", 5.0)),
            forward=forward_all,
        )
        if fault is Fault.REORDER
        else None
    )

    @app.post("/webhook")
    async def webhook(request: Request) -> JSONResponse:
        raw = await request.body()
        sig_header = request.headers.get("stripe-signature", "")
        # Read the id BEFORE any tampering, so the label names the real event.
        event_id = _event_id(raw)
        received_at = time.time()

        plan = _live_plan(fault, fault_params, raw, sig_header)
        results = await (reorderer.submit(plan) if reorderer is not None else forward_all(plan))

        last = results[-1] if results else None
        if plan.force_500:
            upstream = 500
        elif last is None:
            upstream = 200  # DROP: nothing forwarded, and Stripe must not retry it
        elif last["status"] is None:
            upstream = 502
        else:
            upstream = int(last["status"])

        base = {
            "received_at": received_at,
            "fault": fault.value,
            "params": _loggable(fault_params),
            "event_id": event_id,
            "applied": plan.applied,
            "note": plan.note,
            "upstream_status": upstream,
        }
        if not results:
            log.append({**base, "attempt": 1, "attempts": 0, "forwarded": False})
        for i, result in enumerate(results, start=1):
            log.append(
                {**base, "attempt": i, "attempts": len(results), "forwarded": True, **result}
            )

        return JSONResponse(
            {"proxy": "ledgerproof-chaos", "fault": fault.value, "forwarded": len(results)},
            status_code=upstream,
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
