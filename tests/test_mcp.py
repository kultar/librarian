"""The MCP endpoint, exercised with the official MCP client against a real server."""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from librarian.approvals import ApprovalState
from librarian.calls import Caller
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


async def test_capabilities_names_the_changes_an_agent_may_ask_for(
    connect: Connect, librarian: Librarian
) -> None:
    """An agent has to know which operation makes a change before it can ask for it.
    Where it may ask for writes it is told, with whether anybody has allowed writes through
    that operation yet. Where it may only read, writes are not mentioned."""
    async with connect() as session:
        apis = {a["api"]: a for a in data(await session.call_tool("capabilities", {}))["apis"]}
        found = data(await session.call_tool("capabilities", {"query": "add a comment"}))
    tracker = apis["tracker"]
    by_name = {op["operation"]: op for op in tracker["write_operations"]}
    assert by_name["addComment"]["enabled"] is True
    assert tracker["total_write_operations"] == len(tracker["write_operations"]) >= 1
    assert "addComment" not in {op["operation"] for op in tracker["read_operations"]}
    assert not {op["operation"] for op in tracker["read_operations"]} & set(by_name)
    for other in ("weather", "status"):  # no `write_apis` there: nothing about writes
        assert "write_operations" not in apis[other]
    # Looked for in words, the operation that writes is found like one that reads.
    hit = next(a for a in found["apis"] if a["api"] == "tracker")
    assert hit["write_operations"][0]["operation"] == "addComment"

    # Not enabled by anybody: named all the same, and said to be so.
    assert by_name["renameProject"]["enabled"] is False
    # An agent that may not write there is told nothing about writes.
    reader = librarian.capabilities(caller=Caller(agent="b"))
    assert all("write_operations" not in a for a in reader)


def said(result: Any) -> str:
    """What a refused tool call told the agent."""
    assert result.is_error
    return " ".join(getattr(part, "text", "") for part in result.content)


async def test_an_agent_is_told_what_an_operation_takes_and_does_not_have_to_guess(
    connect: Connect,
) -> None:
    """Shown the operations without their arguments, an agent guesses names until one
    fits. It is told them where the operations are listed, and again where it got one
    wrong; and an operation written another way is answered with the one that exists."""
    async with connect() as session:
        apis = {a["api"]: a for a in data(await session.call_tool("capabilities", {}))["apis"]}
        reads = {op["operation"]: op for op in apis["tracker"]["read_operations"]}
        writes = {op["operation"]: op for op in apis["tracker"]["write_operations"]}
        assert reads["getIssue"]["args"] == ["key"] and "body" not in reads["getIssue"]
        assert reads["listIssues"]["args"] == ["team?", "state?", "X-Trace?"]  # ?: may be left out
        assert reads["searchIssues"] == {
            "operation": "searchIssues", "summary": "", "args": [], "body": True
        }  # fmt: skip
        assert writes["addComment"]["args"] == ["key"] and writes["addComment"]["body"] is True

        # Words that fit the API and none of its operations: they are listed all the same.
        found = data(await session.call_tool("capabilities", {"query": "example tracker"}))
        hit = next(a for a in found["apis"] if a["api"] == "tracker")
        assert hit["read_operations"] and hit["write_operations"]

        wrong_name = {"api": "tracker", "operation": "addComment", "args": {"issue": "PAY-1"}}
        assert said(await session.call_tool("write", wrong_name)) == (
            "Error executing tool write: refused: unknown arguments for 'addComment': issue. "
            "It takes: key and a body."
        )
        left_out = {"api": "tracker", "operation": "getIssue"}
        assert said(await session.call_tool("execute", left_out)).endswith(
            "missing required argument 'key' for 'getIssue'. It takes: key."
        )
        # The ? is explained where there is one to explain.
        misnamed = {"api": "tracker", "operation": "listIssues", "args": {"squad": "payments"}}
        assert said(await session.call_tool("execute", misnamed)).endswith(
            "unknown arguments for 'listIssues': squad. It takes: team?, state?, X-Trace? "
            "(a ? marks one that may be left out)."
        )
        for tool, written, meant in (
            ("execute", "get_issue", "getIssue"),
            ("write", "add_comment", "addComment"),
            ("write", "ADDCOMMENT", "addComment"),
        ):
            answer = said(await session.call_tool(tool, {"api": "tracker", "operation": written}))
            assert f"unknown operation {written!r} on 'tracker'. Did you mean {meant!r}?" in answer
        # Nothing is suggested that the agent is not shown anyway: a write to one that reads.
        answer = said(
            await session.call_tool("execute", {"api": "tracker", "operation": "add_comment"})
        )
        assert answer.endswith("unknown operation 'add_comment' on 'tracker'")
        # ...and nothing for a name that is simply not there.
        answer = said(await session.call_tool("execute", {"api": "tracker", "operation": "nope"}))
        assert answer.endswith("unknown operation 'nope' on 'tracker'")


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


