from __future__ import annotations

import base64

import httpx
import pytest

from librarian.audit import MemoryAuditSink
from librarian.calls import Caller, CallRequest
from librarian.executor import CredentialMissing, PolicyDenied
from librarian.registry import BasicAuth
from librarian.secrets import FileSecretStore
from librarian.service import Librarian
from tests.conftest import BASE, Router

CALLER = Caller(agent="agent-a", principal="kim")


@pytest.fixture(autouse=True)
async def tracker_secret(secrets_store: FileSecretStore) -> None:
    await secrets_store.put("tracker/readonly", "s3cret-token")
    await secrets_store.put("weather/key", "wkey")


async def test_bearer_injected_and_result_has_provenance(
    librarian: Librarian, router: Router, audit: MemoryAuditSink
) -> None:
    router.add(
        "GET", f"{BASE}/v1/issues/ABC-1", lambda r: httpx.Response(200, json={"key": "ABC-1"})
    )
    result = await librarian.executor.execute(
        CallRequest(api="tracker", operation="getIssue", args={"key": "ABC-1"}), CALLER
    )
    assert result.ok and result.data == {"key": "ABC-1"}
    sent = router.requests[0]
    assert sent.headers["authorization"] == "Bearer s3cret-token"
    assert result.provenance.api == "tracker"
    assert result.provenance.principal == "kim"
    assert result.provenance.credential_ref == "tracker/readonly"
    assert "s3cret" not in result.model_dump_json()
    assert audit.events[-1].event == "call"
    assert "s3cret" not in audit.events[-1].model_dump_json()


async def test_path_args_are_encoded(librarian: Librarian, router: Router) -> None:
    router.add("GET", f"{BASE}/v1/issues/a%20b%3F", lambda r: httpx.Response(200, json={}))
    result = await librarian.executor.execute(
        CallRequest(api="tracker", operation="getIssue", args={"key": "a b?"}), CALLER
    )
    assert result.ok


async def test_query_api_key_is_redacted(librarian: Librarian, router: Router) -> None:
    router.add(
        "GET", "https://weather.example.test/forecast", lambda r: httpx.Response(200, json=[])
    )
    result = await librarian.executor.execute(
        CallRequest(api="weather", operation="forecast", args={"city": "Oslo"}), CALLER
    )
    assert router.requests[0].url.params["appid"] == "wkey"
    assert "wkey" not in result.provenance.url
    assert "city=Oslo" in result.provenance.url


async def test_basic_auth(
    librarian: Librarian, router: Router, secrets_store: FileSecretStore
) -> None:
    entry = librarian.registry.get("tracker")
    assert entry is not None
    await librarian.registry.save(
        entry.model_copy(update={"auth": BasicAuth(secret_ref="tracker/readonly")})
    )
    librarian.registry.load()
    await secrets_store.put("tracker/readonly", "me@example.test:tok")
    router.add("GET", f"{BASE}/v1/me", lambda r: httpx.Response(200, json={}))
    await librarian.executor.execute(CallRequest(api="tracker", operation="whoami"), CALLER)
    expected = "Basic " + base64.b64encode(b"me@example.test:tok").decode()
    assert router.requests[0].headers["authorization"] == expected


async def test_off_origin_redirect_not_followed(librarian: Librarian, router: Router) -> None:
    router.add(
        "GET",
        f"{BASE}/v1/me",
        lambda r: httpx.Response(302, headers={"location": "https://evil.test/steal"}),
    )
    result = await librarian.executor.execute(
        CallRequest(api="tracker", operation="whoami"), CALLER
    )
    assert result.status_code == 302
    assert len(router.requests) == 1  # never contacted evil.test


async def test_same_origin_redirect_followed(librarian: Librarian, router: Router) -> None:
    router.add(
        "GET", f"{BASE}/v1/me", lambda r: httpx.Response(302, headers={"location": "/v1/me2"})
    )
    router.add("GET", f"{BASE}/v1/me2", lambda r: httpx.Response(200, json={"ok": 1}))
    result = await librarian.executor.execute(
        CallRequest(api="tracker", operation="whoami"), CALLER
    )
    assert result.data == {"ok": 1}
    assert router.requests[1].headers["authorization"] == "Bearer s3cret-token"


async def test_large_responses_are_truncated(librarian: Librarian, router: Router) -> None:
    librarian.executor.max_response_bytes = 100
    router.add("GET", f"{BASE}/v1/me", lambda r: httpx.Response(200, json={"x": "y" * 1000}))
    result = await librarian.executor.execute(
        CallRequest(api="tracker", operation="whoami"), CALLER
    )
    assert result.truncated
    assert isinstance(result.data, str) and len(result.data) <= 100


async def test_pagination_follows_same_origin_links(librarian: Librarian, router: Router) -> None:
    def page1(r: httpx.Request) -> httpx.Response:
        if r.url.params.get("page") == "2":
            return httpx.Response(200, json=[3, 4])
        return httpx.Response(
            200, json=[1, 2], headers={"link": f'<{BASE}/v1/issues?page=2>; rel="next"'}
        )

    router.add("GET", f"{BASE}/v1/issues", page1)
    result = await librarian.executor.execute(
        CallRequest(api="tracker", operation="listIssues", args={"team": "a"}, max_pages=5), CALLER
    )
    assert result.data == [[1, 2], [3, 4]]
    assert result.provenance.pages == 2


async def test_missing_secret_raises_credential_missing(
    librarian: Librarian, secrets_store: FileSecretStore
) -> None:
    (secrets_store.directory / "tracker" / "readonly").unlink()
    with pytest.raises(CredentialMissing):
        await librarian.executor.execute(CallRequest(api="tracker", operation="whoami"), CALLER)


async def test_401_raises_credential_missing(librarian: Librarian, router: Router) -> None:
    router.add("GET", f"{BASE}/v1/me", lambda r: httpx.Response(401))
    with pytest.raises(CredentialMissing, match="401"):
        await librarian.executor.execute(CallRequest(api="tracker", operation="whoami"), CALLER)


async def test_policy_denial_is_audited(librarian: Librarian, audit: MemoryAuditSink) -> None:
    with pytest.raises(PolicyDenied):
        await librarian.executor.execute(
            CallRequest(api="tracker", operation="deleteIssue", args={"key": "A"}), CALLER
        )
    assert audit.events[-1].event == "denied"


async def test_provisional_entries_cannot_reach_private_addresses(
    librarian: Librarian, router: Router
) -> None:
    entry = librarian.registry.get("status")
    assert entry is not None
    await librarian.registry.save(entry.model_copy(update={"base_url": "http://127.0.0.1:9"}))
    librarian.executor.allow_private_provisional = False
    with pytest.raises(PolicyDenied, match="non-public"):
        await librarian.executor.execute(CallRequest(api="status", operation="summary"), CALLER)
    assert router.requests == []


async def test_403_is_reported_not_treated_as_bad_key(
    librarian: Librarian, router: Router, audit: MemoryAuditSink
) -> None:
    router.add("GET", f"{BASE}/v1/issues/SECRET-1", lambda r: httpx.Response(403, json={}))
    result = await librarian.executor.execute(
        CallRequest(api="tracker", operation="getIssue", args={"key": "SECRET-1"}), CALLER
    )
    assert not result.ok and result.status_code == 403
    assert result.error is not None and "lacks permission" in result.error
    assert librarian.broker.pending_for("tracker") is None  # nobody was paged
    assert audit.events[-1].detail == "permission denied by the API"
