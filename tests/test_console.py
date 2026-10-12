"""The operator console on the pages listener: what librarian knows, and the levers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from librarian.audit import MemoryAuditSink
from librarian.calls import Caller
from librarian.console import console_routes
from librarian.registry.models import EntryStatus
from librarian.secrets import FileSecretStore
from librarian.service import Librarian
from tests.test_web import themed
from tests.test_writes import KIM, comment

SIGNED_IN = {"X-Auth-Request-Email": "sam@x.test", "X-Auth-Request-Groups": "staff,api-approvers"}


def console(librarian: Librarian) -> TestClient:
    return TestClient(Starlette(routes=console_routes(librarian)))


async def test_console_needs_a_signed_in_person_unless_opened(librarian: Librarian) -> None:
    approvers = librarian.settings.approvers
    c = console(librarian)
    assert approvers.console == "signed_in" and approvers.mode == "token"
    denied = c.get("/")
    assert denied.status_code == 403 and "needs a signed-in person" in denied.text
    themed(denied)
    approvers.console = "open"
    assert c.get("/").status_code == 200  # a laptop
    approvers.console = "signed_in"
    approvers.mode = "trusted_header"
    approvers.group = "api-approvers"
    assert c.get("/").status_code == 403  # no identity header from the proxy
    assert c.get("/", headers={"X-Auth-Request-Email": "eve@x.test"}).status_code == 403
    assert c.get("/", headers=SIGNED_IN).status_code == 200
    assert c.get("/apis/tracker").status_code == 403  # every console page is guarded
    approvers.console = "off"
    assert c.get("/", headers=SIGNED_IN).status_code == 404


async def test_overview_shows_apis_and_what_waits_on_people(librarian: Librarian) -> None:
    librarian.settings.approvers.console = "open"
    entry = librarian.registry.get("tracker")
    assert entry is not None
    request = await librarian.broker.open(entry, Caller(agent="agent-a"), "missing")
    task = asyncio.create_task(librarian.write(comment(text="hi"), KIM))
    while not librarian.approvals.pending():
        await asyncio.sleep(0.01)
    page = console(librarian).get("/")
    themed(page)
    text = page.text
    assert "Issue Tracker" in text and "/apis/tracker" in text
    assert "weather" in text and "status" in text
    assert f"/credentials/{request.id}?t=" in text  # the one-time link approvers get
    assert "addComment" in text and "notes-bot" in text and "kim@x.test" in text
    assert "/approvals/" in text and "method post" in text
    assert "verified" in text and "provisional" in text
    task.cancel()


async def test_api_page_lists_operations_and_escapes(librarian: Librarian) -> None:
    librarian.settings.approvers.console = "open"
    entry = librarian.registry.get("tracker")
    assert entry is not None
    await librarian.registry.save(entry.model_copy(update={"notes": "<script>alert(1)</script>"}))
    c = console(librarian)
    page = c.get("/apis/tracker")
    themed(page)
    text = page.text
    assert "&lt;script&gt;" in text and "<script>" not in text
    assert "deleteIssue" in text and "method delete" in text and "method get" in text
    assert "tracker/readonly" in text and "tracker/write" in text  # references only
    assert "read (method)" in text or "read (reviewed)" in text
    assert "write" in text
    assert c.get("/apis/nope").status_code == 404


# -- actions -------------------------------------------------------------------------------


def token(librarian: Librarian, identity: str = "someone (console open)") -> str:
    return librarian.approvals.page_token("console", identity)


async def test_actions_need_the_persons_own_form_token(librarian: Librarian) -> None:
    librarian.settings.approvers.console = "open"
    c = console(librarian)
    forged = c.post("/apis/status/block")  # no token: another site's form
    assert forged.status_code == 403 and "not issued to you" in forged.text
    wrong = c.post("/apis/status/block", data={"t": token(librarian, "eve@x.test")})
    assert wrong.status_code == 403
    entry = librarian.registry.get("status")
    assert entry is not None and entry.status is not EntryStatus.BLOCKED  # nothing happened
    # signed-in mode binds the token to the identity the proxy asserts
    approvers = librarian.settings.approvers
    approvers.console, approvers.mode, approvers.group = "signed_in", "trusted_header", None
    sam = {"X-Auth-Request-Email": "sam@x.test"}
    page = c.get("/apis/status", headers=sam)
    assert token(librarian, "sam@x.test") in page.text
    assert token(librarian, "eve@x.test") not in page.text


async def test_block_unblock_and_forget(
    librarian: Librarian, audit: MemoryAuditSink, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    librarian.settings.approvers.console = "open"
    c = console(librarian)
    t = token(librarian)
    assert c.post("/apis/tracker/block", data={"t": t}).status_code == 200
    tracker = librarian.registry.get("tracker")
    assert tracker is not None and tracker.status is EntryStatus.BLOCKED
    assert audit.events[-1].event == "api_blocked" and audit.events[-1].principal
    assert "Unblock" in c.get("/apis/tracker").text
    assert c.post("/apis/tracker/unblock", data={"t": t}).status_code == 200
    tracker = librarian.registry.get("tracker")
    assert tracker is not None and tracker.status is EntryStatus.VERIFIED  # as reviewed
    # a reviewed entry cannot be forgotten from the console
    refused = c.post("/apis/tracker/forget", data={"t": t})
    assert refused.status_code == 400 and "registry repo" in refused.text
    assert librarian.registry.get("tracker") is not None
    # a learned entry (overlay file) can
    from librarian import service as service_module
    from tests.test_openapi import SPEC

    async def fake_fetch(url: str, client: Any, *, net: Any) -> Any:
        return SPEC

    monkeypatch.setattr(service_module, "fetch_spec", fake_fetch)
    librarian.registry.overlay = tmp_path / "overlay"
    await librarian.onboard("learned", spec_url="https://x.example/spec.json")
    assert "Forget this API" in c.get("/apis/learned").text
    assert c.post("/apis/learned/forget", data={"t": t}).status_code == 200
    assert librarian.registry.get("learned") is None
    assert not (tmp_path / "overlay" / "learned.yaml").exists()
    assert audit.events[-1].event == "api_forgotten"


async def test_invalidate_credential_overwrites_the_secret(
    librarian: Librarian, secrets_store: FileSecretStore, audit: MemoryAuditSink
) -> None:
    librarian.settings.approvers.console = "open"
    await secrets_store.put("tracker/readonly", "real-key")
    await secrets_store.put("tracker/write", "real-write-key")
    done = console(librarian).post("/apis/tracker/invalidate", data={"t": token(librarian)})
    assert done.status_code == 200 and "fresh key" in done.text
    assert await secrets_store.get("tracker/readonly") not in (None, "real-key")
    assert await secrets_store.get("tracker/write") not in (None, "real-write-key")
    assert "real-key" not in done.text
    assert audit.events[-1].event == "credential_invalidated"
    assert audit.events[-1].extra["refs"] == ["tracker/readonly", "tracker/write"]
    none = console(librarian).post("/apis/status/invalidate", data={"t": token(librarian)})
    assert none.status_code == 400 and "no credential" in none.text


async def test_relearn_from_a_given_source_and_recheck(
    librarian: Librarian, monkeypatch: pytest.MonkeyPatch
) -> None:
    from librarian import service as service_module
    from tests.test_openapi import SPEC

    async def fake_fetch(url: str, client: Any, *, net: Any) -> Any:
        assert url == "https://status.example.test/openapi.json"
        return SPEC

    monkeypatch.setattr(service_module, "fetch_spec", fake_fetch)
    librarian.settings.approvers.console = "open"
    c = console(librarian)
    t = token(librarian)
    assert "Re-learn from documentation" in c.get("/apis/status").text
    empty = c.post("/apis/status/relearn", data={"t": t, "base_url": "https://x"})
    assert empty.status_code == 400
    done = c.post(
        "/apis/status/relearn",
        data={
            "t": t,
            "spec_url": "https://status.example.test/openapi.json",
            "base_url": "https://status.example.test",
        },
    )
    assert done.status_code == 200 and "Re-learned" in done.text, done.text
    entry = librarian.registry.get("status")
    assert entry is not None and entry.source.kind == "openapi"
    assert len(entry.operations) == len(SPEC["paths"]) or entry.operations
    assert "Re-check now" in c.get("/apis/status").text
    report = c.post("/apis/status/recheck", data={"t": t})
    assert report.status_code == 200 and "Re-check:" in report.text
    # verified entries are not re-learned from the console
    assert "registry repo" in c.get("/apis/tracker").text
    refused = c.post("/apis/tracker/relearn", data={"t": t, "spec_url": "https://x/spec.json"})
    assert refused.status_code == 400


async def test_revoke_grant_and_clear_suspension(
    librarian: Librarian, audit: MemoryAuditSink
) -> None:
    from librarian.approvals import Grant

    librarian.settings.approvers.console = "open"
    librarian.approvals._grants["g1"] = Grant(  # pyright: ignore[reportPrivateUsage]
        id="g1",
        agent="triage-bot",
        api="tracker",
        operation="addComment",
        pinned_args={},
        created_by="kim@x.test",
        source="approval",
    )
    c = console(librarian)
    t = token(librarian)
    assert "/grants/g1/revoke" in c.get("/").text
    assert c.post("/grants/g1/revoke", data={"t": t}).status_code == 200
    assert librarian.approvals._grants["g1"].revoked  # pyright: ignore[reportPrivateUsage]
    assert "/grants/g1/revoke" not in c.get("/").text
    assert c.post("/grants/nope/revoke", data={"t": t}).status_code == 400
    tracker = librarian.registry.get("tracker")
    assert tracker is not None
    op = tracker.operation("listIssues")
    assert op is not None
    await librarian.observations.suspend(tracker, op, "kept answering 404")
    page = c.get("/apis/tracker").text
    assert "suspended: kept answering 404" in page and "/ops/listIssues/clear" in page
    assert c.post("/apis/tracker/ops/listIssues/clear", data={"t": t}).status_code == 200
    assert librarian.observations.suspended(tracker, op) is None
    assert audit.events[-1].event == "suspension_cleared"
    assert c.post("/apis/tracker/ops/nope/clear", data={"t": t}).status_code == 404
