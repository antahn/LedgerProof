"""Integration tests for the webhook ingress.

Real Deduper against the scratch database; recording-fake enqueue; fixed
webhook secret. The endpoint's whole contract: size cap -> raw body -> verify
-> dedupe (row 'received') -> enqueue -> flip to 'queued' -> 200, and nothing
heavier. The outbox tests prove the demonstrated failure is gone: an enqueue
crash leaves a 'received' row that the next delivery recovers instead of a
200 'duplicate' that would silence Stripe's retries forever.
"""

from __future__ import annotations

import json
import time

import psycopg
import pytest
from fastapi.testclient import TestClient

from ledgerproof.ingest.app import MAX_BODY_BYTES, create_app
from ledgerproof.ingest.dedupe import Deduper
from ledgerproof.stripe_io.signature import sign

WEBHOOK_SECRET = "whsec_0123456789abcdef0123456789abcdef"
WRONG_SECRET = "whsec_feedfacefeedfacefeedfacefeedface"


def make_event(
    event_id: str = "evt_001",
    event_type: str = "charge.succeeded",
    object_id: str = "ch_001",
) -> dict:
    return {
        "id": event_id,
        "object": "event",
        "type": event_type,
        "created": int(time.time()),
        "data": {
            "object": {
                "id": object_id,
                "object": "charge",
                "amount": 1000,
                "currency": "usd",
            }
        },
    }


def make_refund_event(
    event_id: str,
    *,
    refund_id: str,
    charge_id: str = "ch_100",
) -> dict:
    """charge.refunded envelope; refunds.data[0] is the newest refund."""
    return {
        "id": event_id,
        "object": "event",
        "type": "charge.refunded",
        "created": int(time.time()),
        "data": {
            "object": {
                "id": charge_id,
                "object": "charge",
                "amount": 1000,
                "amount_refunded": 400,
                "currency": "usd",
                "refunds": {
                    "object": "list",
                    "data": [{"id": refund_id, "object": "refund", "amount": 400}],
                },
            }
        },
    }


