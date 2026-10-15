"""Decisions on verified entries: applied on the proposal's head, merged at once."""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from librarian.audit import MemoryAuditSink
from librarian.calls import WriteRequest
from librarian.openapi import entry_from_spec
from librarian.publish import PublishError
from librarian.registry.models import Access, ApiEntry, Operation
from librarian.registry.store import dump_entry
from librarian.service import Librarian, LibrarianError, WriteNotEnabled
from tests.test_console import DecisionPublisher, console, token
from tests.test_enable_writes import enable_client, without_write_credential
from tests.test_publish import SPEC, FakeGitHub, publisher
from tests.test_writes import KIM


def writable(entry: ApiEntry, op_id: str) -> ApiEntry:
    ops = [
        o.model_copy(update={"access": Access.WRITE, "writable": True}) if o.id == op_id else o
        for o in entry.operations
    ]
    return entry.model_copy(update={"operations": ops})


def is_writable(entry: ApiEntry | None, op_id: str) -> bool:
    op = entry.operation(op_id) if entry is not None else None
    return op is not None and op.writable


async def test_refresh_applies_the_decision_on_the_proposal_head() -> None:
    fake = FakeGitHub(file_exists=True)
    live = entry_from_spec(SPEC, api_id="jira")
    first = next(op.id for op in live.operations if op.access is Access.WRITE)
    second = next(op.id for op in live.operations if op.access is Access.WRITE and op.id != first)
    # An earlier decision is waiting on the proposal branch: the live entry does not have it.
    fake.branch_files["librarian/onboard-jira-20261012"] = dump_entry(writable(live, first))
    url = await publisher(fake).refresh(live, "second decision", lambda e: writable(e, second))
    assert url == "https://github.com/acme/registry/pull/7"
    head = ApiEntry.model_validate(
        yaml.safe_load(fake.branch_files["librarian/onboard-jira-20261012"])
    )
    assert head.operation(first).writable and head.operation(second).writable  # type: ignore[union-attr]
    # the same decision again changes nothing: no commit, no comment
    before = len(fake.calls)
    await publisher(fake).refresh(live, "again", lambda e: writable(e, second))
    assert [m for m, _, _ in fake.calls[before:]] == ["GET", "GET"]
    # without a change, the entry replaces the file (a re-learned entry)
    await publisher(fake).refresh(live, "replace")
    head = ApiEntry.model_validate(
        yaml.safe_load(fake.branch_files["librarian/onboard-jira-20261012"])
    )
    assert not head.operation(first).writable  # type: ignore[union-attr]


class HeadPublisher(DecisionPublisher):
    """A proposal with a head that decisions are applied to, and a merge button."""

    def __init__(self, head: ApiEntry | None = None, merge_error: str | None = None) -> None:
        super().__init__(open_for=set())
        self.head = head
        self.merged: list[str] = []
        self.merge_error = merge_error

    async def refresh(self, entry: Any, note: str, change: Any = None) -> str | None:
        if self.head is None:
            return None
        self.head = change(self.head) if change is not None else entry
        self.refreshed.append((entry.id, note))
        return "https://github.test/pr/open"

    async def propose_update(self, entry: Any, title: str, body: str) -> str:
        self.head = entry
        self.proposed.append((entry.id, title))
        return "https://github.test/pr/update"

    async def merge(self, api_id: str, by: str) -> str | None:
        if self.merge_error:
            raise PublishError(self.merge_error)
        self.merged.append(api_id)
        return "https://github.test/pr/update"


async def add_mystery(librarian: Librarian) -> None:
    entry = librarian.registry.get("tracker")
    assert entry is not None
    mystery = Operation(id="mystery", method="POST", path="/mystery", access=Access.UNKNOWN)
    await librarian.registry.save(
        entry.model_copy(update={"operations": [*entry.operations, mystery]})
    )