async def test_an_answer_too_large_for_one_tool_result_comes_back_as_its_outline(
    connect: Connect, librarian: Librarian, router: Router, secrets_store: FileSecretStore
) -> None:
    """Megabytes are no use to a model, and a gateway or an MCP client on the way refuses
    them with an error that says nothing. The agent is told what is there instead, and how
    to ask for less."""
    await secrets_store.put("tracker/readonly", "read-key")
    issues = [
        {"key": f"PAY-{n}", "fields": {"summary": "s" * 300, "status": "open"}} for n in range(400)
    ]
    router.add(
        "GET",
        f"{BASE}/v1/issues",
        lambda r: httpx.Response(200, json={"issues": issues, "total": 400}),
    )
    librarian.settings.mcp_max_result_bytes = 50_000
    call = {"api": "tracker", "operation": "listIssues", "args": {"team": "payments"}}
    async with connect() as session:
        large = data(await session.call_tool("execute", call))
        assert (large["status"], large["data"], large["status_code"]) == ("too_large", None, 200)
        assert large["outline"] == {
            "issues": {
                "list of": 400,
                "each": {
                    "key": "text",
                    "fields": {"summary": "text of 300 characters", "status": "text"},
                },
            },
            "total": "number",
        }
        assert "too large for one tool result" in large["message"]
        assert "the limit is 50,000" in large["message"]
        assert "`select`" in large["message"] and large["provenance"]["operation"] == "listIssues"
        # Asked for what it needs, it gets it.
        keys = data(await session.call_tool("execute", {**call, "select": ["issues.key", "total"]}))
        assert keys["status"] == "ok" and len(keys["data"]["issues"]) == 400
        assert keys["data"]["issues"][0] == {"key": "PAY-0"}
        # No limit: handed over whole, as before.
        librarian.settings.mcp_max_result_bytes = 0
        whole = data(await session.call_tool("execute", call))
        assert whole["status"] == "ok" and len(whole["data"]["issues"]) == 400


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


COMMENT = {
    "api": "tracker",
    "operation": "addComment",
    "args": {"key": "PAY-1"},
    "body": {"body": "via MCP"},
    "reason": "mcp test",
}


async def test_write_status_waits_for_the_outcome_when_asked_to(
    connect: Connect, librarian: Librarian, router: Router, secrets_store: FileSecretStore
) -> None:
    """An agent whose session nobody can wake (one on kagent) waits for the decision
    inside its turn: one call that answers when the write has ended."""
    await secrets_store.put("tracker/write", "write-key")
    router.add(
        "POST", f"{BASE}/v1/issues/PAY-1/comments", lambda r: httpx.Response(201, json={"id": 5})
    )
    async with connect() as session:
        pending = data(await session.call_tool("write", COMMENT))
        assert pending["status"] == "pending_approval"
        # The answer itself says how: an agent does not have to know librarian's manual.
        assert "with wait_seconds (up to 50) it waits for the decision" in pending["message"]
        asked = {"approval": pending["approval"], "wait_seconds": 20}
        waiting = asyncio.create_task(session.call_tool("write_status", asked))
        await asyncio.sleep(0.3)
        assert not waiting.done()  # nobody has decided: it waits
        started = time.monotonic()
        await librarian.approvals.approve(pending["approval"], "ops@x.test")
        status = data(await asyncio.wait_for(waiting, 10))
        assert status["state"] == "executed" and status["result"]["data"] == {"id": 5}
        assert time.monotonic() - started < 5  # it answered when the write ended, not later
        # Asked again, it answers at once: there is nothing left to wait for.
        started = time.monotonic()
        again = data(await session.call_tool("write_status", asked))
        assert again["state"] == "executed" and time.monotonic() - started < 2
    assert len([r for r in router.requests if r.method == "POST"]) == 1


