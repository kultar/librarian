"""Plans: several writes on one API, shown to the approver together, approved once and
made in order. The example is the one that started it: a blob, a tree, a commit, a
branch and a pull request."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.testclient import TestClient

from librarian.approvals import ApprovalError, ApprovalState, WriteApproval
from librarian.audit import MemoryAuditSink
from librarian.calls import Caller, Limits
from librarian.errors import ApiUnavailable
from librarian.executor import PolicyDenied
from librarian.notifiers import write_blocks, write_outcome_text, write_reply
from librarian.plans import PlanError, PlanRequest, PlanStep, Ref, parse, resolve, step_key
from librarian.registry.models import ApiEntry
from librarian.secrets import FileSecretStore
from librarian.service import Librarian, LibrarianError, WriteNotEnabled, WriteRefused
from librarian.web import approval_routes
from tests.conftest import TRACKER, RecordingNotifier, Router, write_entries
from tests.surfaces import approve_the_write, mcp_at, served
from tests.test_hostile_api import DATABASES, emptied, on_database

FORGE_URL = "https://forge.example.test"
REPO = [{"name": "owner", "in": "path"}, {"name": "repo", "in": "path"}]
WRITE_KEY = "wr1te-key-9876543210"


def op(op_id: str, method: str, path: str, **more: Any) -> dict[str, Any]:
    write = method != "GET"
    return {
        "id": op_id,
        "method": method,
        "path": f"/repos/{{owner}}/{{repo}}{path}",
        "access": "write" if write else "read",
        "writable": write,
        "accepts_body": method in ("POST", "PATCH"),
        "summary": more.pop("summary", ""),
        "parameters": REPO + more.pop("parameters", []),
        **more,
    }


FORGE: dict[str, Any] = {
    "id": "forge",
    "name": "Forge",
    "base_url": FORGE_URL,
    "status": "verified",
    "auth": {"type": "bearer", "secret_ref": "forge/readonly"},
    "write_auth": {"type": "bearer", "secret_ref": "forge/write"},
    "operations": [
        op("getRepo", "GET", ""),
        op("createBlob", "POST", "/blobs", summary="Create a blob"),
        op("createTree", "POST", "/trees"),
        op("createCommit", "POST", "/commits"),
        op("createRef", "POST", "/refs"),
        op("createPull", "POST", "/pulls", summary="Open a pull request"),
        op(
            "labelPull",
            "POST",
            "/pulls/{number}/labels",
            parameters=[{"name": "number", "in": "path"}],
        ),
        op("deleteRef", "DELETE", "/refs/{ref}", parameters=[{"name": "ref", "in": "path"}]),
        {**op("archive", "POST", "/archive"), "writable": False},
        {**op("transfer", "POST", "/transfer"), "writable": False},
    ],
}
WHERE: dict[str, Any] = {"owner": "acme", "repo": "payments"}
AGENT = Caller(agent="release-notes", write_apis=("forge",), owner="leads@x.test")


def steps() -> list[dict[str, Any]]:
    """Five writes; each after the first needs what an earlier one answered."""
    return [
        {"id": "blob", "operation": "createBlob", "args": WHERE, "body": {"content": "notes"}},
        {
            "id": "tree",
            "operation": "createTree",
            "args": WHERE,
            "body": {"base": "ba5e", "tree": [{"path": "NOTES.md", "sha": {"$from": "blob.sha"}}]},
        },
        {
            "id": "commit",
            "operation": "createCommit",
            "args": WHERE,
            "body": {"message": "Notes", "tree": {"$from": "tree.sha"}, "parents": ["ba5e"]},
        },
        {
            "id": "branch",
            "operation": "createRef",
            "args": WHERE,
            "body": {"ref": "refs/heads/notes", "sha": {"$from": "commit.sha"}},
        },
        {
            "id": "pull",
            "operation": "createPull",
            "args": WHERE,
            "body": {"title": "Notes", "head": "notes", "base": "main"},
        },
    ]


def a_plan(**changes: Any) -> PlanRequest:
    return PlanRequest.model_validate(
        {"api": "forge", "steps": steps(), "reason": "Release notes for 2.4", **changes}
    )


class Forge:
    """Answers each write with an id, and remembers what it was sent."""

    ANSWERS: ClassVar[dict[str, dict[str, Any]]] = {
        "/blobs": {"sha": "b10b"},
        "/trees": {"sha": "7ree"},
        "/commits": {"sha": "c0mm17", "tree": {"sha": "7ree"}},
        "/refs": {"ref": "refs/heads/notes"},
        "/pulls": {"number": 12, "html_url": "https://forge.example.test/acme/payments/pull/12"},
        "/pulls/12/labels": {"labels": ["notes"]},
    }

    def __init__(self) -> None:
        self.received: list[tuple[str, Any, httpx.Request]] = []
        self.fail: dict[str, httpx.Response] = {}
        self.answers = dict(self.ANSWERS)

    def handle(self, request: httpx.Request) -> httpx.Response:
        what = request.url.path.removeprefix("/repos/acme/payments")
        self.received.append((what, json.loads(request.content or b"null"), request))
        if what in self.fail:
            return self.fail[what]
        return httpx.Response(201, json=self.answers[what])

    @property
    def order(self) -> list[str]:
        return [what for what, _, _ in self.received]


@pytest.fixture
async def forge(librarian: Librarian, forge_api: Forge) -> Forge:
    forge = forge_api
    await librarian.registry.save(ApiEntry.model_validate(FORGE))
    await librarian.secrets.put("forge/readonly", "r3ad-key-0123456789")
    await librarian.secrets.put("forge/write", WRITE_KEY)
    return forge


async def waiting(librarian: Librarian, count: int = 1) -> list[WriteApproval]:
    """The approvals waiting for a person, once there are `count` of them."""
    for _ in range(500):
        if len(librarian.approvals.pending()) >= count:
            break
        await asyncio.sleep(0.01)
    pending = librarian.approvals.pending()
    assert len(pending) == count, f"{len(pending)} approvals are waiting, not {count}"
    return pending


async def refused(librarian: Librarian, plan: PlanRequest, caller: Caller) -> None:
    """Ask for a plan that must be refused outright: it may not end up waiting for a person."""
    await asyncio.wait_for(librarian.plan(plan, caller), 5)


async def decided(librarian: Librarian, task: asyncio.Task[Any], *, approve: bool = True) -> Any:
    """Wait for the plan's approval to open, decide it, and return what the agent gets."""
    [approval] = await waiting(librarian)
    if approve:
        await librarian.approvals.approve(approval.id, "leads@x.test")
    else:
        await librarian.approvals.deny(approval.id, "leads@x.test")
    return await asyncio.wait_for(task, 5)


