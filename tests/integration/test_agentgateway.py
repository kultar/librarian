"""librarian behind agentgateway, as an MCP server and as an A2A agent.

Two ways a gateway is used, both against agentgateway v1.7:

- **An agent's own key passes through** (port 18600): the gateway routes and observes,
  librarian authenticates the agent as it would without a gateway.
- **The gateway signs people in** (port 18602): it checks each person's token (Keycloak),
  calls librarian with its own key, and passes the token on. librarian checks the token
  itself, so it knows the person with proof and not on the gateway's word.

Run:  docker compose -p libagw -f tests/integration/agentgateway/docker-compose.yaml up -d --build
      LIBRARIAN_INTEGRATION=1 uv run pytest tests/integration/test_agentgateway.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from librarian.mcp_server import sent_bytes
from tests.integration.conftest import wait_until_up

pytestmark = pytest.mark.skipif(
    not os.environ.get("LIBRARIAN_INTEGRATION"), reason="set LIBRARIAN_INTEGRATION=1"
)

GATEWAY = "http://localhost:18600"  # an agent's own key passes through
PEOPLE = "http://localhost:18602"  # the gateway signs people in
LIBRARIAN = "http://localhost:18601"
TOKEN_URL = "http://localhost:18680/realms/lib/protocol/openid-connect/token"
AGENT = {"Authorization": "Bearer key-agent"}
COMPOSE = Path(__file__).parent / "agentgateway" / "docker-compose.yaml"
TOOLS = {
    "capabilities",
    "enable_status",
    "execute",
    "onboard",
    "write",
    "write_plan",
    "write_status",
}


@pytest.fixture(scope="module", autouse=True)
def stack_up() -> None:
    wait_until_up(f"{LIBRARIAN}/healthz", "http://localhost:18680/realms/lib")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:  # the gateway has no health path of its own here
        try:
            if httpx.get(f"{GATEWAY}/.well-known/agent-card.json", timeout=3).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise TimeoutError("agentgateway did not come up")


def token_of(user: str, password: str) -> str:
    """What a person's client holds after signing in: an access token for librarian."""
    response = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "password",
            "client_id": "agent",
            "client_secret": "agent-secret",
            "username": user,
            "password": password,
            "scope": "openid email",
        },
    )
    response.raise_for_status()
    return str(response.json()["access_token"])


