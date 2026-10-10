from __future__ import annotations

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
