"""How long an "allow similar writes" grant lasts: the approver's choice, within a ceiling."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from librarian.approvals import ApprovalError, Grant
from librarian.audit import MemoryAuditSink
from librarian.config import ApproverSettings, Settings
from librarian.secrets import FileSecretStore
from librarian.service import Librarian
from librarian.web import approval_routes, grant_length
from tests.conftest import BASE, Router
from tests.test_console import console, token
from tests.test_writes import KIM, approve_when_open, comment


@pytest.fixture(autouse=True)
async def keys(secrets_store: FileSecretStore, router: Router) -> None:
    await secrets_store.put("tracker/write", "write-key")
    router.add(
        "POST", f"{BASE}/v1/issues/PAY-1/comments", lambda r: httpx.Response(201, json={"id": 7})
    )


def test_the_ceiling_is_off_by_default() -> None:
    assert Settings().writes.max_grant_days is None


async def test_a_grant_can_last_until_revoked(librarian: Librarian, audit: MemoryAuditSink) -> None:
    task = asyncio.create_task(librarian.write(comment(), KIM))
    await approve_when_open(librarian, remember=True, pin=["key"], grant_days=None)
    await asyncio.wait_for(task, 5)
    grant = librarian.approvals.grants()[0]
    assert grant.expires_at is None and grant.usable(datetime.now(UTC) + timedelta(days=4000))
    created = next(ev for ev in audit.events if ev.event == "grant_created")
    assert created.extra["days"] is None
    assert (await librarian.write(comment(text="later"), KIM)).how == "grant"


async def test_custom_lengths_and_the_ceiling(librarian: Librarian) -> None:
    broker = librarian.approvals
    far = broker.grant_expiry(400)
    assert far is not None and far - datetime.now(UTC) > timedelta(days=399)
    with pytest.raises(ApprovalError, match="at least a day"):
        broker.grant_expiry(0)
    broker.max_grant_days = 30
    assert broker.grant_expiry(30) is not None
    with pytest.raises(ApprovalError, match="at most 30 days"):
        broker.grant_expiry(31)
    with pytest.raises(ApprovalError, match="at most 30 days"):
        broker.grant_expiry(None)
    # the ceiling applies when approving, before anything is decided
    task = asyncio.create_task(librarian.write(comment(), KIM))
    with pytest.raises(ApprovalError, match="at most 30 days"):
        await approve_when_open(librarian, remember=True, grant_days=None)
    assert broker.pending()  # still waiting for a valid decision
    task.cancel()


def test_the_page_offers_lengths_within_the_ceiling() -> None:
    from librarian.web import grant_length_field

    free = grant_length_field(None)
    assert "until revoked" in free and "90 days" in free and "a number of days" in free
    capped = grant_length_field(14)
    assert "until revoked" not in capped and "30 days" not in capped and "7 days" in capped
    assert "max=14" in capped
    assert grant_length({"days": "never"}) is None
    assert grant_length({"days": "custom", "days_custom": "45"}) == 45
    assert grant_length({"days": "30"}) == 30
    for bad in ({"days": "custom", "days_custom": ""}, {"days": "custom", "days_custom": "0"}):
        with pytest.raises(ValueError, match="how long"):
            grant_length(bad)


async def test_the_approval_page_takes_the_chosen_length(librarian: Librarian) -> None:
    task = asyncio.create_task(librarian.write(comment(), KIM))
    while not librarian.approvals._approvals:  # pyright: ignore[reportPrivateUsage]
        await asyncio.sleep(0.01)
    approval = next(iter(librarian.approvals._approvals.values()))  # pyright: ignore[reportPrivateUsage]
    t = librarian.approvals.token_for(approval)
    client = TestClient(Starlette(routes=approval_routes(librarian.approvals, ApproverSettings())))
    page = client.get(f"/approvals/{approval.id}?t={t}").text
    assert "until revoked" in page and "a number of days" in page
    bad = client.post(
        f"/approvals/{approval.id}",
        data={"t": t, "action": "grant", "days": "custom", "days_custom": "x"},
    )
    assert bad.status_code == 400 and "how long the grant lasts" in bad.text
    done = client.post(
        f"/approvals/{approval.id}",
        data={"t": t, "action": "grant", "days": "custom", "days_custom": "180"},
    )
    assert done.status_code == 200 and "Approved" in done.text
    await asyncio.wait_for(task, 5)
    grant = librarian.approvals.grants()[0]
    assert grant.expires_at is not None
    assert grant.expires_at - datetime.now(UTC) > timedelta(days=179)


async def test_the_console_adjusts_a_grant(librarian: Librarian, audit: MemoryAuditSink) -> None:
    librarian.settings.approvers.console = "open"
    broker = librarian.approvals
    broker._grants["g1"] = Grant(  # pyright: ignore[reportPrivateUsage]
        id="g1",
        agent="triage-bot",
        api="tracker",
        operation="addComment",
        expires_at=datetime.now(UTC) + timedelta(days=2),
        created_by="kim@x.test",
    )
    broker._grants["cfg"] = Grant(  # pyright: ignore[reportPrivateUsage]
        id="cfg",
        agent="bot",
        api="tracker",
        operation="addComment",
        created_by="config",
        source="config",
    )
    c = console(librarian)
    t = token(librarian)
    page = c.get("/grants").text
    assert "/grants/g1/expiry" in page and "1 days left" in page  # 2 days minus a moment
    # a config grant is changed in the file, not here
    assert "/grants/cfg/expiry" not in page and "until revoked" in page
    done = c.post("/grants/g1/expiry", data={"t": t, "days": "never"})
    assert done.status_code == 200 and "until it is revoked" in done.text
    assert broker._grants["g1"].expires_at is None  # pyright: ignore[reportPrivateUsage]
    assert audit.events[-1].event == "grant_adjusted" and audit.events[-1].extra["days"] is None
    shorter = c.post("/grants/g1/expiry", data={"t": t, "days": "custom", "days_custom": "5"})
    assert shorter.status_code == 200 and "5 more days" in shorter.text
    left = broker._grants["g1"].expires_at - datetime.now(UTC)  # type: ignore[operator]  # pyright: ignore[reportPrivateUsage, reportOptionalOperand]
    assert timedelta(days=4) < left <= timedelta(days=5)
    assert c.post("/grants/cfg/expiry", data={"t": t, "days": "7"}).status_code == 400
    broker.max_grant_days = 3
    capped = c.post("/grants/g1/expiry", data={"t": t, "days": "never"})
    assert capped.status_code == 400 and "at most 3 days" in capped.text
    await broker.revoke_grant("g1", "kim@x.test")
    assert c.post("/grants/g1/expiry", data={"t": t, "days": "1"}).status_code == 400
