"""Tracing and metrics across ingest -> queue -> worker -> DB (brief §6 Phase 4).

Two separate concerns, deliberately not conflated:

**Traces** answer "where did this one event go, and what took the time?" A
webhook's life spans three processes — ingest accepts it, Redis carries it, the
worker posts it — so the trace context is propagated through the queue payload
by hand. Without that the worker's spans are orphans and the one question
traces exist to answer ("why was THIS payment slow?") cannot be asked.

**Metrics** answer "how is the fleet doing right now?" These are the series the
Grafana board reads. The invariant is exported as a metric because money
conservation is an operational property, not just a test assertion: a dashboard
that shows request rates but cannot show whether the books balance is measuring
the wrong system.

Exporters are configured by the caller. Nothing here talks to a collector by
default — an import that opens a network connection is a landmine in tests.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.context import attach, detach
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

logger = logging.getLogger(__name__)

SERVICE_NAME = "ledgerproof"
TRACE_CONTEXT_KEY = "__trace_context__"

REGISTRY = CollectorRegistry()

# --- the six panels the Grafana board is built from ------------------------

INVARIANT_OK = Gauge(
    "ledgerproof_invariant_ok",
    "1 when every currency conserves money, 0 when the books do not balance",
    ["currency"],
    registry=REGISTRY,
)
INVARIANT_DIFFERENCE = Gauge(
    "ledgerproof_invariant_difference_minor",
    "Signed debit-normal minus credit-normal total, in minor units. Nonzero means "
    "the system created or destroyed money",
    ["currency"],
    registry=REGISTRY,
)
BREAKS = Counter(
    "ledgerproof_breaks_total",
    "Correctness breaks observed, by kind",
    ["kind"],
    registry=REGISTRY,
)
REPLAYS = Counter(
    "ledgerproof_idempotent_replays_total",
    "Stripe responses carrying Idempotent-Replayed: true",
    registry=REGISTRY,
)
SHED = Counter(
    "ledgerproof_shed_total",
    "Requests shed, by tier and whether enforcement was live",
    ["tier", "enforced"],
    registry=REGISTRY,
)
QUEUE_DEPTH = Gauge(
    "ledgerproof_queue_depth",
    "Messages waiting on the worker queue",
    registry=REGISTRY,
)
INGEST_LATENCY = Histogram(
    "ledgerproof_ingest_seconds",
    "Webhook ingress latency: verify, dedupe, enqueue, respond",
    ["outcome"],
    # Tight buckets: ingest must answer before Stripe's timeout, so the
    # interesting resolution is milliseconds, not seconds.
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
    registry=REGISTRY,
)
EVENTS = Counter(
    "ledgerproof_events_total",
    "Webhook events by ingress outcome",
    ["outcome"],
    registry=REGISTRY,
)
RATE_LIMITED = Counter(
    "ledgerproof_rate_limited_total",
    "Requests refused by the limiter, by reason (including fail-open events)",
    ["reason"],
    registry=REGISTRY,
)


def metrics_payload() -> tuple[bytes, str]:
    """Prometheus exposition text for a /metrics endpoint."""
    from prometheus_client import CONTENT_TYPE_LATEST

    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def record_invariant(result: Any) -> None:
    """Publish an InvariantResult as gauges, per currency."""
    for check in result.per_currency:
        INVARIANT_OK.labels(currency=check.currency).set(1 if check.ok else 0)
        INVARIANT_DIFFERENCE.labels(currency=check.currency).set(check.difference)


# --- tracing ---------------------------------------------------------------


def configure_tracing(exporter: SpanExporter | None = None) -> TracerProvider:
    """Install a tracer provider. Call once per process, at startup."""
    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
    if exporter is not None:
        provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return provider


def tracer() -> trace.Tracer:
    return trace.get_tracer(SERVICE_NAME)


def inject_trace_context(payload: dict) -> dict:
    """Attach the current span context to a queue message.

    Redis carries no headers, so the context rides inside the payload under a
    reserved key. Handlers must ignore it — it is transport metadata, never
    part of the Stripe event.
    """
    carrier: dict[str, str] = {}
    TraceContextTextMapPropagator().inject(carrier)
    if not carrier:
        return payload
    enriched = dict(payload)
    enriched[TRACE_CONTEXT_KEY] = carrier
    return enriched


def extract_trace_context(payload: dict):
    """Recover the producer's context from a queue message, if present."""
    carrier = payload.get(TRACE_CONTEXT_KEY)
    if not isinstance(carrier, dict):
        return None
    return TraceContextTextMapPropagator().extract(carrier)


def strip_trace_context(payload: dict) -> dict:
    """The event as Stripe sent it, with transport metadata removed."""
    if TRACE_CONTEXT_KEY not in payload:
        return payload
    return {k: v for k, v in payload.items() if k != TRACE_CONTEXT_KEY}


@contextmanager
def attach_context(context: Any | None):
    """Make a context extracted from a queue message current, if there is one."""
    if context is None:
        yield
        return
    token = attach(context)
    try:
        yield
    finally:
        detach(token)


@contextmanager
def span(name: str, **attributes: Any):
    """A span with attributes, recording exceptions before re-raising."""
    with tracer().start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value)
        try:
            yield current
        except Exception as exc:
            current.record_exception(exc)
            current.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
            raise
