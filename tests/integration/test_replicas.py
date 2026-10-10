"""Two replicas on Postgres + AWS Secrets Manager (LocalStack), against go-httpbin.

Run:  docker compose -p libint -f tests/integration/replicas/docker-compose.yaml up -d --build
      LIBRARIAN_INTEGRATION=1 uv run pytest tests/integration/test_replicas.py -v
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

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
    assert echoed["headers"]["Authorization"] == ["Bearer read-secret-1"]
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
    # The write key came from one key of a JSON secret: service/echo#write_token.
    assert echoed["headers"]["Authorization"] == ["Bearer w-123"]


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
