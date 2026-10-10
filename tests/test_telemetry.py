from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from a2a.helpers.proto_helpers import new_data_part
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from librarian.calls import Caller, CallRequest
from librarian.executor import PolicyDenied
from librarian.secrets import FileSecretStore
from librarian.server import create_app
from librarian.service import Librarian
from librarian.telemetry import asgi_middleware
from tests.conftest import BASE, Router
from tests.test_server import KEYS, send

_SPANS = InMemorySpanExporter()
_METRICS = InMemoryMetricReader()


@pytest.fixture(scope="module", autouse=True)
def providers() -> Iterator[None]:
    # Global providers can only be set once per process; module-level proxies in
    # librarian.telemetry bind to these.
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_SPANS))
    trace.set_tracer_provider(provider)
    metrics.set_meter_provider(MeterProvider(metric_readers=[_METRICS]))
    yield


@pytest.fixture(autouse=True)
async def setup(secrets_store: FileSecretStore) -> None:
    _SPANS.clear()
    await secrets_store.put("tracker/readonly", "tok")


def metric_points(name: str) -> list[Any]:
    data = _METRICS.get_metrics_data()
    points: list[Any] = []
    if data is None:
        return points
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name == name:
                    points.extend(metric.data.data_points)
    return points


async def test_calls_produce_spans_and_metrics_without_leaking_trace_headers(
    librarian: Librarian, router: Router
) -> None:
    router.add("GET", f"{BASE}/v1/me", lambda r: httpx.Response(200, json={}))
    await librarian.executor.execute(
        CallRequest(api="tracker", operation="whoami"), Caller(agent="agent-a")
    )
    span = next(s for s in _SPANS.get_finished_spans() if s.name == "librarian.call")
    assert span.attributes is not None
    assert span.attributes["librarian.api"] == "tracker"
    assert span.attributes["http.response.status_code"] == 200
    assert "traceparent" not in router.requests[0].headers  # never forwarded to the API
    calls = [
        p
        for p in metric_points("librarian.api.calls")
        if p.attributes.get("librarian.api") == "tracker"
    ]
    assert calls and calls[0].attributes["http.status_class"] == "2xx"


async def test_denials_are_counted(librarian: Librarian) -> None:
    with pytest.raises(PolicyDenied):
        await librarian.executor.execute(
            CallRequest(api="tracker", operation="deleteIssue", args={"key": "A"}),
            Caller(agent="agent-a"),
        )
    assert metric_points("librarian.policy.denials")


async def test_inbound_trace_context_continues_through_the_librarian(
    librarian: Librarian, router: Router
) -> None:
    router.add("GET", f"{BASE}/v1/me", lambda r: httpx.Response(200, json={}))
    app = create_app(librarian, caller_keys=KEYS)
    app.state.caller_keys.update(KEYS)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=asgi_middleware(app)),
        base_url=librarian.settings.public_url,
        headers={"Authorization": "Bearer key-a"},
    ) as http:
        await send(
            librarian,
            http,
            [new_data_part({"skill": "execute", "api": "tracker", "operation": "whoami"})],
        )
    spans = {s.name: s for s in _SPANS.get_finished_spans()}
    # The (instrumented) A2A client is the calling agent: its trace must continue
    # through the librarian's skill and outbound-call spans, even though the SDK
    # runs the executor in a background task.
    client = spans["a2a.client.transports.jsonrpc.JsonRpcTransport.send_message"]
    assert client.parent is None
    for name in ("librarian.skill", "librarian.call"):
        span = spans[name]
        assert span.context is not None and client.context is not None
        assert span.context.trace_id == client.context.trace_id, name
