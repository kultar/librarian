from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.testclient import TestClient

from librarian.calls import Caller
from librarian.config import ApproverSettings
from librarian.credentials import CredentialRequest
from librarian.secrets import FileSecretStore
from librarian.service import Librarian
from librarian.web import credential_routes
from tests.conftest import BASE, Router


async def open_request(librarian: Librarian) -> tuple[CredentialRequest, str]:
    entry = librarian.registry.get("tracker")
    assert entry is not None
    request = await librarian.broker.open(entry, Caller(agent="agent-a"), "missing")
    return request, librarian.broker.token_for(request)


def client(librarian: Librarian, approvers: ApproverSettings | None = None) -> TestClient:
    app = Starlette(routes=credential_routes(librarian.broker, approvers or ApproverSettings()))
    return TestClient(app)


async def test_page_renders_without_secrets(
    librarian: Librarian, secrets_store: FileSecretStore
) -> None:
    await secrets_store.put("shared/existing", "do-not-show")
    request, token = await open_request(librarian)
    response = client(librarian).get(f"/credentials/{request.id}?t={token}")
    assert response.status_code == 200
    assert "Issue Tracker" in response.text
    assert "shared/existing" in response.text  # names are offered
    assert "do-not-show" not in response.text  # values never are
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


async def test_bad_token_is_rejected(librarian: Librarian) -> None:
    request, _ = await open_request(librarian)
    assert client(librarian).get(f"/credentials/{request.id}?t=nope").status_code == 404
    assert client(librarian).get("/credentials/unknown?t=x").status_code == 404


async def test_submit_key_stores_and_resolves(
    librarian: Librarian, router: Router, secrets_store: FileSecretStore
) -> None:
    router.add("GET", f"{BASE}/v1/me", lambda r: httpx.Response(200, json={}))
    request, token = await open_request(librarian)
    response = client(librarian).post(
        f"/credentials/{request.id}", data={"t": token, "action": "add", "value": "fresh"}
    )
    assert response.status_code == 200, response.text
    assert "connected" in response.text
    assert "fresh" not in response.text
    assert await secrets_store.get("tracker/readonly") == "fresh"
    # the link is now spent
    again = client(librarian).get(f"/credentials/{request.id}?t={token}")
    assert again.status_code == 404


async def test_deny_via_page(librarian: Librarian) -> None:
    request, token = await open_request(librarian)
    response = client(librarian).post(
        f"/credentials/{request.id}", data={"t": token, "action": "deny"}
    )
    assert "denied" in response.text.lower()


async def test_trusted_header_mode_requires_group(librarian: Librarian) -> None:
    request, token = await open_request(librarian)
    approvers = ApproverSettings(mode="trusted_header", group="api-approvers")
    c = client(librarian, approvers)
    url = f"/credentials/{request.id}?t={token}"
    assert c.get(url).status_code == 403
    assert (
        c.get(
            url,
            headers={"X-Auth-Request-Email": "eve@x.test", "X-Auth-Request-Groups": "staff"},
        ).status_code
        == 403
    )
    ok = c.get(
        url,
        headers={
            "X-Auth-Request-Email": "sam@x.test",
            "X-Auth-Request-Groups": "staff,api-approvers",
        },
    )
    assert ok.status_code == 200


# -- theme and chrome ----------------------------------------------------------------------

CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'"
MARK = '<svg viewBox="0 0 512 512"'  # the inline logo (no image source is allowed by the CSP)


def test_page_tokens_match_the_website_theme() -> None:
    """One theme: the :root token rules in site/theme.css are the ones the pages inline."""
    from librarian.web import _TOKENS  # pyright: ignore[reportPrivateUsage]

    theme = (Path(__file__).parents[1] / "site" / "theme.css").read_text()
    rules = [line for line in theme.splitlines() if line.startswith((":root{", "@media"))][:2]
    assert "\n".join(rules) == _TOKENS


def themed(response: Any) -> None:  # starlette's TestClient and httpx responses alike
    assert response.headers["content-security-policy"] == CSP
    assert response.headers["x-frame-options"] == "DENY"
    assert MARK in response.text and "librarian" in response.text
    assert "<link" not in response.text and "<script" not in response.text  # nothing external


async def test_every_credential_page_is_themed(
    librarian: Librarian, router: Router, secrets_store: FileSecretStore
) -> None:
    router.add("GET", f"{BASE}/v1/me", lambda r: httpx.Response(200, json={}))
    await secrets_store.put("shared/existing", "x")
    request, token = await open_request(librarian)
    c = client(librarian)
    url = f"/credentials/{request.id}"
    themed(c.get(f"{url}?t={token}"))  # the form
    themed(c.get(f"{url}?t=nope"))  # expired
    themed(c.post(url, data={"t": token, "action": "add", "value": ""}))  # form with an error
    themed(c.post(url, data={"t": token, "action": "add", "value": "fresh"}))  # connected
    themed(c.post(url, data={"t": token, "action": "deny"}))  # already handled (409)
    request, token = await open_request(librarian)
    themed(c.post(f"/credentials/{request.id}", data={"t": token, "action": "deny"}))  # denied
    strict = client(librarian, ApproverSettings(mode="trusted_header", group="g"))
    themed(strict.get(f"/credentials/{request.id}?t={token}"))  # not allowed


def test_approval_pages_are_themed_and_mark_deletes() -> None:
    from librarian.approvals import WriteApproval
    from librarian.calls import WriteRequest
    from librarian.web import _approval_form, _page  # pyright: ignore[reportPrivateUsage]

    def approval(method: str) -> WriteApproval:
        return WriteApproval(
            fingerprint="f",
            api="tracker",
            api_name="Issue Tracker",
            host="tracker.example.com",
            operation="deleteIssue" if method == "DELETE" else "addComment",
            method=method,
            path="/issues/{key}",
            request=WriteRequest(api="tracker", operation="x", args={"key": "PAY-1"}),
            agent="triage-bot",
            principal="kim@x.test",
            requesters=["kim@x.test"],
            rememberable=method != "DELETE",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    post = _page("Approve", _approval_form(approval("POST"), "tok", None))
    text = bytes(post.body).decode()
    themed(httpx.Response(200, headers=post.headers, text=text))
    assert "method post" in text and "Approve and allow similar" in text
    delete = _page("Approve", _approval_form(approval("DELETE"), "tok", "Boom"))
    text = bytes(delete.body).decode()
    assert "method delete" in text and "Approve and allow similar" not in text
    assert "only ever be approved one request" in text and "Boom" in text
