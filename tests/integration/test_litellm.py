"""librarian behind the LiteLLM gateway, as an MCP server and as an A2A agent.

Run:  docker compose -p liblite -f tests/integration/litellm/docker-compose.yaml up -d --build
      LIBRARIAN_INTEGRATION=1 uv run pytest tests/integration/test_litellm.py -v
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

GATEWAY = "http://localhost:14000"
LIBRARIAN = "http://localhost:18301"
GATEWAY_KEY = {"x-litellm-api-key": "Bearer sk-master"}
COMPOSE = Path(__file__).parent / "litellm" / "docker-compose.yaml"


@pytest.fixture(scope="module", autouse=True)
def stack_up() -> None:
    wait_until_up(f"{LIBRARIAN}/healthz", f"{GATEWAY}/health/liveliness")


def approve_latest() -> None:
    """Approve the newest write the way an approver would (one-time link, token mode)."""
    logs = subprocess.run(
        ["docker", "compose", "-p", "liblite", "-f", str(COMPOSE), "logs", "librarian"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    link = re.findall(r"(http://\S+/approvals/[0-9a-f]+\?t=[0-9a-f]+)", logs)[-1]
    approval, token = re.search(r"/approvals/([0-9a-f]+)\?t=(\w+)", link).groups()  # type: ignore[union-attr]
    page = httpx.post(f"{LIBRARIAN}/approvals/{approval}", data={"t": token, "action": "once"})
    assert "Approved" in page.text


async def test_mcp_through_the_gateway() -> None:
    http = httpx2.AsyncClient(headers=GATEWAY_KEY, timeout=60)
    async with (
        http,
        streamable_http_client(f"{GATEWAY}/librarian/mcp", http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = {t.name for t in (await session.list_tools()).tools}
        assert {
            "librarian-capabilities",
            "librarian-execute",
            "librarian-write",
            "librarian-write_status",
        } <= tools

        read_result = await session.call_tool(
            "librarian-execute",
            {
                "api": "echo",
                "operation": "getAnything",
                "args": {"key": "PAY-1"},
                "select": ["headers.Authorization"],
            },
        )
        data: dict[str, Any] = read_result.structured_content  # type: ignore[assignment]
        assert data["status"] == "ok"
        assert data["data"] == {"headers": {"Authorization": ["Bearer read-1"]}}
        assert data["provenance"]["caller"] == "litellm-gateway"

        written = await session.call_tool(
            "librarian-write",
            {
                "api": "echo",
                "operation": "postComment",
                "args": {"key": "PAY-3"},
                "body": {"body": "via LiteLLM MCP"},
                "reason": "integration",
            },
        )
        pending: dict[str, Any] = written.structured_content  # type: ignore[assignment]
        assert pending["status"] == "pending_approval"
        approve_latest()
        status: dict[str, Any] = {}
        for _ in range(100):
            result = await session.call_tool(
                "librarian-write_status", {"approval": pending["approval"]}
            )
            status = result.structured_content  # type: ignore[assignment]
            if status["state"] == "executed":
                break
            await asyncio.sleep(0.2)
        assert status["state"] == "executed"
        echoed = status["result"]["data"]
        assert echoed["headers"]["Authorization"] == ["Bearer w-litellm"]
        assert json.loads(echoed["data"]) == {"body": "via LiteLLM MCP"}


def a2a(method: str, params: dict[str, Any]) -> dict[str, Any]:
    response = httpx.post(
        f"{GATEWAY}/a2a/librarian",
        json={"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": params},
        headers={**GATEWAY_KEY, "A2A-Version": "1.0"},
        timeout=60,
    )
    response.raise_for_status()
    body: dict[str, Any] = response.json()
    assert "error" not in body, body
    return body["result"]


def message(command: dict[str, Any]) -> dict[str, Any]:
    return {
        "message": {
            "messageId": uuid.uuid4().hex,
            "role": "ROLE_USER",
            "parts": [{"data": command}],
        }
    }


def test_a2a_through_the_gateway_including_task_polling() -> None:
    card = httpx.get(
        f"{GATEWAY}/a2a/librarian/.well-known/agent-card.json", headers=GATEWAY_KEY
    ).json()
    assert "/a2a/librarian" in json.dumps(card)  # the gateway rewrites URLs to itself

    read = a2a(
        "SendMessage",
        message(
            {
                "skill": "execute",
                "api": "echo",
                "operation": "getAnything",
                "args": {"key": "PAY-5"},
            }
        ),
    )
    assert read["task"]["status"]["state"] == "TASK_STATE_COMPLETED"

    write = a2a(
        "SendMessage",
        message(
            {
                "skill": "write",
                "api": "echo",
                "operation": "postComment",
                "args": {"key": "PAY-6"},
                "body": {"body": "via LiteLLM A2A"},
            }
        ),
    )
    task_id = write["task"]["id"]
    assert write["task"]["status"]["state"] == "TASK_STATE_AUTH_REQUIRED"
    approve_latest()
    # LiteLLM forwards GetTask with A2A 0.3 method names; librarian must accept them.
    deadline = time.monotonic() + 30
    while True:
        task = a2a("GetTask", {"id": task_id})
        if task["status"]["state"] == "TASK_STATE_COMPLETED":
            break
        assert time.monotonic() < deadline, task["status"]
        time.sleep(0.5)
    result = task["artifacts"][-1]["parts"][0]["data"]["result"]
    assert result["data"]["headers"]["Authorization"] == ["Bearer w-litellm"]