def link_for(approval: str) -> str:
    """The link librarian's log notifier printed for this approval (what Slack would get).
    By its id: the log can be a moment behind the answer, and the last link in it is then
    an earlier request's."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        logs = subprocess.run(
            ["docker", "compose", "-p", "libagw", "-f", str(COMPOSE), "logs", "librarian"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        links = re.findall(rf"(http://\S+/approvals/{approval}\?t=[0-9a-f]+)", logs)
        if links:
            return links[-1]
        time.sleep(0.5)
    raise AssertionError(f"no link for approval {approval} in librarian's log")


def page_at(link: str) -> str:
    """What an approver sees when they open a link."""
    return httpx.get(link).text


def approve(link: str) -> httpx.Response:
    """What an approver does with a link: press Approve on the page it opens."""
    approval, token = re.search(r"/approvals/([0-9a-f]+)\?t=(\w+)", link).groups()  # type: ignore[union-attr]
    return httpx.post(f"{LIBRARIAN}/approvals/{approval}", data={"t": token, "action": "once"})


@asynccontextmanager
async def mcp(base: str, headers: dict[str, str]) -> AsyncGenerator[ClientSession]:
    http = httpx2.AsyncClient(headers=headers, timeout=60)
    async with (
        http,
        streamable_http_client(f"{base}/mcp", http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


async def told(session: ClientSession, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await session.call_tool(tool, arguments)
    assert not result.is_error, result.content
    return result.structured_content or {}


async def outcome(session: ClientSession, approval: str) -> dict[str, Any]:
    status: dict[str, Any] = {}
    for _ in range(100):
        status = await told(session, "write_status", {"approval": approval})
        if status["state"] in ("executed", "failed"):
            break
        await asyncio.sleep(0.2)
    return status


def a2a(base: str, method: str, params: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    response = httpx.post(
        f"{base}/",
        json={"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": params},
        headers={**headers, "A2A-Version": "1.0"},
        timeout=60,
    )
    response.raise_for_status()
    body: dict[str, Any] = response.json()
    assert "error" not in body, body
    return body["result"]


def send(base: str, command: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    message = {"messageId": uuid.uuid4().hex, "role": "ROLE_USER", "parts": [{"data": command}]}
    return a2a(base, "SendMessage", {"message": message}, headers)["task"]


def given(task: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = task["artifacts"][-1]["parts"][0]["data"]
    return data


READ = {"api": "echo", "operation": "getAnything", "args": {"key": "PAY-1"}}


# -- an agent's own key passes through ---------------------------------------------------


async def test_mcp_through_the_gateway_with_the_agents_own_key() -> None:
    async with mcp(GATEWAY, AGENT) as session:
        tools = {tool.name for tool in (await session.list_tools()).tools}
        assert tools == TOOLS  # one server behind the route: the names are not prefixed

        read = await told(session, "execute", {**READ, "select": ["headers.Authorization"]})
        # The test API answers with the request it received: what librarian sent it was
        # its own key for that API, never the agent's, and the agent does not get to see it.
        assert read["data"] == {"headers": {"Authorization": ["Bearer [redacted]"]}}
        assert read["provenance"]["caller"] == "release-notes"

        asked = await told(
            session,
            "write",
            {
                "api": "echo",
                "operation": "postComment",
                "args": {"key": "PAY-1"},
                "body": {"body": "through agentgateway"},
                "reason": "integration",
            },
        )
        assert asked["status"] == "pending_approval"
        assert "Approved" in approve(link_for(asked["approval"])).text
        written = await outcome(session, asked["approval"])
        assert written["state"] == "executed"
        assert json.loads(written["result"]["data"]["data"]) == {"body": "through agentgateway"}

        plan = await told(
            session,
            "write_plan",
            {
                "api": "echo",
                "reason": "integration",
                "steps": [
                    {
                        "id": "first",
                        "operation": "postComment",
                        "args": {"key": "PAY-1"},
                        "body": {"body": "one"},
                    },
                    {
                        "id": "second",
                        "operation": "postComment",
                        "args": {"key": "PAY-2"},
                        "body": {"body": "two", "after": {"$from": "first.json.body"}},
                    },
                ],
            },
        )
        assert plan["status"] == "pending_approval"
        assert "The 2 writes are being made now" in approve(link_for(plan["approval"])).text
        made = await outcome(session, plan["approval"])
        assert [step["state"] for step in made["steps"]] == ["made", "made"]
        second = json.loads(made["steps"][1]["result"]["data"]["data"])
        assert second == {"body": "two", "after": "one"}


async def test_what_an_agent_may_use_is_asked_through_the_gateway() -> None:
    """`capabilities` behind the gateway: everything, one API by its id, and an API named
    another way, answered in words that reach the agent as they are."""
    async with mcp(GATEWAY, AGENT) as session:
        everything = await told(session, "capabilities", {})
        assert "echo" in {api["api"] for api in everything["apis"]} and "note" not in everything
        one = await told(session, "capabilities", {"api": "echo", "query": "comment"})
        assert [api["api"] for api in one["apis"]] == ["echo"]
        assert one["apis"][0]["write_operations"][0]["operation"] == "postComment"
        wrong = await session.call_tool("capabilities", {"api": "Echo"})
        assert wrong.is_error
        assert "Did you mean 'echo'?" in " ".join(getattr(p, "text", "") for p in wrong.content)


async def test_an_answer_too_large_for_the_gateway_is_answered_with_its_outline() -> None:
    """The gateway holds at most 2 MiB of an MCP server's answer, and over MCP an answer
    is twice the size of its data: handed over whole, 1.2 MB of rows end as "failed to
    send message". librarian does not hand over that much; it says what is there."""
    async with mcp(GATEWAY, AGENT) as session:
        large = await told(session, "execute", {"api": "big", "operation": "getRows"})
        assert (large["status"], large["data"]) == ("too_large", None)
        assert large["outline"] == {
            "rows": {"list of": 12000, "each": {"id": "number", "note": "text"}},
            "total": "number",
        }
        assert "`select`" in large["message"]
        asked = {"api": "big", "operation": "getRows", "select": ["rows.id", "total"]}
        ids = await told(session, "execute", asked)
        assert ids["status"] == "ok" and ids["data"]["total"] == 12000
        assert ids["data"]["rows"][:2] == [{"id": 0}, {"id": 1}]
        # What librarian does hand over arrives: a tool result of just under its limit
        # (1 MB as sent) passes the gateway and the MCP client, which stop at 2 and 1 MiB.
        most = await told(session, "execute", {"api": "big", "operation": "getMost"})
        assert most["status"] == "ok" and len(most["data"]["rows"]) == 3800
        assert 940_000 < sent_bytes(most) < 1_000_000


def test_a2a_through_the_gateway_with_the_agents_own_key() -> None:
    card = httpx.get(f"{GATEWAY}/.well-known/agent-card.json").json()
    # The gateway rewrites the card, so a client that reads it comes back through it.
    assert [i["url"] for i in card["supportedInterfaces"]] == [f"{GATEWAY}/"]
    assert "plan" in {skill["id"] for skill in card["skills"]}

    read = send(GATEWAY, {"skill": "execute", **READ, "select": ["url"]}, AGENT)
    assert read["status"]["state"] == "TASK_STATE_COMPLETED"
    assert given(read)["data"] == {"url": "http://httpbin:8080/anything/PAY-1"}
    assert given(read)["provenance"]["caller"] == "release-notes"

    write = send(
        GATEWAY,
        {
            "skill": "write",
            "api": "echo",
            "operation": "postComment",
            "args": {"key": "PAY-3"},
            "body": {"body": "over A2A"},
            "reason": "integration",
        },
        AGENT,
    )
    assert write["status"]["state"] == "TASK_STATE_AUTH_REQUIRED"
    waiting: str = write["status"]["message"]["parts"][1]["data"]["write_approval"]
    assert "Approved" in approve(link_for(waiting)).text
    task: dict[str, Any] = {}
    for _ in range(100):  # the task is asked for through the gateway too
        task = a2a(GATEWAY, "GetTask", {"id": write["id"]}, AGENT)
        if task["status"]["state"] == "TASK_STATE_COMPLETED":
            break
        time.sleep(0.2)
    assert task["status"]["state"] == "TASK_STATE_COMPLETED", task["status"]
    assert json.loads(given(task)["result"]["data"]["data"]) == {"body": "over A2A"}


