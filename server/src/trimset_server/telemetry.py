"""OpenTelemetry traces and metrics over OTLP (NFR-13). Off unless an endpoint is set.

Span attributes go through `redact`, because URL attributes would otherwise carry proxy
keys from the path (ADR 0011).
"""

import os
from typing import Any

from fastapi import FastAPI
from opentelemetry import metrics, trace
from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
)

from trimset_server.logs import redact


def redact_span(span: Any, _scope: Any = None) -> None:
    """Server request hook: runs after the request attributes are set, so it can overwrite."""
    for key, value in dict(getattr(span, "attributes", None) or {}).items():
        if isinstance(value, str) and (clean := redact(value)) != value:
            span.set_attribute(key, clean)
    name = getattr(span, "name", "")
    if isinstance(name, str) and (clean := redact(name)) != name:
        span.update_name(clean)


def setup_telemetry(app: FastAPI, exporter: SpanExporter | None = None) -> bool:
    """Instrument the app. Tests pass an in-memory exporter; production reads OTEL_* vars."""
    if exporter is None and not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return False
    resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", "trimset")})
    provider = TracerProvider(resource=resource)
    if exporter is None:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        reader = PeriodicExportingMetricReader(OTLPMetricExporter())
        metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))
        trace.set_tracer_provider(provider)
    else:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        server_request_hook=redact_span,
        excluded_urls="/health,/ready",
    )
    AsyncPGInstrumentor().instrument(tracer_provider=provider)  # type: ignore[no-untyped-call]
    return True
