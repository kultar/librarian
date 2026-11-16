"""Credentials made for each call, against services that check them for real.

Keycloak issues an OAuth client-credentials token and only answers "who am I" for a
token it issued; MinIO verifies AWS Signature Version 4 on every request and issues the
short-lived credentials of an assumed role.

Run:  docker compose -p libcred -f tests/integration/credentials/docker-compose.yaml up -d --build
      LIBRARIAN_INTEGRATION=1 uv run pytest tests/integration/test_credentials.py -v
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

LIBRARIAN = "http://localhost:18401"
STORAGE = "http://localhost:19000"
COMPOSE = Path(__file__).parent / "credentials" / "docker-compose.yaml"
HEADERS = {"A2A-Version": "1.0", "Authorization": "Bearer key-a"}
SECRETS = ("reader-secret-0123456789", "not-the-secret-0123456789", "librarian-test-secret")
NAME = "2026/q3 report, final (v2).json"  # spaces, a comma, brackets, a slash
REPORT = {"quarter": "q3", "total": 42, "note": "päid ✓"}


@pytest.fixture(scope="module", autouse=True)
def stack_up() -> None:
    wait_until_up(f"{LIBRARIAN}/healthz", f"{STORAGE}/minio/health/live")


def rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
    response = httpx.post(
        f"{LIBRARIAN}/",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        headers=HEADERS,
        timeout=60,
    )
    response.raise_for_status()
    result: dict[str, Any] = response.json()["result"]
    return result


def send(command: dict[str, Any]) -> dict[str, Any]:
    message = {"messageId": uuid.uuid4().hex, "role": "ROLE_USER", "parts": [{"data": command}]}
    task: dict[str, Any] = rpc("SendMessage", {"message": message})["task"]
    return task


def finished(task: dict[str, Any]) -> dict[str, Any]:
    deadline = time.monotonic() + 40
    while task["status"]["state"] not in (
        "TASK_STATE_COMPLETED",
        "TASK_STATE_FAILED",
        "TASK_STATE_REJECTED",
    ):
        assert time.monotonic() < deadline, task["status"]
        time.sleep(0.5)
        task = rpc("GetTask", {"id": task["id"]})
    return task


def result_of(task: dict[str, Any]) -> dict[str, Any]:
    assert task["status"]["state"] == "TASK_STATE_COMPLETED", task["status"]
    data: dict[str, Any] = task["artifacts"][-1]["parts"][0]["data"]
    return data


def logs() -> str:
    return subprocess.run(
        ["docker", "compose", "-p", "libcred", "-f", str(COMPOSE), "logs", "librarian"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def approve_latest() -> None:
    deadline = time.monotonic() + 20
    links: list[str] = []
    while not links and time.monotonic() < deadline:
        links = re.findall(r"(http://\S+/approvals/[0-9a-f]+\?t=[0-9a-f]+)", logs())
        time.sleep(0.5)
    assert links, "no approval link in librarian's log"
    link = links[-1]
    page = httpx.post(link.split("?")[0], data={"t": link.split("t=")[1], "action": "once"})
    assert "Approved" in page.text, page.text


def write(operation: str, body: Any = None, **args: Any) -> dict[str, Any]:
    command = {"skill": "write", "api": "storage", "operation": operation, "args": args}
    if body is not None:
        command["body"] = body
    task = send({**command, "reason": "integration test"})
    assert task["status"]["state"] == "TASK_STATE_AUTH_REQUIRED", task["status"]
    approve_latest()
    result: dict[str, Any] = result_of(finished(task))["result"]
    return result


def read(api: str, operation: str, **args: Any) -> dict[str, Any]:
    task = send({"skill": "execute", "api": api, "operation": operation, "args": args})
    return result_of(finished(task))


def test_a_token_the_provider_issued_is_accepted_by_the_provider() -> None:
    """The client secret is exchanged at the real token endpoint; the token it returns
    is one Keycloak itself accepts, and it says whose it is."""
    result = read("idp", "whoami")
    assert result["status_code"] == 200, result
    assert result["data"]["preferred_username"] == "service-account-librarian-reader"
    assert result["provenance"]["credential_ref"] == "idp/client"
    assert '"credential": "client-credentials token (openid)"' in logs()


def test_a_client_secret_the_provider_refuses_asks_a_person_for_another() -> None:
    task = send({"skill": "execute", "api": "idp-wrong", "operation": "whoami"})
    assert task["status"]["state"] == "TASK_STATE_AUTH_REQUIRED", task["status"]
    assert "needs a credential" in json.dumps(task["status"])
    log = logs()
    assert "the token endpoint refused the client id and secret (HTTP 401" in log
    assert re.search(r"http://\S+/credentials/[0-9a-f]+\?t=", log)  # the link an approver gets


def test_writes_are_signed_as_an_assumed_role_and_reads_with_librarians_own_identity() -> None:
    made = write("createBucket", bucket="reports")
    assert made["status_code"] == 200, made
    put = write("putObject", REPORT, bucket="reports", key=NAME)
    assert put["status_code"] == 200, put
    got = read("storage", "getObject", bucket="reports", key=NAME)
    assert got["status_code"] == 200 and got["data"] == REPORT, got
    assert got["provenance"]["credential_ref"] is None  # nothing is stored to refer to
    # A prefix with a space and a comma: only a query signed in AWS's own encoding passes.
    listed = read(
        "storage", "listObjects", bucket="reports", **{"list-type": 2, "prefix": "2026/q3 report,"}
    )
    assert listed["status_code"] == 200 and "q3 report, final (v2).json" in listed["data"], listed
    log = logs()
    assert '"credential": "signed as storage-writer"' in log
    assert '"credential": "signed with librarian\'s own AWS identity"' in log


def test_the_server_really_checks_signatures() -> None:
    """What makes the test above mean something: the same request, signed with a
    wrong key, is refused."""
    import boto3  # pyright: ignore[reportMissingTypeStubs]
    from botocore.config import Config  # pyright: ignore[reportMissingTypeStubs]
    from botocore.exceptions import ClientError  # pyright: ignore[reportMissingTypeStubs]

    wrong: Any = boto3.client(  # pyright: ignore[reportUnknownMemberType]
        "s3",
        endpoint_url=STORAGE,
        region_name="us-east-1",
        aws_access_key_id="librarian-test",
        aws_secret_access_key="not-the-signing-key",
        config=Config(s3={"addressing_style": "path"}),
    )
    with pytest.raises(ClientError, match="SignatureDoesNotMatch"):
        wrong.get_object(Bucket="reports", Key=NAME)


def test_nothing_secret_is_in_the_log_or_in_what_agents_got() -> None:
    log = logs()
    for secret in SECRETS:
        assert secret not in log
    assert not re.search(r"Signature=[0-9a-f]{64}", log)
    assert "X-Amz-Security-Token" not in log