def test_librarians_refusal_reaches_the_client_as_it_is() -> None:
    message = {
        "messageId": "m",
        "role": "ROLE_USER",
        "parts": [{"data": {"skill": "capabilities"}}],
    }
    body = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": message}}
    for headers in ({}, {"Authorization": "Bearer not-a-key"}):
        refused = httpx.post(f"{GATEWAY}/", json=body, headers={**headers, "A2A-Version": "1.0"})
        assert refused.status_code == 401


# -- the gateway signs people in ---------------------------------------------------------


def test_an_mcp_client_is_told_where_to_sign_in() -> None:
    challenge = httpx.post(
        f"{PEOPLE}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert challenge.status_code == 401
    # As the MCP specification has it: the refusal names the sign-in metadata.
    where = re.search(r'resource_metadata="([^"]+)"', challenge.headers["www-authenticate"])
    assert where is not None
    assert httpx.get(where.group(1)).json()["resource"] == f"{PEOPLE}/mcp"


async def test_the_gateway_signs_people_in_and_librarian_knows_them_with_proof() -> None:
    kim = {"Authorization": f"Bearer {token_of('kim', 'kim-pass')}"}
    async with mcp(PEOPLE, kim) as session:
        tools = {tool.name for tool in (await session.list_tools()).tools}
        assert tools == TOOLS - {"onboard"}  # a rule in the gateway: not for people
        with pytest.raises(MCPError):
            await session.call_tool("onboard", {"api": "x", "base_url": "https://example.com"})

        read = await told(session, "execute", {**READ, "select": ["url"]})
        who = read["provenance"]
        # The gateway called with its own key; the person is the one librarian verified.
        assert (who["caller"], who["principal"], who["principal_verified"]) == (
            "agentgateway",
            "kim@example.com",
            True,
        )

        asked = await told(
            session,
            "write",
            {
                "api": "echo",
                "operation": "postComment",
                "args": {"key": "PAY-4"},
                "body": {"body": "from kim"},
                "reason": "integration",
            },
        )
        link = link_for(asked["approval"])
        page = page_at(link)
        # The write is kim's: she is the requester who may approve it.
        assert "kim@example.com" in page and "agentgateway" in page
        assert "Approved" in approve(link).text
        assert (await outcome(session, asked["approval"]))["state"] == "executed"


def test_a2a_as_a_person_through_the_gateway() -> None:
    eve = {"Authorization": f"Bearer {token_of('eve', 'eve-pass')}"}
    read = send(PEOPLE, {"skill": "execute", **READ, "select": ["url"]}, eve)
    who = given(read)["provenance"]
    assert (who["caller"], who["principal"], who["principal_verified"]) == (
        "agentgateway",
        "eve@example.com",
        True,
    )
    message = {
        "messageId": "m",
        "role": "ROLE_USER",
        "parts": [{"data": {"skill": "capabilities"}}],
    }
    body = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": message}}
    nobody = httpx.post(f"{PEOPLE}/", json=body, headers={"A2A-Version": "1.0"})
    assert nobody.status_code == 401  # refused by the gateway, before librarian is reached


def test_a_person_cannot_name_somebody_else() -> None:
    """The gateway sets the header that carries the person's token; whatever a client puts
    there is replaced. And librarian checks the token itself: reached directly with the
    gateway's key, a made-up token is refused."""
    kim = token_of("kim", "kim-pass")
    eve = token_of("eve", "eve-pass")
    as_kim = {"Authorization": f"Bearer {kim}", "X-Principal-Token": eve}
    read = send(PEOPLE, {"skill": "execute", **READ, "select": ["url"]}, as_kim)
    assert given(read)["provenance"]["principal"] == "kim@example.com"

    message = {
        "messageId": "m",
        "role": "ROLE_USER",
        "parts": [{"data": {"skill": "capabilities"}}],
    }
    body = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": message}}
    gateway_key = {"Authorization": "Bearer key-gateway", "A2A-Version": "1.0"}
    forged = kim[:-6] + "AAAAAA"  # the same claims, a signature that is not Keycloak's
    refused = httpx.post(
        f"{LIBRARIAN}/", json=body, headers={**gateway_key, "X-Principal-Token": forged}
    )
    assert refused.status_code == 401
