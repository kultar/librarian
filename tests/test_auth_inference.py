"""Specs that declare no authentication, 401s on "no auth" APIs, the learned-API notice,
and model refusals: nothing here asks a person to edit YAML."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ContentFilterError, ModelAPIError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.function import AgentInfo, FunctionModel

from librarian.audit import MemoryAuditSink
from librarian.calls import Caller, CallRequest
from librarian.config import LearningSettings, Settings
from librarian.docs import build_extractor
from librarian.planner import Planner
from librarian.registry.models import ApiKeyAuth, BearerAuth, EntryStatus, NoAuth
from librarian.service import CredentialPending, Librarian, LibrarianError
from tests.conftest import RecordingNotifier, Router
from tests.test_learning import extractor_model

AGENT = Caller(agent="agent-a", authenticated=True, principal="kim@x.test")
NO_AUTH_SPEC: dict[str, Any] = {
    "openapi": "3.0.0",
    "info": {"title": "Hub"},
    "servers": [{"url": "https://api.hub.test"}],
    "paths": {
        "/user": {"get": {"operationId": "getUser", "summary": "The authenticated user"}},
        "/orgs/{org}/repos": {
            "get": {
                "operationId": "listRepos",
                "parameters": [
                    {"name": "org", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
            }
        },
    },
}


def fake_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    from librarian import service as service_module

    async def fetch(url: str, client: Any, *, net: Any) -> Any:
        return NO_AUTH_SPEC

    monkeypatch.setattr(service_module, "fetch_spec", fetch)


async def test_spec_without_auth_gets_the_models_answer_for_approval(
    librarian: Librarian, notifier: RecordingNotifier, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_spec(monkeypatch)
    model, prompts = extractor_model(lambda prompt: {"auth": "bearer", "operations": []})
    librarian.extractor = build_extractor(model)
    outcome = await librarian.onboard(
        "hub", spec_url="https://api.hub.test/openapi.json", caller=AGENT
    )
    entry = outcome.entry
    assert isinstance(entry.auth, BearerAuth) and entry.auth.secret_ref == "hub/readonly"
    assert "inferred from the model's knowledge" in entry.notes
    assert entry.credential.validation_operation == "getUser"  # tests new keys
    assert prompts and "Hub" in prompts[0]
    learned = notifier.learned[-1]
    assert learned["api"] == "hub" and learned["learned_by"] == "kim@x.test"
    assert "auth: bearer (hub/readonly)" in learned["summary"]
    assert "2 operations" in learned["summary"] and learned["warnings"] == []
    assert learned["link"] is None and learned["error"] is None  # no publisher in tests


async def test_spec_without_auth_warns_when_nobody_can_tell(
    librarian: Librarian, notifier: RecordingNotifier, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_spec(monkeypatch)
    librarian.extractor = None  # no model configured
    outcome = await librarian.onboard("hub", spec_url="https://api.hub.test/openapi.json")
    assert isinstance(outcome.entry.auth, NoAuth)
    assert any("no model" in w for w in notifier.learned[-1]["warnings"])
    model, _ = extractor_model(lambda prompt: {"auth": "unknown", "operations": []})
    librarian.extractor = build_extractor(model)
    outcome = await librarian.onboard("hub2", spec_url="https://api.hub.test/openapi.json")
    assert isinstance(outcome.entry.auth, NoAuth)
    assert any("could not tell" in w for w in notifier.learned[-1]["warnings"])
    model, _ = extractor_model(lambda prompt: {"auth": "none", "operations": []})
    librarian.extractor = build_extractor(model)
    outcome = await librarian.onboard("hub3", spec_url="https://api.hub.test/openapi.json")
    assert isinstance(outcome.entry.auth, NoAuth)
    assert notifier.learned[-1]["warnings"] == [] and "model agrees" in outcome.entry.notes


async def test_401_on_a_no_auth_api_infers_the_scheme_and_asks_for_a_key(
    librarian: Librarian, router: Router, audit: MemoryAuditSink
) -> None:
    router.add("GET", "https://status.example.test/summary", lambda r: httpx.Response(401))
    model, _ = extractor_model(
        lambda prompt: {"auth": "api_key_header", "auth_key_name": "X-Key", "operations": []}
    )
    librarian.extractor = build_extractor(model)
    with pytest.raises(CredentialPending):
        await librarian.execute(
            CallRequest(api="status", operation="summary"), AGENT, wait_for_credential=False
        )
    entry = librarian.registry.get("status")
    assert entry is not None and isinstance(entry.auth, ApiKeyAuth) and entry.auth.name == "X-Key"
    assert "inferred after a 401" in entry.notes
    assert librarian.broker.pending_for("status") is not None  # a person is asked for the key
    assert any(ev.event == "auth_inferred" for ev in audit.events)


async def test_401_on_a_no_auth_api_without_a_model_tells_the_caller_and_people_once(
    librarian: Librarian, router: Router, notifier: RecordingNotifier
) -> None:
    router.add("GET", "https://status.example.test/summary", lambda r: httpx.Response(401))
    librarian.extractor = None
    result = await librarian.execute(CallRequest(api="status", operation="summary"), AGENT)
    assert not result.ok and result.status_code == 401
    assert (
        result.error and "declares no authentication" in result.error and "console" in result.error
    )
    assert notifier.changes[-1]["title"] == "Needs a credential"
    assert librarian.broker.pending_for("status") is None  # nothing to ask a key for yet
    before = len(notifier.changes)
    await librarian.execute(CallRequest(api="status", operation="summary"), AGENT)
    assert len(notifier.changes) == before  # told once, not on every call


async def test_console_lets_a_person_choose_the_authentication(
    librarian: Librarian, audit: MemoryAuditSink
) -> None:
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from librarian.console import console_routes

    librarian.settings.approvers.console = "open"
    c = TestClient(Starlette(routes=console_routes(librarian)))
    t = librarian.approvals.page_token("console", "someone (console open)")
    page = c.get("/apis/status").text
    assert "Authentication" in page and "name=scheme" in page
    done = c.post(
        "/apis/status/auth", data={"t": t, "scheme": "api_key_header", "key_name": "X-Api-Key"}
    )
    assert done.status_code == 200 and "Authentication set" in done.text
    entry = librarian.registry.get("status")
    assert entry is not None and isinstance(entry.auth, ApiKeyAuth)
    assert entry.auth.name == "X-Api-Key" and entry.auth.secret_ref == "status/readonly"
    assert audit.events[-1].event == "auth_set" and audit.events[-1].detail == "api_key_header"
    assert c.post("/apis/status/auth", data={"t": t, "scheme": "magic"}).status_code == 400
    assert c.post("/apis/status/auth", data={"t": t, "scheme": "api_key_header"}).status_code == 400
    assert c.post("/apis/tracker/auth", data={"t": t, "scheme": "bearer"}).status_code == 400
    tracker = librarian.registry.get("tracker")
    assert tracker is not None and tracker.status is EntryStatus.VERIFIED
    back = c.post("/apis/status/auth", data={"t": t, "scheme": "none"})
    assert back.status_code == 200
    entry = librarian.registry.get("status")
    assert entry is not None and isinstance(entry.auth, NoAuth)


def refusing(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    raise ContentFilterError("This request triggered restrictions on violative cyber content")


async def test_model_refusals_are_explained_and_audited(
    librarian: Librarian, audit: MemoryAuditSink, router: Router
) -> None:
    librarian.planner = Planner(FunctionModel(refusing), 4, "tool")
    with pytest.raises(LibrarianError, match="declined this request"):
        await librarian.fetch("list private repositories", AGENT)
    assert audit.events[-1].event == "model_refused" and audit.events[-1].caller == "agent-a"
    # the same while learning from documentation (no network: the page is faked)
    html = {"content-type": "text/html; charset=utf-8"}
    router.add(
        "GET",
        "https://thing.example.test/docs",
        lambda r: httpx.Response(200, text="<h1>Thing API</h1><p>GET /v1/x</p>", headers=html),
    )
    librarian.source_client_factory = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(router)
    )
    librarian.extractor = build_extractor(FunctionModel(refusing))
    with pytest.raises(LibrarianError, match="declined this request"):
        await librarian.onboard(
            "thing",
            docs_url="https://thing.example.test/docs",
            base_url="https://thing.example.test",
        )
    assert audit.events[-1].event == "model_refused"


async def test_fallback_model_is_tried_once_on_a_refusal() -> None:
    from librarian.service import _with_fallback  # pyright: ignore[reportPrivateUsage]

    def answers(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("ok")])

    model = FallbackModel(
        FunctionModel(refusing),
        FunctionModel(answers),
        fallback_on=(ModelAPIError, ContentFilterError),
    )
    assert (await Agent(model).run("hi")).output == "ok"
    settings = Settings(
        model="small",
        fallback_model="other",
        model_base_url="http://litellm:4000/v1",
        learning=LearningSettings(),
    )
    from librarian.service import _model_from_settings  # pyright: ignore[reportPrivateUsage]

    main: Any = _model_from_settings(settings)
    wrapped: Any = _with_fallback(main, settings)
    assert isinstance(wrapped, FallbackModel)
