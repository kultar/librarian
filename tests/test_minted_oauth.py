"""OAuth client credentials: the API gets a token that expires by itself; the client
secret only ever goes to the token endpoint."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import ValidationError

from librarian.audit import MemoryAuditSink
from librarian.calls import Caller, CallRequest, WriteRequest
from librarian.errors import ApiUnavailable, CredentialSetupError
from librarian.executor import CredentialMissing, PolicyDenied
from librarian.minted import Minter
from librarian.registry.models import ApiEntry, EntryStatus, OAuth2ClientAuth
from librarian.service import CredentialPending, Librarian
from tests.conftest import RecordingNotifier, Router

CALLER = Caller(agent="agent-a", write_apis=("ledger",))
TOKEN_URL = "https://login.example.test/oauth/token"
API = "https://ledger.example.test"
SECRET = "cl1ent-secret-0123456789"
LEDGER: dict[str, Any] = {
    "id": "ledger",
    "name": "Ledger",
    "base_url": API,
    "status": "verified",
    "auth": {
        "type": "oauth2_client",
        "secret_ref": "ledger/client",
        "token_url": TOKEN_URL,
        "client_id": "librarian",
        "scopes": ["ledger.read"],
    },
    "write_auth": {
        "type": "oauth2_client",
        "secret_ref": "ledger/client",
        "token_url": TOKEN_URL,
        "client_id": "librarian",
        "scopes": ["ledger.write"],
    },
    "operations": [
        {"id": "balance", "method": "GET", "path": "/balance", "access": "read"},
        {
            "id": "book",
            "method": "POST",
            "path": "/entries",
            "access": "write",
            "writable": True,
            "accepts_body": True,
        },
    ],
}
BALANCE = CallRequest(api="ledger", operation="balance")


class Login:
    """A token endpoint, and the API that takes its tokens."""

    def __init__(self) -> None:
        self.asked: list[dict[str, str]] = []
        self.basic: list[str | None] = []
        self.answer: httpx.Response | None = None
        self.calls: list[httpx.Request] = []

    def token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.asked.append(form)
        self.basic.append(request.headers.get("authorization"))
        if self.answer is not None:
            return self.answer
        scope = form.get("scope", "none")
        number = len(self.asked)
        return httpx.Response(
            200,
            json={
                "access_token": f"made-token-{scope}-{number:04d}",
                "token_type": "Bearer",
                "expires_in": 300,
            },
        )

    def api(self, request: httpx.Request) -> httpx.Response:
        """Answers with what it was sent, as a careless API might."""
        self.calls.append(request)
        return httpx.Response(200, json={"sent": request.headers.get("authorization")})


@pytest.fixture
async def login(librarian: Librarian, router: Router) -> Login:
    login = Login()
    router.add("POST", TOKEN_URL, login.token)
    router.add("GET", f"{API}/balance", login.api)
    router.add("POST", f"{API}/entries", login.api)
    await librarian.registry.save(ApiEntry.model_validate(LEDGER))
    await librarian.secrets.put("ledger/client", SECRET)
    return login


def everything(result: Any, audit: MemoryAuditSink) -> str:
    return result.model_dump_json() + "".join(event.model_dump_json() for event in audit.events)


async def test_the_api_gets_a_token_made_for_it_and_never_the_client_secret(
    librarian: Librarian, login: Login, audit: MemoryAuditSink
) -> None:
    result = await librarian.executor.execute(BALANCE, CALLER)
    assert login.asked == [
        {
            "grant_type": "client_credentials",
            "client_id": "librarian",
            "client_secret": SECRET,
            "scope": "ledger.read",
        }
    ]
    [call] = login.calls
    assert call.headers["authorization"] == "Bearer made-token-ledger.read-0001"
    assert SECRET not in str(call.url) + str(dict(call.headers))  # the API never sees it
    # The API repeated the token; the agent does not get it, nor the secret it came from.
    assert result.ok and result.data == {"sent": "Bearer [redacted]"}
    assert result.provenance.credential_ref == "ledger/client"
    left = everything(result, audit)
    assert "made-token" not in left and SECRET not in left
    assert audit.events[-1].extra["credential"] == "client-credentials token (ledger.read)"


async def test_a_token_is_kept_until_shortly_before_it_expires(
    librarian: Librarian, login: Login
) -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    librarian.executor.minter = Minter(librarian.executor.client, clock=lambda: now)
    for _ in range(3):
        await librarian.executor.execute(BALANCE, CALLER)
    assert len(login.asked) == 1  # made once, used three times
    now += timedelta(seconds=239)  # it lasts 300 s; a minute before the end it is not used
    await librarian.executor.execute(BALANCE, CALLER)
    assert len(login.asked) == 1
    now += timedelta(seconds=2)
    await librarian.executor.execute(BALANCE, CALLER)
    assert len(login.asked) == 2
    assert login.calls[-1].headers["authorization"] == "Bearer made-token-ledger.read-0002"


async def test_a_write_gets_its_own_token_with_its_own_scopes(
    librarian: Librarian, login: Login
) -> None:
    await librarian.executor.execute(BALANCE, CALLER)
    entry = librarian.registry.get("ledger")
    assert entry is not None
    op = entry.operation("book")
    assert op is not None
    request = WriteRequest(api="ledger", operation="book", body={"amount": 5})
    result = await librarian.executor.execute_write(entry, op, request, CALLER)
    assert [form["scope"] for form in login.asked] == ["ledger.read", "ledger.write"]
    assert login.calls[-1].headers["authorization"] == "Bearer made-token-ledger.write-0002"
    assert result.data == {"sent": "Bearer [redacted]"}
    await librarian.executor.execute(BALANCE, CALLER)  # a read after it: the read token
    assert login.calls[-1].headers["authorization"] == "Bearer made-token-ledger.read-0001"


async def test_the_id_and_secret_can_travel_as_http_basic(
    librarian: Librarian, login: Login
) -> None:
    entry = librarian.registry.get("ledger")
    assert entry is not None and isinstance(entry.auth, OAuth2ClientAuth)
    basic = entry.auth.model_copy(update={"client_auth": "basic", "audience": "ledger-api"})
    await librarian.registry.save(entry.model_copy(update={"auth": basic}))
    await librarian.executor.execute(BALANCE, CALLER)
    assert login.asked == [
        {"grant_type": "client_credentials", "scope": "ledger.read", "audience": "ledger-api"}
    ]
    expected = base64.b64encode(f"librarian:{SECRET}".encode()).decode()
    assert login.basic == [f"Basic {expected}"]


async def test_a_refused_client_secret_asks_a_person_for_the_right_one(
    librarian: Librarian, login: Login, notifier: RecordingNotifier, audit: MemoryAuditSink
) -> None:
    # The endpoint repeats what it was sent, as some do: none of it is passed on.
    login.answer = httpx.Response(
        401, json={"error": "invalid_client", "error_description": f"bad secret {SECRET}"}
    )
    with pytest.raises(CredentialMissing) as refused:
        await librarian.executor.execute(BALANCE, CALLER)
    assert refused.value.reason == (
        "the token endpoint refused the client id and secret (HTTP 401, invalid_client)"
    )
    assert login.calls == []  # nothing went to the API
    with pytest.raises(CredentialPending):
        await librarian.execute(BALANCE, CALLER, wait_for_credential=False)
    asked = [request for kind, request in notifier.events if kind == "opened"]
    assert [(r.api, r.secret_ref) for r in asked] == [("ledger", "ledger/client")]
    said = str(refused.value) + "".join(event.model_dump_json() for event in audit.events)
    assert SECRET not in said and "bad secret" not in said


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (httpx.Response(400, json={"error": "invalid_scope"}), "(HTTP 400, invalid_scope)"),
        (httpx.Response(500, text="<html>oops</html>"), "(HTTP 500)"),
        (httpx.Response(200, json={"access_token": ""}), "(HTTP 200)"),
        (httpx.Response(400, json={"error": "see https://evil.example/?x=<script>"}), "(HTTP 400)"),
    ],
    ids=["a scope it does not know", "a server error", "no token in the answer", "odd error text"],
)
async def test_an_endpoint_that_will_not_issue_a_token_is_a_setup_problem(
    librarian: Librarian,
    login: Login,
    notifier: RecordingNotifier,
    answer: httpx.Response,
    expected: str,
) -> None:
    """A new secret would not help, so nobody is asked for one: the agent is told what to
    have fixed."""
    login.answer = answer
    with pytest.raises(CredentialSetupError) as problem:
        await librarian.execute(BALANCE, CALLER, wait_for_credential=False)
    assert str(problem.value) == (
        f"Ledger: the token endpoint did not issue a token {expected}: check the token URL, "
        "the scopes and the audience of its authentication"
    )
    assert login.calls == [] and notifier.events == []


async def test_a_token_that_is_not_a_bearer_token_is_not_used(
    librarian: Librarian, login: Login
) -> None:
    login.answer = httpx.Response(200, json={"access_token": "t0ken-0123", "token_type": "mac"})
    with pytest.raises(CredentialSetupError, match="not a bearer token"):
        await librarian.executor.execute(BALANCE, CALLER)
    assert login.calls == []


async def test_an_unreachable_token_endpoint_is_reported_as_such(
    librarian: Librarian, login: Login, router: Router
) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("All connection attempts failed")

    router.add("POST", TOKEN_URL, down)
    with pytest.raises(ApiUnavailable) as gone:
        await librarian.executor.execute(BALANCE, CALLER)
    assert str(gone.value) == (
        "Ledger's token endpoint gave no answer: it could not be reached. Nothing was sent "
        "to Ledger: retry later."
    )
    assert not gone.value.sent and login.calls == []


async def test_an_api_that_refuses_a_fresh_token_is_reported_without_asking_for_a_key(
    librarian: Librarian, login: Login, router: Router, notifier: RecordingNotifier
) -> None:
    router.add(
        "GET",
        f"{API}/balance",
        lambda r: httpx.Response(401, json={"message": "token audience is not ledger"}),
    )
    result = await librarian.execute(BALANCE, CALLER, wait_for_credential=False)
    assert result.status_code == 401 and not result.ok
    assert result.error == (
        "Ledger refused this request (HTTP 401) although the credential was made for it a "
        "moment ago: check what its oauth2_client authentication asks for (scopes, audience, "
        "permissions, the role) in the console. The API said: token audience is not ledger"
    )
    assert notifier.events == []  # the client secret is fine: nobody is asked for another
    # The refused token is dropped: the next call starts with a new one.
    await librarian.execute(BALANCE, CALLER, wait_for_credential=False)
    assert len(login.asked) == 2


async def test_only_an_approved_entry_may_send_a_secret_to_a_token_endpoint(
    librarian: Librarian, login: Login
) -> None:
    entry = librarian.registry.get("ledger")
    assert entry is not None
    await librarian.registry.save(entry.model_copy(update={"status": EntryStatus.PROVISIONAL}))
    with pytest.raises(PolicyDenied, match="oauth2_client authentication, which needs a verified"):
        await librarian.executor.execute(BALANCE, CALLER)
    assert login.asked == [] and login.calls == []


def test_where_the_secret_goes_must_be_https() -> None:
    def auth(**changes: Any) -> OAuth2ClientAuth:
        return OAuth2ClientAuth.model_validate({**LEDGER["auth"], **changes})

    assert auth(token_url="http://localhost:8180/token").token_url.startswith("http://localhost")
    for bad in ("http://login.example.test/token", "ftp://x/y", "https://u:p@login.example.test/"):
        with pytest.raises(ValidationError):
            auth(token_url=bad)
    with pytest.raises(ValidationError, match="may not set"):
        auth(extra_token_params={"client_secret": "x"})
    assert auth(extra_token_params={"resource": "urn:ledger"}).extra_token_params
    with pytest.raises(ValidationError, match="reserved prefix"):
        ApiEntry.model_validate({**LEDGER, "auth": {**LEDGER["auth"], "secret_ref": "librarian/x"}})
