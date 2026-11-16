"""A GitHub App instead of a stored personal token: each call goes out with a token made
for it, valid for an hour, limited to the repository it names, read-only for a read."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from librarian.audit import MemoryAuditSink
from librarian.calls import Caller, CallRequest, WriteRequest
from librarian.errors import CredentialSetupError
from librarian.executor import CredentialMissing
from librarian.minted import Minter, pem
from librarian.registry.models import ApiEntry, GitHubAppTokenAuth
from librarian.service import Librarian
from tests.conftest import Router

CALLER = Caller(agent="agent-a", write_apis=("hub",))
HUB_URL = "https://hub.example.test"
APP = {"type": "github_app", "secret_ref": "hub/app-key", "app_id": 12345}
OWNER_REPO = [{"name": "owner", "in": "path"}, {"name": "repo", "in": "path"}]
HUB: dict[str, Any] = {
    "id": "hub",
    "name": "Code Hub",
    "base_url": HUB_URL,
    "status": "verified",
    "auth": APP,
    "write_auth": APP,
    "operations": [
        {
            "id": "getRepo",
            "method": "GET",
            "path": "/repos/{owner}/{repo}",
            "access": "read",
            "parameters": OWNER_REPO,
        },
        {
            "id": "listOrgRepos",
            "method": "GET",
            "path": "/orgs/{org}/repos",
            "access": "read",
            "parameters": [{"name": "org", "in": "path"}],
        },
        {"id": "me", "method": "GET", "path": "/user", "access": "read"},
        {
            "id": "search",
            "method": "GET",
            "path": "/search/code",
            "access": "read",
            "parameters": [{"name": "owner", "in": "query"}, {"name": "q", "in": "query"}],
        },
        {
            "id": "createIssue",
            "method": "POST",
            "path": "/repos/{owner}/{repo}/issues",
            "access": "write",
            "writable": True,
            "accepts_body": True,
            "parameters": OWNER_REPO,
        },
    ],
}
INSTALLED = {"contents": "write", "issues": "write", "metadata": "read", "workflows": "write"}


@pytest.fixture(scope="module")
def app_key() -> str:
    """A private key in the form GitHub hands them out."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()


