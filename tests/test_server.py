"""End-to-end: a real A2A client talking to the librarian app in-process."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from a2a.client import Client, ClientConfig, create_client
from a2a.helpers.proto_helpers import get_data_parts, get_message_text, new_data_part, new_text_part
from a2a.types import (
    GetTaskRequest,
    Message,
    Part,
    Role,
    SendMessageRequest,
    StreamResponse,
    TaskState,
)
from google.protobuf.json_format import ParseDict
from google.protobuf.struct_pb2 import Struct

from librarian.secrets import FileSecretStore
from librarian.server import build_agent_card, create_app
from librarian.service import Librarian
from tests.conftest import BASE, Router

KEYS = {"agent-a": "key-a"}


@dataclass
class Outcome:
    states: list[int] = field(default_factory=list[int])
    data: list[Any] = field(default_factory=list[Any])
    texts: list[str] = field(default_factory=list[str])
    task_id: str = ""

    @property
    def final(self) -> int:
        return self.states[-1]


@pytest.fixture
async def http(librarian: Librarian) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(librarian, caller_keys=KEYS)
    app.state.caller_keys.update(KEYS)  # lifespan does this when served for real
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url=librarian.settings.public_url,
        headers={"Authorization": "Bearer key-a"},
        timeout=10,
    ) as client:
        yield client


async def a2a_client(librarian: Librarian, http: httpx.AsyncClient) -> Client:
    return await create_client(
        build_agent_card(librarian), client_config=ClientConfig(httpx_client=http, streaming=False)
    )


async def send(
    librarian: Librarian,
    http: httpx.AsyncClient,
    parts: list[Part],
    metadata: dict[str, Any] | None = None,
) -> Outcome:
    client = await a2a_client(librarian, http)
    message = Message(role=Role.ROLE_USER, message_id=uuid.uuid4().hex, parts=parts)
    request = SendMessageRequest(message=message)
    if metadata:
        request.metadata.CopyFrom(ParseDict(metadata, Struct()))
    outcome = Outcome()
    async for response in client.send_message(request):
        _collect(response, outcome)
    return outcome


def _collect(response: StreamResponse, outcome: Outcome) -> None:
    if response.HasField("task"):
        task = response.task
        outcome.task_id = task.id
        outcome.states.append(task.status.state)
        for artifact in task.artifacts:
            outcome.data.extend(get_data_parts(artifact.parts))
        if task.status.HasField("message"):
            outcome.texts.append(get_message_text(task.status.message))
    if response.HasField("status_update"):
        outcome.task_id = response.status_update.task_id
        status = response.status_update.status
        outcome.states.append(status.state)
        if status.HasField("message"):
            outcome.texts.append(get_message_text(status.message))
    if response.HasField("artifact_update"):
        outcome.data.extend(get_data_parts(response.artifact_update.artifact.parts))
    if response.HasField("message"):
        outcome.texts.append(get_message_text(response.message))


async def test_agent_card_lists_skills(http: httpx.AsyncClient) -> None:
    response = await http.get("/.well-known/agent-card.json")
    assert response.status_code == 200
    card = response.json()
    skills = {s["id"] for s in card["skills"]}
    assert {"execute", "capabilities", "onboard"} <= skills
    assert "fetch" not in skills  # no model configured
    assert "bearer" in card["securitySchemes"]


async def test_unauthenticated_calls_are_rejected(
    librarian: Librarian, http: httpx.AsyncClient
) -> None:
    response = await http.post("/", json={}, headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


async def test_execute_end_to_end(
    librarian: Librarian, http: httpx.AsyncClient, router: Router, secrets_store: FileSecretStore
) -> None:
    await secrets_store.put("tracker/readonly", "tok")
    router.add(
        "GET", f"{BASE}/v1/issues/ABC-1", lambda r: httpx.Response(200, json={"key": "ABC-1"})
    )
    outcome = await send(
        librarian,
        http,
        [
            new_data_part(
                {
                    "skill": "execute",
                    "api": "tracker",
                    "operation": "getIssue",
                    "args": {"key": "ABC-1"},
                }
            )
        ],
        metadata={"principal": "kim"},
    )
    assert outcome.final == TaskState.TASK_STATE_COMPLETED, outcome.texts
    result = outcome.data[-1]
    assert result["data"] == {"key": "ABC-1"}
    assert result["provenance"]["caller"] == "agent-a"
    assert result["provenance"]["principal"] == "kim"


async def test_capabilities(librarian: Librarian, http: httpx.AsyncClient) -> None:
    outcome = await send(librarian, http, [new_data_part({"skill": "capabilities"})])
    apis = {a["api"] for a in outcome.data[-1]["apis"]}
    assert apis == {"tracker", "weather", "status"}


async def test_write_is_rejected(librarian: Librarian, http: httpx.AsyncClient) -> None:
    outcome = await send(
        librarian,
        http,
        [
            new_data_part(
                {
                    "skill": "execute",
                    "api": "tracker",
                    "operation": "deleteIssue",
                    "args": {"key": "A"},
                }
            )
        ],
    )
    assert outcome.final == TaskState.TASK_STATE_REJECTED
    assert any("not a read" in t for t in outcome.texts)


async def test_text_without_model_fails_cleanly(
    librarian: Librarian, http: httpx.AsyncClient
) -> None:
    outcome = await send(librarian, http, [new_text_part("get me the open bugs")])
    assert outcome.final == TaskState.TASK_STATE_FAILED
    assert any("no model is configured" in t for t in outcome.texts)


async def test_missing_credential_waits_then_completes(
    librarian: Librarian, http: httpx.AsyncClient, router: Router
) -> None:
    router.add("GET", f"{BASE}/v1/me", lambda r: httpx.Response(200, json={"me": "bot"}))

    async def approver() -> None:
        while (pending := librarian.broker.pending_for("tracker")) is None:
            await asyncio.sleep(0.01)
        assert await librarian.broker.provide_value(pending.id, "granted", "sam") is None

    helper = asyncio.create_task(approver())
    outcome = await send(
        librarian,
        http,
        [new_data_part({"skill": "execute", "api": "tracker", "operation": "whoami"})],
    )
    # A blocking send returns at the interrupted AUTH_REQUIRED state; the task keeps
    # running out of band and the caller polls (or subscribes) for the result.
    assert outcome.final == TaskState.TASK_STATE_AUTH_REQUIRED
    assert any("needs a credential" in t for t in outcome.texts)
    await helper
    client = await a2a_client(librarian, http)
    task = await client.get_task(GetTaskRequest(id=outcome.task_id))
    for _ in range(200):
        if task.status.state == TaskState.TASK_STATE_COMPLETED:
            break
        await asyncio.sleep(0.02)
        task = await client.get_task(GetTaskRequest(id=outcome.task_id))
    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    data = [d for a in task.artifacts for d in get_data_parts(a.parts)]
    assert data[-1]["data"] == {"me": "bot"}


def test_json_safe_preserves_large_integers() -> None:
    from librarian.server import json_safe

    data = {"id": 1234567890123456789, "small": 5, "flag": True, "items": [2**60, "x"]}
    assert json_safe(data) == {
        "id": "1234567890123456789",
        "small": 5,
        "flag": True,
        "items": [str(2**60), "x"],
    }


async def test_caller_profiles_limit_capabilities_and_onboarding(librarian: Librarian) -> None:
    from librarian.config import CallerKey

    librarian.settings.callers = [
        CallerKey(
            agent="agent-a",
            key_ref="librarian/callers/agent-a",
            apis=["weather"],
            can_onboard=False,
        )
    ]
    app = create_app(librarian, caller_keys=KEYS)
    app.state.caller_keys.update(KEYS)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=librarian.settings.public_url,
        headers={"Authorization": "Bearer key-a"},
    ) as http:
        caps = await send(librarian, http, [new_data_part({"skill": "capabilities"})])
        assert [a["api"] for a in caps.data[-1]["apis"]] == ["weather"]
        denied = await send(
            librarian,
            http,
            [new_data_part({"skill": "execute", "api": "tracker", "operation": "whoami"})],
        )
        assert denied.final == TaskState.TASK_STATE_REJECTED
        assert any("not allowed to use API" in t for t in denied.texts)
        onboard = await send(
            librarian,
            http,
            [new_data_part({"skill": "onboard", "api": "x", "base_url": "https://x.example"})],
        )
        assert onboard.final == TaskState.TASK_STATE_FAILED
        assert any("not allowed to onboard" in t for t in onboard.texts)


async def test_execute_select_over_a2a(
    librarian: Librarian, http: httpx.AsyncClient, router: Router, secrets_store: FileSecretStore
) -> None:
    await secrets_store.put("tracker/readonly", "tok")
    router.add(
        "GET",
        f"{BASE}/v1/issues/ABC-1",
        lambda r: httpx.Response(200, json={"key": "ABC-1", "fields": {"summary": "s", "x": 1}}),
    )
    outcome = await send(
        librarian,
        http,
        [
            new_data_part(
                {
                    "skill": "execute",
                    "api": "tracker",
                    "operation": "getIssue",
                    "args": {"key": "ABC-1"},
                    "select": ["key", "fields.summary"],
                }
            )
        ],
    )
    assert outcome.data[-1]["data"] == {"key": "ABC-1", "fields": {"summary": "s"}}


async def test_write_end_to_end_over_a2a(
    librarian: Librarian, router: Router, secrets_store: FileSecretStore
) -> None:
    from librarian.config import CallerKey

    await secrets_store.put("tracker/write", "write-key")
    router.add(
        "POST", f"{BASE}/v1/issues/PAY-1/comments", lambda r: httpx.Response(201, json={"id": 9})
    )
    librarian.settings.callers = [
        CallerKey(
            agent="agent-a",
            key_ref="librarian/callers/agent-a",
            write_apis=["tracker"],
            owner="ops@x.test",
        )
    ]
    app = create_app(librarian, caller_keys=KEYS)
    app.state.caller_keys.update(KEYS)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=librarian.settings.public_url,
        headers={"Authorization": "Bearer key-a"},
    ) as http:
        outcome = await send(
            librarian,
            http,
            [
                new_data_part(
                    {
                        "skill": "write",
                        "api": "tracker",
                        "operation": "addComment",
                        "args": {"key": "PAY-1"},
                        "body": {"body": "Fixed in 2.3"},
                        "reason": "close the loop with the customer",
                    }
                )
            ],
        )
        assert outcome.final == TaskState.TASK_STATE_AUTH_REQUIRED
        assert any("waiting for human approval" in t for t in outcome.texts)
        approval = next(iter(librarian.approvals._approvals.values()))  # pyright: ignore[reportPrivateUsage]
        assert approval.requesters == ["ops@x.test"]  # the owner stands in
        await librarian.approvals.approve(approval.id, "ops@x.test")

        client = await a2a_client(librarian, http)
        task = await client.get_task(GetTaskRequest(id=outcome.task_id))
        for _ in range(200):
            if task.status.state == TaskState.TASK_STATE_COMPLETED:
                break
            await asyncio.sleep(0.02)
            task = await client.get_task(GetTaskRequest(id=outcome.task_id))
        assert task.status.state == TaskState.TASK_STATE_COMPLETED
        data = [d for a in task.artifacts for d in get_data_parts(a.parts)][-1]
        assert data["how"] == "approved" and data["result"]["data"] == {"id": 9}

        denied = await send(
            librarian,
            http,
            [
                new_data_part(
                    {
                        "skill": "write",
                        "api": "tracker",
                        "operation": "renameProject",
                        "args": {"p": "x"},
                    }
                )
            ],
        )
        assert denied.final == TaskState.TASK_STATE_REJECTED
