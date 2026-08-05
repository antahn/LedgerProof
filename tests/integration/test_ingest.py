"""Integration tests for the webhook ingress (LEDGERPROOF_BRIEF §5.3).

Real Deduper against the scratch database; recording-fake enqueue; fixed
webhook secret. The endpoint's whole contract: raw body -> verify -> dedupe ->
enqueue -> 200, and nothing heavier.
"""

from __future__ import annotations

import json
import time

import psycopg
import pytest
from fastapi.testclient import TestClient

from ledgerproof.ingest.app import create_app
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


def deliver(client: TestClient, event: dict, *, secret: str = WEBHOOK_SECRET):
    body = json.dumps(event, separators=(",", ":")).encode()
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
    assert event_rows(db_url) == [("evt_001", "charge.succeeded", "ch_001", "queued")]


def test_invalid_signature_rejected_without_side_effects(client, db_url, enqueued) -> None:
    resp = deliver(client, make_event(), secret=WRONG_SECRET)
    assert resp.status_code == 400
    assert enqueued == []
    assert event_rows(db_url) == []


def test_same_event_id_twice_is_duplicate(client, db_url, enqueued) -> None:
    event = make_event()
    first = deliver(client, event)
    second = deliver(client, event)
    assert first.json() == {"status": "queued"}
    assert second.status_code == 200
    assert second.json() == {"status": "duplicate"}
    assert len(enqueued) == 1
    assert row_count(db_url, "stripe_events") == 1


def test_distinct_event_ids_same_object_is_duplicate(client, db_url, enqueued) -> None:
    # Stripe documents that two distinct Event objects can be generated for
    # the same underlying state change: event.id alone is insufficient.
    first = deliver(client, make_event(event_id="evt_001", object_id="ch_001"))
    second = deliver(client, make_event(event_id="evt_002", object_id="ch_001"))
    assert first.json() == {"status": "queued"}
    assert second.status_code == 200
    assert second.json() == {"status": "duplicate"}
    assert len(enqueued) == 1
    assert row_count(db_url, "stripe_events") == 1


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
