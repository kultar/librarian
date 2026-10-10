"""OpenTelemetry traces and metrics.

Instrumentation uses only `opentelemetry-api`, which is a no-op until an SDK is
configured. `setup_telemetry` configures the SDK and OTLP exporters (install
the `otel` extra); the endpoint and headers come from the standard
`OTEL_EXPORTER_OTLP_*` environment variables.

Trace context is *accepted* from calling agents (so their traces continue
through the librarian) but never forwarded to the APIs it calls.
"""

from __future__ import annotations

import logging
from typing import Any

from opentelemetry import metrics, trace

log = logging.getLogger(__name__)

tracer = trace.get_tracer("librarian")
_meter = metrics.get_meter("librarian")

calls = _meter.create_counter(
    "librarian.api.calls", unit="{call}", description="Outbound API calls by result"
)
denials = _meter.create_counter(
    "librarian.policy.denials", unit="{call}", description="Calls refused by the policy engine"
)
call_duration = _meter.create_histogram(
    "librarian.api.call.duration", unit="ms", description="Outbound API call duration"
)
credential_requests = _meter.create_counter(
    "librarian.credential.requests", unit="{request}", description="Credential requests by state"
)


def status_class(code: int) -> str:
    return f"{code // 100}xx" if code else "error"


def setup_telemetry(service_name: str = "librarian") -> bool:
    """Configure the OTel SDK with OTLP exporters. Returns False if the SDK is missing."""
    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.warning("telemetry enabled but the `otel` extra is not installed; skipping")
        return False

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    metrics.set_meter_provider(
        MeterProvider(
            resource=resource,
            metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())],
        )
    )
    try:
        from pydantic_ai import Agent

        Agent.instrument_all(True)  # model-call spans (gen_ai semantic conventions)
    except Exception:  # pragma: no cover - optional
        log.debug("pydantic-ai instrumentation unavailable", exc_info=True)
    return True


def asgi_middleware(app: Any) -> Any:
    """Wrap an ASGI app so inbound `traceparent` headers start our spans."""
    try:
        from opentelemetry.instrumentation.asgi import (  # pyright: ignore[reportMissingTypeStubs]
            OpenTelemetryMiddleware,
        )
    except ImportError:
        return app
    return OpenTelemetryMiddleware(app, excluded_urls="/healthz")