async def test_a_plan_is_one_approval_and_its_writes_are_made_in_order(
    librarian: Librarian, forge: Forge, notifier: RecordingNotifier, audit: MemoryAuditSink
) -> None:
    task = asyncio.create_task(librarian.plan(a_plan(), AGENT))
    [approval] = await waiting(librarian)
    assert forge.received == []  # nothing is sent before someone approves
    assert (approval.operation, len(approval.steps), approval.rememberable) == ("plan", 5, False)
    first = approval.steps[0]
    assert (first.method, first.path) == ("POST", "/repos/{owner}/{repo}/blobs")
    assert [kind for kind, _ in notifier.write_events] == ["opened"]  # one card, not five

    await librarian.approvals.approve(approval.id, "leads@x.test")
    outcome = await asyncio.wait_for(task, 5)
    assert forge.order == ["/blobs", "/trees", "/commits", "/refs", "/pulls"]
    # Each step was sent with what the step before it answered.
    assert forge.received[1][1]["tree"] == [{"path": "NOTES.md", "sha": "b10b"}]
    assert forge.received[2][1]["tree"] == "7ree"
    assert forge.received[3][1] == {"ref": "refs/heads/notes", "sha": "c0mm17"}
    assert all(r.headers["authorization"] == f"Bearer {WRITE_KEY}" for _, _, r in forge.received)

    assert outcome.how == "approved" and outcome.result.data["number"] == 12
    assert outcome.message == (
        "Approved by leads@x.test as one plan; all 5 writes were made, in order."
    )
    assert outcome.steps is not None
    assert [(s["id"], s["state"]) for s in outcome.steps] == [
        ("blob", "made"),
        ("tree", "made"),
        ("commit", "made"),
        ("branch", "made"),
        ("pull", "made"),
    ]
    assert outcome.steps[0]["result"]["data"] == {"sha": "b10b"}
    assert approval.state is ApprovalState.EXECUTED and approval.note == "done: 5 writes in order"
    status = librarian.write_status(approval)
    assert status["state"] == "executed" and len(status["steps"]) == 5
    assert [kind for kind, _ in notifier.write_events] == ["opened", "closed"]
    events = [event.event for event in audit.events]
    assert events.count("write_requested") == 1 and events.count("write") == 5
    # Each write says which approval it was made under, and which step of it it was.
    made = [event.extra for event in audit.events if event.event == "write"]
    assert [extra["step"] for extra in made] == ["blob", "tree", "commit", "branch", "pull"]
    assert {extra["approval"] for extra in made} == {approval.id}
    assert WRITE_KEY not in json.dumps(status) + "".join(e.model_dump_json() for e in audit.events)


async def test_a_denied_plan_sends_nothing(librarian: Librarian, forge: Forge) -> None:
    task = asyncio.create_task(librarian.plan(a_plan(), AGENT))
    with pytest.raises(WriteRefused, match="denied"):
        await decided(librarian, task, approve=False)
    assert forge.received == []


def test_what_a_plan_may_say() -> None:
    def changed(index: int, **change: Any) -> list[dict[str, Any]]:
        new = steps()
        new[index] = {**new[index], **change}
        return new

    for bad, why in (
        ({"steps": []}, "at least 1"),
        ({"steps": steps() * 5}, "at most 20"),
        ({"steps": changed(1, id="blob")}, "two steps are called 'blob'"),
        # A step can only take a value from one that runs before it.
        ({"steps": changed(0, body={"x": {"$from": "tree.sha"}})}, "no step before it"),
        ({"steps": changed(1, args={**WHERE, "repo": {"$from": "tree.name"}})}, "no step before"),
        ({"steps": changed(1, body={"x": {"$from": "blob"}})}, "is not a reference"),
        ({"steps": changed(1, body={"x": {"$from": "blob..sha"}})}, "is not a reference"),
        ({"steps": changed(1, id="Tree 1")}, "String should match"),
        ({"steps": changed(1, colour="blue")}, "Extra inputs"),
    ):
        with pytest.raises(ValidationError, match=why):
            a_plan(**bad)
    plan = a_plan()
    assert plan.steps[1].references() == [Ref.model_validate({"$from": "blob.sha"})]
    # An object with other keys beside "$from" is just data.
    plain = a_plan(steps=changed(0, body={"x": {"$from": "nothing.here", "and": 1}}))
    assert plain.steps[0].references() == []


async def test_every_step_is_checked_before_anyone_is_asked(
    librarian: Librarian, forge: Forge, notifier: RecordingNotifier
) -> None:
    def with_step(index: int, **change: Any) -> PlanRequest:
        new = steps()
        new[index] = {**new[index], **change}
        return a_plan(steps=new)

    for plan, why in (
        (with_step(2, operation="nope"), r"step 3 \(commit\): unknown operation 'nope'"),
        (with_step(2, operation="getRepo"), r"step 3 \(commit\): operation 'getRepo' is not"),
        (with_step(1, args={"owner": "acme"}), r"step 2 \(tree\): missing required argument"),
        (with_step(1, args={**WHERE, "extra": 1}), r"step 2 \(tree\): unknown arguments"),
        (a_plan(api="weather"), "may not write to API 'weather'"),
    ):
        with pytest.raises(PolicyDenied, match=why):
            await refused(librarian, plan, AGENT)
    assert librarian.approvals.pending() == [] and notifier.write_events == []
    assert forge.received == []


async def test_operations_nobody_enabled_yet_are_asked_for_together(
    librarian: Librarian, forge: Forge, notifier: RecordingNotifier
) -> None:
    new = steps()
    new[3] = {"id": "archive", "operation": "archive", "args": WHERE}
    new[4] = {"id": "transfer", "operation": "transfer", "args": WHERE}
    with pytest.raises(WriteNotEnabled) as asked:
        await refused(librarian, a_plan(steps=new), AGENT)
    assert sorted(asked.value.request.operations) == ["archive", "transfer"]  # one request
    assert librarian.approvals.pending() == [] and forge.received == []


