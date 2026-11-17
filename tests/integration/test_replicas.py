"""Two replicas on Postgres + AWS Secrets Manager (LocalStack), against go-httpbin.

Run:  docker compose -p libint -f tests/integration/replicas/docker-compose.yaml up -d --build
      LIBRARIAN_INTEGRATION=1 uv run pytest tests/integration/test_replicas.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from tests.integration.conftest import wait_until_up

pytestmark = pytest.mark.skipif(
    not os.environ.get("LIBRARIAN_INTEGRATION"), reason="set LIBRARIAN_INTEGRATION=1"
)


@pytest.fixture(scope="module", autouse=True)
def stack_up() -> None:
    wait_until_up("http://localhost:18101/healthz", "http://localhost:18102/healthz")


A, B = "http://localhost:18101", "http://localhost:18102"
COMPOSE = Path(__file__).parent / "replicas" / "docker-compose.yaml"
HEADERS = {"A2A-Version": "1.0", "Authorization": "Bearer key-a"}


def rpc(base: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
    body = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": params}
    response = httpx.post(f"{base}/", json=body, headers=HEADERS, timeout=60)
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    assert "error" not in data, data
    return data["result"]


def send(base: str, command: dict[str, Any], principal: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {
        "message": {
            "messageId": uuid.uuid4().hex,
            "role": "ROLE_USER",
            "parts": [{"data": command}],
        }
    }
    if principal:
        params["metadata"] = {"principal": principal}
    return rpc(base, "SendMessage", params)["task"]


def wait_task(base: str, task_id: str, state: str = "TASK_STATE_COMPLETED") -> dict[str, Any]:
    deadline = time.monotonic() + 60
    task: dict[str, Any] = {}
    while time.monotonic() < deadline:
        task = rpc(base, "GetTask", {"id": task_id})
        if task["status"]["state"] == state:
            return task
        time.sleep(0.5)
    raise AssertionError(f"task {task_id} stuck in {task.get('status')}")


def logged_link(service: str, kind: str) -> str:
    """The newest approval link the log notifier printed (what Slack would receive)."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        logs = subprocess.run(
            ["docker", "compose", "-p", "libint", "-f", str(COMPOSE), "logs", service],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        links = re.findall(rf"(http://\S+/{kind}/[0-9a-f]+\?t=[0-9a-f]+)", logs)
        if links:
            return links[-1]
        time.sleep(0.5)
    raise AssertionError(f"no {kind} link in {service} logs")


def on(base: str, link: str) -> str:
    """The same link, opened on another replica."""
    return re.sub(r"^http://[^/]+", base, link)


