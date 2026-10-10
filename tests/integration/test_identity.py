"""Real sign-in and verified principals: Keycloak + oauth2-proxy + librarian.

Run:  docker compose -p libid -f tests/integration/identity/docker-compose.yaml up -d --build
      LIBRARIAN_INTEGRATION=1 uv run pytest tests/integration/test_identity.py -v
"""

from __future__ import annotations

import html
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
    wait_until_up("http://localhost:18201/healthz", "http://localhost:4180/ping")


A2A = "http://localhost:18201"
PAGES = "http://localhost:4180"
TOKEN_URL = "http://localhost:8180/realms/lib/protocol/openid-connect/token"
COMPOSE = Path(__file__).parent / "identity" / "docker-compose.yaml"


def principal_token(user: str, password: str) -> str:
    """What an agent would obtain for its user: an access token with audience librarian."""
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


def send(
    command: dict[str, Any], token: str | None = None, principal: str | None = None
) -> dict[str, Any]:
    headers = {"A2A-Version": "1.0", "Authorization": "Bearer key-a"}
    if token:
        headers["X-Principal-Token"] = token
    params: dict[str, Any] = {
        "message": {
            "messageId": uuid.uuid4().hex,
            "role": "ROLE_USER",
            "parts": [{"data": command}],
        }
    }
    if principal:
        params["metadata"] = {"principal": principal}
    response = httpx.post(
        f"{A2A}/",
        json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": params},
        headers=headers,
        timeout=60,
    )
    if response.status_code != 200:
        return {"http_status": response.status_code, "body": response.json()}
    task: dict[str, Any] = response.json()["result"]["task"]
    return task


def get_task(task_id: str) -> dict[str, Any]:
    response = httpx.post(
        f"{A2A}/",
        json={"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": task_id}},
        headers={"A2A-Version": "1.0", "Authorization": "Bearer key-a"},
    )
    task: dict[str, Any] = response.json()["result"]
    return task


def logged_link(kind: str) -> str:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        logs = subprocess.run(
            ["docker", "compose", "-p", "libid", "-f", str(COMPOSE), "logs", "librarian"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        links = re.findall(rf"(http://\S+/{kind}/[0-9a-f]+\?t=[0-9a-f]+)", logs)
        if links:
            return links[-1]
        time.sleep(0.5)
    raise AssertionError(f"no {kind} link in librarian logs")


def browser(user: str, password: str, url: str) -> tuple[httpx.Client, httpx.Response]:
    """Open `url` like a person: oauth2-proxy -> Keycloak login form -> back to the page."""
    client = httpx.Client(follow_redirects=True, timeout=30)

    def like_a_browser(response: httpx.Response) -> None:
        # Browsers treat http://localhost as a secure context and send `Secure` cookies
        # (Keycloak's AUTH_SESSION_ID) back; Python's cookie jar does not, so re-add them.
        for header in response.headers.get_list("set-cookie"):
            if "secure" not in header.lower():
                continue
            name, _, rest = header.partition("=")
            value = rest.split(";", 1)[0]
            path = re.search(r"(?i)path=([^;]+)", header)
            # http.cookiejar files dot-less hosts like "localhost" as "localhost.local".
            host = response.url.host
            domain = host if "." in host else f"{host}.local"
            client.cookies.set(
                name.strip(), value, domain=domain, path=path.group(1) if path else "/"
            )

    client.event_hooks["response"] = [like_a_browser]
    login_page = client.get(url)
    assert "kc-form-login" in login_page.text, "expected the Keycloak login form"
    action = html.unescape(re.search(r'action="([^"]+)"', login_page.text).group(1))  # type: ignore[union-attr]
    page = client.post(action, data={"username": user, "password": password, "credentialId": ""})
    return client, page


def test_principals_are_verified_from_real_tokens() -> None:
    task = send(
        {"skill": "execute", "api": "echo", "operation": "whoami"},
        token=principal_token("kim", "kim-pass"),
    )
    provenance = task["artifacts"][-1]["parts"][0]["data"]["provenance"]
    assert provenance["principal"] == "kim@example.com"
    assert provenance["principal_verified"] is True


def test_claims_without_a_token_are_refused_in_required_mode() -> None:
    task = send(
        {"skill": "execute", "api": "echo", "operation": "whoami"}, principal="ceo@example.com"
    )
    assert task["status"]["state"] == "TASK_STATE_REJECTED"


def test_tampered_tokens_are_refused() -> None:
    header, payload, signature = principal_token("eve", "eve-pass").split(".")
    forged = principal_token("kim", "kim-pass").split(".")[1]
    result = send(
        {"skill": "execute", "api": "echo", "operation": "whoami"},
        token=f"{header}.{forged}.{signature}",
    )
    assert result["http_status"] == 401
    _ = payload


def test_pages_cannot_be_reached_with_forged_headers() -> None:
    response = httpx.get(
        f"{PAGES}/approvals/x?t=y",
        headers={"X-Forwarded-Email": "kim@example.com", "X-Forwarded-Groups": "api-approvers"},
    )
    assert response.status_code in (302, 403)  # sent to sign-in, never to librarian
    assert httpx.get(f"{A2A}/approvals/x?t=y").status_code == 404  # agents' port has no pages


def test_only_the_requester_can_approve_their_write() -> None:
    task = send(
        {
            "skill": "write",
            "api": "echo",
            "operation": "postComment",
            "args": {"key": "PAY-9"},
            "body": {"body": "approved by kim"},
            "reason": "integration",
        },
        token=principal_token("kim", "kim-pass"),
    )
    assert task["status"]["state"] == "TASK_STATE_AUTH_REQUIRED"
    link = logged_link("approvals")
    pages_link = re.sub(r"^http://[^/]+", PAGES, link)

    _, as_eve = browser("eve", "eve-pass", pages_link)
    assert as_eve.status_code == 403 and "Only the requester" in as_eve.text

    client, as_kim = browser("kim", "kim-pass", pages_link)
    assert as_kim.status_code == 200 and "Approve a write" in as_kim.text
    approval_id, token = re.search(r"/approvals/([0-9a-f]+)\?t=(\w+)", link).groups()  # type: ignore[union-attr]
    done = client.post(f"{PAGES}/approvals/{approval_id}", data={"t": token, "action": "once"})
    assert "Approved" in done.text

    deadline = time.monotonic() + 30
    while get_task(task["id"])["status"]["state"] != "TASK_STATE_COMPLETED":
        assert time.monotonic() < deadline, "write did not complete"
        time.sleep(0.5)
    echoed = get_task(task["id"])["artifacts"][-1]["parts"][0]["data"]["result"]["data"]
    assert echoed["headers"]["Authorization"] == ["Bearer w-identity"]


def test_credential_page_requires_the_approver_group() -> None:
    task = send({"skill": "execute", "api": "locked", "operation": "whoami"})
    assert task["status"]["state"] == "TASK_STATE_AUTH_REQUIRED"
    link = re.sub(r"^http://[^/]+", PAGES, logged_link("credentials"))
    _, as_eve = browser("eve", "eve-pass", link)
    assert as_eve.status_code == 403 and "api-approvers" in as_eve.text
    _, as_kim = browser("kim", "kim-pass", link)
    assert as_kim.status_code == 200 and "Access needed" in as_kim.text