async def test_write_status_waits_no_longer_than_it_was_asked_or_allowed_to(
    connect: Connect, librarian: Librarian, secrets_store: FileSecretStore
) -> None:
    await secrets_store.put("tracker/write", "write-key")
    async with connect() as session:
        pending = data(await session.call_tool("write", COMMENT))
        approval = pending["approval"]

        async def asked(seconds: int) -> tuple[str, float]:
            started = time.monotonic()
            answer = await asyncio.wait_for(
                session.call_tool("write_status", {"approval": approval, "wait_seconds": seconds}),
                8,
            )
            return data(answer)["state"], time.monotonic() - started

        try:
            state, took = await asked(1)
            assert state == "pending" and 0.9 < took < 4
            # However long an agent asks for, librarian waits no longer than it is set to...
            librarian.settings.mcp_max_wait_seconds = 1
            state, took = await asked(3600)
            assert state == "pending" and 0.9 < took < 4
            # ...and not at all when that is switched off.
            librarian.settings.mcp_max_wait_seconds = 0
            state, took = await asked(3600)
            assert state == "pending" and took < 0.9
            again = data(await session.call_tool("write", COMMENT))  # the same request
            assert again["approval"] == approval and "wait_seconds" not in again["message"]
            # A refusal ends the wait as an approval does.
            librarian.settings.mcp_max_wait_seconds = 50
            refused = asyncio.create_task(asked(20))
            await asyncio.sleep(0.3)
            await librarian.approvals.deny(approval, "ops@x.test")
            state, took = await asyncio.wait_for(refused, 10)
            assert state == "denied" and took < 5
        finally:  # whatever went wrong above, no request is left waiting in the server
            await librarian.approvals.expire(approval, "the test is over")


async def test_write_to_a_not_enabled_operation_asks_the_approvers(
    connect: Connect, librarian: Librarian
) -> None:
    async with connect() as session:
        result = data(
            await session.call_tool(
                "write",
                {
                    "api": "tracker",
                    "operation": "renameProject",
                    "args": {"p": "PAY"},
                    "reason": "the team was renamed",
                    "also_needs": ["nope", "getIssue"],
                },
            )
        )
        asked = librarian.enablement.pending()
        assert len(asked) == 1 and asked[0].reasons == ["the team was renamed"]
        assert result["status"] == "write_not_enabled" and result["enable_request"] == asked[0].id
        assert result["operations"] == ["renameProject"]
        assert "approvers have been asked" in result["message"]
        status = data(await session.call_tool("enable_status", {"enable_request": asked[0].id}))
        assert status["state"] == "pending" and status["operations"] == ["renameProject"]
        via_write = data(await session.call_tool("write_status", {"approval": asked[0].id}))
        assert via_write["state"] == "pending"
        nope = await session.call_tool("enable_status", {"enable_request": "nope"})
        assert nope.is_error


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


async def test_onboard_from_documentation_over_mcp(
    connect: Connect, librarian: Librarian, router: Router
) -> None:
    from librarian.docs import build_extractor
    from tests.test_learning import BASE_URL, DOCS, by_page, extractor_model

    html = {"content-type": "text/html"}
    from tests.test_learning import INDEX_HTML, REFERENCE, REFERENCE_HTML

    router.add("GET", DOCS, lambda r: httpx.Response(200, text=INDEX_HTML, headers=html))
    router.add("GET", REFERENCE, lambda r: httpx.Response(200, text=REFERENCE_HTML, headers=html))
    librarian.source_client_factory = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(router)
    )
    model, _ = extractor_model(by_page)
    librarian.extractor = build_extractor(model)
    librarian.settings.callers[0].apis.append("widgets")
    async with connect() as session:
        result = data(
            await session.call_tool(
                "onboard", {"api": "widgets", "docs_url": DOCS, "base_url": BASE_URL}
            )
        )
    assert result["learned_from"] == "docs"
    assert result["read_operations"] == 3


async def test_a_registry_that_cannot_be_written_to_is_said_in_words(
    connect: Connect, librarian: Librarian, router: Router, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a database, what librarian learns goes into its registry folder. Where that
    is mounted read-only (a ConfigMap, a checkout) there is nowhere to keep it: the agent
    is told so, with what to change, instead of an error with no reason."""
    from librarian.docs import build_extractor
    from tests.test_learning import (
        BASE_URL,
        DOCS,
        INDEX_HTML,
        REFERENCE,
        REFERENCE_HTML,
        by_page,
        extractor_model,
    )

    html = {"content-type": "text/html"}
    router.add("GET", DOCS, lambda r: httpx.Response(200, text=INDEX_HTML, headers=html))
    router.add("GET", REFERENCE, lambda r: httpx.Response(200, text=REFERENCE_HTML, headers=html))
    librarian.source_client_factory = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(router)
    )
    model, _ = extractor_model(by_page)
    librarian.extractor = build_extractor(model)
    librarian.settings.callers[0].apis.append("widgets")

    def read_only(self: Path, *args: Any, **kwargs: Any) -> int:
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(Path, "write_text", read_only)
    async with connect() as session:
        refused = await session.call_tool(
            "onboard", {"api": "widgets", "docs_url": DOCS, "base_url": BASE_URL}
        )
    assert refused.is_error
    said = " ".join(getattr(part, "text", "") for part in refused.content)
    assert "cannot save API 'widgets'" in said and "Read-only file system" in said
    assert "database_url" in said and "registry_overlay_dir" in said
    assert librarian.registry.get("widgets") is None
