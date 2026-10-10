"""The MCP endpoint, exercised with the official MCP client against a real server."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import httpx
import httpx2
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from librarian.approvals import ApprovalState
from librarian.config import CallerKey
from librarian.secrets import FileSecretStore
from librarian.server import create_app
from librarian.service import Librarian
from tests.conftest import BASE, Router

KEYS = {"agent-a": "key-a"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
async def server(librarian: Librarian) -> AsyncGenerator[str]:
    librarian.settings.callers = [
        CallerKey(
            agent="agent-a",
            key_ref="librarian/callers/agent-a",
            apis=["tracker", "weather", "status"],
            write_apis=["tracker"],
            owner="ops@x.test",
        )
    ]
    app = create_app(librarian, caller_keys=KEYS)
    port = free_port()
    instance = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(instance.serve())
    while not instance.started:
        await asyncio.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    instance.should_exit = True
    await task


Connect = Callable[..., AbstractAsyncContextManager[ClientSession]]


@pytest.fixture
def connect(server: str) -> Connect:
    @asynccontextmanager
    async def open_session(key: str = "key-a") -> AsyncGenerator[ClientSession]:
        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=30)
        async with (
            http,
            streamable_http_client(f"{server}/mcp", http_client=http) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            yield session

    return open_session


def data(result: Any) -> dict[str, Any]:
    assert not result.is_error, result.content
    structured: dict[str, Any] = result.structured_content
    return structured


async def test_tools_are_listed_with_hints(connect: Connect) -> None:
    async with connect() as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}
    assert {"capabilities", "execute", "write", "write_status", "onboard"} <= set(tools)
    assert "fetch" not in tools  # no planner model configured
    execute, write = tools["execute"].annotations, tools["write"].annotations
    assert execute is not None and execute.read_only_hint is True
    assert write is not None and write.destructive_hint is True


async def test_unauthenticated_clients_are_refused(server: str) -> None:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{server}/mcp", json={}, headers={"Authorization": "Bearer wrong"}
        )
    assert response.status_code == 401


async def test_capabilities_respects_the_callers_allowlist(connect: Connect) -> None:
    async with connect() as session:
        apis = data(await session.call_tool("capabilities", {}))["apis"]
    assert {a["api"] for a in apis} == {"tracker", "weather", "status"}


async def test_execute_with_select(
    connect: Connect, router: Router, secrets_store: FileSecretStore
) -> None:
    await secrets_store.put("tracker/readonly", "tok")
    router.add(
        "GET",
        f"{BASE}/v1/issues/ABC-1",
        lambda r: httpx.Response(200, json={"key": "ABC-1", "fields": {"summary": "s", "x": 1}}),
    )
    async with connect() as session:
        result = data(
            await session.call_tool(
                "execute",
                {
                    "api": "tracker",
                    "operation": "getIssue",
                    "args": {"key": "ABC-1"},
                    "select": ["key", "fields.summary"],
                },
            )
        )
    assert result["status"] == "ok"
    assert result["data"] == {"key": "ABC-1", "fields": {"summary": "s"}}
    assert result["provenance"]["caller"] == "agent-a"


async def test_policy_refusals_are_tool_errors(connect: Connect) -> None:
    async with connect() as session:
        result = await session.call_tool(
            "execute", {"api": "tracker", "operation": "deleteIssue", "args": {"key": "A"}}
        )
    assert result.is_error
    assert "not a read" in str(result.content)


async def test_missing_credential_returns_pending_without_waiting(
    connect: Connect, librarian: Librarian
) -> None:
    async with connect() as session:
        result = data(
            await asyncio.wait_for(
                session.call_tool("execute", {"api": "tracker", "operation": "whoami"}), 10
            )
        )
    assert result["status"] == "credential_pending"
    assert librarian.broker.pending_for("tracker") is not None  # approvers were notified


async def test_write_returns_pending_then_runs_after_approval(
    connect: Connect, librarian: Librarian, router: Router, secrets_store: FileSecretStore
) -> None:
    await secrets_store.put("tracker/write", "write-key")
    router.add(
        "POST", f"{BASE}/v1/issues/PAY-1/comments", lambda r: httpx.Response(201, json={"id": 5})
    )
    async with connect() as session:
        pending = data(
            await session.call_tool(
                "write",
                {
                    "api": "tracker",
                    "operation": "addComment",
                    "args": {"key": "PAY-1"},
                    "body": {"body": "via MCP"},
                    "reason": "mcp test",
                },
            )
        )
        assert pending["status"] == "pending_approval" and pending["state"] == "pending"
        # A human approves (on the approval page); librarian finishes the write itself.
        await librarian.approvals.approve(pending["approval"], "ops@x.test")
        status: dict[str, Any] = {}
        for _ in range(100):
            status = data(
                await session.call_tool("write_status", {"approval": pending["approval"]})
            )
            if status["state"] == ApprovalState.EXECUTED.value:
                break
            await asyncio.sleep(0.02)
    assert status["state"] == "executed"
    assert status["result"]["data"] == {"id": 5}
    sent = [r for r in router.requests if r.method == "POST"]
    assert len(sent) == 1 and sent[0].headers["authorization"] == "Bearer write-key"


async def test_write_status_is_private_to_the_requesting_agent(
    connect: Connect, librarian: Librarian, secrets_store: FileSecretStore
) -> None:
    await secrets_store.put("tracker/write", "write-key")
    async with connect() as session:
        pending = data(
            await session.call_tool(
                "write",
                {
                    "api": "tracker",
                    "operation": "addComment",
                    "args": {"key": "PAY-2"},
                    "body": {"body": "x"},
                },
            )
        )
    approval = await librarian.approvals.load(pending["approval"])
    assert approval is not None
    approval.agent = "someone-else"
    async with connect() as session:
        result = await session.call_tool("write_status", {"approval": pending["approval"]})
    assert result.is_error


async def test_mcp_accepts_requests_for_any_host_name(server: str) -> None:
    # Gateways and other pods reach librarian by service name, not "localhost".
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        },
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{server}/mcp",
            json=body,
            headers={
                "Authorization": "Bearer key-a",
                "Host": "librarian:8080",
                "Accept": "application/json, text/event-stream",
            },
        )
    assert response.status_code == 200, response.text
