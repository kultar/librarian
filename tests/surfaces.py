"""The ways an agent reaches librarian, as test helpers.

Each helper runs one read or one approved write through a real protocol stack (the A2A
client against the app, the MCP client against a served app) and reports what the agent
ended up with in one common shape, so a test can make the same claims on every surface.
"""

from __future__ import annotations

import asyncio
import json
import socket
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import httpx2
import uvicorn
from a2a.client import ClientConfig, create_client
from a2a.helpers.proto_helpers import get_data_parts, get_message_text, new_data_part
from a2a.types import GetTaskRequest, Message, Role, SendMessageRequest, Task, TaskState
from google.protobuf.json_format import MessageToDict
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from librarian.approvals import ApprovalState
from librarian.config import CallerKey
from librarian.server import build_agent_card, create_app
from librarian.service import Librarian

KEYS = {"agent-a": "key-a"}
APPROVER = "ops@x.test"
READ = {"api": "tracker", "operation": "whoami"}
WRITE = {
    "api": "tracker",
    "operation": "addComment",
    "args": {"key": "PAY-1"},
    "body": {"body": "hello"},
    "reason": "a test",
}
# Three writes as one plan. The one in the middle is the one a test makes misbehave: the
# first answers as usual, and the last needs what the middle one answered.
PLAN: dict[str, Any] = {
    "api": "tracker",
    "reason": "a test",
    "steps": [
        {
            "id": "first",
            "operation": "addComment",
            "args": {"key": "PAY-0"},
            "body": {"body": "one"},
        },
        {
            "id": "middle",
            "operation": "addComment",
            "args": {"key": "PAY-1"},
            "body": {"body": "two", "after": {"$from": "first.id"}},
        },
        {
            "id": "last",
            "operation": "addComment",
            "args": {"key": "PAY-2"},
            "body": {"body": "three", "after": {"$from": "middle.id"}},
        },
    ],
}
A2A_STATES = {
    TaskState.TASK_STATE_COMPLETED: "completed",
    TaskState.TASK_STATE_FAILED: "failed",
    TaskState.TASK_STATE_REJECTED: "rejected",
    TaskState.TASK_STATE_AUTH_REQUIRED: "waiting",
}
DONE = {"completed", "failed", "rejected"}
ENDED = (ApprovalState.EXECUTED.value, ApprovalState.FAILED.value)


@dataclass
class Reply:
    """What the agent ended up with."""

    state: str  # completed | failed | rejected | waiting
    text: str  # what it was told in words, if anything
    data: Any  # the result it was given, if any
    raw: str  # everything it received, as text
    ref: str = ""  # the task (A2A) or approval (MCP) to ask about again


def allow_writes(librarian: Librarian, apis: tuple[str, ...] = ("tracker",)) -> None:
    """The one calling agent of these tests: may read everything and write to tracker."""
    librarian.settings.callers = [
        CallerKey(
            agent="agent-a",
            key_ref="librarian/callers/agent-a",
            write_apis=list(apis),
            owner=APPROVER,
        )
    ]


async def approve_the_write(librarian: Librarian) -> str:
    """Approve the write that is waiting for a person."""
    approvals = librarian.approvals._approvals  # pyright: ignore[reportPrivateUsage]
    for _ in range(500):
        if any(a.state is ApprovalState.PENDING for a in approvals.values()):
            break
        await asyncio.sleep(0.01)
    approval_id = next(a.id for a in approvals.values() if a.state is ApprovalState.PENDING)
    await librarian.approvals.approve(approval_id, APPROVER)
    return approval_id


# -- A2A ------------------------------------------------------------------------------


def _a2a_reply(task: Task) -> Reply:
    texts = [get_message_text(task.status.message)] if task.status.HasField("message") else []
    data = [part for artifact in task.artifacts for part in get_data_parts(artifact.parts)]
    return Reply(
        state=A2A_STATES.get(task.status.state, str(task.status.state)),
        text=" ".join(texts),
        data=data[-1] if data else None,
        raw=json.dumps(MessageToDict(task), default=str),
        ref=task.id,
    )


async def _a2a_exchange(
    librarian: Librarian, http: httpx.AsyncClient, command: dict[str, Any], *, approve: bool
) -> Reply:
    client = await create_client(
        build_agent_card(librarian),
        client_config=ClientConfig(httpx_client=http, streaming=False),
    )
    message = Message(
        role=Role.ROLE_USER, message_id=uuid.uuid4().hex, parts=[new_data_part(command)]
    )
    task: Task | None = None
    async for response in client.send_message(SendMessageRequest(message=message)):
        if response.HasField("task"):
            task = response.task
    assert task is not None, "the A2A call returned no task"
    if approve and A2A_STATES.get(task.status.state) == "waiting":
        await approve_the_write(librarian)
    for _ in range(500):
        if A2A_STATES.get(task.status.state) in DONE:
            break
        await asyncio.sleep(0.02)
        task = await client.get_task(GetTaskRequest(id=task.id))
    return _a2a_reply(task)