async def test_a_step_that_fails_ends_the_plan_and_says_what_was_made(
    librarian: Librarian, forge: Forge
) -> None:
    forge.fail["/commits"] = httpx.Response(422, json={"message": "tree is not valid"})
    task = asyncio.create_task(librarian.plan(a_plan(), AGENT))
    outcome = await decided(librarian, task)
    assert forge.order == ["/blobs", "/trees", "/commits"]  # the last two were never sent
    assert outcome.result.status_code == 422
    assert [(s["id"], s["state"]) for s in outcome.steps] == [
        ("blob", "made"),
        ("tree", "made"),
        ("commit", "failed"),
        ("branch", "not sent"),
        ("pull", "not sent"),
    ]
    assert outcome.message == (
        "Plan stopped: step 3 of 5 (commit) did not succeed: HTTP 422. The steps before it "
        "were made and are not undone: blob, tree."
    )
    approval = next(iter(librarian.approvals._approvals.values()))  # pyright: ignore[reportPrivateUsage]
    assert approval.state is ApprovalState.FAILED and "blob, tree" in (approval.note or "")
    assert len(approval.step_results) == 3


async def test_an_api_that_stops_answering_mid_plan_is_reported_with_what_is_known(
    librarian: Librarian, forge: Forge, router: Router
) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("All connection attempts failed")

    router.add("POST", f"{FORGE_URL}/repos/acme/payments/trees", down)
    task = asyncio.create_task(librarian.plan(a_plan(), AGENT))
    with pytest.raises(ApiUnavailable) as gone:
        await decided(librarian, task)
    assert not gone.value.sent
    assert str(gone.value) == (
        "plan stopped: step 2 of 5 (tree) got no answer from Forge: it could not be reached. "
        "It was not sent. The steps before it were made and are not undone: blob."
    )
    assert forge.order == ["/blobs"]
    approval = next(iter(librarian.approvals._approvals.values()))  # pyright: ignore[reportPrivateUsage]
    assert approval.state is ApprovalState.FAILED
    states = [s["state"] for s in librarian.write_status(approval)["steps"]]
    assert states == ["made", "not sent", "not sent", "not sent", "not sent"]


async def test_a_step_that_may_have_arrived_is_said_to_be_unknown(
    librarian: Librarian, forge: Forge, router: Router
) -> None:
    """Sent, and then no answer: nobody can tell whether the API made it."""

    def silent(request: httpx.Request) -> httpx.Response:
        forge.received.append(("/commits", None, request))
        raise httpx.ReadTimeout("timed out")

    router.add("POST", f"{FORGE_URL}/repos/acme/payments/commits", silent)
    task = asyncio.create_task(librarian.plan(a_plan(), AGENT))
    with pytest.raises(ApiUnavailable) as gone:
        await decided(librarian, task)
    assert gone.value.sent
    assert str(gone.value) == (
        "plan stopped: step 3 of 5 (commit) got no answer from Forge: it did not answer in "
        "time. It may have reached Forge: check before sending it again. The steps before it "
        "were made and are not undone: blob, tree."
    )
    assert forge.order == ["/blobs", "/trees", "/commits"]  # and nothing after it
    approval = next(iter(librarian.approvals._approvals.values()))  # pyright: ignore[reportPrivateUsage]
    states = [s["state"] for s in librarian.write_status(approval)["steps"]]
    assert states == ["made", "made", "unknown", "not sent", "not sent"]


async def test_a_step_is_not_sent_when_what_it_needs_did_not_come_back(
    librarian: Librarian, forge: Forge
) -> None:
    forge.answers["/blobs"] = {"id": "b10b"}  # not under `sha`, as the plan expects
    task = asyncio.create_task(librarian.plan(a_plan(), AGENT))
    with pytest.raises(WriteRefused) as stopped:
        await decided(librarian, task)
    assert str(stopped.value) == (
        "plan stopped: step 2 of 5 (tree) could not be prepared: what step 'blob' answered "
        "has no 'sha' (needed for 'blob.sha'). The steps before it were made and are not "
        "undone: blob."
    )
    assert forge.order == ["/blobs"]


async def test_a_body_taken_whole_from_an_earlier_step_has_to_be_one(
    librarian: Librarian, forge: Forge
) -> None:
    whole = [steps()[0], {**steps()[1], "body": {"$from": "blob.sha"}}]
    task = asyncio.create_task(librarian.plan(a_plan(steps=whole), AGENT))
    with pytest.raises(WriteRefused) as stopped:
        await decided(librarian, task)
    assert str(stopped.value) == (
        "plan stopped: step 2 of 2 (tree) could not be prepared: the request body has to be "
        "an object or a list, not a string. The steps before it were made and are not undone: "
        "blob."
    )
    assert forge.order == ["/blobs"]
    approval = next(iter(librarian.approvals._approvals.values()))  # pyright: ignore[reportPrivateUsage]
    assert approval.state is ApprovalState.FAILED  # and not left "executing"


def test_what_a_step_becomes_once_the_steps_before_it_have_answered() -> None:
    step = PlanStep.model_validate(
        {
            "id": "label",
            "operation": "labelPull",
            "args": {"number": {"$from": "pull.number"}, "names": {"$from": "pull.labels"}},
            "body": {"head": {"$from": "pull.head"}, "first": {"$from": "pull.labels.0"}},
        }
    )
    answered = {"pull": {"number": 12, "labels": ["a", "b"], "head": {"ref": "notes"}}}
    request = resolve(step, "forge", answered, "key:label")
    assert request.args == {"number": 12, "names": ["a", "b"]}
    assert request.body == {"head": {"ref": "notes"}, "first": "a"}
    assert (request.api, request.operation, request.idempotency_key) == (
        "forge",
        "labelPull",
        "key:label",
    )
    for answer, why in (
        ({"number": None, "labels": []}, "'pull.number' is nothing"),
        ({"number": {"a": 1}, "labels": []}, "'pull.number' is an object"),
        ({"number": 1, "labels": [1, "a"]}, "'pull.labels' is a list"),
        ({"number": 1}, "has no 'labels'"),
        ({"number": 1, "labels": []}, "has no 'head'"),
        (
            {"number": 1, "labels": [], "head": 1},
            r"has no 'labels.0' \(needed for 'pull.labels.0'\)",
        ),
    ):
        with pytest.raises(PlanError, match=why):
            resolve(step, "forge", {"pull": answer})
    with pytest.raises(PlanError, match="step 'pull' gave no answer"):
        resolve(step, "forge", {})


