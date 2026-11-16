"""A write always ends, and its record says what is known about it.

Once a replica has taken an approved write to send it, the approval is "executing".
If it stayed there (the replica was stopped, or something failed that nobody had
thought of), nobody could tell whether the write was made or may be tried again. So
every way out of "executing" ends in a final state with an honest note, and a repeat
of the same request is never sent beside one that is still open.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from librarian.approvals import ApprovalState, WriteApproval
from librarian.calls import Caller, CallResult, WriteRequest
from librarian.secrets import FileSecretStore
from librarian.service import Librarian, WriteRefused
from tests.conftest import BASE, RecordingNotifier, Router
from tests.test_state import replica

KIM = Caller(agent="notes-bot", principal="kim@x.test", write_apis=("tracker",))
WRITE_KEY = "wr1te-key-9876543210"
COMMENT = WriteRequest(
    api="tracker",
    operation="addComment",
    args={"key": "PAY-1"},
    body={"body": "hello"},
    idempotency_key="once-only",
)


async def approved(librarian: Librarian) -> WriteApproval:
    """An approval a person has said yes to, which nobody has run yet."""
    entry = librarian.registry.get("tracker")
    assert entry is not None
    op = entry.operation("addComment")
    assert op is not None
    approval = await librarian.approvals.open(entry, op, COMMENT, KIM)
    await librarian.approvals.approve(approval.id, "kim@x.test")
    return approval


async def test_a_write_a_stopped_replica_left_open_is_closed_as_unknown(
    tmp_path: Path, router: Router
) -> None:
    router.add(
        "POST", f"{BASE}/v1/issues/PAY-1/comments", lambda r: httpx.Response(201, json={"id": 7})
    )
    first = replica(tmp_path, router)
    await first.start()
    await first.secrets.put("tracker/write", WRITE_KEY)
    approval = await approved(first)
    assert await first.approvals.claim(approval)  # a replica takes it to send it...
    await first.aclose()  # ...and is stopped before it has an answer

    second = replica(tmp_path, router)
    await second.start()  # looks for interrupted writes, and finds none old enough yet
    found = await second.approvals.load(approval.id)
    assert found is not None and found.state is ApprovalState.EXECUTING

    # While it is open, the same request is refused: a second copy must not go out.
    with pytest.raises(WriteRefused, match="still being run"):
        await second.request_write(COMMENT, KIM)

    assert await second.approvals.close_interrupted(timedelta(0)) == 1
    closed = await second.approvals.load(approval.id)
    assert closed is not None and closed.state is ApprovalState.FAILED
    assert closed.note and "may or may not have reached Issue Tracker" in closed.note
    assert second.write_status(closed)["state"] == "failed"
    events = second.audit.events  # type: ignore[attr-defined]
    assert [e.event for e in events if e.event == "write_interrupted"] == ["write_interrupted"]
    notifier = second.notifier
    assert isinstance(notifier, RecordingNotifier)
    assert [(kind, a.id) for kind, a in notifier.write_events] == [("closed", approval.id)]
    assert router.requests == []  # librarian never guesses and sends it again itself

    # Closing happens once, whichever replica gets there first.
    assert await second.approvals.close_interrupted(timedelta(0)) == 0
    # Now that it has ended, the agent may ask again (and a person decides again).
    again = await second.request_write(COMMENT, KIM)
    assert isinstance(again, WriteApproval) and again.id != approval.id
    assert again.state is ApprovalState.PENDING
    await second.aclose()


async def test_a_write_being_sent_right_now_is_left_alone(tmp_path: Path, router: Router) -> None:
    librarian = replica(tmp_path, router)
    await librarian.start()
    await librarian.secrets.put("tracker/write", WRITE_KEY)
    approval = await approved(librarian)
    assert await librarian.approvals.claim(approval)
    assert await librarian.approvals.close_interrupted() == 0  # the default: 15 minutes
    assert approval.state is ApprovalState.EXECUTING and approval.claimed_at is not None
    await librarian.aclose()


async def test_a_failure_nobody_thought_of_still_ends_the_approval(
    librarian: Librarian,
    router: Router,
    secrets_store: FileSecretStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await secrets_store.put("tracker/write", WRITE_KEY)

    async def explode(*args: Any, **kwargs: Any) -> CallResult:
        raise RuntimeError(f"something about Bearer {WRITE_KEY}")

    monkeypatch.setattr(librarian.executor, "execute_write", explode)
    approval = await approved(librarian)
    with pytest.raises(WriteRefused) as refused:
        await librarian._run_approved(approval, KIM)  # pyright: ignore[reportPrivateUsage]
    assert approval.state is ApprovalState.FAILED
    told = f"{refused.value} {approval.note}"
    assert "librarian failed while making this write (RuntimeError at" in told
    assert "It may have reached Issue Tracker: check before sending it again" in told
    assert WRITE_KEY not in told  # the failure's own message is not passed on


async def test_a_result_that_cannot_be_kept_does_not_undo_what_is_known(
    librarian: Librarian,
    router: Router,
    secrets_store: FileSecretStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await secrets_store.put("tracker/write", WRITE_KEY)
    router.add(
        "POST", f"{BASE}/v1/issues/PAY-1/comments", lambda r: httpx.Response(201, json={"id": 7})
    )
    real_dump = CallResult.model_dump
    calls = 0

    def dump_fails_once(self: CallResult, **kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("cannot be serialised")
        return real_dump(self, **kwargs)

    monkeypatch.setattr(CallResult, "model_dump", dump_fails_once)
    approval = await approved(librarian)
    outcome = await asyncio.wait_for(
        librarian._run_approved(approval, KIM),  # pyright: ignore[reportPrivateUsage]
        5,
    )
    assert outcome.result.status_code == 201  # the agent still gets the answer
    assert approval.state is ApprovalState.EXECUTED and approval.result is None
    assert approval.note and "answered HTTP 201, but the answer could not be kept" in approval.note
    assert len([r for r in router.requests if r.method == "POST"]) == 1


async def test_a_sent_write_is_recorded_even_if_reading_its_answer_fails(
    librarian: Librarian,
    router: Router,
    secrets_store: FileSecretStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the moment a write has gone out, nothing may stop it being reported."""
    await secrets_store.put("tracker/write", WRITE_KEY)
    router.add(
        "POST", f"{BASE}/v1/issues/PAY-1/comments", lambda r: httpx.Response(201, json={"id": 7})
    )

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"something about Bearer {WRITE_KEY}")

    monkeypatch.setattr("librarian.executor._refusal", explode)
    entry = librarian.registry.get("tracker")
    assert entry is not None
    op = entry.operation("addComment")
    assert op is not None
    result = await librarian.executor.execute_write(entry, op, COMMENT, KIM)
    assert result.status_code == 201 and result.ok and result.data is None
    assert result.error == "Issue Tracker answered HTTP 201, but its answer could not be read"
    written = [e for e in librarian.audit.events if e.event == "write"]  # type: ignore[attr-defined]
    assert [(e.status_code, e.detail) for e in written] == [(201, "the answer could not be read")]
    assert WRITE_KEY not in result.model_dump_json() + written[0].model_dump_json()
