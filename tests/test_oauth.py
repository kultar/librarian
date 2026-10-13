"""Acting as the person: per-person OAuth sign-in, tokens per principal, refresh, isolation."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from librarian.audit import MemoryAuditSink
from librarian.calls import Caller, CallRequest
from librarian.config import ApproverSettings
from librarian.console import consent_routes, console_routes
from librarian.executor import PolicyDenied
from librarian.oauth import UserTokens, authorize_url, principal_key
from librarian.registry.models import ApiEntry, EntryStatus, OAuth2UserAuth
from librarian.service import ConsentPending, Librarian
from tests.conftest import RecordingNotifier, Router

KIM = Caller(agent="drive-bot", principal="kim@x.test", principal_verified=True)
BOB = Caller(agent="drive-bot", principal="bob@x.test", principal_verified=True)
TOKEN_URL = "https://auth.example.test/token"
DRIVE: dict[str, Any] = {
    "id": "drive",
    "name": "Drive",
    "base_url": "https://drive.example.test",
    "status": "verified",
    "auth": {
        "type": "oauth2_user",
        "authorization_url": "https://auth.example.test/authorize",
        "token_url": TOKEN_URL,
        "scopes": ["files.read"],
        "client_id": "librarian-client",
        "client_secret_ref": "drive/oauth-client",
        "extra_authorize_params": {"access_type": "offline"},
    },
    "operations": [{"id": "listFiles", "method": "GET", "path": "/files", "access": "read"}],
}


class Provider:
    """A fake OAuth provider plus the API behind it."""

    def __init__(self) -> None:
        self.exchanges: list[dict[str, str]] = []
        self.seen_tokens: list[str] = []

    def token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.exchanges.append(form)
        assert form["client_id"] == "librarian-client" and form["client_secret"] == "shh"
        if form["grant_type"] == "authorization_code":
            who = form["code"].removeprefix("code-")
            assert form["redirect_uri"].endswith("/oauth/callback") and form["code_verifier"]
            return httpx.Response(
                200,
                json={
                    "access_token": f"tok-{who}",
                    "refresh_token": f"ref-{who}",
                    "expires_in": 3600,
                    "scope": "files.read",
                },
            )
        if form["grant_type"] == "refresh_token":
            who = form["refresh_token"].removeprefix("ref-")
            return httpx.Response(200, json={"access_token": f"tok-{who}-2", "expires_in": 3600})
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def files(self, request: httpx.Request) -> httpx.Response:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        self.seen_tokens.append(token)
        if not token.startswith("tok-"):
            return httpx.Response(401, json={"error": "unauthorized"})
        return httpx.Response(200, json={"owner": token})


@pytest.fixture
async def drive(librarian: Librarian, router: Router) -> Provider:
    provider = Provider()
    router.add("POST", TOKEN_URL, provider.token)
    router.add("GET", "https://drive.example.test/files", provider.files)
    librarian.consents._client_factory = lambda: httpx.AsyncClient(  # pyright: ignore[reportPrivateUsage]
        transport=httpx.MockTransport(router)
    )
    await librarian.secrets.put("drive/oauth-client", "shh")
    await librarian.registry.save(ApiEntry.model_validate(DRIVE))
    return provider


def pages(librarian: Librarian) -> TestClient:
    return TestClient(
        Starlette(routes=[*consent_routes(librarian), *console_routes(librarian)]),
        follow_redirects=False,
    )


async def sign_in(librarian: Librarian, notifier: RecordingNotifier, who: str) -> None:
    """Walk the person through the link, the redirect and the callback."""
    kind, consent, link = notifier.consents[-1]
    assert kind == "opened" and link and consent.principal == f"{who}@x.test"
    c = pages(librarian)
    path = link.removeprefix("https://librarian.example.test")
    page = c.get(path)
    assert page.status_code == 200 and "Sign in to Drive" in page.text and "files.read" in page.text
    match = re.search(r"<a class=btn href='([^']+)'", page.text)
    assert match, "the page carries a plain link to the provider (CSP blocks form redirects)"
    target = urlsplit(match.group(1).replace("&amp;", "&"))
    assert (
        f"{target.scheme}://{target.netloc}{target.path}" == "https://auth.example.test/authorize"
    )
    params = {k: v[0] for k, v in parse_qs(target.query).items()}
    assert params["client_id"] == "librarian-client" and params["scope"] == "files.read"
    assert params["code_challenge_method"] == "S256" and params["access_type"] == "offline"
    assert params["redirect_uri"] == "https://librarian.example.test/oauth/callback"
    assert params["state"].startswith(consent.id)
    back = c.get("/oauth/callback", params={"code": f"code-{who}", "state": params["state"]})
    assert back.status_code == 200 and "Signed in to Drive" in back.text
    assert notifier.consents[-1][0] == "closed" and str(consent.state) == "connected"


async def test_each_person_signs_in_once_and_calls_carry_their_own_token(
    librarian: Librarian, drive: Provider, notifier: RecordingNotifier, audit: MemoryAuditSink
) -> None:
    request = CallRequest(api="drive", operation="listFiles")
    with pytest.raises(ConsentPending) as pending:
        await librarian.execute(request, KIM, wait_for_credential=False)
    assert "kim@x.test must sign in" in str(pending.value)
    assert drive.seen_tokens == []  # nothing was tried without a token
    await sign_in(librarian, notifier, "kim")
    result = await librarian.execute(request, KIM)
    assert result.ok and result.data == {"owner": "tok-kim"}
    ref = result.provenance.credential_ref
    assert ref is not None
    assert ref == principal_key("drive", "kim@x.test") and "kim@x.test" not in ref
    # the same request for Bob never uses Kim's token
    with pytest.raises(ConsentPending):
        await librarian.execute(request, BOB, wait_for_credential=False)
    assert drive.seen_tokens == ["tok-kim"]
    await sign_in(librarian, notifier, "bob")
    bob = await librarian.execute(request, BOB)
    assert bob.data == {"owner": "tok-bob"} and drive.seen_tokens[-1] == "tok-bob"
    stored = await librarian.secrets.get(principal_key("drive", "kim@x.test"))
    assert stored and json.loads(stored)["access_token"] == "tok-kim"
    assert any(
        ev.event == "consent_request" and ev.principal == "kim@x.test" for ev in audit.events
    )
    assert [c.principal for c in await librarian.consents.connections("drive")] == []  # no DB here


async def test_expired_tokens_are_refreshed_and_rejected_ones_ask_again(
    librarian: Librarian, drive: Provider, notifier: RecordingNotifier
) -> None:
    old = UserTokens(
        access_token="tok-kim",
        refresh_token="ref-kim",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    await librarian.secrets.put(principal_key("drive", "kim@x.test"), old.model_dump_json())
    result = await librarian.execute(CallRequest(api="drive", operation="listFiles"), KIM)
    assert result.data == {"owner": "tok-kim-2"}  # refreshed before the call
    assert drive.exchanges[-1]["grant_type"] == "refresh_token"
    stored = await librarian.secrets.get(principal_key("drive", "kim@x.test"))
    assert stored and json.loads(stored)["refresh_token"] == "ref-kim"  # kept when not rotated
    # a token the API rejects is dropped and the person is asked again
    bad = UserTokens(access_token="stale", expires_at=None)
    await librarian.secrets.put(principal_key("drive", "kim@x.test"), bad.model_dump_json())
    with pytest.raises(ConsentPending):
        await librarian.execute(
            CallRequest(api="drive", operation="listFiles"), KIM, wait_for_credential=False
        )
    assert notifier.consents[-1][0] == "opened"
    assert not await librarian.secrets.get(principal_key("drive", "kim@x.test"))


async def test_per_person_apis_need_a_person_a_verified_one_and_a_verified_entry(
    librarian: Librarian, drive: Provider
) -> None:
    request = CallRequest(api="drive", operation="listFiles")
    with pytest.raises(PolicyDenied, match="acts as the person"):
        await librarian.execute(request, Caller(agent="nobody-bot"))
    librarian.executor.require_verified_principal = True  # principals.mode != off
    with pytest.raises(PolicyDenied, match="must be verified"):
        await librarian.execute(request, Caller(agent="drive-bot", principal="kim@x.test"))
    entry = librarian.registry.get("drive")
    assert entry is not None
    await librarian.registry.save(entry.model_copy(update={"status": EntryStatus.PROVISIONAL}))
    with pytest.raises(PolicyDenied, match="verified entry"):
        await librarian.execute(request, KIM)
    assert drive.seen_tokens == []


async def test_only_the_person_may_use_their_sign_in_link(
    librarian: Librarian, drive: Provider, notifier: RecordingNotifier
) -> None:
    with pytest.raises(ConsentPending):
        await librarian.execute(
            CallRequest(api="drive", operation="listFiles"), KIM, wait_for_credential=False
        )
    _, consent, link = notifier.consents[-1]
    assert link
    path = link.removeprefix("https://librarian.example.test")
    librarian.settings.approvers = ApproverSettings(mode="trusted_header")
    c = pages(librarian)
    assert c.get(path).status_code == 403  # nobody signed in at the proxy
    eve = c.get(path, headers={"X-Auth-Request-Email": "eve@x.test"})
    assert eve.status_code == 403 and "for someone else" in eve.text
    assert c.get(path, headers={"X-Auth-Request-Email": "Kim@X.test"}).status_code == 200
    assert c.get(f"/connect/{consent.id}?t=nope").status_code == 404
    # a callback with a forged state is refused; a declined sign-in tells the agent
    assert (
        c.get("/oauth/callback", params={"code": "x", "state": f"{consent.id}.nope"}).status_code
        == 404
    )
    token = path.split("t=")[1]
    denied = c.post(
        f"/connect/{consent.id}",
        data={"t": token, "action": "deny"},
        headers={"X-Auth-Request-Email": "kim@x.test"},
    )
    assert denied.status_code == 200 and str(consent.state) == "denied"


async def test_console_shows_and_disconnects_signed_in_people(
    librarian: Librarian, drive: Provider, notifier: RecordingNotifier, audit: MemoryAuditSink
) -> None:
    with pytest.raises(ConsentPending):
        await librarian.execute(
            CallRequest(api="drive", operation="listFiles"), KIM, wait_for_credential=False
        )
    librarian.settings.approvers.console = "open"
    c = pages(librarian)
    assert "Sign-in to <b>Drive</b> by kim@x.test" in c.get("/").text
    await sign_in(librarian, notifier, "kim")
    page = c.get("/apis/drive").text
    assert "each person signs in themselves" in page and "files.read" in page
    t = librarian.approvals.page_token("console", "someone (console open)")
    done = c.post("/apis/drive/disconnect", data={"t": t, "principal": "kim@x.test"})
    assert done.status_code == 200 and "Disconnected" in done.text
    assert not await librarian.secrets.get(principal_key("drive", "kim@x.test"))
    assert (
        audit.events[-1].event == "person_disconnected" and audit.events[-1].detail == "kim@x.test"
    )


def test_authorize_url_and_keys() -> None:
    auth = OAuth2UserAuth.model_validate(DRIVE["auth"])
    url = authorize_url(auth, "https://l/oauth/callback", "id.tok", "verifier-123")
    params = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert params["response_type"] == "code" and params["state"] == "id.tok"
    assert "code_challenge" in params and params["code_challenge"] != "verifier-123"
    assert principal_key("drive", "Kim@X.test") == principal_key("drive", " kim@x.test ")
    assert principal_key("drive", "kim@x.test") != principal_key("drive", "bob@x.test")
    with pytest.raises(ValueError, match="https"):
        OAuth2UserAuth.model_validate({**DRIVE["auth"], "token_url": "http://auth.example.test/t"})
    with pytest.raises(ValueError, match="client_secret_ref"):
        ApiEntry.model_validate(
            {**DRIVE, "auth": {**DRIVE["auth"], "client_secret_ref": "librarian/x"}}
        )


async def test_sign_in_management_from_the_console(
    librarian: Librarian, drive: Provider, notifier: RecordingNotifier, audit: MemoryAuditSink
) -> None:
    with pytest.raises(ConsentPending):
        await librarian.execute(
            CallRequest(api="drive", operation="listFiles"), KIM, wait_for_credential=False
        )
    await sign_in(librarian, notifier, "kim")
    librarian.settings.approvers.console = "open"
    c = pages(librarian)
    t = librarian.approvals.page_token("console", "someone (console open)")
    page = c.get("/apis/drive").text
    assert "Check all sign-ins now" in page and "Disconnect everyone" in page
    assert "href='#people'" in page and "needs a database" in page  # no listing without one
    checked = c.post("/apis/drive/check-signins", data={"t": t})
    assert checked.status_code == 200 and "Sign-ins checked" in checked.text
    assert audit.events[-1].event == "signins_checked"
    everyone = c.post("/apis/drive/disconnect-all", data={"t": t})
    assert everyone.status_code == 200 and "Everyone disconnected" in everyone.text
    assert audit.events[-1].event == "people_disconnected"
    assert c.post("/apis/tracker/check-signins", data={"t": t}).status_code == 400
    # /me needs the proxy's identity; with it, a person can disconnect themselves
    librarian.settings.approvers = ApproverSettings(mode="trusted_header", console="signed_in")
    kim = {"X-Auth-Request-Email": "kim@x.test"}
    mine = c.get("/me", headers=kim).text
    assert "kim@x.test" in mine and "needs a database" in mine  # no listing without one
    tk = librarian.approvals.page_token("console", "kim@x.test")
    done = c.post("/me/disconnect", data={"t": tk, "api": "drive"}, headers=kim)
    assert done.status_code == 200 and "Disconnected" in done.text
    assert audit.events[-1].event == "person_disconnected"
    assert not await librarian.secrets.get(principal_key("drive", "kim@x.test"))


async def test_refresh_now_drops_sign_ins_the_provider_rejects(
    librarian: Librarian, drive: Provider, router: Router
) -> None:
    entry = librarian.registry.get("drive")
    assert entry is not None and isinstance(entry.auth, OAuth2UserAuth)
    good = UserTokens(access_token="tok-kim", refresh_token="ref-kim")
    await librarian.secrets.put(principal_key("drive", "kim@x.test"), good.model_dump_json())
    assert await librarian.consents.refresh_now(entry, entry.auth, "kim@x.test") is None
    stored = await librarian.secrets.get(principal_key("drive", "kim@x.test"))
    assert stored and json.loads(stored)["access_token"] == "tok-kim-2"
    router.add("POST", TOKEN_URL, lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
    why = await librarian.consents.refresh_now(entry, entry.auth, "kim@x.test")
    assert why and "400" in why
    assert not await librarian.secrets.get(principal_key("drive", "kim@x.test"))
    assert await librarian.consents.refresh_now(entry, entry.auth, "bob@x.test") == "not signed in"