async def test_two_decisions_in_a_row_both_survive_and_are_merged(
    librarian: Librarian, audit: MemoryAuditSink
) -> None:
    librarian.settings.approvers.console = "open"
    await without_write_credential(librarian)
    await add_mystery(librarian)
    pub = HeadPublisher()
    librarian.publisher = pub
    flow = WriteRequest(api="tracker", operation="renameProject", args={"p": "PAY"}, reason="r")
    with pytest.raises(WriteNotEnabled) as info:
        await librarian.write(flow, KIM)
    asked = info.value.request
    client = enable_client(librarian)
    t = librarian.enablement.token_for(asked)
    done = client.post(f"/enable/{asked.id}", data={"t": t, "action": "enable", "key": "read"})
    assert done.status_code == 200 and "Merged; live once the registry syncs" in done.text
    assert pub.merged == ["tracker"] and pub.head is not None
    assert (
        is_writable(pub.head, "renameProject") and pub.head.write_secret_ref == "tracker/readonly"
    )  # type: ignore[union-attr]
    assert asked.note is not None and asked.note.startswith("merged")
    status = await librarian.enable_status(asked.id, KIM)
    assert status["state"] == "enabled" and "waiting for the registry to sync" in status["message"]

    # a second decision lands on top of the first, not instead of it
    c = console(librarian)
    decided = c.post(
        "/apis/tracker/ops/mystery/access", data={"t": token(librarian), "choice": "read"}
    )
    assert decided.status_code == 200 and "Merged; live once the registry syncs" in decided.text
    assert (
        is_writable(pub.head, "renameProject") and pub.head.write_secret_ref == "tracker/readonly"
    )  # type: ignore[union-attr]
    assert pub.head.operation("mystery").access is Access.READ  # type: ignore[union-attr]
    assert (
        "enabled writes for `renameProject`" in pub.head.notes
        and "marked `mystery` as read" in pub.head.notes
    )
    assert pub.merged == ["tracker", "tracker"]
    assert (
        audit.events[-1].event == "operation_access_set"
        and audit.events[-1].extra["merged"] is True
    )
    # the live entry is untouched until the registry syncs
    live = librarian.registry.get("tracker")
    assert live is not None and live.write_auth is None


async def test_lost_decisions_can_be_reapplied_from_the_console(librarian: Librarian) -> None:
    librarian.settings.approvers.console = "open"
    await without_write_credential(librarian)
    pub = HeadPublisher()
    librarian.publisher = pub
    with pytest.raises(WriteNotEnabled) as info:
        await librarian.write(
            WriteRequest(api="tracker", operation="renameProject", args={"p": "PAY"}, reason="r"),
            KIM,
        )
    await librarian.enable_writes(info.value.request.id, "sam@x.test", "read")
    assert pub.head is not None and is_writable(pub.head, "renameProject")  # type: ignore[union-attr]
    # a 0.17.1 proposal built from the live entry dropped the decision
    live = librarian.registry.get("tracker")
    assert live is not None
    pub.head = live
    pub.merged.clear()
    c = console(librarian)
    page = c.get("/apis/tracker").text
    assert "Enabled writes not live yet" in page and "/apis/tracker/reapply" in page
    done = c.post("/apis/tracker/reapply", data={"t": token(librarian)})
    assert done.status_code == 200 and "renameProject" in done.text and "Merged" in done.text
    assert (
        is_writable(pub.head, "renameProject") and pub.head.write_secret_ref == "tracker/readonly"
    )  # type: ignore[union-attr]
    assert pub.merged == ["tracker"]
    # once the registry has it, there is nothing to re-apply
    await librarian.registry.save(pub.head)
    assert "Enabled writes not live yet" not in c.get("/apis/tracker").text
    with pytest.raises(LibrarianError, match="already live"):
        await librarian.reapply_decisions("tracker", "sam@x.test")


async def test_merging_can_be_turned_off_or_refused(
    librarian: Librarian, audit: MemoryAuditSink
) -> None:
    await add_mystery(librarian)
    librarian.settings.publish.merge_console_decisions = False
    pub = HeadPublisher()
    librarian.publisher = pub
    _, proposal = await librarian.set_operation_access("tracker", "mystery", "read", "sam")
    assert not proposal.merged and proposal.note == "proposed for review" and pub.merged == []
    librarian.settings.publish.merge_console_decisions = True
    librarian.publisher = HeadPublisher(merge_error="Base branch requires a review")
    _, proposal = await librarian.set_operation_access("tracker", "mystery", "read", "sam")
    assert not proposal.merged and "refused the merge" in proposal.note
    assert proposal.link == "https://github.test/pr/update"
    assert [ev.event for ev in audit.events][-2:] == [
        "proposal_merge_failed",
        "operation_access_set",
    ]
