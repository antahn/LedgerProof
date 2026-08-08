"""Traces that cross the queue, and metrics a dashboard can actually read."""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from ledgerproof.ledger.invariant import CurrencyCheck, InvariantResult
from ledgerproof.observability import (
    INVARIANT_DIFFERENCE,
    INVARIANT_OK,
    TRACE_CONTEXT_KEY,
    attach_context,
    configure_tracing,
    extract_trace_context,
    inject_trace_context,
    metrics_payload,
    record_invariant,
    span,
    strip_trace_context,
)

EVENT = {"id": "evt_1", "type": "charge.succeeded", "data": {"object": {"id": "ch_1"}}}


@pytest.fixture(scope="module")
def _provider():
    # OTel permits ONE global TracerProvider per process: a second
    # configure_tracing() logs "Overriding ... is not allowed" and silently
    # keeps the first, so a per-test provider would attach an exporter that
    # never receives a span. Configure once, clear between tests.
    return configure_tracing()


@pytest.fixture()
def exporter(_provider) -> InMemorySpanExporter:
    recorder = InMemorySpanExporter()
    _provider.add_span_processor(SimpleSpanProcessor(recorder))
    recorder.clear()
    return recorder


def test_trace_context_survives_the_queue(exporter) -> None:
    """Ingest's span and the worker's must land in ONE trace.

    Redis carries no headers, so the context rides in the payload. If this
    breaks, worker spans become orphans and 'why was this payment slow?'
    stops being answerable.
    """
    with span("ingest.webhook"):
        carried = inject_trace_context(EVENT)

    assert TRACE_CONTEXT_KEY in carried

    parent = extract_trace_context(carried)
    with attach_context(parent), span("worker.process_event"):
        pass

    ingest, worker = (s for s in exporter.get_finished_spans())
    assert ingest.context.trace_id == worker.context.trace_id, (
        "worker span is an orphan: the context did not survive the queue"
    )
    assert worker.parent is not None
    assert worker.parent.span_id == ingest.context.span_id


def test_handlers_never_see_transport_metadata() -> None:
    # The event must reach mapping exactly as Stripe sent it; an unexpected
    # top-level key is the kind of thing that surfaces as a mapping bug.
    with span("ingest.webhook"):
        carried = inject_trace_context(EVENT)
    assert strip_trace_context(carried) == EVENT
    assert TRACE_CONTEXT_KEY not in strip_trace_context(carried)


def test_stripping_is_safe_when_nothing_was_injected() -> None:
    assert strip_trace_context(EVENT) == EVENT
    assert extract_trace_context(EVENT) is None


def test_no_context_without_an_active_span_does_not_corrupt_the_payload() -> None:
    # Outside a span there is nothing to propagate; the payload must be
    # untouched rather than carrying an empty carrier.
    configure_tracing()
    assert inject_trace_context(EVENT) == EVENT


def test_span_records_exceptions_and_reraises(exporter) -> None:
    try:
        with span("worker.process_event", **{"lp.event_id": "evt_1"}):
            raise ValueError("boom")
    except ValueError:
        pass
    (recorded,) = exporter.get_finished_spans()
    assert recorded.status.status_code.name == "ERROR"
    assert recorded.attributes["lp.event_id"] == "evt_1"
    assert any(e.name == "exception" for e in recorded.events)


def test_invariant_is_exported_as_a_metric() -> None:
    """Money conservation is an operational property, not just a test assertion."""
    record_invariant(
        InvariantResult(
            per_currency=(
                CurrencyCheck(currency="USD", sum_debit_normal=100, sum_credit_normal=100),
                CurrencyCheck(currency="EUR", sum_debit_normal=100, sum_credit_normal=93),
            )
        )
    )
    assert INVARIANT_OK.labels(currency="USD")._value.get() == 1
    assert INVARIANT_OK.labels(currency="EUR")._value.get() == 0
    assert INVARIANT_DIFFERENCE.labels(currency="EUR")._value.get() == 7


def test_metrics_endpoint_exposes_the_dashboard_series() -> None:
    payload, content_type = metrics_payload()
    text = payload.decode()
    assert "text/plain" in content_type
    for series in (
        "ledgerproof_invariant_ok",
        "ledgerproof_invariant_difference_minor",
        "ledgerproof_breaks_total",
        "ledgerproof_idempotent_replays_total",
        "ledgerproof_shed_total",
        "ledgerproof_queue_depth",
        "ledgerproof_ingest_seconds",
    ):
        assert series in text, f"{series} missing: a Grafana panel would read empty"