def test_no_two_steps_of_a_plan_are_sent_with_the_same_key() -> None:
    """An API that honours the key would answer the second with what it answered the first."""
    assert step_key(None, "blob") is None
    assert step_key("release-2.4", "blob") == "release-2.4:blob"
    for key in ("k" * 120, "k" * 128):  # as long as a key may be
        keys = {step_key(key, step) for step in ("blob", "tree", "commit", "tree-2")}
        assert len(keys) == 4 and all(k is not None and len(k) <= 128 for k in keys)


def test_what_is_wrong_with_a_plan_is_said_in_one_line() -> None:
    def changed(index: int, **change: Any) -> dict[str, Any]:
        new = steps()
        new[index] = {**new[index], **change}
        return {"api": "forge", "steps": new}

    for bad, said in (
        (
            {"api": "forge", "steps": [{"id": "x", "operation": ""}]},
            "invalid plan: steps.0.operation: String should have at least 1 character",
        ),
        (
            {"api": "forge", "steps": steps()[1:]},
            "invalid plan: step 'tree' refers to 'blob.sha', and no step before it is called "
            "'blob'",
        ),
        (
            changed(1, args={**WHERE, "repo": {"$from": "blob"}}),
            "invalid plan: steps.1.args.repo.$from: 'blob' is not a reference: write "
            "<step>.<field>, e.g. blob.sha",
        ),
        (
            changed(1, args={**WHERE, "repo": {"from": "blob.sha"}}),
            "invalid plan: steps.1.args.repo.$from: Field required",
        ),
        ({"steps": steps()}, "invalid plan: api: Field required"),
    ):
        with pytest.raises(LibrarianError) as refused:
            parse(bad)
        assert str(refused.value) == said
    assert parse({"api": "forge", "steps": steps()}) == a_plan(reason="")


async def test_a_key_the_api_sends_back_is_not_kept_or_passed_on(
    librarian: Librarian, forge: Forge
) -> None:
    forge.answers["/blobs"] = {"sha": WRITE_KEY, "note": f"made with {WRITE_KEY}"}
    task = asyncio.create_task(librarian.plan(a_plan(), AGENT))
    outcome = await decided(librarian, task)
    approval = next(iter(librarian.approvals._approvals.values()))  # pyright: ignore[reportPrivateUsage]
    assert WRITE_KEY not in approval.model_dump_json() + json.dumps(outcome.steps)
    # What the next step was given in its place is what the agent was shown.
    assert forge.received[1][1]["tree"] == [{"path": "NOTES.md", "sha": "[redacted]"}]


async def test_a_step_is_checked_again_with_the_values_it_was_filled_with(
    librarian: Librarian, forge: Forge
) -> None:
    """What comes back from an API is not trusted to be a safe argument."""
    forge.answers["/pulls"] = {"number": "12/../../admin"}
    new = [
        steps()[4],
        {
            "id": "label",
            "operation": "labelPull",
            "args": {**WHERE, "number": {"$from": "pull.number"}},
            "body": {"labels": ["notes"]},
        },
    ]
    task = asyncio.create_task(librarian.plan(a_plan(steps=new), AGENT))
    with pytest.raises(WriteRefused, match=r"step 2 of 2 \(label\) is no longer allowed: path arg"):
        await decided(librarian, task)
    assert forge.order == ["/pulls"]

    # With an answer that is a safe value, the same plan goes through.
    forge.answers["/pulls"] = {"number": 12}
    task = asyncio.create_task(librarian.plan(a_plan(steps=new), AGENT))
    outcome = await decided(librarian, task)
    assert forge.order[-1] == "/pulls/12/labels" and outcome.result.ok


async def test_an_operation_disabled_after_approval_is_not_sent(
    librarian: Librarian, forge: Forge
) -> None:
    task = asyncio.create_task(librarian.plan(a_plan(), AGENT))
    [approval] = await waiting(librarian)
    entry = librarian.registry.get("forge")
    assert entry is not None
    operations = [
        o.model_copy(update={"writable": False}) if o.id == "createRef" else o
        for o in entry.operations
    ]
    await librarian.registry.save(entry.model_copy(update={"operations": operations}))
    await librarian.approvals.approve(approval.id, "leads@x.test")
    with pytest.raises(WriteRefused, match=r"step 4 of 5 \(branch\) is no longer allowed"):
        await asyncio.wait_for(task, 5)
    assert forge.order == ["/blobs", "/trees", "/commits"]


async def test_limits_hold_inside_a_plan(librarian: Librarian, forge: Forge) -> None:
    limited = AGENT.model_copy(
        update={"limits": {"forge": Limits(arguments={"repo": ("payments",)})}}
    )
    outside = steps()
    outside[4] = {**outside[4], "args": {"owner": "acme", "repo": "vault"}}
    with pytest.raises(PolicyDenied, match=r"step 5 \(pull\): .*repo='vault' is outside"):
        await refused(librarian, a_plan(steps=outside), limited)
    # A limited argument has to be in the plan for the approver to see, not filled in later.
    later = steps()
    later[1] = {**later[1], "args": {"owner": "acme", "repo": {"$from": "blob.sha"}}}
    with pytest.raises(PolicyDenied, match="'repo' must be given in the plan itself"):
        await refused(librarian, a_plan(steps=later), limited)
    assert librarian.approvals.pending() == [] and forge.received == []


async def test_a_delete_has_to_say_what_it_deletes(
    librarian: Librarian, forge: Forge, router: Router
) -> None:
    """The approver sees what a plan removes: none of it may come from an earlier step."""
    removed: list[str] = []

    def delete(request: httpx.Request) -> httpx.Response:
        removed.append(request.url.path)
        return httpx.Response(204)

    router.add("DELETE", f"{FORGE_URL}/repos/acme/payments/refs/old-notes", delete)
    undo: dict[str, Any] = {"id": "undo", "operation": "deleteRef"}
    for taken in (
        {"args": {**WHERE, "ref": {"$from": "blob.sha"}}},
        {"args": {**WHERE, "ref": "old-notes"}, "body": {"reason": {"$from": "blob.sha"}}},
    ):
        with pytest.raises(PolicyDenied, match=r"step 2 \(undo\): .* is a DELETE: what it deletes"):
            await refused(librarian, a_plan(steps=[steps()[0], {**undo, **taken}]), AGENT)
    assert librarian.approvals.pending() == [] and forge.received == []
    # Written out, it is put to a person like any other step, and made when they approve.
    plain = a_plan(steps=[steps()[0], {**undo, "args": {**WHERE, "ref": "old-notes"}}])
    task = asyncio.create_task(librarian.plan(plain, AGENT))
    outcome = await decided(librarian, task)
    assert [s["state"] for s in outcome.steps] == ["made", "made"]
    assert removed == ["/repos/acme/payments/refs/old-notes"]