def artifact(task: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = task["artifacts"][-1]["parts"][0]["data"]
    return data


def secrets_manager() -> Any:
    import boto3  # pyright: ignore[reportMissingTypeStubs]

    return boto3.client(  # pyright: ignore[reportUnknownMemberType]
        "secretsmanager",
        endpoint_url="http://localhost:14566",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


def test_both_replicas_serve_the_same_registry() -> None:
    for base in (A, B):
        assert httpx.get(f"{base}/healthz").json()["apis"] == 1
        card = httpx.get(f"{base}/.well-known/agent-card.json").json()
        assert {s["id"] for s in card["skills"]} >= {"execute", "write"}


def test_credential_requested_on_a_is_provided_on_b_and_lands_in_aws() -> None:
    task = send(
        A, {"skill": "execute", "api": "echo", "operation": "getAnything", "args": {"key": "PAY-1"}}
    )
    assert task["status"]["state"] == "TASK_STATE_AUTH_REQUIRED"
    link = logged_link("librarian-a", "credentials")

    # The approver's browser lands on replica B (shared signing key + database).
    page = httpx.get(on(B, link))
    assert page.status_code == 200, page.text
    request_id, token = re.search(r"/credentials/([0-9a-f]+)\?t=(\w+)", link).groups()  # type: ignore[union-attr]
    done = httpx.post(
        f"{B}/credentials/{request_id}",
        data={"t": token, "action": "add", "value": "read-secret-1"},
    )
    assert "connected" in done.text, done.text
    assert "tested OK" in done.text  # validated with GET /bearer through the real API

    # The task waiting on replica A resumes; replica B can see it too (shared task store).
    finished = wait_task(A, task["id"])
    echoed = artifact(finished)["data"]
    # The test API answers with the request it received. librarian removes the key it
    # sent from the answer, so the agent never sees it.
    assert echoed["headers"]["Authorization"] == ["Bearer [redacted]"]
    assert "read-secret-1" not in json.dumps(finished)
    assert wait_task(B, task["id"])["id"] == task["id"]

    stored = secrets_manager().get_secret_value(SecretId="lib/echo/read")["SecretString"]
    assert stored == "read-secret-1"


def test_write_requested_on_a_is_approved_on_b_with_json_key_secret() -> None:
    task = send(
        A,
        {
            "skill": "write",
            "api": "echo",
            "operation": "postComment",
            "args": {"key": "PAY-1"},
            "body": {"body": "hello"},
            "reason": "integration test",
        },
    )
    assert task["status"]["state"] == "TASK_STATE_AUTH_REQUIRED"
    link = logged_link("librarian-a", "approvals")
    approval_id, token = re.search(r"/approvals/([0-9a-f]+)\?t=(\w+)", link).groups()  # type: ignore[union-attr]
    page = httpx.get(on(B, link))
    assert page.status_code == 200 and "postComment" in page.text
    approved = httpx.post(f"{B}/approvals/{approval_id}", data={"t": token, "action": "once"})
    assert "Approved" in approved.text, approved.text

    finished = wait_task(A, task["id"])
    result = artifact(finished)["result"]
    echoed = result["data"]
    assert echoed["method"] == "POST"
    assert json.loads(echoed["data"]) == {"body": "hello"}
    # The write key came from one key of a JSON secret: service/echo#write_token. It is
    # five characters long, too short for librarian to remove from an answer (it would
    # mangle ordinary data), which is what lets this test see which value was sent.
    assert echoed["headers"]["Authorization"] == ["Bearer w-123"]


def plan_steps() -> list[dict[str, Any]]:
    """Three writes. The second sends what the first answered; the third goes where the
    second's answer says (the test API answers with the request it received)."""
    return [
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
        {
            "id": "third",
            "operation": "postComment",
            "args": {"key": {"$from": "second.json.after"}},
            "body": {"body": "three"},
        },
    ]


async def tool(base: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """One MCP tool call to one replica, as the agent with the key `key-a`."""
    http = httpx2.AsyncClient(headers={"Authorization": "Bearer key-a"}, timeout=60)
    async with (
        http,
        streamable_http_client(f"{base}/mcp", http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.call_tool(name, arguments)
        assert not result.is_error, result.content
        return result.structured_content or {}


def decide(base: str, link: str, action: str = "once") -> httpx.Response:
    """What an approver does with a link: press a button on the page it opens."""
    approval_id, token = re.search(r"/approvals/([0-9a-f]+)\?t=(\w+)", link).groups()  # type: ignore[union-attr]
    return httpx.post(f"{base}/approvals/{approval_id}", data={"t": token, "action": action})


def check_plan_made(steps: list[dict[str, Any]]) -> None:
    assert [(step["id"], step["state"]) for step in steps] == [
        ("first", "made"),
        ("second", "made"),
        ("third", "made"),
    ]
    sent = [step["result"]["data"] for step in steps]
    assert [answer["url"].rsplit("/", 1)[-1] for answer in sent] == ["PAY-1", "PAY-2", "one"]
    assert [json.loads(answer["data"]) for answer in sent] == [
        {"body": "one"},
        {"body": "two", "after": "one"},
        {"body": "three"},
    ]


def test_a_plan_asked_for_on_a_is_approved_on_b_and_made_in_order() -> None:
    task = send(
        A,
        {"skill": "plan", "api": "echo", "reason": "integration test", "steps": plan_steps()},
    )
    assert task["status"]["state"] == "TASK_STATE_AUTH_REQUIRED"
    link = logged_link("librarian-a", "approvals")
    page = httpx.get(on(B, link))  # the other replica shows the plan, every request of it
    assert page.status_code == 200 and "Approve a plan for Echo" in page.text
    assert "<code>/anything/comments/PAY-1</code>" in page.text
    assert "<code>/anything/comments/{key}</code>" in page.text  # where it goes is not known yet
    assert "from step <code>second</code>" in page.text
    refused = decide(B, link, "grant")
    assert refused.status_code == 400 and "never as a grant" in refused.text
    approved = decide(B, link)
    assert "The 3 writes are being made now, in order." in approved.text, approved.text

    made = artifact(wait_task(A, task["id"]))
    assert (
        made["message"] == "Approved by link holder as one plan; all 3 writes were made, in order."
    )
    check_plan_made(made["steps"])


async def test_a_plan_over_mcp_reads_the_same_on_both_replicas() -> None:
    """Nothing waits on a person over MCP: librarian makes the plan itself once approved.
    Asked how it stands, the replica that makes it and the one that only reads the record
    say the same."""
    asked = await tool(
        A, "write_plan", {"api": "echo", "reason": "integration test", "steps": plan_steps()}
    )
    assert asked["status"] == "pending_approval", asked
    assert [step["state"] for step in asked["steps"]] == ["waiting"] * 3
    link = logged_link("librarian-a", "approvals")
    approval_id: str = asked["approval"]
    assert f"/approvals/{approval_id}?" in link
    approved = decide(B, link)
    assert "The 3 writes are being made now, in order." in approved.text, approved.text

    seen: dict[str, dict[str, Any]] = {}
    for _ in range(120):
        for base in (A, B):
            seen[base] = await tool(base, "write_status", {"approval": approval_id})
            assert len(seen[base]["steps"]) == 3
        if all(status["state"] == "executed" for status in seen.values()):
            break
        await asyncio.sleep(0.25)
    assert seen[A]["state"] == seen[B]["state"] == "executed", seen
    check_plan_made(seen[A]["steps"])
    assert seen[A]["steps"] == seen[B]["steps"]


async def test_an_agent_waiting_on_one_replica_has_the_outcome_the_other_made() -> None:
    """Asked for on A, which also makes it once approved. The agent's next call lands on B
    and waits there (`wait_seconds`): B learns of the decision and of the result only from
    the database, and answers when the write has ended, not when its time is up."""
    asked = await tool(
        A,
        "write",
        {
            "api": "echo",
            "operation": "postComment",
            "args": {"key": "PAY-9"},
            "body": {"body": "waited for"},
            "reason": "integration test",
        },
    )
    assert asked["status"] == "pending_approval", asked
    approval_id: str = asked["approval"]
    asking = {"approval": approval_id, "wait_seconds": 40}
    waiting = asyncio.create_task(tool(B, "write_status", asking))
    await asyncio.sleep(1.5)
    assert not waiting.done()  # nobody has decided yet
    link = ""
    for _ in range(40):
        link = logged_link("librarian-a", "approvals")
        if f"/approvals/{approval_id}?" in link:
            break
        await asyncio.sleep(0.5)
    started = time.monotonic()
    assert "Approved" in decide(A, link).text
    status = await asyncio.wait_for(waiting, 30)
    assert status["state"] == "executed", status
    assert json.loads(status["result"]["data"]["data"]) == {"body": "waited for"}
    assert time.monotonic() - started < 15


def test_pending_request_and_its_link_survive_a_restart() -> None:
    sm = secrets_manager()
    sm.delete_secret(SecretId="lib/echo/read", ForceDeleteWithoutRecovery=True)
    time.sleep(2)  # librarian caches secret values for 1s in this stack
    task = send(
        A, {"skill": "execute", "api": "echo", "operation": "getAnything", "args": {"key": "PAY-2"}}
    )
    assert task["status"]["state"] == "TASK_STATE_AUTH_REQUIRED"
    link = logged_link("librarian-a", "credentials")
    subprocess.run(
        ["docker", "compose", "-p", "libint", "-f", str(COMPOSE), "restart", "librarian-a"],
        check=True,
        capture_output=True,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{A}/healthz", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(1)
    assert httpx.get(link).status_code == 200  # same link, after the restart


def test_an_api_learned_on_a_is_usable_on_b_at_once() -> None:
    """No shared volume: the learned entry travels through Postgres."""
    learned = send(
        A,
        {
            "skill": "onboard",
            "api": "apisguru",
            "spec_url": "https://api.apis.guru/v2/openapi.yaml",
        },
    )
    assert learned["status"]["state"] == "TASK_STATE_COMPLETED", learned["status"]
    read = send(
        B, {"skill": "execute", "api": "apisguru", "operation": "getMetrics", "select": ["numAPIs"]}
    )
    assert read["status"]["state"] == "TASK_STATE_COMPLETED", read["status"]
    assert artifact(read)["data"]["numAPIs"] > 0
    assert httpx.get(f"{B}/healthz").json()["apis"] == 2  # B lists it now too


def test_both_replicas_survive_a_database_restart() -> None:
    """Like a writer failover: pooled connections die; requests must not fail."""
    for base in (A, B):  # make sure both replicas hold pooled connections
        assert send(base, {"skill": "capabilities"})["status"]["state"] == "TASK_STATE_COMPLETED"
    subprocess.run(
        ["docker", "compose", "-p", "libint", "-f", str(COMPOSE), "restart", "postgres"],
        check=True,
        capture_output=True,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        ready = subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                "libint",
                "-f",
                str(COMPOSE),
                "exec",
                "-T",
                "postgres",
                "pg_isready",
                "-U",
                "lib",
            ],
            capture_output=True,
        )
        if ready.returncode == 0:
            break
        time.sleep(1)
    for base in (A, B):  # the first request after the restart, on each replica
        task = send(base, {"skill": "capabilities"})
        assert task["status"]["state"] == "TASK_STATE_COMPLETED", task["status"]


def test_one_rate_limit_across_replicas_and_shared_activity() -> None:
    """Last on purpose: it uses up agent-a's read allowance on echo for this minute.
    The limit is 60/min; counted per replica, 130 reads split across two would all pass."""
    # An earlier test deleted the read key to leave a request pending; put one back.
    secrets_manager().create_secret(Name="lib/echo/read", SecretString="read-secret-2")
    time.sleep(2)  # the 1s value cache
    accepted, refused = 0, None
    for i in range(130):  # a fixed window can roll over once during the burst, hence 130
        task = send(
            A if i % 2 else B,
            {"skill": "execute", "api": "echo", "operation": "getAnything", "args": {"key": "RL"}},
        )
        state = task["status"]["state"]
        if state == "TASK_STATE_REJECTED":
            refused = task
            break
        assert state == "TASK_STATE_COMPLETED", task["status"]
        accepted += 1
    assert refused is not None, "130 reads across two replicas all passed: limits are per replica"
    assert accepted <= 120, accepted
    assert "rate limit exceeded" in json.dumps(refused["status"])

    # The audit copy in the database is shared: replica B shows what both did.
    page = httpx.get(f"{B}/activity?api=echo")
    assert page.status_code == 200, page.text
    assert "rate limit exceeded" in page.text and "getAnything" in page.text
    overview = httpx.get(f"{A}/")
    assert "Last 7 days" in overview.text and "<em>denied</em>" in overview.text
