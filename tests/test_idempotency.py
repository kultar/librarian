"""Idempotency keys across replicas and decisions; what the agent is told about a write."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from librarian.approvals import ApprovalState, Grant, WriteApproval
from librarian.calls import Caller, CallRequest
from librarian.config import ApproverSettings
from librarian.credentials import CredentialRequest
from librarian.registry.models import ApiEntry
from librarian.secrets import FileSecretStore
from librarian.service import Librarian, WriteOutcome
from librarian.web import credential_routes
from tests.conftest import BASE, Router
from tests.test_state import replica
from tests.test_writes import KIM, approve_when_open, comment


@pytest.fixture(autouse=True)
async def keys(secrets_store: FileSecretStore, router: Router) -> None:
    await secrets_store.put("tracker/readonly", "read-key")
    await secrets_store.put("tracker/write", "write-key")
    router.add(
        "POST", f"{BASE}/v1/issues/PAY-1/comments", lambda r: httpx.Response(201, json={"id": 7})
    )


async def two_replicas(tmp_path: Path, router: Router) -> tuple[Librarian, Librarian]:
    a, b = replica(tmp_path, router), replica(tmp_path, router)
    await a.start()
    await b.start()
    await a.secrets.put("tracker/write", "w")
    counter = {"n": 0}

    def created(_: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        return httpx.Response(201, json={"id": counter["n"]})

    router.add("POST", f"{BASE}/v1/issues/PAY-1/comments", created)
    return a, b


async def test_a_repeat_on_another_replica_returns_the_first_result(
    tmp_path: Path, router: Router
) -> None:
    a, b = await two_replicas(tmp_path, router)
    task = asyncio.create_task(a.write(comment(idempotency_key="blob-1"), KIM))
    await approve_when_open(a)
    first = await asyncio.wait_for(task, 5)
    assert first.how == "approved" and first.result.data == {"id": 1}
    assert first.message.startswith("Approved by kim@x.test")
    # the same key again, on the other replica (and after a restart it is the same path)
    again = await b.write(comment(idempotency_key="blob-1"), KIM)
    assert again.how == "idempotent" and again.result.data == {"id": 1}
    assert "Not sent again" in again.message and "blob-1" in again.message
    assert len([r for r in router.requests if r.method == "POST"]) == 1
    # a different key is a new write
    other = asyncio.create_task(b.write(comment(idempotency_key="blob-2"), KIM))
    while not b.approvals.pending():
        await asyncio.sleep(0.01)
    await b.approvals.approve(b.approvals.pending()[0].id, "kim@x.test")
    assert (await asyncio.wait_for(other, 5)).result.data == {"id": 2}
    await a.aclose()
    await b.aclose()


async def test_a_repeat_while_pending_joins_the_earlier_approval(
    tmp_path: Path, router: Router
) -> None:
    a, b = await two_replicas(tmp_path, router)
    pending = await a.request_write(comment(idempotency_key="commit-1"), KIM)
    assert isinstance(pending, WriteApproval)
    repeat = await b.request_write(comment(idempotency_key="commit-1"), KIM)
    assert isinstance(repeat, WriteApproval) and repeat.id == pending.id  # not a second card
    assert b.approvals.pending() == [repeat]
    await a.aclose()
    await b.aclose()


async def test_grant_and_automatic_writes_are_idempotent_too(
    tmp_path: Path, router: Router
) -> None:
    a, b = await two_replicas(tmp_path, router)
    a.approvals._grants["g"] = Grant(  # pyright: ignore[reportPrivateUsage]
        id="g", agent="notes-bot", api="tracker", operation="addComment", created_by="kim@x.test"
    )
    under_grant = await a.write(comment(idempotency_key="tree-1"), KIM)
    assert under_grant.how == "grant" and under_grant.approval_id is not None
    assert "standing grant (g, created by kim@x.test, until it is revoked)" in under_grant.message
    # write_status knows the write, on either replica
    found = await b.approvals.load(under_grant.approval_id)
    assert found is not None and found.state is ApprovalState.EXECUTED
    status = b.write_status(found)
    assert status["result"]["data"] == {"id": 1} and status["result_as_of"]
    assert "may have changed since" in status["message"]
    again = await b.write(comment(idempotency_key="tree-1"), KIM)
    assert again.how == "idempotent" and again.result.data == {"id": 1}
    assert again.grant_id == "g"
    # automatic approval: the same
    entry = a.registry.get("tracker")
    assert entry is not None
    ops = [
        o.model_copy(update={"approval": "auto", "auto_limit_per_day": 5})
        if o.id == "addComment"
        else o
        for o in entry.operations
    ]
    await a.registry.save(entry.model_copy(update={"operations": ops}))
    del a.approvals._grants["g"]  # pyright: ignore[reportPrivateUsage]
    auto = await a.write(comment(idempotency_key="ref-1"), KIM)
    assert auto.how == "auto" and "Approved automatically" in auto.message
    assert auto.approval_id is not None
    repeat = await a.write(comment(idempotency_key="ref-1"), KIM)
    assert repeat.how == "idempotent" and repeat.result.data == auto.result.data
    assert len([r for r in router.requests if r.method == "POST"]) == 2
    await a.aclose()
    await b.aclose()


async def test_a_failed_or_denied_write_does_not_block_the_key(librarian: Librarian) -> None:
    task = asyncio.create_task(librarian.write(comment(idempotency_key="k"), KIM))
    while not librarian.approvals._approvals:  # pyright: ignore[reportPrivateUsage]
        await asyncio.sleep(0.01)
    approval = next(iter(librarian.approvals._approvals.values()))  # pyright: ignore[reportPrivateUsage]
    await librarian.approvals.deny(approval.id, "kim@x.test")
    with_error = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(with_error[0], Exception)
    fresh = await librarian.request_write(comment(idempotency_key="k"), KIM)
    assert isinstance(fresh, WriteApproval) and fresh.id != approval.id


async def test_write_status_says_the_result_is_as_of_execution(librarian: Librarian) -> None:
    task = asyncio.create_task(librarian.write(comment(), KIM))
    approval_id = await approve_when_open(librarian)
    outcome: WriteOutcome = await asyncio.wait_for(task, 5)
    assert outcome.how == "approved" and outcome.message == "Approved by kim@x.test and run once."
    found = await librarian.approvals.load(approval_id)
    assert found is not None and found.executed_at is not None
    status = librarian.write_status(found)
    assert status["result_as_of"] == found.executed_at.isoformat()
    assert "Read it again for its current state" in status["message"]


async def test_saml_enforcement_is_explained(librarian: Librarian, router: Router) -> None:
    body = {
        "message": "Resource protected by organization SAML enforcement. You must grant your "
        "Personal Access token access to this organization.",
        "documentation_url": "https://docs.github.com/articles/authenticating-to-a-github-organization-with-saml-single-sign-on/",
    }
    router.add("GET", f"{BASE}/v1/issues/ORG-1", lambda r: httpx.Response(403, json=body))
    caller = Caller(agent="agent-a")
    result = await librarian.executor.execute(
        CallRequest(api="tracker", operation="getIssue", args={"key": "ORG-1"}), caller
    )
    assert not result.ok and result.error is not None
    assert "SAML single sign-on" in result.error and "Configure SSO" in result.error


async def test_credential_page_mentions_saml_for_github(librarian: Librarian) -> None:
    entry = librarian.registry.get("tracker")
    assert entry is not None
    github = ApiEntry.model_validate(
        {
            **entry.model_dump(mode="json", exclude_none=True, exclude={"host"}),
            "id": "github",
            "name": "GitHub",
            "base_url": "https://api.github.com",
        }
    )
    await librarian.registry.save(github)
    request = await librarian.broker.open(github, Caller(agent="agent-a"), "missing")
    assert isinstance(request, CredentialRequest)
    token = librarian.broker.token_for(request)
    client = TestClient(Starlette(routes=credential_routes(librarian.broker, ApproverSettings())))
    page = client.get(f"/credentials/{request.id}?t={token}").text
    assert "SAML single sign-on" in page and "Configure SSO" in page
    tracker = await librarian.broker.open(entry, Caller(agent="agent-b"), "missing")
    other = client.get(f"/credentials/{tracker.id}?t={librarian.broker.token_for(tracker)}").text
    assert "SAML" not in other