async def test_the_same_plan_is_not_run_twice(librarian: Librarian, forge: Forge) -> None:
    plan = a_plan(idempotency_key="release-2.4")
    first = asyncio.create_task(librarian.plan(plan, AGENT))
    await waiting(librarian)
    # Asked again while it waits: the same approval, not a second one.
    second = asyncio.create_task(librarian.plan(plan, AGENT))
    await asyncio.sleep(0.05)
    assert len(librarian.approvals.pending()) == 1
    outcome = await decided(librarian, first)
    with pytest.raises(WriteRefused, match=r"plan execut|plan is already being run"):
        await asyncio.wait_for(second, 5)  # one of the two runs it; the other is told so
    assert len(forge.received) == 5
    # Each step goes out with its own key, made from the plan's, so the API can tell too.
    assert [r.headers["idempotency-key"] for _, _, r in forge.received][:2] == [
        "release-2.4:blob",
        "release-2.4:tree",
    ]
    again = await librarian.plan(plan, AGENT)
    assert again.how == "idempotent" and again.approval_id == outcome.approval_id
    assert [s["state"] for s in again.steps or []] == ["made"] * 5
    assert len(forge.received) == 5  # nothing was sent again


async def test_an_approval_is_for_exactly_the_plan_that_was_shown(
    librarian: Librarian, forge: Forge
) -> None:
    shown = asyncio.create_task(librarian.plan(a_plan(), AGENT))
    other = steps()
    other[4] = {**other[4], "body": {"title": "Notes", "head": "notes", "base": "release"}}
    changed = asyncio.create_task(librarian.plan(a_plan(steps=other), AGENT))
    first, second = await waiting(librarian, 2)  # a plan changed anywhere is another plan
    assert first.steps[4].body == {"title": "Notes", "head": "notes", "base": "main"}
    await librarian.approvals.approve(first.id, "leads@x.test")
    await asyncio.wait_for(shown, 5)
    assert len(forge.received) == 5 and forge.received[4][1]["base"] == "main"
    assert second.state is ApprovalState.PENDING  # the other still waits for its own decision
    await librarian.approvals.deny(second.id, "leads@x.test")
    with pytest.raises(WriteRefused, match="plan denied"):
        await asyncio.wait_for(changed, 5)
    assert len(forge.received) == 5


async def test_a_plan_counts_as_that_many_writes(librarian: Librarian, forge: Forge) -> None:
    from librarian.policy import RateLimiter

    # More writes than an agent may make in a minute: waiting would not help, so it is told
    # what would, and none of the minute is used up.
    librarian.policy.write_rate_limiter = RateLimiter(4)
    with pytest.raises(PolicyDenied, match=r"a plan of 5 writes is more than .* \(4, `writes.rate"):
        await refused(librarian, a_plan(), AGENT)
    two = a_plan(steps=[steps()[0], {**steps()[0], "id": "again"}])
    asked = asyncio.create_task(librarian.plan(two, AGENT))
    await waiting(librarian)  # ...there is still room for a plan of two (and then two more)
    # A plan that fits by itself, when the minute is already partly used: it counts as five.
    librarian.policy.write_rate_limiter = RateLimiter(8)
    first = asyncio.create_task(librarian.plan(a_plan(), AGENT))
    await waiting(librarian, 2)
    other = steps()
    other[0] = {**other[0], "body": {"content": "other notes"}}
    with pytest.raises(PolicyDenied, match=r"rate limit exceeded .*a plan of 5 writes counts as 5"):
        await refused(librarian, a_plan(steps=other), AGENT)
    assert len(librarian.approvals.pending()) == 2 and forge.received == []
    for task in (asked, first):
        task.cancel()


def page_client(librarian: Librarian) -> TestClient:
    return TestClient(
        Starlette(routes=approval_routes(librarian.approvals, librarian.settings.approvers))
    )


async def test_the_approver_sees_every_write_as_it_will_be_sent(
    librarian: Librarian, forge: Forge
) -> None:
    labelled = [
        *steps(),
        {
            "id": "label",
            "operation": "labelPull",
            "args": {**WHERE, "number": {"$from": "pull.number"}},
            "body": {"labels": ["notes"]},
        },
    ]
    task = asyncio.create_task(librarian.plan(a_plan(steps=labelled), AGENT))
    [approval] = await waiting(librarian)
    link = librarian.approvals.link(approval).removeprefix(librarian.settings.public_url)
    c = page_client(librarian)
    page = c.get(link).text
    assert "Approve a plan for Forge" in page and "wants to make 6 writes" in page
    assert "Release notes for 2.4" in page
    # Each request where it will go, with the arguments the plan gives put in...
    for number, path in enumerate(("blobs", "trees", "commits", "refs", "pulls"), start=1):
        heading = (
            f"{number}. <span class='method post'>POST</span><code>/repos/acme/payments/{path}"
        )
        assert heading in page
    assert "&quot;content&quot;: &quot;notes&quot;" in page  # ...its body as it will be sent...
    # ...and what is not known yet: marked in a body, said in words for an argument.
    assert "<span class=from>{&quot;$from&quot;: &quot;blob.sha&quot;}</span>" in page
    assert "<code>/repos/acme/payments/pulls/{number}/labels</code>" in page
    assert "<code>number</code> from step <code>pull</code>" in page
    assert page.index("is not known yet") < page.index("1. <span class='method post'>")
    assert "Open a pull request" in page
    assert "allow similar" not in page  # a plan is never remembered as a grant
    with pytest.raises(ApprovalError, match="can only be approved once"):
        await librarian.approvals.approve(approval.id, "leads@x.test", remember=True)
    token = link.split("t=")[1]
    refused = c.post(link.split("?")[0], data={"t": token, "action": "grant"})
    assert refused.status_code == 400 and "never as a grant" in refused.text
    assert forge.received == []
    done = c.post(link.split("?")[0], data={"t": token, "action": "once"})
    assert done.status_code == 200 and "The 6 writes are being made now, in order." in done.text
    outcome = await asyncio.wait_for(task, 5)
    assert len(forge.received) == 6 and outcome.result.data == {"labels": ["notes"]}


