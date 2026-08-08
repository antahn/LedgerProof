"""Chaos-proxy tests.

No Postgres, no Stripe: the subject here is the wire. A real uvicorn target on
an ephemeral port records what it actually received — bytes, signature header,
and the enter/exit timestamps of every request — because the properties that
matter cannot be observed from the client side. Sequential-vs-parallel is the
sharpest example: a CONCURRENT_DUPLICATE that quietly serialized would still
return eight 200s while proving nothing about races, so overlap is asserted
from the server's own clock.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    # harness/ is not an installed package (only src/ledgerproof is).
    sys.path.insert(0, str(REPO_ROOT))


from harness.faults import Delivery, Expectation, Fault, FaultPlan
from harness.proxy import ChaosProxy, _backdated, _downgraded, _flip_byte, make_proxy_app
from ledgerproof.stripe_io.signature import SignatureVerificationError, sign, verify

SECRET = "whsec_" + "c" * 32

# Deliberately non-canonical: irregular spacing, escapes, and a key order no
# serializer would reproduce. Any re-serialization anywhere in the proxy path
# changes these bytes and breaks the HMAC — which is exactly what the
# byte-identity assertions are here to catch.
LIVE_BODY = (
    b'{"id": "evt_live_0001",  "object":"event", "type": "charge.succeeded",'
    b'"created": 1754300000, "data" : {"object": {"id":"ch_live_0001","amount":1000,'
    b'"memo":"caf\\u00e9 \\u2014 latte"}}}'
)


@dataclass
class Hit:
    """One request as the TARGET saw it."""

    body: bytes
    sig: str
    enter: float
    body_read: float
    exit: float


class Target:
    def __init__(self, hold_s: float) -> None:
        self.hold_s = hold_s
        self.hits: list[Hit] = []
        self.inflight = 0
        self.max_inflight = 0
        self.lock = threading.Lock()


def _target_app(target: Target) -> FastAPI:
    app = FastAPI()

    @app.post("/webhook")
    async def webhook(request: Request) -> dict[str, str]:
        enter = time.perf_counter()
        with target.lock:
            target.inflight += 1
            target.max_inflight = max(target.max_inflight, target.inflight)
        raw = await request.body()
        body_read = time.perf_counter()
        await asyncio.sleep(target.hold_s)
        with target.lock:
            target.inflight -= 1
            target.hits.append(
                Hit(
                    body=raw,
                    sig=request.headers.get("stripe-signature", ""),
                    enter=enter,
                    body_read=body_read,
                    exit=time.perf_counter(),
                )
            )
        return {"status": "ok"}

    @app.post("/hang")
    async def hang() -> dict[str, str]:
        # Long enough to trip a 1s client timeout, short enough that server
        # shutdown is not held hostage by it.
        await asyncio.sleep(3.0)
        return {"status": "late"}

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


@contextlib.contextmanager
def _serve(app: FastAPI):
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15.0
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started, "target server did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15.0)


@pytest.fixture()
def serve():
    """Factory: start a recording target server, return (Target, base_url)."""
    stack = contextlib.ExitStack()

    def _start(hold_s: float = 0.02) -> tuple[Target, str]:
        target = Target(hold_s)
        base = stack.enter_context(_serve(_target_app(target)))
        return target, base

    with stack:
        yield _start


def make_body(event_id: str) -> bytes:
    event = {
        "id": event_id,
        "object": "event",
        "type": "charge.succeeded",
        "created": 1754300000,
        "data": {"object": {"id": event_id.replace("evt", "ch"), "amount": 1000}},
    }
    return json.dumps(event, separators=(",", ":")).encode()


def make_delivery(event_id: str = "evt_h000001", **kwargs: Any) -> Delivery:
    body = kwargs.pop("body", None) or make_body(event_id)
    return Delivery(body=body, sig_header=sign(body, SECRET), event_id=event_id, **kwargs)


def make_plan(deliveries, *, fault: Fault = Fault.NONE, params: dict | None = None, **kwargs):
    return FaultPlan(
        fault=fault,
        params=params or {},
        deliveries=tuple(deliveries),
        expectation=Expectation(
            posted_event_ids=frozenset(d.event_id for d in deliveries),
            rejected_event_ids=frozenset(),
            balances_delta={},
        ),
        **kwargs,
    )


def test_fault_contract_shapes_are_available() -> None:
    # The proxy is written against these shapes and nothing else; if they
    # drift, every scenario below is testing fiction.
    assert len(list(Fault)) == 14
    assert Fault.CONCURRENT_DUPLICATE.value == "CONCURRENT_DUPLICATE"


def test_serial_duplicate_sends_four_identical_non_overlapping_requests(serve) -> None:
    target, base = serve(hold_s=0.03)
    delivery = make_delivery()
    proxy = ChaosProxy(f"{base}/webhook", timeout=10.0)

    results = proxy.execute(make_plan([delivery] * 4, fault=Fault.DUPLICATE, params={"n": 4}))

    assert [r.status_code for r in results] == [200, 200, 200, 200]
    assert [r.error for r in results] == [None] * 4
    assert len(target.hits) == 4
    assert {h.body for h in target.hits} == {delivery.body}
    assert {h.sig for h in target.hits} == {delivery.sig_header}
    # Serial means serial: never two in flight, and each window closes before
    # the next opens.
    assert target.max_inflight == 1
    ordered = sorted(target.hits, key=lambda h: h.enter)
    assert all(a.exit <= b.enter for a, b in itertools.pairwise(ordered))


def test_concurrent_duplicate_requests_genuinely_overlap(serve) -> None:
    target, base = serve(hold_s=0.3)
    delivery = make_delivery("evt_h000002")
    plan = make_plan(
        [delivery] * 8, fault=Fault.CONCURRENT_DUPLICATE, params={"n": 8}, concurrent=True
    )
    proxy = ChaosProxy(f"{base}/webhook", timeout=15.0)

    results = proxy.execute(plan)

    assert [r.status_code for r in results] == [200] * 8
    assert len(target.hits) == 8
    assert target.max_inflight == 8
    # Every request window contains one shared instant: a genuine race, not
    # eight requests that merely returned 200.
    assert max(h.enter for h in target.hits) < min(h.exit for h in target.hits)


def test_concurrent_results_come_back_in_plan_order(serve) -> None:
    _, base = serve(hold_s=0.05)
    deliveries = [make_delivery(f"evt_order_{i}") for i in range(4)]
    proxy = ChaosProxy(f"{base}/webhook", timeout=15.0)

    results = proxy.execute(make_plan(deliveries, concurrent=True))

    assert [r.event_id for r in results] == [d.event_id for d in deliveries]


def test_delay_before_send_is_honored_and_excluded_from_duration(serve) -> None:
    _, base = serve(hold_s=0.01)
    proxy = ChaosProxy(f"{base}/webhook", timeout=10.0)
    plan = make_plan([make_delivery(delay_before_s=0.5)], fault=Fault.DELAY)

    start = time.perf_counter()
    results = proxy.execute(plan)
    elapsed = time.perf_counter() - start

    assert elapsed >= 0.5
    assert results[0].status_code == 200
    # duration_ms times the REQUEST, not the wait before it — the runner
    # reports latency, and a delay fault would otherwise swamp the signal.
    assert results[0].duration_ms < 500


def test_slow_loris_holds_the_connection_while_target_stays_responsive(serve) -> None:
    target, base = serve(hold_s=0.0)
    hold_s = 1.5
    delivery = make_delivery("evt_loris", slow_loris_s=hold_s)
    proxy = ChaosProxy(f"{base}/webhook", timeout=15.0)

    results: list[Any] = []
    sender = threading.Thread(target=lambda: results.extend(proxy.execute(make_plan([delivery]))))
    sender.start()
    try:
        time.sleep(0.4)
        health = httpx.get(f"{base}/healthz", timeout=5.0)
        answered_at = time.perf_counter()
    finally:
        sender.join(timeout=30.0)

    assert not sender.is_alive()
    assert health.status_code == 200
    assert results[0].status_code == 200
    hit = target.hits[0]
    # The dribbled body took roughly the hold time to arrive...
    assert hit.body_read - hit.enter >= hold_s * 0.7
    # ...the server answered /healthz while it was still arriving...
    assert hit.enter < answered_at < hit.exit
    # ...and chunking did not corrupt a single byte.
    assert hit.body == delivery.body
    assert hit.sig == delivery.sig_header


def test_refused_connection_is_a_result_not_an_exception() -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()
    proxy = ChaosProxy(f"http://127.0.0.1:{dead_port}/webhook", timeout=2.0)

    results = proxy.execute(make_plan([make_delivery("evt_dead")]))

    assert len(results) == 1
    assert results[0].status_code is None
    assert results[0].error is not None
    assert results[0].event_id == "evt_dead"
    assert results[0].body == ""


def test_unresponsive_target_times_out_into_a_result(serve) -> None:
    _, base = serve(hold_s=0.0)
    proxy = ChaosProxy(f"{base}/hang", timeout=1.0)

    start = time.perf_counter()
    results = proxy.execute(make_plan([make_delivery("evt_hang")]))
    elapsed = time.perf_counter() - start

    assert results[0].status_code is None
    assert "Timeout" in (results[0].error or "")
    # Hard cap: the runner must never inherit an unbounded wait.
    assert elapsed < 3.0


def test_on_kill_fires_exactly_once_after_the_first_send(serve) -> None:
    _, base = serve(hold_s=0.05)
    proxy = ChaosProxy(f"{base}/webhook", timeout=10.0)
    plan = make_plan(
        [make_delivery("evt_kill_1"), make_delivery("evt_kill_2")],
        fault=Fault.PARTIAL_WRITE,
        kill_worker_after_s=0.15,
    )
    calls: list[float] = []

    start = time.perf_counter()
    results = proxy.execute(plan, on_kill=lambda: calls.append(time.perf_counter()))

    assert len(calls) == 1, "the worker must be killed once, not once per delivery"
    assert calls[0] - start >= 0.15
    # execute() returned only after the kill landed, so the runner can restart
    # the worker knowing it is already down.
    assert [r.status_code for r in results] == [200, 200]


def test_kill_timer_starts_at_the_first_send_not_at_execute_entry(serve) -> None:
    _, base = serve(hold_s=0.01)
    proxy = ChaosProxy(f"{base}/webhook", timeout=10.0)
    plan = make_plan(
        [make_delivery("evt_kill_delayed", delay_before_s=0.6)],
        fault=Fault.PARTIAL_WRITE,
        kill_worker_after_s=0.1,
    )
    calls: list[float] = []

    start = time.perf_counter()
    proxy.execute(plan, on_kill=lambda: calls.append(time.perf_counter()))

    assert len(calls) == 1
    # Killing at 0.1s would have killed an idle worker: PARTIAL_WRITE has to
    # land while the event is being processed.
    assert calls[0] - start >= 0.6


def test_send_delivers_one_delivery_outside_a_plan(serve) -> None:
    # The PARTIAL_WRITE redelivery: the runner issues it only after it has
    # restarted the worker, so it cannot come from execute().
    target, base = serve(hold_s=0.0)
    proxy = ChaosProxy(f"{base}/webhook", timeout=10.0)
    delivery = make_delivery("evt_redeliver")

    result = proxy.send(delivery)

    assert result.status_code == 200
    assert result.event_id == "evt_redeliver"
    assert [h.body for h in target.hits] == [delivery.body]


def test_drop_plan_sends_nothing(serve) -> None:
    target, base = serve(hold_s=0.0)
    proxy = ChaosProxy(f"{base}/webhook", timeout=5.0)

    assert proxy.execute(make_plan([], fault=Fault.DROP)) == []
    assert target.hits == []


def test_proxy_app_forwards_bytes_and_signature_untouched(serve, tmp_path: Path) -> None:
    target, base = serve(hold_s=0.0)
    log_path = tmp_path / "logs" / "proxy.jsonl"
    app = make_proxy_app(f"{base}/webhook", fault=Fault.NONE, log_path=log_path)
    sig = sign(LIVE_BODY, SECRET)

    with TestClient(app) as client:
        response = client.post(
            "/webhook",
            content=LIVE_BODY,
            headers={"Stripe-Signature": sig, "Content-Type": "application/json"},
        )
        health = client.get("/healthz")

    assert response.status_code == 200
    assert health.json() == {"status": "ok"}
    assert len(target.hits) == 1
    hit = target.hits[0]
    assert hit.body == LIVE_BODY
    assert hit.sig == sig
    # The real proof: what arrived still verifies against the secret it was
    # signed with. Any re-serialization would fail here.
    verify(hit.body, hit.sig, SECRET)

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["fault"] == "NONE"
    assert record["event_id"] == "evt_live_0001"
    assert record["status"] == 200
    assert record["forwarded"] is True
    assert record["applied"] is True


def test_proxy_app_tamper_changes_one_byte_and_keeps_the_signature(serve, tmp_path: Path) -> None:
    target, base = serve(hold_s=0.0)
    log_path = tmp_path / "proxy.jsonl"
    app = make_proxy_app(f"{base}/webhook", fault=Fault.TAMPER_BODY, log_path=log_path)
    sig = sign(LIVE_BODY, SECRET)

    with TestClient(app) as client:
        client.post(
            "/webhook",
            content=LIVE_BODY,
            headers={"Stripe-Signature": sig, "Content-Type": "application/json"},
        )

    hit = target.hits[0]
    assert hit.sig == sig, "the header must be untouched — the BODY is the lie"
    assert len(hit.body) == len(LIVE_BODY)
    assert sum(a != b for a, b in zip(hit.body, LIVE_BODY, strict=True)) == 1
    with pytest.raises(SignatureVerificationError):
        verify(hit.body, hit.sig, SECRET)
    record = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    # The label names the REAL event even though the forwarded body is a lie.
    assert record["fault"] == "TAMPER_BODY"
    assert record["event_id"] == "evt_live_0001"


def test_proxy_app_drop_forwards_nothing_but_still_logs_the_label(serve, tmp_path: Path) -> None:
    target, base = serve(hold_s=0.0)
    log_path = tmp_path / "proxy.jsonl"
    app = make_proxy_app(f"{base}/webhook", fault=Fault.DROP, log_path=log_path)

    with TestClient(app) as client:
        response = client.post(
            "/webhook",
            content=LIVE_BODY,
            headers={"Stripe-Signature": sign(LIVE_BODY, SECRET)},
        )

    assert response.status_code == 200, "a dropped event must not provoke a Stripe retry"
    assert target.hits == []
    record = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["fault"] == "DROP"
    assert record["forwarded"] is False


def test_proxy_app_duplicate_forwards_n_times_and_labels_each(serve, tmp_path: Path) -> None:
    target, base = serve(hold_s=0.0)
    log_path = tmp_path / "proxy.jsonl"
    app = make_proxy_app(
        f"{base}/webhook", fault=Fault.DUPLICATE, params={"n": 3}, log_path=log_path
    )

    with TestClient(app) as client:
        response = client.post(
            "/webhook",
            content=LIVE_BODY,
            headers={"Stripe-Signature": sign(LIVE_BODY, SECRET)},
        )

    assert response.status_code == 200
    assert len(target.hits) == 3
    assert {h.body for h in target.hits} == {LIVE_BODY}
    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert [r["attempt"] for r in records] == [1, 2, 3]
    assert all(r["fault"] == "DUPLICATE" and r["attempts"] == 3 for r in records)


def test_proxy_app_respond_500_forwards_anyway(serve, tmp_path: Path) -> None:
    target, base = serve(hold_s=0.0)
    log_path = tmp_path / "proxy.jsonl"
    app = make_proxy_app(f"{base}/webhook", fault=Fault.RESPOND_500, log_path=log_path)

    with TestClient(app) as client:
        response = client.post(
            "/webhook",
            content=LIVE_BODY,
            headers={"Stripe-Signature": sign(LIVE_BODY, SECRET)},
        )

    # The 500 is what makes Stripe retry, and the retry is the thing under
    # test — so the delivery must ALSO have reached ingest.
    assert response.status_code == 500
    assert len(target.hits) == 1
    record = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["upstream_status"] == 500
    assert record["status"] == 200


def test_proxy_app_reorder_releases_in_reverse_arrival_order(serve, tmp_path: Path) -> None:
    target, base = serve(hold_s=0.0)
    app = make_proxy_app(
        f"{base}/webhook",
        fault=Fault.REORDER,
        params={"n": 2, "max_hold_s": 10.0},
        log_path=tmp_path / "proxy.jsonl",
    )
    first, second = make_body("evt_first"), make_body("evt_second")

    def post(body: bytes, sink: list) -> None:
        sink.append(
            httpx.post(
                f"{proxy_base}/webhook",
                content=body,
                headers={"Stripe-Signature": sign(body, SECRET)},
                timeout=20.0,
            )
        )

    with _serve(app) as proxy_base:
        responses: list = []
        held = threading.Thread(target=post, args=(first, responses))
        held.start()
        time.sleep(0.3)  # the first delivery is now parked in the proxy's buffer
        post(second, responses)
        held.join(timeout=20.0)

    assert [r.status_code for r in responses] == [200, 200]
    arrived = [json.loads(h.body)["id"] for h in target.hits]
    assert arrived == ["evt_second", "evt_first"]


def test_proxy_app_slow_loris_dribbles_without_corrupting_the_body(serve, tmp_path: Path) -> None:
    target, base = serve(hold_s=0.0)
    hold_s = 0.8
    app = make_proxy_app(
        f"{base}/webhook",
        fault=Fault.SLOW_LORIS,
        params={"hold_s": hold_s},
        log_path=tmp_path / "proxy.jsonl",
    )
    sig = sign(LIVE_BODY, SECRET)

    with TestClient(app) as client:
        response = client.post("/webhook", content=LIVE_BODY, headers={"Stripe-Signature": sig})

    assert response.status_code == 200
    hit = target.hits[0]
    assert hit.body_read - hit.enter >= hold_s * 0.7
    assert hit.body == LIVE_BODY
    verify(hit.body, hit.sig, SECRET)


def test_proxy_app_duplicate_object_reissues_a_second_signed_envelope(
    serve, tmp_path: Path
) -> None:
    target, base = serve(hold_s=0.0)
    log_path = tmp_path / "proxy.jsonl"
    app = make_proxy_app(
        f"{base}/webhook",
        fault=Fault.DUPLICATE_OBJECT,
        params={"secret": SECRET},
        log_path=log_path,
    )

    with TestClient(app) as client:
        client.post(
            "/webhook", content=LIVE_BODY, headers={"Stripe-Signature": sign(LIVE_BODY, SECRET)}
        )

    assert len(target.hits) == 2
    original, reissued = (json.loads(h.body) for h in target.hits)
    assert original["id"] != reissued["id"]
    # Different event, same money movement — which is the entire point of the
    # second dedupe key.
    assert original["type"] == reissued["type"]
    assert original["data"]["object"]["id"] == reissued["data"]["object"]["id"]
    for hit in target.hits:
        verify(hit.body, hit.sig, SECRET)
    # The log is a committed artifact; the signing secret must not ride along.
    log_text = log_path.read_text(encoding="utf-8")
    assert SECRET not in log_text
    assert json.loads(log_text.splitlines()[0])["params"] == {"secret": "<redacted>"}


def test_backdated_without_a_secret_moves_only_the_timestamp() -> None:
    header = sign(LIVE_BODY, SECRET)
    stale, applied = _backdated(header, LIVE_BODY, 600, None)

    assert applied
    assert stale.split(",")[1] == header.split(",")[1]
    assert int(stale.split(",")[0][2:]) <= int(header.split(",")[0][2:]) - 600
    with pytest.raises(SignatureVerificationError):
        verify(LIVE_BODY, stale, SECRET)


def test_backdated_with_the_secret_is_stale_but_otherwise_perfect() -> None:
    stale, applied = _backdated(sign(LIVE_BODY, SECRET), LIVE_BODY, 600, SECRET)

    assert applied
    with pytest.raises(SignatureVerificationError) as raised:
        verify(LIVE_BODY, stale, SECRET)
    # Rejected for recency alone: roll the clock back and the HMAC is perfect,
    # so STALE_TIMESTAMP tests replay protection and nothing else.
    assert "tolerance" in raised.value.reason
    verify(LIVE_BODY, stale, SECRET, now=int(time.time()) - 600)


def test_downgrade_relabels_v1_digests_as_v0() -> None:
    header = sign(LIVE_BODY, SECRET)
    downgraded, applied = _downgraded(header)

    assert applied
    assert "v1=" not in downgraded
    assert downgraded.count("v0=") == 1
    assert downgraded.split(",")[0] == header.split(",")[0]
    # The digest is still correct; only its scheme label changed, so a
    # rejection can only be about the label.
    assert downgraded.split("v0=")[1] == header.split("v1=")[1]


def test_flip_byte_keeps_length_and_parseability() -> None:
    flipped = _flip_byte(LIVE_BODY)

    assert len(flipped) == len(LIVE_BODY)
    assert flipped != LIVE_BODY
    assert json.loads(flipped)["object"] == "event"
