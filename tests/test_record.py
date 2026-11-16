"""Decisions are made in the database; the record is written afterwards, in the
background, and never stands between a person's decision and its effect."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from librarian.audit import MemoryAuditSink
from librarian.calls import Caller, WriteRequest
from librarian.enablement import EnableState
from librarian.publish import PublishError
from librarian.record import commit_message
from librarian.registry.models import Access, ApiEntry, EntryStatus
from librarian.service import Librarian, LibrarianError, WriteNotEnabled
from tests.conftest import RecordPublisher, Router
from tests.test_console import console, token
from tests.test_enable_writes import without_write_credential
from tests.test_openapi import SPEC
from tests.test_state import replica
from tests.test_writes import KIM

AGENT = Caller(agent="agent-a")


def writable(entry: ApiEntry | None, op_id: str) -> bool:
    op = entry.operation(op_id) if entry is not None else None
    return op is not None and op.writable


async def test_two_decisions_in_a_row_are_both_in_effect_and_both_recorded(
    librarian: Librarian, audit: MemoryAuditSink
) -> None:
    record = RecordPublisher()
    librarian.publisher = record
    await librarian.set_operation_access("tracker", "renameProject", "write", "sam@x.test")
    await librarian.set_operation_access("tracker", "getIssue", "exclude", "kim@x.test")
    live = librarian.registry.get("tracker")
    assert live is not None and writable(live, "renameProject")
    assert live.operation("getIssue").access is Access.WRITE  # type: ignore[union-attr]
    # one commit per decision, each naming who decided
    assert [m.splitlines()[0] for _, m in record.commits] == [
        "tracker: renameProject is write",
        "tracker: getIssue is exclude",
    ]
    assert "Decided by sam@x.test in librarian." in record.commits[0][1]
    assert "Decided by kim@x.test in librarian." in record.commits[1][1]
    written = record.files["tracker"]
    assert writable(written, "renameProject") and not writable(written, "getIssue")
    assert [ev.event for ev in audit.events].count("recorded") == 2
    assert audit.events[-1].event == "operation_access_set" and audit.events[-1].extra == {
        "recorded": True
    }
    status = await librarian.recorder.status("tracker")
    assert status is not None and status.state == "recorded" and status.messages == []
    assert status.url == "https://github.test/acme/registry/commit/2"


async def test_what_librarian_looked_up_at_runtime_is_not_written_to_the_record(
    librarian: Librarian,
) -> None:
    record = RecordPublisher()
    librarian.publisher = record
    tracker = librarian.registry.get("tracker")
    assert tracker is not None
    # Names read from the API with its credential (discovery) stay in the database.
    await librarian.registry.save(
        tracker.model_copy(update={"glossary": {"customfield_10020": "Payroll sprint"}})
    )
    assert record.commits == []  # not a decision
    await librarian.block("tracker", "sam@x.test")
    assert record.files["tracker"].status is EntryStatus.BLOCKED
    assert record.files["tracker"].glossary == {}
    live = librarian.registry.get("tracker")
    assert live is not None and live.glossary == {"customfield_10020": "Payroll sprint"}


async def test_the_server_writes_the_record_in_the_background(librarian: Librarian) -> None:
    gate = asyncio.Event()

    class Slow(RecordPublisher):
        async def record(self, entry: ApiEntry, message: str) -> str:
            await gate.wait()  # GitHub is slow today
            return await super().record(entry, message)

    record = Slow()
    librarian.publisher = record
    librarian.recorder.background = True  # as the server runs
    _, applied = await librarian.set_operation_access(
        "tracker", "renameProject", "write", "sam@x.test"
    )
    # The decision returned without waiting for GitHub, and it is already in effect.
    assert applied.recorded and applied.note == "in effect now; being written to the record"
    assert writable(librarian.registry.get("tracker"), "renameProject")
    assert record.commits == []
    pending = await librarian.recorder.status("tracker")
    assert pending is not None and pending.state == "pending" and len(pending.messages) == 1
    # A second decision while the first write is still out goes into the same write.
    await librarian.set_operation_access("tracker", "getIssue", "exclude", "sam@x.test")
    gate.set()
    await librarian.recorder.drain()
    written = record.files["tracker"]
    assert writable(written, "renameProject") and not writable(written, "getIssue")
    done = await librarian.recorder.status("tracker")
    assert done is not None and done.state == "recorded" and done.messages == []


async def test_a_failed_write_is_kept_and_tried_again_later(
    librarian: Librarian, audit: MemoryAuditSink
) -> None:
    record = RecordPublisher()
    record.fail = "GitHub is down"
    librarian.publisher = record
    _, applied = await librarian.set_operation_access(
        "tracker", "renameProject", "write", "sam@x.test"
    )
    assert applied.recorded  # it is on its way; the decision does not depend on it arriving
    assert writable(librarian.registry.get("tracker"), "renameProject")
    failed = await librarian.recorder.status("tracker")
    assert failed is not None and failed.state == "failed" and failed.error == "GitHub is down"
    assert failed.attempts == 1 and len(failed.messages) == 1
    assert [s.api for s in await librarian.recorder.unwritten()] == ["tracker"]
    assert any(ev.event == "record_failed" for ev in audit.events)

    # Not hammered: the next try waits (two minutes after one failure, up to an hour).
    record.fail = None
    assert await librarian.recorder.retry_due() == 0 and record.commits == []
    failed.updated_at = datetime.now(UTC) - timedelta(minutes=3)
    assert await librarian.recorder.retry_due() == 1
    assert writable(record.files["tracker"], "renameProject")
    # the commit still says what was decided, and by whom
    assert record.commits[0][1].startswith("tracker: renameProject is write\n")
    assert "Decided by sam@x.test in librarian." in record.commits[0][1]
    assert await librarian.recorder.unwritten() == []
    assert await librarian.recorder.retry_due() == 0


def test_several_decisions_in_one_write_are_all_named() -> None:
    message = commit_message("tracker", ["tracker: a is write\n\nby sam", "tracker: b is read"])
    assert message.startswith("tracker: 2 decisions\n\n")
    assert "a is write" in message and "b is read" in message
    assert commit_message("tracker", ["tracker: only one"]) == "tracker: only one"
    assert "up to date" in commit_message("tracker", [])


async def test_a_write_left_by_a_replica_that_went_away_is_picked_up(
    tmp_path: Path, router: Router
) -> None:
    a, b = replica(tmp_path, router), replica(tmp_path, router)
    record = RecordPublisher()
    a.publisher = b.publisher = record
    await a.start()
    await b.start()
    record.fail = "GitHub is down"
    # (`a` is the replica that decided; its own write failed and then it went away)
    a.recorder.background = False
    await a.set_operation_access("tracker", "renameProject", "write", "sam@x.test")
    record.fail = None
    # The other replica sees the decision at once, and that its record is still owed.
    await b.registry.refresh_stored()
    assert writable(b.registry.get("tracker"), "renameProject")
    owed = await b.recorder.unwritten()
    assert [s.api for s in owed] == ["tracker"] and owed[0].state == "failed"
    assert await b.recorder.retry_due() == 0  # not due yet
    stale = owed[0].model_copy(update={"updated_at": datetime.now(UTC) - timedelta(hours=2)})
    await b.state.save_doc("record", "tracker", "failed", stale.model_dump_json())  # type: ignore[union-attr]
    assert await b.recorder.retry_due() == 1
    assert writable(record.files["tracker"], "renameProject")
    assert await a.recorder.unwritten() == []  # both replicas see it settled
    await a.aclose()
    await b.aclose()


async def test_decisions_on_two_replicas_do_not_overwrite_each_other(
    tmp_path: Path, router: Router
) -> None:
    a, b = replica(tmp_path, router), replica(tmp_path, router)
    record = RecordPublisher()
    a.publisher = b.publisher = record
    await a.start()
    await b.start()
    await a.set_operation_access("tracker", "renameProject", "write", "sam@x.test")
    # `b` has not swept since: it must not decide on top of what it last saw.
    await b.set_operation_access("tracker", "getIssue", "exclude", "kim@x.test")
    await a.aclose()
    await b.aclose()
    for lib in (a, b):
        await lib.registry.refresh_stored()
        live = lib.registry.get("tracker")
        assert writable(live, "renameProject")
        assert live is not None and live.operation("getIssue").access is Access.WRITE  # type: ignore[union-attr]
    written = record.files["tracker"]
    assert writable(written, "renameProject") and not writable(written, "getIssue")


async def test_a_lost_database_is_rebuilt_from_the_record(
    tmp_path: Path, router: Router, monkeypatch: pytest.MonkeyPatch
) -> None:
    from librarian import service as service_module

    async def fetch(url: str, client: Any, *, net: Any) -> Any:
        return SPEC

    monkeypatch.setattr(service_module, "fetch_spec", fetch)
    record = RecordPublisher()
    first = replica(tmp_path / "old", router)
    first.publisher = record
    await first.start()
    await first.onboard("jira", spec_url="https://x.example/spec.json", caller=AGENT)
    await first.approve_api("jira", "sam@x.test")
    await first.set_operation_access("tracker", "renameProject", "write", "sam@x.test")
    await first.aclose()
    assert set(record.files) == {"jira", "tracker"}

    # A new database (and no registry files at all).
    fresh = replica(tmp_path / "new", router)
    (tmp_path / "new" / "registry" / "tracker.yaml").unlink()
    fresh.publisher = record
    record.unreadable = {"broken.yaml": "expected a mapping"}
    await fresh.start()
    assert fresh.registry.all() == []  # nothing is read from the record on its own
    report = await fresh.restore_from_record("sam@x.test")
    assert sorted(report["restored"]) == ["jira", "tracker"] and report["kept"] == []
    assert report["unreadable"] == {"broken.yaml": "expected a mapping"}
    jira = fresh.registry.get("jira")
    assert jira is not None and jira.status is EntryStatus.VERIFIED
    assert writable(fresh.registry.get("tracker"), "renameProject")
    # Restoring again leaves what the database has alone...
    await fresh.set_operation_access("tracker", "renameProject", "exclude", "sam@x.test")
    await fresh.recorder.drain()
    record.files["tracker"] = record.files["tracker"].model_copy(update={"description": "Old"})
    again = await fresh.restore_from_record("sam@x.test")
    assert again["restored"] == [] and sorted(again["kept"]) == ["jira", "tracker"]
    kept = fresh.registry.get("tracker")
    assert kept is not None and kept.description != "Old"
    # ...unless told to take the record's version.
    await fresh.restore_from_record("sam@x.test", replace=True)
    replaced = fresh.registry.get("tracker")
    assert replaced is not None and replaced.description == "Old"
    await fresh.aclose()

    none = replica(tmp_path / "none", router)
    with pytest.raises(LibrarianError, match="no external record"):
        await none.restore_from_record("sam@x.test")
    record.fail = "GitHub is down"
    none.publisher = record
    with pytest.raises(LibrarianError, match="could not read the record: GitHub is down"):
        await none.restore_from_record("sam@x.test")
    await none.aclose()


async def test_pull_requests_from_before_0_20_are_closed_when_the_server_starts(
    tmp_path: Path, router: Router
) -> None:
    lib = replica(tmp_path, router)
    record = RecordPublisher()
    record.old_proposals = ["https://github.test/acme/registry/pull/7"]
    lib.publisher = record
    await lib.start()
    await asyncio.gather(*lib._background)  # pyright: ignore[reportPrivateUsage]
    assert record.old_proposals == []
    assert record.closed_with is not None and "approves in its own database" in record.closed_with
    assert isinstance(lib.audit, MemoryAuditSink)
    closed = next(ev for ev in lib.audit.events if ev.event == "old_proposals_closed")
    assert closed.extra["closed"] == ["https://github.test/acme/registry/pull/7"]
    await lib.aclose()

    class Down(RecordPublisher):
        async def close_proposals(self, reason: str) -> list[str]:
            raise PublishError("GitHub is down")

    other = replica(tmp_path / "other", router)
    other.publisher = Down()
    await other.start()  # a GitHub outage does not keep librarian from starting
    await asyncio.gather(*other._background)  # pyright: ignore[reportPrivateUsage]
    await other.aclose()


async def test_writes_enabled_before_0_20_but_never_merged_can_be_applied(
    librarian: Librarian, audit: MemoryAuditSink
) -> None:
    librarian.settings.approvers.console = "open"
    await without_write_credential(librarian)
    with pytest.raises(WriteNotEnabled) as info:
        await librarian.write(
            WriteRequest(api="tracker", operation="renameProject", args={"p": "PAY"}, reason="r"),
            KIM,
        )
    asked = info.value.request
    # What 0.19 left behind: the request says enabled, the change sat in a pull request.
    await librarian.enablement.decide(
        asked.id,
        EnableState.ENABLED,
        "sam@x.test",
        "proposed for review",
        review_url="https://github.com/acme/registry/pull/6",
        enabled=["renameProject"],
    )
    assert not writable(librarian.registry.get("tracker"), "renameProject")
    status = await librarian.enable_status(asked.id, KIM)
    assert status["state"] == "enabled"
    assert "an approver to apply it in the console" in status["message"]
    c = console(librarian)
    page = c.get("/apis/tracker").text
    assert "Enabled writes not in effect" in page and "/apis/tracker/reapply" in page
    done = c.post("/apis/tracker/reapply", data={"t": token(librarian)})
    assert done.status_code == 200 and "renameProject" in done.text and "In effect now" in done.text
    live = librarian.registry.get("tracker")
    assert writable(live, "renameProject")
    assert live is not None and live.write_secret_ref == "tracker/readonly"
    assert (await librarian.enable_status(asked.id, KIM))["state"] == "live"
    assert "Enabled writes not in effect" not in c.get("/apis/tracker").text
    with pytest.raises(LibrarianError, match="already in effect"):
        await librarian.reapply_decisions("tracker", "sam@x.test")
    # a later decision to switch the operation off is not mistaken for a lost one
    await librarian.set_operation_access("tracker", "renameProject", "exclude", "sam@x.test")
    assert "Enabled writes not in effect" not in c.get("/apis/tracker").text
    assert any(ev.event == "decisions_reapplied" for ev in audit.events)


async def test_without_a_record_decisions_are_simply_in_effect(librarian: Librarian) -> None:
    assert librarian.publisher is None
    _, applied = await librarian.set_operation_access(
        "tracker", "renameProject", "write", "sam@x.test"
    )
    assert not applied.recorded and applied.note == "in effect now"
    assert writable(librarian.registry.get("tracker"), "renameProject")
    with pytest.raises(LibrarianError, match="no external record"):
        await librarian.record_now("tracker", "sam@x.test")
    assert await librarian.recorder.retry_due() == 0


async def test_httpx_errors_from_github_do_not_reach_the_person(librarian: Librarian) -> None:
    class Unreachable(RecordPublisher):
        async def record(self, entry: ApiEntry, message: str) -> str:
            raise httpx.ConnectError("connection refused")

    librarian.publisher = Unreachable()
    _, applied = await librarian.set_operation_access(
        "tracker", "renameProject", "write", "sam@x.test"
    )
    assert applied.recorded and writable(librarian.registry.get("tracker"), "renameProject")
    status = await librarian.recorder.status("tracker")
    assert status is not None and status.state == "failed" and "refused" in (status.error or "")