async def test_the_card_and_the_reply_describe_the_plan(librarian: Librarian, forge: Forge) -> None:
    task = asyncio.create_task(librarian.plan(a_plan(), AGENT))
    [approval] = await waiting(librarian)
    card = json.dumps(write_blocks(approval, "https://x.test/approvals/1?t=2", []))
    assert "Plan approval needed: Forge" in card
    assert "*Plan:* 5 writes, in order" in card and "1. `createBlob` POST /repos/" in card
    assert "Review plan" in card and "allow similar" not in card
    await librarian.approvals.approve(approval.id, "leads@x.test")
    await asyncio.wait_for(task, 5)
    assert write_reply(approval) == (
        ":white_check_mark: Done: all 5 writes of the plan were made on Forge, in order."
    )
    assert write_outcome_text(approval) == (
        ":white_check_mark: Forge plan of 5 writes executed by leads@x.test · "
        "done: 5 writes in order"
    )
    # And when it stops part of the way, the thread says where and what stays made.
    forge.fail["/refs"] = httpx.Response(409, json={"message": "Reference already exists"})
    task = asyncio.create_task(librarian.plan(a_plan(), AGENT))
    partial = await decided(librarian, task)
    stopped = await librarian.approvals.load(partial.approval_id)
    assert stopped is not None
    assert write_reply(stopped) == (
        ":x: Approved by leads@x.test, but the plan stopped: step 4 of 5 (branch) did not "
        "succeed: HTTP 409. The steps before it were made and are not undone: blob, tree, commit."
    )


# -- more than one replica, and a database between them ----------------------------------
#
# What follows is about what is kept: how far a plan got, who is running it, what another
# replica makes of it. SQLite takes anything; the same runs against a real Postgres when
# LIBRARIAN_TEST_POSTGRES_URL names a database that may be emptied.

Replicas = Callable[[], Awaitable[Librarian]]


@pytest.fixture(params=DATABASES)
async def replicas(
    request: pytest.FixtureRequest, tmp_path: Path, router: Router, forge_api: Forge
) -> AsyncIterator[Replicas]:
    """One deployment: every call starts another replica of it on the same database."""
    url: str = request.param or f"sqlite+aiosqlite:///{tmp_path}/state.db"
    if request.param:
        await emptied(url)
    write_entries(tmp_path / "registry", TRACKER, FORGE)
    await FileSecretStore(tmp_path / "secrets").put("forge/write", WRITE_KEY)
    started: list[Librarian] = []

    async def another() -> Librarian:
        replica = on_database(tmp_path, router, url)
        await replica.start()
        started.append(replica)
        return replica

    yield another
    for replica in started:
        await gone(replica)


@pytest.fixture
def forge_api(router: Router) -> Forge:
    """The API itself, for tests that bring their own librarians."""
    forge = Forge()
    for path in Forge.ANSWERS:
        router.add("POST", f"{FORGE_URL}/repos/acme/payments{path}", forge.handle)
    return forge


async def gone(replica: Librarian) -> None:
    """Stop a replica as a stopped process is stopped: what it was doing in the
    background ends with it."""
    for task in list(replica._background):  # pyright: ignore[reportPrivateUsage]
        task.cancel()
    await replica.aclose()


async def claimed_plan(librarian: Librarian) -> WriteApproval:
    approval = await librarian._start_plan(a_plan(), AGENT)  # pyright: ignore[reportPrivateUsage]
    assert isinstance(approval, WriteApproval)
    await librarian.approvals.approve(approval.id, "leads@x.test")
    assert await librarian.approvals.claim(approval)
    return approval


async def test_a_plan_a_stopped_replica_left_open_says_how_far_it_got(
    replicas: Replicas, forge_api: Forge
) -> None:
    first = await replicas()
    approval = await claimed_plan(first)
    approval.step_results += [{"ok": True, "status_code": 201}] * 2  # two steps answered...
    assert await first.approvals.progress(approval)
    await gone(first)  # ...and the replica is stopped

    second = await replicas()
    assert await second.approvals.close_interrupted(timedelta(0)) == 1
    closed = await second.approvals.load(approval.id)
    assert closed is not None and closed.state is ApprovalState.FAILED
    assert closed.note == (
        "interrupted: librarian stopped during this plan. Of its 5 steps, 2 had answered "
        "(blob, tree); step 3 (commit) may or may not have reached Forge, and nothing is "
        "undone. Check before sending anything again."
    )
    states = [s["state"] for s in second.write_status(closed)["steps"]]
    assert states == ["made", "made", "unknown", "not sent", "not sent"]
    assert forge_api.received == []  # librarian did not carry on by itself


@pytest.mark.parametrize(
    ("answered", "state", "note"),
    [
        (
            [True] * 5,
            ApprovalState.EXECUTED,
            "done: 5 writes in order (librarian stopped before it could record the end)",
        ),
        (
            [True, True, False],
            ApprovalState.FAILED,
            "step 3 of 5 (commit) did not succeed, and librarian stopped before it could "
            "record the end. The steps before it were made and are not undone: blob, tree.",
        ),
        (
            [False],
            ApprovalState.FAILED,
            "step 1 of 5 (blob) did not succeed, and librarian stopped before it could record "
            "the end. No step was made before it.",
        ),
        (
            [],
            ApprovalState.FAILED,
            "interrupted: librarian stopped during this plan. Of its 5 steps, 0 had answered "
            "(none); step 1 (blob) may or may not have reached Forge, and nothing is undone. "
            "Check before sending anything again.",
        ),
    ],
)
async def test_a_plan_left_open_ends_as_what_was_recorded_says(
    replicas: Replicas, answered: list[bool], state: ApprovalState, note: str
) -> None:
    first, second = await replicas(), await replicas()
    approval = await claimed_plan(first)
    approval.step_results += [{"ok": ok, "status_code": 201 if ok else 422} for ok in answered]
    assert await first.approvals.progress(approval)
    assert await second.approvals.close_interrupted(timedelta(0)) == 1
    closed = await second.approvals.load(approval.id)
    assert closed is not None and (closed.state, closed.note) == (state, note)


