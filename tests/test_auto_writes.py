"""Automatic approval: an approver decides in advance, per operation, with guard rails."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.testclient import TestClient

from librarian.approvals import WriteApproval
from librarian.audit import MemoryAuditSink
from librarian.calls import WriteRequest
from librarian.console import auto_routes
from librarian.executor import PolicyDenied
from librarian.registry.models import Access, ApiEntry, Operation
from librarian.registry.store import dump_entry
from librarian.secrets import FileSecretStore
from librarian.service import Librarian, WriteNotEnabled
from tests.conftest import BASE, RecordingNotifier, Router
from tests.test_console import console, token
from tests.test_enable_writes import enable_client, without_write_credential
from tests.test_proposals import HeadPublisher
from tests.test_writes import BOT, KIM, comment


@pytest.fixture(autouse=True)
async def keys(secrets_store: FileSecretStore, router: Router) -> None:
    await secrets_store.put("tracker/readonly", "read-key")
    await secrets_store.put("tracker/write", "write-key")
    router.add(
        "POST", f"{BASE}/v1/issues/PAY-1/comments", lambda r: httpx.Response(201, json={"id": 7})
    )


async def make_auto(librarian: Librarian, op_id: str = "addComment", cap: int = 100) -> ApiEntry:
    entry = librarian.registry.get("tracker")
    assert entry is not None
    ops = [
        o.model_copy(update={"approval": "auto", "auto_limit_per_day": cap}) if o.id == op_id else o
        for o in entry.operations
    ]
    updated = entry.model_copy(update={"operations": ops})
    await librarian.registry.save(updated)
    return updated


def test_automatic_approval_needs_a_writable_non_delete_operation() -> None:
    with pytest.raises(ValidationError, match="writable"):
        Operation(id="x", method="POST", path="/x", access=Access.WRITE, approval="auto")
    with pytest.raises(ValidationError, match="DELETE"):
        Operation(
            id="x", method="DELETE", path="/x", access=Access.WRITE, writable=True, approval="auto"
        )
    op = Operation(id="x", method="POST", path="/x", access=Access.WRITE, writable=True)
    assert (
        "approval" not in op.model_dump_json() and "auto_limit_per_day" not in op.model_dump_json()
    )
    auto = op.model_copy(update={"approval": "auto", "auto_limit_per_day": 5})
    assert (
        '"approval":"auto"' in auto.model_dump_json()
        and '"auto_limit_per_day":5' in auto.model_dump_json()
    )


async def test_automatic_writes_run_without_a_person_and_are_announced(
    librarian: Librarian, router: Router, notifier: RecordingNotifier, audit: MemoryAuditSink
) -> None:
    await make_auto(librarian)
    outcome = await asyncio.wait_for(librarian.write(comment(reason="ack"), KIM), 5)
    assert outcome.how == "auto" and outcome.result.data == {"id": 7}
    assert librarian.approvals.pending() == []  # no card, nothing to decide
    sent = [r for r in router.requests if r.method == "POST"]
    assert len(sent) == 1 and sent[0].headers["authorization"] == "Bearer write-key"
    kind, (notice, used, cap, link) = notifier.write_events[-1]
    assert kind == "auto" and used == 1 and cap == 100
    assert isinstance(notice, WriteApproval) and notice.agent == "notes-bot"
    assert link.startswith("https://librarian.example.test/auto/tracker/addComment?t=")
    events = [ev.event for ev in audit.events]
    assert events[-2:] == ["write", "write_auto"]
    assert audit.events[-1].detail == "auto-approved (1 of 100 today)"
    # the same policy checks still apply: a caller that may not write is refused
    with pytest.raises(PolicyDenied, match="may not write"):
        await librarian.write(comment(), KIM.model_copy(update={"write_apis": ()}))


async def test_past_the_daily_cap_a_person_is_asked_again(
    librarian: Librarian, audit: MemoryAuditSink
) -> None:
    await make_auto(librarian, cap=2)
    for _ in range(2):
        assert (await librarian.write(comment(), KIM)).how == "auto"
    pending = await librarian.request_write(comment(text="third"), BOT)
    assert isinstance(pending, WriteApproval) and pending.state.value == "pending"
    assert any(ev.event == "write_auto_capped" for ev in audit.events)


async def test_a_suspended_operation_is_not_auto_approved(librarian: Librarian) -> None:
    entry = await make_auto(librarian)
    op = entry.operation("addComment")
    assert op is not None
    await librarian.observations.suspend(entry, op, "the API changed under it")
    with pytest.raises(PolicyDenied, match="suspended"):
        await librarian.write(comment(), KIM)


async def test_the_fyi_link_puts_approval_back_with_one_click(
    librarian: Librarian, notifier: RecordingNotifier
) -> None:
    await make_auto(librarian)
    pub = HeadPublisher()
    librarian.publisher = pub
    await librarian.write(comment(), KIM)
    _, (_, _, _, link) = notifier.write_events[-1]
    client = TestClient(Starlette(routes=auto_routes(librarian)))
    path = link.removeprefix("https://librarian.example.test")
    page = client.get(path)
    assert page.status_code == 200 and "approved automatically, up to 100 a day" in page.text
    assert client.get("/auto/tracker/addComment?t=nope").status_code == 404
    tok = path.split("t=")[1]
    done = client.post("/auto/tracker/addComment", data={"t": tok})
    assert done.status_code == 200 and "needs approval again" in done.text and "Merged" in done.text
    assert pub.head is not None and pub.head.operation("addComment").approval == "each"  # type: ignore[union-attr]
    assert pub.merged == ["tracker"]
    assert "approved each time" in pub.head.notes


async def test_console_switches_approval_per_operation(
    librarian: Librarian, audit: MemoryAuditSink
) -> None:
    librarian.settings.approvers.console = "open"
    pub = HeadPublisher()
    librarian.publisher = pub
    c = console(librarian)
    t = token(librarian)
    page = c.get("/apis/tracker").text
    assert page.count("<option value=auto") == page.count("/ops/") - 1  # every op but DELETE
    assert "<option value=write selected>" in page  # addComment: approved each time today
    done = c.post(
        "/apis/tracker/ops/addComment/access", data={"t": t, "choice": "auto", "cap": "25"}
    )
    assert done.status_code == 200 and "Merged" in done.text
    op = pub.head.operation("addComment")  # type: ignore[union-attr]
    assert op is not None and op.writable and op.approval == "auto" and op.auto_limit_per_day == 25
    assert "approved automatically (up to 25/day)" in pub.head.notes  # type: ignore[union-attr]
    assert "approval: auto" in dump_entry(pub.head)  # type: ignore[arg-type]
    bad = c.post("/apis/tracker/ops/deleteIssue/access", data={"t": t, "choice": "auto"})
    assert bad.status_code == 400 and "never approved automatically" in bad.text
    # a read decision can be changed too, and writes disabled
    back = c.post("/apis/tracker/ops/getIssue/access", data={"t": t, "choice": "exclude"})
    assert back.status_code == 200
    excluded = pub.head.operation("getIssue")  # type: ignore[union-attr]
    assert excluded is not None and excluded.access is Access.WRITE and not excluded.writable
    assert audit.events[-1].event == "operation_access_set" and audit.events[-1].detail == "exclude"


async def test_console_write_choice_on_an_entry_without_a_write_key_asks_for_one(
    librarian: Librarian,
) -> None:
    librarian.settings.approvers.console = "open"
    await without_write_credential(librarian)
    librarian.publisher = HeadPublisher()
    c = console(librarian)
    done = c.post(
        "/apis/tracker/ops/renameProject/access",
        data={"t": token(librarian), "choice": "auto", "cap": "10"},
    )
    assert done.status_code == 200 and "Add the write credential" in done.text
    key = librarian.broker.pending_for("tracker", "write")
    assert key is not None and key.secret_ref == "tracker/write"
    head = librarian.publisher.head  # type: ignore[attr-defined]
    assert head is not None
    op = head.operation("renameProject")
    assert op is not None
    assert op.writable and op.approval == "auto" and op.auto_limit_per_day == 10


async def test_enable_page_offers_automatic_approval_for_the_flow(librarian: Librarian) -> None:
    from librarian.registry.models import Operation as Op

    entry = librarian.registry.get("tracker")
    assert entry is not None
    purge = Op(id="purge", method="DELETE", path="/purge", access=Access.WRITE)
    await librarian.registry.save(
        entry.model_copy(update={"operations": [*entry.operations, purge]})
    )
    pub = HeadPublisher()
    librarian.publisher = pub
    flow = WriteRequest(api="tracker", operation="renameProject", args={"p": "PAY"}, reason="r")
    with pytest.raises(WriteNotEnabled) as info:
        await librarian.write(flow.model_copy(update={"also_needs": ["purge"]}), KIM)
    asked = info.value.request
    client = enable_client(librarian)
    t = librarian.enablement.token_for(asked)
    page = client.get(f"/enable/{asked.id}?t={t}").text
    assert (
        "Approve writes automatically" in page and "DELETE operations always need approval" in page
    )
    done = client.post(
        f"/enable/{asked.id}",
        data={
            "t": t,
            "action": "enable",
            "op": ["renameProject", "purge"],
            "approval": "auto",
            "cap": "7",
        },
    )
    assert done.status_code == 200
    head = pub.head
    assert head is not None
    rename, purge_op = head.operation("renameProject"), head.operation("purge")
    assert rename is not None and rename.writable and rename.approval == "auto"
    assert rename.auto_limit_per_day == 7
    assert purge_op is not None and purge_op.writable and purge_op.approval == "each"
    assert "DELETE operations always need approval: purge" in head.notes


async def test_the_daily_count_is_shared_across_replicas(tmp_path: Any, router: Router) -> None:
    from tests.test_state import replica

    first = replica(tmp_path, router)
    await first.start()
    await first.secrets.put("tracker/write", "w")
    router.add(
        "POST", f"{BASE}/v1/issues/PAY-1/comments", lambda r: httpx.Response(201, json={"id": 1})
    )
    entry = first.registry.get("tracker")
    assert entry is not None
    ops = [
        o.model_copy(update={"approval": "auto", "auto_limit_per_day": 1})
        if o.id == "addComment"
        else o
        for o in entry.operations
    ]
    await first.registry.save(entry.model_copy(update={"operations": ops}))
    assert (await first.write(comment(), KIM)).how == "auto"
    second = replica(tmp_path, router)
    await second.start()
    pending = await second.request_write(comment(text="again"), KIM)
    assert isinstance(pending, WriteApproval)  # the other replica's write counted here
    await first.aclose()
    await second.aclose()