class Hub:
    """Enough of GitHub: the App's own endpoints, and a few of the API's."""

    def __init__(self, key: str) -> None:
        private = serialization.load_pem_private_key(key.encode(), password=None)
        self.public = private.public_key()
        self.as_app: list[str] = []  # paths asked for with the App's own token
        self.issued: list[dict[str, Any]] = []  # what each installation token was asked for
        self.calls: list[httpx.Request] = []
        self.app_refused = False

    def _is_the_app(self, request: httpx.Request) -> httpx.Response | None:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        claims = jwt.decode(token, self.public, algorithms=["RS256"])  # pyright: ignore[reportArgumentType]
        assert claims["iss"] == "12345" and claims["exp"] - claims["iat"] <= 600
        self.as_app.append(f"{request.method} {request.url.path}")
        if self.app_refused:
            return httpx.Response(401, json={"message": "A JSON web token could not be decoded"})
        return None

    def installation(self, request: httpx.Request) -> httpx.Response:
        return self._is_the_app(request) or httpx.Response(200, json={"id": 4242})

    def not_installed(self, request: httpx.Request) -> httpx.Response:
        return self._is_the_app(request) or httpx.Response(404, json={"message": "Not Found"})

    def describe(self, request: httpx.Request) -> httpx.Response:
        return self._is_the_app(request) or httpx.Response(200, json={"permissions": INSTALLED})

    def issue(self, request: httpx.Request) -> httpx.Response:
        refused = self._is_the_app(request)
        if refused is not None:
            return refused
        asked: dict[str, Any] = json.loads(request.content or b"{}")
        self.issued.append(asked)
        expires = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        token = f"inst-token-{len(self.issued):04d}-{'-'.join(asked.get('repositories', ['all']))}"
        return httpx.Response(201, json={"token": token, "expires_at": expires})

    def api(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        return httpx.Response(200, json={"sent": request.headers.get("authorization")})


@pytest.fixture
async def hub(librarian: Librarian, router: Router, app_key: str) -> Hub:
    hub = Hub(app_key)
    for path in ("/repos/acme/payments/installation", "/orgs/acme/installation"):
        router.add("GET", HUB_URL + path, hub.installation)
    for path in ("/repos/acme/vault/installation", "/users/acme/installation"):
        router.add("GET", HUB_URL + path, hub.not_installed)
    router.add("GET", f"{HUB_URL}/app/installations/4242", hub.describe)
    router.add("POST", f"{HUB_URL}/app/installations/4242/access_tokens", hub.issue)
    for method, path in (
        ("GET", "/repos/acme/payments"),
        ("GET", "/orgs/acme/repos"),
        ("GET", "/user"),
        ("GET", "/search/code"),
        ("POST", "/repos/acme/payments/issues"),
    ):
        router.add(method, HUB_URL + path, hub.api)
    await librarian.registry.save(ApiEntry.model_validate(HUB))
    await librarian.secrets.put("hub/app-key", app_key)
    return hub


def read(operation: str = "getRepo", **args: Any) -> CallRequest:
    return CallRequest(api="hub", operation=operation, args=args)


PAYMENTS: dict[str, Any] = {"owner": "acme", "repo": "payments"}


async def write(librarian: Librarian) -> Any:
    entry = librarian.registry.get("hub")
    assert entry is not None
    op = entry.operation("createIssue")
    assert op is not None
    request = WriteRequest(api="hub", operation="createIssue", args=PAYMENTS, body={"title": "x"})
    return await librarian.executor.execute_write(entry, op, request, CALLER)


async def test_a_read_gets_a_token_for_that_repository_that_cannot_write(
    librarian: Librarian, hub: Hub, router: Router, app_key: str, audit: MemoryAuditSink
) -> None:
    result = await librarian.executor.execute(read(**PAYMENTS), CALLER)
    assert hub.as_app == [
        "GET /repos/acme/payments/installation",  # where is the App installed for this call?
        "GET /app/installations/4242",  # what may it do there?
        "POST /app/installations/4242/access_tokens",
    ]
    assert hub.issued == [
        {
            "repositories": ["payments"],
            # Everything turned down to read; what only exists as "write" is left out.
            "permissions": {"contents": "read", "issues": "read", "metadata": "read"},
        }
    ]
    [call] = hub.calls
    assert call.headers["authorization"] == "Bearer inst-token-0001-payments"
    assert result.ok and result.data == {"sent": "Bearer [redacted]"}
    assert audit.events[-1].extra["credential"] == (
        "installation token for acme/payments, read-only"
    )
    # The private key signs; it is never sent, and neither it nor the token comes back.
    piece = "".join(app_key.splitlines()[1:3])
    for request in router.requests:
        sent = str(request.url) + str(dict(request.headers)) + request.content.decode()
        assert "PRIVATE KEY" not in sent and piece not in sent.replace("\n", "")
    left = result.model_dump_json() + "".join(e.model_dump_json() for e in audit.events)
    assert "inst-token" not in left and "PRIVATE KEY" not in left


async def test_a_write_gets_its_own_token_and_a_read_never_uses_it(
    librarian: Librarian, hub: Hub
) -> None:
    await librarian.executor.execute(read(**PAYMENTS), CALLER)
    result = await write(librarian)
    assert hub.issued[1] == {"repositories": ["payments"]}  # as the App is installed
    assert hub.calls[-1].headers["authorization"] == "Bearer inst-token-0002-payments"
    assert result.ok and result.data == {"sent": "Bearer [redacted]"}
    await librarian.executor.execute(read(**PAYMENTS), CALLER)
    assert hub.calls[-1].headers["authorization"] == "Bearer inst-token-0001-payments"
    assert len(hub.issued) == 2  # both kept: nothing new was asked for


async def test_what_the_entry_asks_for_is_never_more_than_read_on_a_read(
    librarian: Librarian, hub: Hub
) -> None:
    entry = librarian.registry.get("hub")
    assert entry is not None
    chosen = GitHubAppTokenAuth.model_validate(
        {**APP, "permissions": {"issues": "write", "contents": "read"}}
    )
    await librarian.registry.save(entry.model_copy(update={"auth": chosen, "write_auth": chosen}))
    await librarian.executor.execute(read(**PAYMENTS), CALLER)
    await write(librarian)
    assert [asked["permissions"] for asked in hub.issued] == [
        {"issues": "read", "contents": "read"},
        {"issues": "write", "contents": "read"},
    ]
    assert "GET /app/installations/4242" not in hub.as_app  # nothing to look up


async def test_a_repository_the_app_is_not_installed_on_is_a_setup_problem(
    librarian: Librarian, hub: Hub
) -> None:
    with pytest.raises(CredentialSetupError) as problem:
        await librarian.executor.execute(read(owner="acme", repo="vault"), CALLER)
    assert str(problem.value) == "Code Hub: the GitHub App is not installed on acme/vault"
    assert hub.issued == [] and hub.calls == []


async def test_a_call_that_names_an_organisation_gets_a_token_for_it(
    librarian: Librarian, hub: Hub, audit: MemoryAuditSink
) -> None:
    await librarian.executor.execute(read("listOrgRepos", org="acme"), CALLER)
    assert hub.as_app[0] == "GET /orgs/acme/installation"
    assert "repositories" not in hub.issued[0]
    assert audit.events[-1].extra["credential"] == "installation token for acme, read-only"


async def test_a_call_that_names_no_owner_needs_an_installation_in_the_entry(
    librarian: Librarian, hub: Hub
) -> None:
    with pytest.raises(CredentialSetupError, match="set installation_id"):
        await librarian.executor.execute(read("me"), CALLER)
    # An argument that is not part of the path does not choose whose token is made.
    with pytest.raises(CredentialSetupError, match="set installation_id"):
        await librarian.executor.execute(read("search", owner="acme", q="x"), CALLER)
    assert hub.as_app == [] and hub.calls == []
    entry = librarian.registry.get("hub")
    assert entry is not None
    fixed = GitHubAppTokenAuth.model_validate({**APP, "installation_id": 4242})
    await librarian.registry.save(entry.model_copy(update={"auth": fixed}))
    result = await librarian.executor.execute(read("me"), CALLER)
    assert result.ok and "repositories" not in hub.issued[0]
    assert hub.as_app[0] == "GET /app/installations/4242"  # no search for the installation


async def test_tokens_are_kept_per_repository_until_they_expire(
    librarian: Librarian, hub: Hub
) -> None:
    now = datetime.now(UTC)
    librarian.executor.minter = Minter(librarian.executor.client, clock=lambda: now)
    for _ in range(3):
        await librarian.executor.execute(read(**PAYMENTS), CALLER)
    assert len(hub.issued) == 1 and len(hub.as_app) == 3
    now += timedelta(minutes=58)  # a token lasts an hour
    await librarian.executor.execute(read(**PAYMENTS), CALLER)
    assert len(hub.issued) == 1
    now += timedelta(minutes=1, seconds=30)
    await librarian.executor.execute(read(**PAYMENTS), CALLER)
    assert len(hub.issued) == 2


async def test_a_key_github_does_not_accept_asks_a_person_for_the_right_one(
    librarian: Librarian, hub: Hub
) -> None:
    hub.app_refused = True
    with pytest.raises(CredentialMissing) as refused:
        await librarian.executor.execute(read(**PAYMENTS), CALLER)
    assert refused.value.reason == (
        "GitHub refused the App's private key (HTTP 401): check app_id and the key"
    )
    await librarian.secrets.put("hub/app-key", "this is not a key at all")
    with pytest.raises(CredentialMissing) as unusable:
        await librarian.executor.execute(read(**PAYMENTS), CALLER)
    assert unusable.value.reason == "the GitHub App's private key is not usable"
    assert hub.calls == []


async def test_a_key_whose_line_breaks_were_lost_still_works(
    librarian: Librarian, hub: Hub, app_key: str
) -> None:
    """Pasted into a one-line field, or stored with the line breaks written out."""
    for mangled in (" ".join(app_key.split("\n")), app_key.replace("\n", "\\n")):
        assert pem(mangled) == app_key
        await librarian.secrets.put("hub/app-key", mangled)
        librarian.executor.minter = Minter(librarian.executor.client)
        result = await librarian.executor.execute(read(**PAYMENTS), CALLER)
        assert result.ok
    assert pem("not a key") == "not a key"
