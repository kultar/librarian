"""Noticing API changes: spec re-checks, runtime signals, suspensions, one-replica runs."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import httpx
import pytest

from librarian.approvals import ApprovalState, WriteApproval
from librarian.calls import Caller, CallRequest, WriteRequest
from librarian.drift import diff, proposed_update
from librarian.executor import PolicyDenied
from librarian.openapi import entry_from_spec
from librarian.registry.models import Access, ApiEntry, BearerAuth, EntryStatus
from librarian.reviews import ReviewState
from librarian.secrets import FileSecretStore
from librarian.service import Librarian, WriteRefused
from tests.conftest import RecordingNotifier, RecordPublisher, Router

HOST = "https://items.example.test"
SPEC_URL = f"{HOST}/openapi.json"
AGENT = Caller(agent="agent-a", write_apis=("items",), owner="ops@x.test")


def path_op(op_id: str, **extra: Any) -> dict[str, Any]:
    return {"operationId": op_id, **extra}


V1: dict[str, Any] = {
    "openapi": "3.0.0",
    "info": {"title": "Items"},
    "servers": [{"url": HOST}],
    "components": {"securitySchemes": {"t": {"type": "http", "scheme": "bearer"}}},
    "paths": {
        "/items": {"get": path_op("listItems", parameters=[{"name": "q", "in": "query"}])},
        "/items/{id}": {"get": path_op("getItem", parameters=[{"name": "id", "in": "path"}])},
        "/items/{id}/archive": {
            "get": path_op("archiveInfo", parameters=[{"name": "id", "in": "path"}])
        },
        "/items/{id}/notes": {
            "post": path_op("addNote", parameters=[{"name": "id", "in": "path"}])
        },
        "/old": {"get": path_op("oldThing")},
    },
}

V2: dict[str, Any] = copy.deepcopy(V1)
V2["paths"]["/items/{id}/archive"] = {  # a read became a write: safety
    "post": path_op("archiveInfo", parameters=[{"name": "id", "in": "path"}])
}
V2["paths"]["/items/{id}/notes"]["post"]["parameters"].append(  # breaking, on a writable op
    {"name": "author", "in": "query", "required": True}
)
del V2["paths"]["/old"]  # removed
V2["paths"]["/new"] = {"get": path_op("newThing")}  # added


class Spec:
    def __init__(self) -> None:
        self.current = V1

    def __call__(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=self.current)


@pytest.fixture
def spec(router: Router, librarian: Librarian) -> Spec:
    served = Spec()
    router.add("GET", SPEC_URL, served)
    librarian.source_client_factory = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(router)
    )
    return served


async def save_items(librarian: Librarian, status: EntryStatus) -> ApiEntry:
    entry = entry_from_spec(V1, api_id="items", spec_url=SPEC_URL)
    ops = [
        o.model_copy(update={"writable": True}) if o.id == "addNote" else o
        for o in entry.operations
    ]
    entry = entry.model_copy(
        update={
            "status": status,
            "operations": ops,
            "write_auth": BearerAuth(secret_ref="items/write"),
        }
    )
    await librarian.registry.save(entry)
    stored = librarian.registry.get("items")
    assert stored is not None
    return stored


def test_diff_sorts_changes_by_risk() -> None:
    old = entry_from_spec(V1, api_id="items", spec_url=SPEC_URL)
    new = entry_from_spec(V2, api_id="items", spec_url=SPEC_URL)
    found = {(c.operation, c.severity, c.kind) for c in diff(old, new)}
    assert found == {
        ("archiveInfo", "safety", "changed"),
        ("addNote", "breaking", "changed"),
        ("oldThing", "breaking", "removed"),
        ("newThing", "additive", "added"),
    }
    assert diff(old, entry_from_spec(V1, api_id="items", spec_url=SPEC_URL)) == []


def test_generated_ids_that_change_are_not_reported_as_changes() -> None:
    renamed = copy.deepcopy(V1)
    renamed["paths"]["/old"]["get"]["operationId"] = "fetchOld"
    old = entry_from_spec(V1, api_id="items", spec_url=SPEC_URL)
    assert diff(old, entry_from_spec(renamed, api_id="items", spec_url=SPEC_URL)) == []


async def changed_items(librarian: Librarian, spec: Spec) -> ApiEntry:
    """A verified API whose source just changed: re-checked, so an update waits."""
    entry = await save_items(librarian, EntryStatus.VERIFIED)
    assert (await librarian.recheck("items")).status == "unchanged"
    spec.current = V2
    return entry


async def test_verified_entry_changes_are_held_back_until_an_approver_decides(
    librarian: Librarian,
    spec: Spec,
    router: Router,
    notifier: RecordingNotifier,
    secrets_store: FileSecretStore,
) -> None:
    from tests.test_reviews import pages

    record = RecordPublisher()
    librarian.publisher = record
    entry = await changed_items(librarian, spec)
    report = await librarian.recheck("items")
    assert report.status == "changed"

    # The approved entry itself is untouched...
    assert librarian.registry.get("items") == entry
    # ...but the risky operations are held back until a person looks.
    obs = librarian.observations
    assert obs.suspended(entry, op_of(entry, "archiveInfo")) is not None  # method changed
    assert obs.suspended(entry, op_of(entry, "addNote")) is not None  # writable + breaking
    assert obs.suspended(entry, op_of(entry, "getItem")) is None
    await secrets_store.put("items/readonly", "tok")
    with pytest.raises(PolicyDenied, match="suspended"):
        await librarian.execute(
            CallRequest(api="items", operation="archiveInfo", args={"id": "1"}), AGENT
        )
    with pytest.raises(PolicyDenied, match="suspended"):
        await librarian.request_write(
            WriteRequest(api="items", operation="addNote", args={"id": "1"}), AGENT
        )
    router.add("GET", f"{HOST}/old", lambda r: httpx.Response(200, json={"ok": True}))
    old = await librarian.execute(CallRequest(api="items", operation="oldThing"), AGENT)
    assert any("no longer in the source" in w for w in old.warnings)

    # The approvers get one card for it, with the worst change first and what was done.
    kind, review, link = notifier.reviews[-1]
    assert kind == "opened" and review.kind == "update" and report.review == review.id
    assert notifier.changes == []  # no second notice beside the card
    assert review.changes[0].startswith("[safety]") and len(review.changes) == 4
    assert any("suspended archiveInfo" in w for w in review.warnings)
    assert "the worst is safety" in review.summary
    assert record.commits == []  # nothing is decided yet, so nothing is written down
    assert link
    path = link.removeprefix("https://librarian.example.test")
    page = pages(librarian).get(path)
    assert page.status_code == 200 and "Items changed at its source" in page.text
    assert "method changed GET -&gt; POST" in page.text and "Approve the update" in page.text

    # What approving would do keeps the decisions people made and leaves the rest to them.
    pending = await librarian.pending_update("items")
    assert pending is not None
    proposal, changes = pending
    assert proposal.status is EntryStatus.VERIFIED and len(changes) == 4
    assert op_of(proposal, "archiveInfo").method == "POST"
    assert op_of(proposal, "archiveInfo").writable is False
    assert op_of(proposal, "addNote").writable is True  # same request, still enabled
    assert "author" in {p.name for p in op_of(proposal, "addNote").parameters}
    assert op_of(proposal, "oldThing").suspended is not None  # kept for the approver
    assert op_of(proposal, "newThing").writable is False

    # Nothing new to say the second time round.
    assert (await librarian.recheck("items")).status == "unchanged"

    # A decision made while the update waits is not lost when it is approved.
    await librarian.set_operation_access("items", "listItems", "exclude", "sam@x.test")
    done = pages(librarian).post(
        f"/reviews/{review.id}", data={"t": link.split("t=")[1], "action": "approve"}
    )
    assert done.status_code == 200 and "the update was applied" in done.text
    updated = librarian.registry.get("items")
    assert updated is not None and updated.status is EntryStatus.VERIFIED
    assert op_of(updated, "archiveInfo").method == "POST"
    assert op_of(updated, "addNote").writable is True
    assert op_of(updated, "newThing").writable is False
    assert op_of(updated, "listItems").access is Access.WRITE  # sam's exclusion survived
    assert not op_of(updated, "listItems").writable
    assert "link holder approved an update from the API's source (4 change(s))" in updated.notes
    # The new definitions lift the suspensions they address.
    assert obs.suspended(updated, op_of(updated, "addNote")) is None
    assert obs.suspended(updated, op_of(updated, "archiveInfo")) is None
    assert op_of(updated, "oldThing").suspended is not None  # until someone removes it
    assert review.state is ReviewState.APPROVED
    assert await librarian.pending_update("items") is None
    api, message = record.commits[-1]
    assert api == "items" and message.startswith("items: updated from its source\n")
    assert "method changed GET -> POST" in message
    assert op_of(record.files["items"], "archiveInfo").method == "POST"


async def test_denying_an_update_keeps_the_entry_and_the_suspensions(
    librarian: Librarian, spec: Spec, notifier: RecordingNotifier
) -> None:
    entry = await changed_items(librarian, spec)
    await librarian.recheck("items")
    _, review, _ = notifier.reviews[-1]
    done = await librarian.deny_review(review.id, "sam@x.test")
    assert done.state is ReviewState.DENIED and done.note == "the entry stays as it is"
    assert librarian.registry.get("items") == entry  # not forgotten, not changed
    assert librarian.observations.suspended(entry, op_of(entry, "archiveInfo")) is not None
    assert await librarian.pending_update("items") is None
    with pytest.raises(LookupError):
        await librarian.approve_review(review.id, "sam@x.test", {})


async def test_provisional_entries_simply_follow_the_source(
    librarian: Librarian, spec: Spec, notifier: RecordingNotifier
) -> None:
    await save_items(librarian, EntryStatus.PROVISIONAL)
    spec.current = V2
    report = await librarian.recheck("items")
    assert report.status == "changed" and report.review is None
    updated = librarian.registry.get("items")
    assert updated is not None and updated.status is EntryStatus.PROVISIONAL
    ids = {o.id for o in updated.operations}
    assert "newThing" in ids and "oldThing" not in ids
    # nobody approved it yet, so there is no update to approve: the owners are told
    assert not notifier.reviews
    assert notifier.changes[-1]["link"] == "https://librarian.example.test/apis/items"
    assert any("[safety]" in d for d in notifier.changes[-1]["details"])


async def test_recheck_reports_source_errors_and_skips_manual_entries(
    librarian: Librarian, spec: Spec, router: Router
) -> None:
    await save_items(librarian, EntryStatus.VERIFIED)
    router.add("GET", SPEC_URL, lambda r: httpx.Response(503))
    report = await librarian.recheck("items")
    assert report.status == "error" and "503" in (report.detail or "")
    assert (await librarian.recheck("tracker")).status == "skipped"


async def test_approved_write_is_refused_if_suspended_before_it_runs(
    librarian: Librarian, spec: Spec, secrets_store: FileSecretStore
) -> None:
    entry = await save_items(librarian, EntryStatus.VERIFIED)
    await secrets_store.put("items/write", "w")
    started = await librarian._start_write(  # pyright: ignore[reportPrivateUsage]
        WriteRequest(api="items", operation="addNote", args={"id": "1"}), AGENT
    )
    assert isinstance(started, WriteApproval)
    approved = await librarian.approvals.approve(started.id, "ops@x.test")
    assert approved.state is ApprovalState.APPROVED
    await librarian.observations.suspend(entry, op_of(entry, "addNote"), "API changed")
    with pytest.raises(WriteRefused, match="suspended after approval"):
        await librarian._run_approved(approved, AGENT)  # pyright: ignore[reportPrivateUsage]


# -- runtime signals ------------------------------------------------------------------

STATUS = "https://status.example.test/summary"
CALL = CallRequest(api="status", operation="summary")


async def test_deprecation_headers_warn_callers_and_owners_once(
    librarian: Librarian, router: Router, notifier: RecordingNotifier
) -> None:
    router.add(
        "GET",
        STATUS,
        lambda r: httpx.Response(
            200, json={"ok": True}, headers={"Deprecation": "true", "Sunset": "2027-01-01"}
        ),
    )
    first = await librarian.execute(CALL, AGENT)
    second = await librarian.execute(CALL, AGENT)
    assert any("deprecated (sunset 2027-01-01)" in w for w in second.warnings)
    assert first.warnings == second.warnings
    assert [c["title"] for c in notifier.changes] == [
        "Status page: the API marks an operation deprecated"
    ]
    assert notifier.changes[0]["group"] is None  # informational: no ping


async def test_repeated_gone_responses_are_reported(
    librarian: Librarian, router: Router, notifier: RecordingNotifier
) -> None:
    router.add("GET", STATUS, lambda r: httpx.Response(410, json={}))
    for _ in range(3):
        await librarian.execute(CALL, AGENT)
    assert [c["title"] for c in notifier.changes] == [
        "Status page: an operation may have been removed"
    ]


async def test_response_shape_changes_are_reported(
    librarian: Librarian, router: Router, notifier: RecordingNotifier
) -> None:
    body: dict[str, Any] = {"incidents": [{"id": 1}], "page": 1, "total": 1}
    router.add("GET", STATUS, lambda r: httpx.Response(200, json=body))
    await librarian.execute(CALL, AGENT)
    body = {"data": {"items": []}}
    router.add("GET", STATUS, lambda r: httpx.Response(200, json=body))
    for _ in range(2):
        await librarian.execute(CALL, AGENT)
    assert notifier.changes == []  # one odd response is not a change
    await librarian.execute(CALL, AGENT)
    assert notifier.changes[-1]["title"] == "Status page: responses changed shape"
    assert "incidents" in notifier.changes[-1]["details"][0]


# -- persistence and one-replica scheduling -------------------------------------------


async def test_suspensions_are_shared_and_rechecks_run_on_one_replica(
    tmp_path: Path, router: Router
) -> None:
    from tests.test_state import replica

    router.add("GET", SPEC_URL, lambda r: httpx.Response(200, json=V2))
    a, b = replica(tmp_path, router), replica(tmp_path, router)
    for lib in (a, b):
        lib.source_client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(router))
        await lib.start()
    entry = await save_items(a, EntryStatus.VERIFIED)
    b.registry.load()

    reports_a, reports_b = await a.recheck_all_if_due(), await b.recheck_all_if_due()
    assert [r.api for r in reports_a if r.status == "changed"] == ["items"]
    assert reports_b == []  # the other replica had this round
    await b.observations.load()
    assert b.observations.suspended(entry, op_of(entry, "archiveInfo")) is not None
    assert await a.recheck_all_if_due() == []  # not due again yet
    await a.aclose()
    await b.aclose()


def op_of(entry: ApiEntry, op_id: str) -> Any:
    found = entry.operation(op_id)
    assert found is not None, op_id
    return found


def test_proposal_is_a_valid_entry_even_when_referenced_operations_vanish() -> None:
    old = entry_from_spec(V1, api_id="items", spec_url=SPEC_URL)
    old = old.model_copy(
        update={
            "credential": old.credential.model_copy(update={"validation_operation": "oldThing"})
        }
    )
    new = entry_from_spec(V2, api_id="items", spec_url=SPEC_URL)
    proposal = proposed_update(old, new, diff(old, new))
    ApiEntry.model_validate(proposal.model_dump(mode="json", by_alias=True))