async def _a2a(librarian: Librarian, command: dict[str, Any], *, approve: bool) -> Reply:
    """In process: the app without a network in between (and without its start-up)."""
    allow_writes(librarian)
    app = create_app(librarian, caller_keys=KEYS)
    app.state.caller_keys.update(KEYS)  # the lifespan does this when served for real
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=librarian.settings.public_url,
        headers={"Authorization": "Bearer key-a"},
        timeout=20,
    ) as http:
        return await _a2a_exchange(librarian, http, command, approve=approve)


async def a2a_read(librarian: Librarian, **more: Any) -> Reply:
    return await _a2a(librarian, {"skill": "execute", **READ, **more}, approve=False)


async def a2a_write(librarian: Librarian) -> Reply:
    return await _a2a(librarian, {"skill": "write", **WRITE}, approve=True)


async def a2a_plan(librarian: Librarian) -> Reply:
    return await _a2a(librarian, {"skill": "plan", **PLAN}, approve=True)


def _over_http(base: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base, headers={"Authorization": "Bearer key-a"}, timeout=20)


async def a2a_served(librarian: Librarian, base: str, *, write: bool) -> Reply:
    """A read or an approved write over real HTTP, against a served librarian."""
    command = {"skill": "write", **WRITE} if write else {"skill": "execute", **READ}
    async with _over_http(base) as http:
        return await _a2a_exchange(librarian, http, command, approve=write)


async def a2a_task(librarian: Librarian, base: str, task_id: str) -> Reply:
    """A task as a served librarian has it now (another replica, later)."""
    async with _over_http(base) as http:
        client = await create_client(
            build_agent_card(librarian),
            client_config=ClientConfig(httpx_client=http, streaming=False),
        )
        return _a2a_reply(await client.get_task(GetTaskRequest(id=task_id)))


# -- served for real ------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@asynccontextmanager
async def served(
    librarian: Librarian, write_apis: tuple[str, ...] = ("tracker",)
) -> AsyncGenerator[str]:
    """The whole app on a real port, started and stopped as in production (so with its
    database and its task store set up, and closed afterwards). Yields its address."""
    allow_writes(librarian, write_apis)
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    librarian.settings.a2a_url = base  # where the agent card sends A2A clients
    app = create_app(librarian, caller_keys=KEYS)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    try:
        yield base
    finally:
        server.should_exit = True
        await serving


# -- MCP ------------------------------------------------------------------------------


@asynccontextmanager
async def mcp_at(base: str) -> AsyncGenerator[ClientSession]:
    http = httpx2.AsyncClient(headers={"Authorization": "Bearer key-a"}, timeout=30)
    async with (
        http,
        streamable_http_client(f"{base}/mcp", http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


@asynccontextmanager
async def mcp_session(librarian: Librarian) -> AsyncGenerator[ClientSession]:
    async with served(librarian) as base, mcp_at(base) as session:
        yield session


def _mcp_reply(result: Any, state: str | None = None) -> Reply:
    text = " ".join(getattr(part, "text", "") for part in result.content)
    if result.is_error:
        return Reply(state="failed", text=text, data=None, raw=text)
    structured: dict[str, Any] = result.structured_content or {}
    return Reply(state=state or "completed", text=text, data=structured, raw=json.dumps(structured))


async def mcp_read(librarian: Librarian, **more: Any) -> Reply:
    async with mcp_session(librarian) as session:
        return _mcp_reply(await session.call_tool("execute", {**READ, **more}))


async def mcp_write_in(
    librarian: Librarian,
    session: ClientSession,
    tool: str = "write",
    asking: dict[str, Any] | None = None,
) -> Reply:
    """Ask for a write (or, with `tool` and `asking`, a plan), approve it and wait for
    what became of it."""
    asked = await session.call_tool(tool, dict(WRITE if asking is None else asking))
    if asked.is_error or (asked.structured_content or {}).get("status") != "pending_approval":
        return _mcp_reply(asked)
    approval_id = await approve_the_write(librarian)
    status: Any = asked
    for _ in range(500):
        status = await session.call_tool("write_status", {"approval": approval_id})
        if status.is_error or (status.structured_content or {}).get("state") in ENDED:
            break
        await asyncio.sleep(0.02)
    final = (status.structured_content or {}).get("state")
    reply = _mcp_reply(status, "completed" if final == ApprovalState.EXECUTED.value else "failed")
    reply.ref = approval_id
    return reply


async def mcp_write(librarian: Librarian) -> Reply:
    async with mcp_session(librarian) as session:
        return await mcp_write_in(librarian, session)


async def mcp_plan(librarian: Librarian) -> Reply:
    async with mcp_session(librarian) as session:
        return await mcp_write_in(librarian, session, "write_plan", PLAN)
