from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from librarian.calls import Caller, CallRequest
from librarian.credentials import RequestState
from librarian.registry import EntryStatus
from librarian.secrets import FileSecretStore
from librarian.service import CredentialUnavailable, Librarian
from tests.conftest import BASE, RecordingNotifier, Router

A = Caller(agent="agent-a", principal="kim")
B = Caller(agent="agent-b")


def tracker(lib: Librarian):  # type: ignore[no-untyped-def]
    entry = lib.registry.get("tracker")
    assert entry is not None
    return entry


async def test_requests_are_deduplicated(librarian: Librarian, notifier: RecordingNotifier) -> None:
    first = await librarian.broker.open(tracker(librarian), A, "missing")
    second = await librarian.broker.open(tracker(librarian), B, "missing")
    assert first.id == second.id
    assert first.waiting == 2
    assert first.requested_by == ["agent-a (for kim)", "agent-b"]
    assert [e for e, _ in notifier.events] == ["opened", "updated"]
    assert first.channel == "#tracker-admins"
    assert first.approver_group == "S0TRACKER"


async def test_waiting_call_resumes_after_key_is_added(
    librarian: Librarian, router: Router, notifier: RecordingNotifier
) -> None:
    router.add("GET", f"{BASE}/v1/me", lambda r: httpx.Response(200, json={"me": 1}))
    router.add("GET", f"{BASE}/v1/issues/X-1", lambda r: httpx.Response(200, json={"key": "X-1"}))
    auth_required: list[str] = []

    async def on_auth(req):  # type: ignore[no-untyped-def]
        auth_required.append(req.id)

    task = asyncio.create_task(
        librarian.execute(
            CallRequest(api="tracker", operation="getIssue", args={"key": "X-1"}), A, on_auth
        )
    )
    while not auth_required:
        await asyncio.sleep(0.01)
    error = await librarian.broker.provide_value(auth_required[0], "  new-token  ", "sam")
    assert error is None
    result = await asyncio.wait_for(task, 5)
    assert result.data == {"key": "X-1"}
    request = librarian.broker.get(auth_required[0])
    assert request is not None and request.state is RequestState.FULFILLED
    assert request.resolution_note is not None and "tested OK" in request.resolution_note
    # validation call + retried call both carried the new key
    assert all(r.headers["authorization"] == "Bearer new-token" for r in router.requests)
    assert notifier.events[-1][0] == "closed"


async def test_failed_validation_keeps_request_open(librarian: Librarian, router: Router) -> None:
    router.add("GET", f"{BASE}/v1/me", lambda r: httpx.Response(401))
    request = await librarian.broker.open(tracker(librarian), A, "missing")
    error = await librarian.broker.provide_value(request.id, "bad", "sam")
    assert error is not None and "did not work" in error
    assert request.state is RequestState.PENDING


async def test_deny_blocks_api_and_fails_waiters(librarian: Librarian) -> None:
    seen: list[str] = []

    async def on_auth(req):  # type: ignore[no-untyped-def]
        seen.append(req.id)

    task = asyncio.create_task(
        librarian.execute(CallRequest(api="tracker", operation="whoami"), A, on_auth)
    )
    while not seen:
        await asyncio.sleep(0.01)
    await librarian.broker.deny(seen[0], "sam")
    with pytest.raises(CredentialUnavailable, match="denied"):
        await asyncio.wait_for(task, 5)
    assert tracker(librarian).status is EntryStatus.BLOCKED


async def test_use_existing_secret_repoints_entry(
    librarian: Librarian, router: Router, secrets_store: FileSecretStore
) -> None:
    router.add("GET", f"{BASE}/v1/me", lambda r: httpx.Response(200, json={}))
    await secrets_store.put("shared/tracker-bot", "bot-token")
    request = await librarian.broker.open(tracker(librarian), A, "missing")
    assert await librarian.broker.use_existing(request.id, "nope/missing", "sam") is not None
    assert await librarian.broker.use_existing(request.id, "shared/tracker-bot", "sam") is None
    assert tracker(librarian).secret_ref == "shared/tracker-bot"
    assert router.requests[-1].headers["authorization"] == "Bearer bot-token"


async def test_tokens_are_one_time_and_expire(librarian: Librarian) -> None:
    request = await librarian.broker.open(tracker(librarian), A, "missing")
    token = librarian.broker.token_for(request)
    assert librarian.broker.check_token(request, token)
    assert not librarian.broker.check_token(request, "wrong")
    request.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert not librarian.broker.check_token(request, token)
    await librarian.broker.sweep()
    assert request.state is RequestState.EXPIRED
    assert not librarian.broker.check_token(request, token)


async def test_wait_times_out_without_expiring(librarian: Librarian) -> None:
    request = await librarian.broker.open(tracker(librarian), A, "missing")
    result = await librarian.broker.wait(request.id, max_wait=0.05)
    assert result.state is RequestState.PENDING
    assert result.waiting == 0