async def test_a_plan_at_work_is_not_taken_for_interrupted(replicas: Replicas) -> None:
    """A plan takes as long as all its steps: each one recorded restarts the clock."""
    first, second = await replicas(), await replicas()
    approval = await claimed_plan(first)
    long_ago = datetime.now(UTC) - timedelta(minutes=40)
    approval.claimed_at = long_ago  # taken 40 minutes ago, and a step has just answered
    approval.step_results.append({"ok": True, "status_code": 201})
    assert await first.approvals.progress(approval)
    assert await second.approvals.close_interrupted() == 0
    approval.progressed_at = long_ago  # nothing heard of it for 40 minutes
    await first.approvals._save(approval)  # pyright: ignore[reportPrivateUsage]
    assert await second.approvals.close_interrupted() == 1
    # By the record it is closed now: the replica that had it is told so, and stops.
    assert not await first.approvals.progress(approval)


async def test_a_plan_closed_as_interrupted_sends_nothing_more(
    replicas: Replicas, forge_api: Forge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replica that did not answer for so long that its plan was closed (and someone may
    have acted on that) does not carry on with the plan when it comes back."""
    first, second = await replicas(), await replicas()
    send = first.executor.execute_write

    async def and_meanwhile(entry: Any, op: Any, request: Any, caller: Any, **more: Any) -> Any:
        result = await send(entry, op, request, caller, **more)
        if op.id == "createTree":
            assert await second.approvals.close_interrupted(timedelta(0)) == 1
        return result

    monkeypatch.setattr(first.executor, "execute_write", and_meanwhile)
    approval = await first._start_plan(a_plan(), AGENT)  # pyright: ignore[reportPrivateUsage]
    assert isinstance(approval, WriteApproval)
    await first.approvals.approve(approval.id, "leads@x.test")
    with pytest.raises(WriteRefused) as stopped:
        await first._run_approved(approval, AGENT)  # pyright: ignore[reportPrivateUsage]
    assert str(stopped.value) == (
        "plan stopped: step 2 of 5 (tree) was made, and nothing after it was sent: meanwhile "
        "the plan had been closed as interrupted, because librarian did not answer for too "
        "long. The steps before it were made and are not undone: blob."
    )
    assert forge_api.order == ["/blobs", "/trees"]
    # The record now says what the replica that made the writes knows.
    kept = await second.approvals.load(approval.id)
    assert kept is not None and kept.state is ApprovalState.FAILED
    states = [s["state"] for s in second.write_status(kept)["steps"]]
    assert states == ["made", "made", "not sent", "not sent", "not sent"]


async def test_asking_how_a_plan_stands_does_not_take_back_what_it_has_recorded(
    replicas: Replicas, forge_api: Forge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`write_status`, an approver reloading the page, a repeat with the same key: each
    reads the approval from the database. On the replica that is making the plan, that
    is behind what the replica knows; an answer that arrives a moment late must not
    replace it, or a step that was made drops out of the record."""
    only = await replicas()
    approval = await only._start_plan(a_plan(), AGENT)  # pyright: ignore[reportPrivateUsage]
    assert isinstance(approval, WriteApproval)
    store = only.approvals.store
    assert store is not None
    read = store.get_doc
    late: list[str | None] = []
    send = only.executor.execute_write

    async def answered_late(kind: str, doc_id: str) -> str | None:
        return late[0]

    async def and_meanwhile(entry: Any, op: Any, request: Any, caller: Any, **more: Any) -> Any:
        if op.id == "createTree":
            late.append(await read("approval", approval.id))  # read now: one step recorded...
        if op.id == "createRef":
            monkeypatch.setattr(store, "get_doc", answered_late)  # ...answered two steps on
            asked = await only.approvals.load(approval.id)
            monkeypatch.setattr(store, "get_doc", read)
            assert asked is not None and len(asked.step_results) == 3
        return await send(entry, op, request, caller, **more)

    monkeypatch.setattr(only.executor, "execute_write", and_meanwhile)
    await only.approvals.approve(approval.id, "leads@x.test")
    outcome = await only._run_approved(approval, AGENT)  # pyright: ignore[reportPrivateUsage]
    assert outcome.steps is not None and [s["state"] for s in outcome.steps] == ["made"] * 5
    kept = await (await replicas()).approvals.load(approval.id)  # as another replica reads it
    assert kept is not None and kept.state is ApprovalState.EXECUTED
    assert [result["data"] for result in kept.step_results] == [
        {"sha": "b10b"},
        {"sha": "7ree"},
        {"sha": "c0mm17", "tree": {"sha": "7ree"}},
        {"ref": "refs/heads/notes"},
        Forge.ANSWERS["/pulls"],
    ]
    # Once it has ended, reading it back works as before.
    assert (await only.approvals.load(approval.id)) is approval


async def test_a_plan_approved_after_a_restart_is_still_made(
    replicas: Replicas, forge_api: Forge
) -> None:
    """Asked for without waiting (as over MCP), so librarian makes it by itself once it is
    approved: also when it was restarted in between and knows the agent only by name."""
    first = await replicas()
    pending = await first.request_plan(a_plan(), AGENT)
    assert isinstance(pending, WriteApproval)
    await gone(first)

    second = await replicas()
    await second.approvals.approve(pending.id, "leads@x.test")
    done = pending
    for _ in range(300):
        done = await second.approvals.load(pending.id) or done
        if done.state in (ApprovalState.EXECUTED, ApprovalState.FAILED):
            break
        await asyncio.sleep(0.02)
    assert done.state is ApprovalState.EXECUTED, done.note
    assert forge_api.order == ["/blobs", "/trees", "/commits", "/refs", "/pulls"]
    # What every step answered was kept, and reads back as it was.
    assert [result["data"] for result in done.step_results][:2] == [
        {"sha": "b10b"},
        {"sha": "7ree"},
    ]


async def test_a_step_that_cannot_be_recorded_ends_the_plan(
    librarian: Librarian, forge: Forge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing more is sent without a record of what was sent so far."""
    record = librarian.approvals.progress

    async def database_gone(approval: WriteApproval) -> bool:
        if len(approval.step_results) == 2:
            raise RuntimeError("the database is gone")
        return await record(approval)

    monkeypatch.setattr(librarian.approvals, "progress", database_gone)
    task = asyncio.create_task(librarian.plan(a_plan(), AGENT))
    with pytest.raises(WriteRefused) as stopped:
        await decided(librarian, task)
    assert re.fullmatch(
        r"plan stopped: step 2 of 5 \(tree\) was made, and nothing after it was sent: "
        r"librarian could not record it \(RuntimeError at test_plans\.py:\d+\)\. The steps "
        r"before it were made and are not undone: blob\.",
        str(stopped.value),
    ), str(stopped.value)
    assert forge.order == ["/blobs", "/trees"]
    approval = next(iter(librarian.approvals._approvals.values()))  # pyright: ignore[reportPrivateUsage]
    assert approval.state is ApprovalState.FAILED
    states = [s["state"] for s in librarian.write_status(approval)["steps"]]
    assert states == ["made", "made", "not sent", "not sent", "not sent"]


async def test_a_step_that_fails_inside_librarian_still_ends_the_plan(
    librarian: Librarian, forge: Forge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Something nobody thought of, in the middle of sending: the plan is not left
    "executing", and the step is not said to be unsent."""
    send = librarian.executor.execute_write

    async def breaks(entry: Any, op: Any, request: Any, caller: Any, **more: Any) -> Any:
        if op.id == "createCommit":
            raise KeyError("something nobody thought of")
        return await send(entry, op, request, caller, **more)

    monkeypatch.setattr(librarian.executor, "execute_write", breaks)
    task = asyncio.create_task(librarian.plan(a_plan(), AGENT))
    with pytest.raises(WriteRefused) as stopped:
        await decided(librarian, task)
    assert re.fullmatch(
        r"plan stopped: step 3 of 5 \(commit\) failed inside librarian \(KeyError at "
        r"test_plans\.py:\d+\)\. It may have reached Forge: check before sending it again\. "
        r"The steps before it were made and are not undone: blob, tree\.",
        str(stopped.value),
    ), str(stopped.value)
    assert "nobody thought of" not in str(stopped.value)  # the message could hold anything
    approval = next(iter(librarian.approvals._approvals.values()))  # pyright: ignore[reportPrivateUsage]
    assert approval.state is ApprovalState.FAILED
    states = [s["state"] for s in librarian.write_status(approval)["steps"]]
    assert states == ["made", "made", "unknown", "not sent", "not sent"]
    assert forge.order == ["/blobs", "/trees"]


def test_how_long_a_write_may_take_before_it_counts_as_interrupted(tmp_path: Path) -> None:
    """Far longer than one request may take, also where requests are allowed to be slow."""
    from librarian.config import Settings

    def after(**settings: Any) -> timedelta:
        return Librarian(
            Settings(registry_dir=tmp_path, secrets_dir=tmp_path, database_url=None, **settings)
        ).approvals.interrupted_after

    assert after() == timedelta(minutes=15)
    assert after(request_timeout_seconds=300) == timedelta(minutes=50)


async def test_a_plan_over_mcp(librarian: Librarian, forge: Forge) -> None:
    """A plan through MCP, against the app served for real."""
    async with served(librarian, write_apis=("tracker", "forge")) as base:
        async with mcp_at(base) as session:
            tools = {tool.name for tool in (await session.list_tools()).tools}
            assert "write_plan" in tools
            asked = await session.call_tool(
                "write_plan", {"api": "forge", "steps": steps(), "reason": "Notes for 2.4"}
            )
            pending: dict[str, Any] = asked.structured_content or {}
            assert pending["status"] == "pending_approval", asked.content
            assert "plan of 5 writes on Forge" in pending["message"]
            assert [s["state"] for s in pending["steps"]] == ["waiting"] * 5
            await approve_the_write(librarian)
            status: dict[str, Any] = {}
            for _ in range(200):
                polled = await session.call_tool("write_status", {"approval": pending["approval"]})
                status = polled.structured_content or {}
                if status["state"] == "executed":
                    break
                await asyncio.sleep(0.02)
            assert [s["state"] for s in status["steps"]] == ["made"] * 5
            assert status["result"]["data"]["number"] == 12
            wrong = await session.call_tool(
                "write_plan", {"api": "forge", "steps": [{"id": "x", "operation": ""}]}
            )
            said = " ".join(getattr(part, "text", "") for part in wrong.content)
            assert wrong.is_error and "invalid plan: steps.0.operation" in said
        assert forge.order == ["/blobs", "/trees", "/commits", "/refs", "/pulls"]


async def test_a_plan_over_a2a(librarian: Librarian, forge: Forge) -> None:
    from a2a.helpers.proto_helpers import new_data_part
    from a2a.types import GetTaskRequest, TaskState

    from librarian.config import CallerKey
    from librarian.server import create_app
    from tests.test_server import KEYS, a2a_client, send

    librarian.settings.callers = [
        CallerKey(
            agent="agent-a",
            key_ref="librarian/callers/agent-a",
            write_apis=["forge"],
            owner="leads@x.test",
        )
    ]
    app = create_app(librarian, caller_keys=KEYS)
    app.state.caller_keys.update(KEYS)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=librarian.settings.public_url,
        headers={"Authorization": "Bearer key-a"},
    ) as http:
        command = {"skill": "plan", "api": "forge", "steps": steps(), "reason": "Notes"}
        outcome = await send(librarian, http, [new_data_part(command)])
        assert outcome.final == TaskState.TASK_STATE_AUTH_REQUIRED
        assert any("A plan of 5 writes to Forge is waiting" in text for text in outcome.texts)
        [approval] = librarian.approvals.pending()
        await librarian.approvals.approve(approval.id, "leads@x.test")
        client = await a2a_client(librarian, http)
        task = await client.get_task(GetTaskRequest(id=outcome.task_id))
        for _ in range(200):
            if task.status.state == TaskState.TASK_STATE_COMPLETED:
                break
            await asyncio.sleep(0.02)
            task = await client.get_task(GetTaskRequest(id=outcome.task_id))
        assert task.status.state == TaskState.TASK_STATE_COMPLETED
        from a2a.helpers.proto_helpers import get_data_parts

        data = [d for a in task.artifacts for d in get_data_parts(a.parts)][-1]
        assert data["how"] == "approved" and [s["state"] for s in data["steps"]] == ["made"] * 5
        wrong = await send(
            librarian,
            http,
            [new_data_part({"skill": "plan", "api": "forge", "steps": steps()[1:]})],
        )
        assert wrong.final == TaskState.TASK_STATE_FAILED
        assert any("invalid plan: step 'tree' refers to 'blob.sha'" in text for text in wrong.texts)
        card = (await http.get("/.well-known/agent-card.json")).json()
        assert "plan" in {skill["id"] for skill in card["skills"]}
    assert forge.order == ["/blobs", "/trees", "/commits", "/refs", "/pulls"]
