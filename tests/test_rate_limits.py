"""Upstream rate limiting is told apart from a permission problem, with the reset time."""

from __future__ import annotations

import time

import httpx
import pytest

from librarian.audit import MemoryAuditSink
from librarian.calls import Caller, CallRequest
from librarian.secrets import FileSecretStore
from librarian.service import Librarian
from tests.conftest import BASE, Router
from tests.test_writes import KIM, comment

CALLER = Caller(agent="agent-a", principal="kim")


@pytest.fixture(autouse=True)
async def keys(secrets_store: FileSecretStore) -> None:
    await secrets_store.put("tracker/readonly", "read-key-1234567890")
    await secrets_store.put("tracker/write", "write-key")


async def test_an_exhausted_github_core_limit_is_not_a_permission_problem(
    librarian: Librarian, router: Router, audit: MemoryAuditSink
) -> None:
    reset = int(time.time()) + 15 * 60
    body = {
        "message": "API rate limit exceeded for user ID 42. If you reach out to GitHub "
        "Support for help, please include the request ID ABC.",
        "documentation_url": "https://docs.github.com/rest/overview/rate-limits-for-the-rest-api",
    }
    router.add(
        "GET",
        f"{BASE}/v1/issues/RL-1",
        lambda r: httpx.Response(
            403,
            json=body,
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)},
        ),
    )
    result = await librarian.executor.execute(
        CallRequest(api="tracker", operation="getIssue", args={"key": "RL-1"}), CALLER
    )
    assert not result.ok and result.status_code == 403 and result.error is not None
    assert "rate limiting this credential" in result.error
    assert "permissions are fine" in result.error and "lacks permission" not in result.error
    expected = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(reset))
    assert f"until {expected} (in 15 min); retry then" in result.error
    assert "API rate limit exceeded for user ID 42" in result.error
    assert librarian.broker.pending_for("tracker") is None  # nobody was paged
    event = audit.events[-1]
    assert event.event == "call" and event.detail is not None
    assert event.detail.startswith("rate limited by the API: the rate limit is exhausted until")
    assert event.extra["upstream"].startswith("API rate limit exceeded")


async def test_a_429_with_retry_after_on_a_write_says_when(
    librarian: Librarian, router: Router, audit: MemoryAuditSink
) -> None:
    router.add(
        "POST",
        f"{BASE}/v1/issues/PAY-1/comments",
        lambda r: httpx.Response(
            429, json={"error": "Too many requests"}, headers={"Retry-After": "120"}
        ),
    )
    entry = librarian.registry.get("tracker")
    assert entry is not None
    op = entry.operation("addComment")
    assert op is not None
    result = await librarian.executor.execute_write(entry, op, comment(), KIM)
    assert not result.ok and result.status_code == 429 and result.error is not None
    assert "rate limiting this credential (HTTP 429)" in result.error
    assert "(in 2 min); retry then" in result.error
    assert "check the write credential" not in result.error
    assert audit.events[-1].event == "write"
    assert audit.events[-1].detail is not None and "in 2 min" in audit.events[-1].detail


async def test_a_secondary_limit_without_headers_is_still_recognised(
    librarian: Librarian, router: Router
) -> None:
    body = {"message": "You have exceeded a secondary rate limit. Please wait a few minutes."}
    router.add("GET", f"{BASE}/v1/issues/RL-2", lambda r: httpx.Response(403, json=body))
    result = await librarian.executor.execute(
        CallRequest(api="tracker", operation="getIssue", args={"key": "RL-2"}), CALLER
    )
    assert result.error is not None
    assert "a secondary rate limit is exhausted and the API gave no reset time" in result.error


async def test_a_plain_403_quotes_the_api_with_credentials_removed(
    librarian: Librarian, router: Router, audit: MemoryAuditSink
) -> None:
    body = {
        "message": "Resource not accessible by integration (token ghp_abcdefghijklmnopqrstuv0123)"
    }
    router.add("GET", f"{BASE}/v1/issues/RL-3", lambda r: httpx.Response(403, json=body))
    result = await librarian.executor.execute(
        CallRequest(api="tracker", operation="getIssue", args={"key": "RL-3"}), CALLER
    )
    assert result.error is not None and "lacks permission" in result.error
    assert "Resource not accessible by integration" in result.error
    assert "ghp_" not in result.error and "[redacted]" in result.error
    assert audit.events[-1].detail == "permission denied by the API"
    assert "ghp_" not in audit.events[-1].extra["upstream"]
    # the credential itself never appears, even if an API echoed it
    router.add(
        "GET",
        f"{BASE}/v1/issues/RL-4",
        lambda r: httpx.Response(400, json={"message": "bad token read-key-1234567890"}),
    )
    result = await librarian.executor.execute(
        CallRequest(api="tracker", operation="getIssue", args={"key": "RL-4"}), CALLER
    )
    assert result.status_code == 400 and result.error is None
    assert audit.events[-1].extra["upstream"] == "bad token [redacted]"