class FlakyEnqueue:
    """EnqueueFn fake: raises on the first `failures` calls, records after."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.sent: list[dict] = []

    def __call__(self, event: dict) -> None:
        if self.failures > 0:
            self.failures -= 1
            raise ConnectionError("queue backend unavailable")
        self.sent.append(event)


@pytest.fixture()
def enqueued() -> list[dict]:
    return []


@pytest.fixture()
def client(db_url: str, enqueued: list[dict]) -> TestClient:
    app = create_app(
        webhook_secret=WEBHOOK_SECRET,
        deduper=Deduper(db_url),
        enqueue=enqueued.append,
    )
    return TestClient(app)


def deliver(client: TestClient, payload, *, secret: str = WEBHOOK_SECRET):
    body = json.dumps(payload, separators=(",", ":")).encode()
    return client.post(
        "/webhook", content=body, headers={"Stripe-Signature": sign(body, secret)}
    )


def event_rows(db_url: str) -> list[tuple]:
    with psycopg.connect(db_url) as conn:
        return conn.execute(
            "SELECT id, event_type, object_id, status FROM stripe_events ORDER BY id"
        ).fetchall()


def row_count(db_url: str, table: str) -> int:
    assert table in {"transactions", "entries", "stripe_events"}
    with psycopg.connect(db_url) as conn:
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_healthz(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_valid_signature_queues_once(client, db_url, enqueued) -> None:
    event = make_event()
    resp = deliver(client, event)
    assert resp.status_code == 200
    assert resp.json() == {"status": "queued"}
    assert enqueued == [event]
    # Row was inserted 'received' before the enqueue and flipped 'queued'
    # only after the enqueue succeeded (transactional-outbox shape).
    assert event_rows(db_url) == [("evt_001", "charge.succeeded", "ch_001", "queued")]


def test_invalid_signature_rejected_without_side_effects(client, db_url, enqueued) -> None:
    resp = deliver(client, make_event(), secret=WRONG_SECRET)
    assert resp.status_code == 400
    assert enqueued == []
    assert event_rows(db_url) == []


def test_same_event_id_twice_records_one_row(client, db_url, enqueued) -> None:
    # The dedupe property that matters is ONE event-log row (and, downstream,
    # one ledger transaction) — not the response body. Both answers are 200 to
    # Stripe. Since the first delivery is still non-terminal, the redelivery
    # re-enqueues rather than answering 'duplicate': see H1 in FINDINGS.md,
    # where treating 'queued' as done lost money after a worker died.
    event = make_event()
    first = deliver(client, event)
    second = deliver(client, event)
    assert first.json() == {"status": "queued"}
    assert second.status_code == 200
    assert row_count(db_url, "stripe_events") == 1
    assert {e["id"] for e in enqueued} == {"evt_001"}


def test_distinct_event_ids_same_object_records_one_row(client, db_url, enqueued) -> None:
    # Stripe documents that two distinct Event objects can be generated for
    # the same underlying state change: event.id alone is insufficient. The
    # second key catches it — one row, whatever the response body says.
    first = deliver(client, make_event(event_id="evt_001", object_id="ch_001"))
    second = deliver(client, make_event(event_id="evt_002", object_id="ch_001"))
    assert first.json() == {"status": "queued"}
    assert second.status_code == 200
    assert row_count(db_url, "stripe_events") == 1
    assert {e["id"] for e in enqueued} == {"evt_001"}


def test_reserialized_body_with_original_signature_rejected(client, db_url, enqueued) -> None:
    # Raw-body proof at the endpoint level: a JSON round-trip that changes
    # bytes but not meaning must break verification.
    event = make_event()
    original = json.dumps(event, separators=(",", ":")).encode()
    header = sign(original, WEBHOOK_SECRET)
    reserialized = json.dumps(json.loads(original), indent=2).encode()
    assert reserialized != original

    resp = client.post(
        "/webhook", content=reserialized, headers={"Stripe-Signature": header}
    )
    assert resp.status_code == 400
    assert enqueued == []
    assert event_rows(db_url) == []


def test_200_before_any_heavy_work(client, db_url, enqueued) -> None:
    # The enqueue fake records without processing; if the 200 depended on any
    # worker work, this test could not pass with an inert queue. The only
    # side effects visible after the response are the dedupe/queue records.
    resp = deliver(client, make_event())
    assert resp.status_code == 200
    assert resp.json() == {"status": "queued"}
    assert row_count(db_url, "transactions") == 0
    assert row_count(db_url, "entries") == 0
    assert len(enqueued) == 1
    assert event_rows(db_url) == [("evt_001", "charge.succeeded", "ch_001", "queued")]


# ---------------------------------------------------------------------------
# Transactional outbox: recorded-but-never-enqueued events are recoverable.
# ---------------------------------------------------------------------------


def test_enqueue_failure_then_redelivery_recovers(db_url) -> None:
    # The exact demonstrated failure: enqueue dies AFTER the dedupe row is
    # durable. The old code answered Stripe's retry with 200 'duplicate' and
    # the event was lost forever.
    flaky = FlakyEnqueue(failures=1)
    app = create_app(webhook_secret=WEBHOOK_SECRET, deduper=Deduper(db_url), enqueue=flaky)
    client = TestClient(app)
    event = make_event()

    first = deliver(client, event)
    assert first.status_code == 500  # explicit 500 so Stripe retries
    assert flaky.sent == []
    assert event_rows(db_url) == [("evt_001", "charge.succeeded", "ch_001", "received")]

    # Stripe retries the exact same signed payload; enqueue is healthy now.
    second = deliver(client, event)
    assert second.status_code == 200
    assert second.json() == {"status": "queued"}  # recovery, not 'duplicate'
    assert flaky.sent == [event]  # enqueued exactly once overall
    assert event_rows(db_url) == [("evt_001", "charge.succeeded", "ch_001", "queued")]


def test_received_row_recovered_via_object_key_match(db_url) -> None:
    # Recovery must also work when the retry arrives as a DISTINCT event for
    # the same state change (blocked by the object key, not the event id).
    # The STORED payload is re-enqueued — that is the row the worker advances.
    flaky = FlakyEnqueue(failures=1)
    app = create_app(webhook_secret=WEBHOOK_SECRET, deduper=Deduper(db_url), enqueue=flaky)
    client = TestClient(app)
    original = make_event(event_id="evt_001", object_id="ch_001")

    assert deliver(client, original).status_code == 500
    second = deliver(client, make_event(event_id="evt_002", object_id="ch_001"))
    assert second.status_code == 200
    assert second.json() == {"status": "queued"}
    assert flaky.sent == [original]
    assert event_rows(db_url) == [("evt_001", "charge.succeeded", "ch_001", "queued")]


def test_redelivery_recovers_a_queued_row_whose_worker_died(client, db_url, enqueued) -> None:
    # Minimal repro of harness finding H1. 'queued' means "we handed it to the
    # queue", never "it completed" — an unclean worker death leaves the row
    # exactly here. Answering 'duplicate' would tell Stripe to stop retrying
    # the one delivery that could still save the event, so a redelivery must
    # re-enqueue any NON-TERMINAL row. Safe: the worker is idempotent
    # end-to-end, so a redundant re-enqueue costs one no-op.
    event = make_event()
    assert deliver(client, event).json() == {"status": "queued"}
    assert event_rows(db_url) == [("evt_001", "charge.succeeded", "ch_001", "queued")]

    redelivery = deliver(client, event)
    assert redelivery.status_code == 200
    assert redelivery.json() == {"status": "queued"}
    assert len(enqueued) == 2
    assert event_rows(db_url) == [("evt_001", "charge.succeeded", "ch_001", "queued")]


@pytest.mark.parametrize("terminal", ["processed", "no_money_moved", "failed"])
def test_duplicate_of_terminal_row_not_reenqueued(client, db_url, enqueued, terminal) -> None:
    # The other half of H1: a row the worker finished with is genuinely a
    # duplicate. 'failed' counts as terminal — retries are exhausted and the
    # row is the reconciler's dead-letter signal; auto-redriving it would erase
    # the evidence rather than fix anything.
    event = make_event()
    deliver(client, event)
    with psycopg.connect(db_url) as conn:
        conn.execute("UPDATE stripe_events SET status = %s WHERE id = 'evt_001'", (terminal,))

    dup = deliver(client, event)
    assert dup.json() == {"status": "duplicate"}
    assert len(enqueued) == 1
    assert event_rows(db_url) == [("evt_001", "charge.succeeded", "ch_001", terminal)]


def test_worker_written_status_never_clobbered(client, db_url, enqueued) -> None:
    # mark_queued is guarded on status='received': a late redelivery must not
    # regress a row the worker has already advanced.
    event = make_event()
    deliver(client, event)
    with psycopg.connect(db_url) as conn:
        conn.execute("UPDATE stripe_events SET status = 'processed' WHERE id = 'evt_001'")

    dup = deliver(client, event)
    assert dup.json() == {"status": "duplicate"}
    assert len(enqueued) == 1
    assert event_rows(db_url) == [("evt_001", "charge.succeeded", "ch_001", "processed")]


# ---------------------------------------------------------------------------
# Signed-but-malformed bodies and the size cap.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        [1, 2, 3],
        {"foo": 1},
        {"id": "evt_x"},  # missing type
        {"id": 123, "type": "charge.succeeded"},  # id not a string
    ],
)
def test_signed_non_event_json_rejected(client, db_url, enqueued, payload) -> None:
    resp = deliver(client, payload)
    assert resp.status_code == 400
    assert resp.json() == {"error": "not an event"}
    assert enqueued == []
    assert event_rows(db_url) == []


def test_oversized_body_rejected_without_row_or_enqueue(client, db_url, enqueued) -> None:
    event = make_event()
    event["padding"] = "x" * (MAX_BODY_BYTES + 1)
    resp = deliver(client, event)  # even a VALID signature does not help
    assert resp.status_code == 400
    assert resp.json() == {"error": "body too large"}
    assert enqueued == []
    assert event_rows(db_url) == []


# ---------------------------------------------------------------------------
# Money-movement identity: refunds dedupe on the refund id, not the charge id.
# ---------------------------------------------------------------------------


def test_two_partial_refunds_of_one_charge_both_accepted(client, db_url, enqueued) -> None:
    # Two partial refunds of one charge are two distinct money movements;
    # keying dedupe on data.object.id (the charge) would drop the second one.
    first = deliver(client, make_refund_event("evt_r1", refund_id="re_001"))
    second = deliver(client, make_refund_event("evt_r2", refund_id="re_002"))
    assert first.json() == {"status": "queued"}
    assert second.json() == {"status": "queued"}
    assert len(enqueued) == 2
    assert event_rows(db_url) == [
        ("evt_r1", "charge.refunded", "re_001", "queued"),
        ("evt_r2", "charge.refunded", "re_002", "queued"),
    ]


def test_same_refund_id_twice_records_one_row(client, db_url, enqueued) -> None:
    first = deliver(client, make_refund_event("evt_r1", refund_id="re_001"))
    # Distinct event id, same refund: same money movement, must dedupe to one
    # row (the re-enqueue of a still-non-terminal row is H1's fix; the ledger
    # constraints make it a no-op).
    second = deliver(client, make_refund_event("evt_r2", refund_id="re_001"))
    assert first.json() == {"status": "queued"}
    assert second.status_code == 200
    assert row_count(db_url, "stripe_events") == 1
    assert {e["id"] for e in enqueued} == {"evt_r1"}
