"""Enable-write requests: a refused write asks the approvers instead of failing silently."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from librarian.audit import MemoryAuditSink
from librarian.calls import Caller, WriteRequest
from librarian.config import ApproverSettings
from librarian.console import enable_routes
from librarian.enablement import EnableState, EnableWriteRequest
from librarian.executor import PolicyDenied
from librarian.notifiers import (
    WebhookNotifier,
    card_blocks,
    enable_blocks,
    enable_outcome_text,
)
from librarian.registry.models import BearerAuth
from librarian.secrets import FileSecretStore
from librarian.service import Librarian, WriteNotEnabled
from librarian.web import credential_routes
from tests.conftest import BASE, RecordingNotifier, Router
from tests.test_console import DecisionPublisher, console, token
from tests.test_notifiers_and_net import make_request
from tests.test_web import themed
from tests.test_writes import BOT, KIM

# TRACKER's renameProject is `access: write` but not writable: the shape found in a real
# deployment, where hundreds of GitHub operations wait for someone to enable them.
RENAME = WriteRequest(
    api="tracker", operation="renameProject", args={"p": "PAY"}, reason="rename for the new team"
)


class ProposalPublisher(DecisionPublisher):
    """Keeps the proposed entries, not just their titles."""

    def __init__(self) -> None:
        super().__init__(open_for=set())
        self.entries: list[tuple[Any, str]] = []

    async def propose_update(self, entry: Any, title: str, body: str) -> str:
        self.entries.append((entry, title))
        return await super().propose_update(entry, title, body)


@pytest.fixture(autouse=True)
async def read_key(secrets_store: FileSecretStore) -> None:
    await secrets_store.put("tracker/readonly", "read-key")


async def without_write_credential(librarian: Librarian) -> None:
    entry = librarian.registry.get("tracker")
    assert entry is not None
    await librarian.registry.save(entry.model_copy(update={"write_auth": None}))


def enable_client(librarian: Librarian) -> TestClient:
    return TestClient(Starlette(routes=enable_routes(librarian)))


# -- asking ------------------------------------------------------------------------------


async def test_refused_write_asks_the_approvers_once(
    librarian: Librarian, notifier: RecordingNotifier, audit: MemoryAuditSink
) -> None:
    with pytest.raises(WriteNotEnabled) as info:
        await librarian.write(RENAME, KIM)
    asked = info.value.request
    assert asked.needs_enabling and not asked.needs_credential  # the tracker has a write key
    assert "approvers have been asked" in str(info.value) and asked.id in str(info.value)
    # a second agent asking joins the same request instead of opening another
    with pytest.raises(WriteNotEnabled) as again:
        await librarian.write(RENAME.model_copy(update={"reason": "same thing"}), BOT)
    assert again.value.request.id == asked.id and asked.asks == 2
    assert asked.requested_by == ["notes-bot (for kim@x.test)", "triage-bot"]
    assert asked.reasons == ["rename for the new team", "same thing"]
    assert [ev for ev, _, _ in notifier.enables] == ["opened", "updated"]
    link = notifier.enables[0][2]
    assert link is not None and link.startswith("https://librarian.example.test/enable/")
    assert [ev.event for ev in audit.events].count("enable_write_requested") == 2
    assert audit.events[-1].extra == {
        "request": asked.id,
        "asks": 2,
        "operations": ["renameProject"],
    }
    # declining tells the next asker no, without asking the approvers again
    done = await librarian.decline_writes(asked.id, "sam@x.test", "not for agents")
    assert done.state is EnableState.DECLINED and done.note == "not for agents"
    with pytest.raises(PolicyDenied, match="declined to enable it \\(not for agents\\)"):
        await librarian.write(RENAME, KIM)
    assert librarian.enablement.pending() == [] and len(notifier.enables) == 3
    assert audit.events[-1].event == "write_denied" and "declined" in (
        audit.events[-1].detail or ""
    )


async def test_reads_and_unknown_operations_are_still_plain_denials(librarian: Librarian) -> None:
    with pytest.raises(PolicyDenied, match="not enabled for writes"):
        await librarian.write(
            WriteRequest(api="tracker", operation="getIssue", args={"key": "X"}), KIM
        )
    with pytest.raises(PolicyDenied, match="may not write"):
        await librarian.write(RENAME, Caller(agent="reader", principal="kim@x.test"))
    assert librarian.enablement.pending() == []


# -- enabling ----------------------------------------------------------------------------


async def test_enable_proposes_the_change_and_asks_for_a_write_credential(
    librarian: Librarian,
    router: Router,
    notifier: RecordingNotifier,
    audit: MemoryAuditSink,
    secrets_store: FileSecretStore,
) -> None:
    await without_write_credential(librarian)
    pub = ProposalPublisher()
    librarian.publisher = pub
    router.add("GET", f"{BASE}/v1/me", lambda r: httpx.Response(200, json={"me": 1}))
    with pytest.raises(WriteNotEnabled) as info:
        await librarian.write(RENAME, KIM)
    asked = info.value.request
    assert asked.needs_enabling and asked.needs_credential

    client = enable_client(librarian)
    t = librarian.enablement.token_for(asked)
    assert client.get(f"/enable/{asked.id}?t=nope").status_code == 404
    page = client.get(f"/enable/{asked.id}?t={t}")
    assert page.status_code == 200
    themed(page)
    assert "notes-bot (for kim@x.test) wants to write to Issue Tracker" in page.text
    assert "renameProject" in page.text and "rename for the new team" in page.text
    assert "tracker/write" in page.text and "tracker/readonly" in page.text  # the key choice
    done = client.post(f"/enable/{asked.id}", data={"t": t, "action": "enable", "key": "dedicated"})
    assert done.status_code == 200
    assert "Proposed for review; a write credential was asked for" in done.text
    assert "pr/update" in done.text and "Add the write credential" in done.text

    # the proposal carries both changes; the live verified entry does not change
    proposed, title = pub.entries[-1]
    assert title == "Issue Tracker: enable writes for renameProject"
    op = proposed.operation("renameProject")
    assert op is not None and op.writable and proposed.write_secret_ref == "tracker/write"
    assert "enabled writes for `renameProject`" in proposed.notes
    assert "Reason given: rename for the new team" in proposed.notes
    live = librarian.registry.get("tracker")
    assert live is not None and live.write_auth is None
    assert not live.operation("renameProject").writable  # type: ignore[union-attr]

    # the write key is asked for through the usual secure page, with write wording
    key = librarian.broker.pending_for("tracker", "write")
    assert key is not None and key.purpose == "write" and key.secret_ref == "tracker/write"
    assert key.auth_type == "bearer" and not key.writes_possible
    assert key.create_url == "https://tracker.example.test/settings/tokens"
    assert key.instructions.startswith("Create a credential that may only PUT /projects/{p}")
    assert notifier.events[-1] == ("opened", key)
    creds = TestClient(Starlette(routes=credential_routes(librarian.broker, ApproverSettings())))
    kt = librarian.broker.token_for(key)
    text = creds.get(f"/credentials/{key.id}?t={kt}").text
    assert "needs a write credential" in text and "Create a key that can write" in text
    assert "allow_writes" not in text and "existing secret" not in text  # no read-key reuse
    assert "Waiting" not in text
    saved = creds.post(
        f"/credentials/{key.id}", data={"t": kt, "action": "add", "value": "new-write-key"}
    )
    assert saved.status_code == 200 and "write credential added" in saved.text
    assert "tested OK" in saved.text
    assert await secrets_store.get("tracker/write") == "new-write-key"
    assert await secrets_store.get("tracker/readonly") == "read-key"  # untouched
    tested = [r for r in router.requests if r.url.path.endswith("/me")]
    assert tested and tested[-1].headers["authorization"] == "Bearer new-write-key"

    assert asked.state is EnableState.ENABLED and asked.decided_by == "link holder"
    assert asked.review_url == "https://github.test/pr/update" and asked.key_request == key.id
    assert librarian.enablement.pending() == []
    events = [ev.event for ev in audit.events]
    assert "enable_write_enabled" in events and "credential_request" in events
    assert client.get(f"/enable/{asked.id}?t={t}").status_code == 404  # decided once


async def test_denying_the_write_key_does_not_block_the_api(
    librarian: Librarian, router: Router
) -> None:
    await without_write_credential(librarian)
    librarian.publisher = ProposalPublisher()
    with pytest.raises(WriteNotEnabled) as info:
        await librarian.write(RENAME, KIM)
    await librarian.enable_writes(info.value.request.id, "sam@x.test")
    key = librarian.broker.pending_for("tracker", "write")
    assert key is not None
    error = await librarian.broker.use_existing(key.id, "tracker/readonly", "sam@x.test")
    assert error == "A write credential is added by pasting it."  # never re-points the read key
    await librarian.broker.deny(key.id, "sam@x.test")
    entry = librarian.registry.get("tracker")
    assert entry is not None and entry.status.value == "verified"  # reads carry on
    assert "write credential denied" in (key.resolution_note or "")
    assert librarian.enablement.pending() == []


async def test_console_enables_an_operation_with_the_read_key(
    librarian: Librarian, audit: MemoryAuditSink
) -> None:
    librarian.settings.approvers.console = "open"
    await without_write_credential(librarian)
    pub = ProposalPublisher()
    librarian.publisher = pub
    c = console(librarian)
    t = token(librarian)
    page = c.get("/apis/tracker").text
    assert "/ops/renameProject/access" in page and "Change</button>" in page
    assert "/ops/addComment/enable" in page and "Add a write credential" in page
    assert "/ops/getIssue/enable" not in page
    done = c.post("/apis/tracker/ops/renameProject/enable", data={"t": t, "key": "read"})
    assert done.status_code == 200 and "Proposed for review" in done.text
    assert "write credential was asked" not in done.text
    proposed, _ = pub.entries[-1]
    op = proposed.operation("renameProject")
    assert op is not None and op.writable and proposed.write_secret_ref == "tracker/readonly"
    assert "Writes use the read credential `tracker/readonly` too" in proposed.notes
    assert librarian.broker.pending_for("tracker", "write") is None  # no key asked for
    assert audit.events[-1].event == "enable_write_enabled"
    request = next(iter(librarian.enablement._requests.values()))  # pyright: ignore[reportPrivateUsage]
    assert request.requested_by == ["someone (console open)"] and request.state.value == "enabled"
    bad = c.post("/apis/tracker/ops/getIssue/enable", data={"t": t})
    assert bad.status_code == 400 and "read operation" in bad.text
    # with a write key in place, an already enabled operation has nothing to enable
    entry = librarian.registry.get("tracker")
    assert entry is not None
    await librarian.registry.save(
        entry.model_copy(update={"write_auth": BearerAuth(secret_ref="tracker/write")})
    )
    done = c.post("/apis/tracker/ops/addComment/enable", data={"t": t})
    assert done.status_code == 400 and "already enabled" in done.text


async def test_console_lists_wanted_writes_and_the_page_declines(librarian: Librarian) -> None:
    librarian.settings.approvers.console = "open"
    with pytest.raises(WriteNotEnabled) as info:
        await librarian.write(RENAME, KIM)
    asked = info.value.request
    c = console(librarian)
    overview = c.get("/").text
    assert (
        "Enable writes on <b>Issue Tracker</b>" in overview and f"/enable/{asked.id}?t=" in overview
    )
    assert "Enable writes on" in c.get("/waiting").text
    detail = c.get("/apis/tracker").text
    assert "Wanted writes" in detail and "wants to write: decide" in detail
    assert "/ops/renameProject/access" not in detail  # the request's page decides, not a control
    client = enable_client(librarian)
    t = librarian.enablement.token_for(asked)
    done = client.post(
        f"/enable/{asked.id}", data={"t": t, "action": "decline", "note": "not for agents"}
    )
    assert done.status_code == 200 and "not for agents" in done.text
    assert client.post(f"/enable/{asked.id}", data={"t": t, "action": "decline"}).status_code == 404
    assert "Enable writes on" not in c.get("/").text
    assert (
        "/ops/renameProject/access" in c.get("/apis/tracker").text
    )  # a person can still enable it


# -- persistence --------------------------------------------------------------------------------


async def test_requests_and_declines_survive_restart(tmp_path: Any, router: Router) -> None:
    from tests.test_state import replica

    first = replica(tmp_path, router)
    await first.start()
    with pytest.raises(WriteNotEnabled):
        await first.write(RENAME, KIM)
    with pytest.raises(WriteNotEnabled):
        await first.write(RENAME.model_copy(update={"reason": "again"}), BOT)
    await first.aclose()

    second = replica(tmp_path, router)
    await second.start()
    pending = second.enablement.pending()
    assert [p.operation for p in pending] == ["renameProject"] and pending[0].asks == 2
    await second.decline_writes(pending[0].id, "sam@x.test")
    await second.aclose()

    third = replica(tmp_path, router)
    await third.start()
    with pytest.raises(PolicyDenied, match="declined"):
        await third.write(RENAME, KIM)
    # the decline lapses: asking is possible again
    declined = third.enablement.declined_for("tracker", "renameProject")
    assert declined is not None
    declined.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await third.enablement._save(declined)  # pyright: ignore[reportPrivateUsage]
    await third.enablement.sweep()
    assert third.enablement.declined_for("tracker", "renameProject") is None
    with pytest.raises(WriteNotEnabled) as info:
        await third.write(RENAME, KIM)
    # another replica sees the open request without a restart
    other = replica(tmp_path, router)
    await other.start()
    await other.enablement.refresh()
    assert [r.id for r in other.enablement.pending()] == [info.value.request.id]
    await third.aclose()
    await other.aclose()


# -- notifications ------------------------------------------------------------------------------


def sample() -> EnableWriteRequest:
    return EnableWriteRequest(
        api="github",
        api_name="GitHub",
        host="api.github.com",
        operation="issues/create-comment",
        method="POST",
        path="/repos/{owner}/{repo}/issues/{n}/comments",
        summary="Create an issue comment",
        needs_enabling=True,
        needs_credential=True,
        requested_by=["release-bot (for kim)"],
        reasons=["post the release notes"],
        expires_at=datetime.now(UTC) + timedelta(days=7),
        approver_group="S0APPROVE1",
    )


def test_slack_card_for_a_wanted_write() -> None:
    request = sample()
    text = json.dumps(enable_blocks(request, "https://lib.test/enable/x?t=1"))
    assert "release-bot (for kim) wants to write to GitHub: issues/create-comment" in text
    assert "post the release notes" in text and "<!subteam^S0APPROVE1>" in text
    assert "none yet; enabling asks for one" in text and "https://lib.test/enable/x?t=1" in text
    request.state = EnableState.DECLINED
    request.decided_by = "sam"
    request.note = "not for agents"
    assert enable_outcome_text(request) == (
        ":no_entry: GitHub: writes for issues/create-comment declined by sam · not for agents"
    )
    assert "Decide" not in json.dumps(enable_blocks(request, None))
    # a credential card for a write key says what it is for
    key = make_request(purpose="write", secret_ref="jira/write")
    card = json.dumps(card_blocks(key, "https://lib.test/credentials/k?t=1"))
    assert "access needed (write credential)" in card and "pick an existing secret" not in card


async def test_webhook_events_for_enable_requests() -> None:
    received: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(json.loads(request.content))
        return httpx.Response(204)

    notifier = WebhookNotifier(
        "https://hooks.test/x", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    request = sample()
    await notifier.enable_opened(request, "https://lib.test/enable/x?t=1")
    await notifier.enable_closed(request)
    assert [r["event"] for r in received] == ["enable_write.opened", "enable_write.closed"]
    assert received[0]["link"] == "https://lib.test/enable/x?t=1"
    assert received[0]["request"]["operation"] == "issues/create-comment"
    assert "notifier_ref" not in received[0]["request"]


# -- several operations at once, and where a request stands ------------------------------


async def with_flow_operations(librarian: Librarian) -> None:
    from librarian.registry.models import Access, Operation

    entry = librarian.registry.get("tracker")
    assert entry is not None
    extra = [
        Operation(id="createTree", method="POST", path="/trees", access=Access.WRITE),
        Operation(id="createCommit", method="POST", path="/commits", access=Access.WRITE),
    ]
    await librarian.registry.save(
        entry.model_copy(update={"operations": [*entry.operations, *extra]})
    )


async def test_one_request_covers_a_whole_flow_and_reports_its_status(
    librarian: Librarian, notifier: RecordingNotifier
) -> None:
    from librarian.service import LibrarianError

    await with_flow_operations(librarian)
    flow = RENAME.model_copy(
        update={"also_needs": ["createTree", "createCommit", "getIssue", "nope"]}
    )
    with pytest.raises(WriteNotEnabled) as info:
        await librarian.write(flow, KIM)
    asked = info.value.request
    assert asked.operations == ["renameProject", "createTree", "createCommit"]
    assert "and 2 more on this request" in str(info.value)
    # another agent asking for one of them joins the same request
    with pytest.raises(WriteNotEnabled) as again:
        await librarian.write(
            WriteRequest(api="tracker", operation="createCommit", reason="same flow"), BOT
        )
    assert again.value.request.id == asked.id and asked.asks == 2
    assert "(+2 more)" in json.dumps(enable_blocks(asked, "https://l/e"))
    status = await librarian.enable_status(asked.id, KIM)
    assert status["state"] == "pending" and status["operations"] == asked.operations
    assert (await librarian.enable_status(asked.id, BOT))["state"] == "pending"
    with pytest.raises(LibrarianError, match="for this agent"):
        await librarian.enable_status(asked.id, Caller(agent="stranger"))

    pub = ProposalPublisher()
    librarian.publisher = pub
    client = enable_client(librarian)
    t = librarian.enablement.token_for(asked)
    page = client.get(f"/enable/{asked.id}?t={t}").text
    assert "3 operations in this flow" in page and "name=op value='createCommit'" in page
    assert "Enable writes for 3 operations" in page
    done = client.post(
        f"/enable/{asked.id}",
        data={"t": t, "action": "enable", "op": ["renameProject", "createTree"]},
    )
    assert done.status_code == 200 and "Writes enabled for renameProject and 1 more" in done.text
    proposed, title = pub.entries[-1]
    assert title == "Issue Tracker: enable writes for renameProject, createTree"
    assert (
        proposed.operation("renameProject").writable and proposed.operation("createTree").writable
    )  # type: ignore[union-attr]
    assert not proposed.operation("createCommit").writable  # type: ignore[union-attr]
    assert "Left disabled: `createCommit`" in proposed.notes
    assert asked.enabled == ["renameProject", "createTree"]
    status = await librarian.enable_status(asked.id, KIM)
    assert (
        status["state"] == "enabled" and "waiting for the review to be merged" in status["message"]
    )
    assert (
        status["review_url"] == "https://github.test/pr/update" and status["live_operations"] == []
    )
    # once the proposal is merged (the registry syncs), the request is live
    await librarian.registry.save(proposed)
    status = await librarian.enable_status(asked.id, KIM)
    assert status["state"] == "live" and status["live_operations"] == [
        "renameProject",
        "createTree",
    ]
    # the operation left disabled can be asked for again, without being declined
    with pytest.raises(WriteNotEnabled) as third:
        await librarian.write(
            WriteRequest(api="tracker", operation="createCommit", reason="now this"), KIM
        )
    assert third.value.request.id != asked.id and third.value.request.operations == ["createCommit"]


async def test_declined_operations_are_left_out_of_later_flows(librarian: Librarian) -> None:
    await with_flow_operations(librarian)
    with pytest.raises(WriteNotEnabled) as info:
        await librarian.write(
            WriteRequest(api="tracker", operation="createCommit", reason="x"), KIM
        )
    await librarian.decline_writes(info.value.request.id, "sam@x.test", "never commits")
    status = await librarian.enable_status(info.value.request.id, KIM)
    assert (
        status["state"] == "declined" and "never commits" in status["message"] and status["until"]
    )
    with pytest.raises(WriteNotEnabled) as again:
        await librarian.write(RENAME.model_copy(update={"also_needs": ["createCommit"]}), KIM)
    assert again.value.request.operations == ["renameProject"]  # the declined one is not re-asked
