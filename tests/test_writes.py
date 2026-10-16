"""Writes: policy, approvals, grants, autonomous agents, pages and A2A."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.testclient import TestClient

from librarian.approvals import ApprovalState, Grant, WriteApproval
from librarian.calls import Caller, WriteRequest
from librarian.config import ApproverSettings
from librarian.executor import PolicyDenied
from librarian.registry import ApiEntry, EntryStatus, Operation
from librarian.registry.models import Owners
from librarian.secrets import FileSecretStore
from librarian.service import Librarian, WriteRefused
from librarian.web import approval_routes, credential_routes
from tests.conftest import BASE, TRACKER, RecordingNotifier, Router

KIM = Caller(agent="notes-bot", principal="kim@x.test", write_apis=("tracker",))
BOT = Caller(agent="triage-bot", owner="ops@x.test", write_apis=("tracker",))
ORPHAN = Caller(agent="orphan-bot", write_apis=("tracker",))


def comment(key: str = "PAY-1", text: str = "hello", **kw: Any) -> WriteRequest:
    return WriteRequest(
        api="tracker", operation="addComment", args={"key": key}, body={"body": text}, **kw
    )


@pytest.fixture(autouse=True)
async def creds(secrets_store: FileSecretStore, router: Router) -> None:
    await secrets_store.put("tracker/readonly", "read-key")
    await secrets_store.put("tracker/write", "write-key")
    router.add(
        "POST", f"{BASE}/v1/issues/PAY-1/comments", lambda r: httpx.Response(201, json={"id": 7})
    )
    router.add(
        "POST", f"{BASE}/v1/issues/PAY-2/comments", lambda r: httpx.Response(201, json={"id": 8})
    )


async def approve_when_open(librarian: Librarian, **kwargs: Any) -> str:
    while not librarian.approvals._approvals:  # pyright: ignore[reportPrivateUsage]
        await asyncio.sleep(0.01)
    approval = next(iter(librarian.approvals._approvals.values()))  # pyright: ignore[reportPrivateUsage]
    await librarian.approvals.approve(approval.id, "kim@x.test", **kwargs)
    return approval.id


# -- models and policy -------------------------------------------------------------


def test_only_write_operations_can_be_writable() -> None:
    with pytest.raises(ValidationError, match="only access: write"):
        Operation(id="x", method="GET", path="/x", access="read", writable=True)  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="never be remembered"):
        Operation(id="x", method="DELETE", path="/x", access="write", rememberable=True)  # type: ignore[arg-type]
    assert Operation(id="x", method="DELETE", path="/x", access="write").rememberable is False  # type: ignore[arg-type]


def test_write_credential_cannot_be_internal() -> None:
    with pytest.raises(ValidationError, match="reserved"):
        ApiEntry.model_validate(
            {**TRACKER, "write_auth": {"type": "bearer", "secret_ref": "librarian/x"}}
        )


@pytest.mark.parametrize(
    ("caller", "request_", "reason"),
    [
        (Caller(agent="reader"), comment(), "may not write"),
        (
            KIM,
            WriteRequest(api="tracker", operation="renameProject", args={"p": "x"}),
            "not enabled for writes",
        ),
        (
            KIM,
            WriteRequest(api="tracker", operation="getIssue", args={"key": "A"}),
            "not enabled for writes",
        ),
        (KIM, WriteRequest(api="tracker", operation="addComment", args={}), "missing required"),
    ],
)
def test_write_policy_denials(
    librarian: Librarian, caller: Caller, request_: WriteRequest, reason: str
) -> None:
    decision = librarian.policy.check_write(request_, caller)
    assert not decision.allowed and reason in decision.reason


async def test_writes_need_verified_entry_and_write_credential(librarian: Librarian) -> None:
    entry = librarian.registry.get("tracker")
    assert entry is not None
    await librarian.registry.save(entry.model_copy(update={"status": EntryStatus.PROVISIONAL}))
    assert "not verified" in librarian.policy.check_write(comment(), KIM).reason
    await librarian.registry.save(entry.model_copy(update={"write_auth": None}))
    assert "no write credential" in librarian.policy.check_write(comment(), KIM).reason


# -- approval flow -------------------------------------------------------------------


async def test_approved_write_runs_exactly_once_with_write_key(
    librarian: Librarian, router: Router, notifier: RecordingNotifier
) -> None:
    hooked: list[str] = []

    async def hook(approval: Any) -> None:
        hooked.append(approval.id)

    task = asyncio.create_task(librarian.write(comment(reason="ack customer"), KIM, hook))
    approval_id = await approve_when_open(librarian)
    outcome = await asyncio.wait_for(task, 5)
    assert outcome.how == "approved" and outcome.result.data == {"id": 7}
    assert hooked == [approval_id]
    sent = [r for r in router.requests if r.method == "POST"]
    assert len(sent) == 1
    assert sent[0].headers["authorization"] == "Bearer write-key"  # never the read key
    assert json.loads(sent[0].content) == {"body": "hello"}
    approval = librarian.approvals._approvals[approval_id]  # pyright: ignore[reportPrivateUsage]
    assert approval.state is ApprovalState.EXECUTED
    assert [e for e, _ in notifier.write_events] == ["opened", "closed"]


async def test_denied_write_is_refused(librarian: Librarian, router: Router) -> None:
    task = asyncio.create_task(librarian.write(comment(), KIM))
    while not librarian.approvals._approvals:  # pyright: ignore[reportPrivateUsage]
        await asyncio.sleep(0.01)
    approval = next(iter(librarian.approvals._approvals.values()))  # pyright: ignore[reportPrivateUsage]
    await librarian.approvals.deny(approval.id, "kim@x.test")
    with pytest.raises(WriteRefused, match="denied"):
        await asyncio.wait_for(task, 5)
    assert not [r for r in router.requests if r.method == "POST"]


async def test_agent_that_stops_waiting_expires_its_approval(librarian: Librarian) -> None:
    impatient = KIM.model_copy(update={"approval_wait_seconds": 0.05})
    with pytest.raises(WriteRefused, match="expired"):
        await librarian.write(comment(), impatient)
    approval = next(iter(librarian.approvals._approvals.values()))  # pyright: ignore[reportPrivateUsage]
    assert approval.state is ApprovalState.EXPIRED
    assert not librarian.approvals.check_token(approval, librarian.approvals.token_for(approval))


async def test_nobody_can_approve_an_orphan_agent_without_a_group(librarian: Librarian) -> None:
    # Signed-in approvers (trusted_header) but no `approvers.group`: only requesters decide,
    # and an agent with no owner acting for no one has none.
    librarian.settings.approvers.mode = "trusted_header"
    librarian.approvals.deciding_group = None
    with pytest.raises(PolicyDenied, match=r"no one can approve.*approvers\.group"):
        await librarian.write(comment(), ORPHAN)
    assert not librarian.approvals._approvals  # pyright: ignore[reportPrivateUsage]
    # the Slack user group is who gets pinged, not who may decide
    librarian.settings.notifier.slack.approver_group = "S0PINGED"
    with pytest.raises(PolicyDenied, match="no one can approve"):
        await librarian.write(comment(), ORPHAN)
    # in token mode whoever holds the link decides, so the write is asked
    librarian.settings.approvers.mode = "token"
    task = asyncio.create_task(librarian.write(comment(), ORPHAN))
    while not librarian.approvals._approvals:  # pyright: ignore[reportPrivateUsage]
        await asyncio.sleep(0.01)
    task.cancel()


async def test_orphan_agent_goes_to_the_group(librarian: Librarian) -> None:
    librarian.approvals.deciding_group = "api-approvers"  # approvers.group, the IdP's name
    task = asyncio.create_task(librarian.write(comment(), ORPHAN))
    while not librarian.approvals._approvals:  # pyright: ignore[reportPrivateUsage]
        await asyncio.sleep(0.01)
    approval = next(iter(librarian.approvals._approvals.values()))  # pyright: ignore[reportPrivateUsage]
    assert approval.requesters == []
    assert approval.approver_group == "S0TRACKER"  # the Slack group from the entry's owners
    broker = librarian.approvals
    assert broker.may_approve(approval, "eve@x.test", {"api-approvers"}) is None
    # the Slack group is who gets pinged, not who decides
    assert broker.may_approve(approval, "eve@x.test", {"S0TRACKER"}) is not None
    assert (
        broker.may_approve(approval, "eve@x.test", set())
        == "Only the requester or members of api-approvers can approve this write."
    )
    task.cancel()


async def test_the_refusal_names_who_may_approve(librarian: Librarian) -> None:
    """The bug: an agent with an owner who never signs in, no Slack group and the IdP group
    in `approvers.group` said "Only the requester or members of None can approve"."""
    broker = librarian.approvals
    broker.deciding_group = "1725aa2a-group-object-id"
    owned = ORPHAN.model_copy(update={"owner": "infra@x.test"})
    task = asyncio.create_task(librarian.write(comment(), owned))
    while not broker._approvals:  # pyright: ignore[reportPrivateUsage]
        await asyncio.sleep(0.01)
    approval = next(iter(broker._approvals.values()))  # pyright: ignore[reportPrivateUsage]
    assert broker.may_approve(approval, "sam@x.test", {"1725aa2a-group-object-id"}) is None
    why = broker.may_approve(approval, "sam@x.test", {"staff"})
    assert why == (
        "Only the requester (infra@x.test) or members of 1725aa2a-group-object-id "
        "can approve this write."
    )
    broker.deciding_group = None
    assert broker.may_approve(approval, "sam@x.test", {"staff"}) == (
        "Only the requester (infra@x.test) or the approver group can approve this write."
        " No approver group is configured (approvers.group)."
    )
    assert "None" not in str(broker.may_approve(approval, "sam@x.test", set()))
    task.cancel()


def test_who_may_approve(librarian: Librarian) -> None:
    from librarian.approvals import WriteApproval

    approval = WriteApproval(
        fingerprint="f",
        api="tracker",
        api_name="T",
        host="h",
        operation="addComment",
        method="POST",
        path="/x",
        request=comment(),
        agent="triage-bot",
        requesters=["ops@x.test"],
        approver_group="admins",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    broker = librarian.approvals
    broker.deciding_group = "admins"
    broker.approver_mode = "requester"
    assert broker.may_approve(approval, "OPS@x.test", set()) is None  # owner, case-insensitive
    assert broker.may_approve(approval, "eve@x.test", {"admins"}) is not None
    broker.approver_mode = "group"
    assert broker.may_approve(approval, "ops@x.test", set()) is not None
    assert broker.may_approve(approval, "eve@x.test", {"admins"}) is None
    assert broker.may_approve(approval, None, set()) is None  # token mode: link holder


async def test_identical_pending_writes_share_one_approval(librarian: Librarian) -> None:
    first = asyncio.create_task(librarian.write(comment(), KIM))
    second = asyncio.create_task(librarian.write(comment(), KIM))
    await asyncio.sleep(0.1)
    assert len(librarian.approvals._approvals) == 1  # pyright: ignore[reportPrivateUsage]
    first.cancel()
    second.cancel()


async def test_idempotency_key_returns_first_result(librarian: Librarian, router: Router) -> None:
    task = asyncio.create_task(librarian.write(comment(idempotency_key="k1"), KIM))
    await approve_when_open(librarian)
    await asyncio.wait_for(task, 5)
    again = await librarian.write(comment(idempotency_key="k1"), KIM)
    assert again.how == "idempotent" and again.result.data == {"id": 7}
    assert len([r for r in router.requests if r.method == "POST"]) == 1


# -- grants ------------------------------------------------------------------------------


async def test_approve_and_allow_similar_creates_a_pinned_grant(
    librarian: Librarian, router: Router, notifier: RecordingNotifier
) -> None:
    task = asyncio.create_task(librarian.write(comment("PAY-1"), KIM))
    await approve_when_open(librarian, remember=True, pin=["key"], grant_days=7, grant_uses=2)
    await asyncio.wait_for(task, 5)

    # Same agent, person, operation and pinned key: runs at once, no new approval.
    again = await librarian.write(comment("PAY-1", text="follow-up"), KIM)
    assert again.how == "grant"
    assert "grant_used" in [e for e, _ in notifier.write_events]

    # Different key: outside the grant, so a new approval is needed.
    other = asyncio.create_task(librarian.write(comment("PAY-2"), KIM))
    await asyncio.sleep(0.1)
    assert not other.done()
    other.cancel()

    # Another agent never benefits from KIM's grant.
    assert librarian.approvals.find_grant(comment("PAY-1"), BOT) is None
    # max_uses=2 counts uses under the grant: one left.
    grant = librarian.approvals.grants()[0]
    assert grant.uses == 1 and grant.pinned_args == {"key": "PAY-1"}


async def test_delete_approvals_cannot_be_remembered(librarian: Librarian, router: Router) -> None:
    router.add("DELETE", f"{BASE}/v1/issues/PAY-9", lambda r: httpx.Response(204))
    request = WriteRequest(api="tracker", operation="deleteIssue", args={"key": "PAY-9"})
    task = asyncio.create_task(librarian.write(request, KIM))
    while not librarian.approvals._approvals:  # pyright: ignore[reportPrivateUsage]
        await asyncio.sleep(0.01)
    approval = next(iter(librarian.approvals._approvals.values()))  # pyright: ignore[reportPrivateUsage]
    assert not approval.rememberable
    from librarian.approvals import ApprovalError

    with pytest.raises(ApprovalError):
        await librarian.approvals.approve(approval.id, "kim@x.test", remember=True)
    task.cancel()


def test_grant_limits() -> None:
    now = datetime.now(UTC)
    grant = Grant(
        agent="a",
        api="tracker",
        operation="addComment",
        created_by="x",
        max_uses_per_day=1,
        expires_at=now + timedelta(days=1),
    )
    caller = Caller(agent="a", write_apis=("tracker",))
    assert grant.matches(comment(), caller, now)
    grant.uses_today, grant.day = 1, now.date().isoformat()
    assert not grant.matches(comment(), caller, now)  # daily cap
    grant.day = "1999-01-01"
    assert grant.matches(comment(), caller, now)  # a new day
    assert not grant.matches(comment(), caller, now + timedelta(days=2))  # expired
    grant.revoked = True
    assert not grant.matches(comment(), caller, now)


async def test_configured_grant_lets_autonomous_agent_write_unattended(
    librarian: Librarian, router: Router, notifier: RecordingNotifier
) -> None:
    librarian.approvals._grants["cfg"] = Grant(  # pyright: ignore[reportPrivateUsage]
        id="cfg",
        agent="triage-bot",
        api="tracker",
        operation="addComment",
        pinned_args={"key": "PAY-1"},
        max_uses_per_day=50,
        created_by="config",
        source="config",
        notify_owner=True,
    )
    outcome = await librarian.write(comment("PAY-1"), BOT)
    assert outcome.how == "grant" and outcome.grant_id == "cfg"
    assert notifier.write_events[-1][0] == "grant_used"
    await librarian.approvals.revoke_grant("cfg", "ops@x.test")  # kill switch
    pending = asyncio.create_task(librarian.write(comment("PAY-1"), BOT))
    await asyncio.sleep(0.1)
    assert not pending.done()  # now needs approval again
    pending.cancel()


# -- pages ---------------------------------------------------------------------------------


async def test_approval_page_shows_request_and_approves(librarian: Librarian) -> None:
    task = asyncio.create_task(librarian.write(comment(text="<script>x</script>"), KIM))
    while not librarian.approvals._approvals:  # pyright: ignore[reportPrivateUsage]
        await asyncio.sleep(0.01)
    approval = next(iter(librarian.approvals._approvals.values()))  # pyright: ignore[reportPrivateUsage]
    token = librarian.approvals.token_for(approval)
    settings = ApproverSettings(mode="trusted_header")
    client = TestClient(Starlette(routes=approval_routes(librarian.approvals, settings)))
    url = f"/approvals/{approval.id}?t={token}"

    stranger = client.get(url, headers={"X-Auth-Request-Email": "eve@x.test"})
    assert stranger.status_code == 403  # not the requester, not in the group
    page = client.get(url, headers={"X-Auth-Request-Email": "kim@x.test"})
    assert page.status_code == 200
    assert "addComment" in page.text and "&lt;script&gt;" in page.text  # escaped body
    assert "Approve and allow similar" in page.text
    assert "kim@x.test" in page.text  # the requester is shown
    done = client.post(
        f"/approvals/{approval.id}",
        data={"t": token, "action": "once"},
        headers={"X-Auth-Request-Email": "kim@x.test"},
    )
    assert done.status_code == 200 and "Approved" in done.text
    outcome = await asyncio.wait_for(task, 5)
    assert outcome.how == "approved"
    assert client.get(url, headers={"X-Auth-Request-Email": "kim@x.test"}).status_code == 404


async def test_approval_page_lets_the_identity_provider_group_decide(librarian: Librarian) -> None:
    """The live scenario behind 0.18.2: oauth2-proxy sends X-Forwarded-* headers, the
    approver group is an Entra object id in approvers.group, the agent's owner is a
    distribution list that never signs in, there is no Slack group and no principal."""
    settings = ApproverSettings(
        mode="trusted_header",
        group="1725aa2a-cad6-453e-a043-20f04bf27ef6",
        user_header="X-Forwarded-Email",
        groups_header="X-Forwarded-Groups",
    )
    librarian.settings.approvers = settings
    librarian.approvals.deciding_group = settings.group
    librarian.settings.notifier.slack.approver_group = None
    entry = librarian.registry.get("tracker")
    assert entry is not None
    await librarian.registry.save(entry.model_copy(update={"owners": Owners()}))
    gateway = Caller(agent="litellm-gateway", write_apis=("*",), owner="infrastructure@x.test")
    task = asyncio.create_task(librarian.write(comment(), gateway))
    while not librarian.approvals._approvals:  # pyright: ignore[reportPrivateUsage]
        await asyncio.sleep(0.01)
    approval = next(iter(librarian.approvals._approvals.values()))  # pyright: ignore[reportPrivateUsage]
    assert approval.approver_group is None and approval.principal is None
    token = librarian.approvals.token_for(approval)
    client = TestClient(Starlette(routes=approval_routes(librarian.approvals, settings)))
    url = f"/approvals/{approval.id}?t={token}"
    member = {
        "X-Forwarded-Email": "kultar@x.test",
        "X-Forwarded-Groups": f"other-group-id,{settings.group}",
    }
    outsider = {"X-Forwarded-Email": "kultar@x.test", "X-Forwarded-Groups": "other-group-id"}
    refused = client.get(url, headers=outsider)
    assert refused.status_code == 403 and "None" not in refused.text
    assert (
        "Only the requester (infrastructure@x.test) or members of "
        f"{settings.group} can approve this write." in refused.text
    )
    assert client.get(url, headers=member).status_code == 200
    done = client.post(
        f"/approvals/{approval.id}", data={"t": token, "action": "once"}, headers=member
    )
    assert done.status_code == 200 and "Approved" in done.text
    assert (await asyncio.wait_for(task, 5)).how == "approved"
    assert approval.decided_by == "kultar@x.test"


async def test_credential_page_can_enable_writes(librarian: Librarian, router: Router) -> None:
    entry = librarian.registry.get("tracker")
    assert entry is not None
    await librarian.registry.save(entry.model_copy(update={"write_auth": None}))
    router.add("GET", f"{BASE}/v1/me", lambda r: httpx.Response(200, json={}))
    entry = librarian.registry.get("tracker")
    assert entry is not None
    request = await librarian.broker.open(entry, KIM, "missing")
    assert request.writes_possible
    client = TestClient(Starlette(routes=credential_routes(librarian.broker, ApproverSettings())))
    token = librarian.broker.token_for(request)
    assert "approved writes" in client.get(f"/credentials/{request.id}?t={token}").text
    client.post(
        f"/credentials/{request.id}",
        data={"t": token, "action": "add", "value": "both-key", "allow_writes": "1"},
    )
    updated = librarian.registry.get("tracker")
    assert updated is not None and updated.write_secret_ref == "tracker/readonly"


# -- persistence -----------------------------------------------------------------------------


async def test_pending_approvals_and_grants_survive_restart(tmp_path: Any, router: Router) -> None:
    from tests.test_state import replica

    first = replica(tmp_path, router)
    await first.start()
    await first.secrets.put("tracker/write", "w")
    task = asyncio.create_task(first.write(comment(), KIM))
    await approve_when_open(first, remember=True, pin=["key"])
    await asyncio.wait_for(task, 5)
    pending = asyncio.create_task(first.write(comment("PAY-2"), KIM))
    await asyncio.sleep(0.1)
    pending.cancel()
    await first.aclose()

    second = replica(tmp_path, router)
    await second.start()
    assert second.approvals.find_grant(comment("PAY-1"), KIM) is not None
    assert any(
        a.state is ApprovalState.PENDING
        for a in second.approvals._approvals.values()  # pyright: ignore[reportPrivateUsage]
    )
    await second.aclose()


async def test_detached_write_runs_exactly_once_across_replicas(
    tmp_path: Any, router: Router, monkeypatch: pytest.MonkeyPatch
) -> None:
    from librarian import approvals as approvals_module
    from tests.test_state import replica

    monkeypatch.setattr(approvals_module, "POLL_SECONDS", 0.05)
    a = replica(tmp_path, router)
    await a.start()
    await a.secrets.put("tracker/write", "w")
    pending = await a.request_write(comment("PAY-1"), KIM)  # an MCP-style caller
    assert isinstance(pending, WriteApproval)

    b = replica(tmp_path, router)
    await b.start()  # picks the detached write up too
    await a.approvals.approve(pending.id, "kim@x.test")

    for _ in range(200):
        final = await b.approvals.load(pending.id)
        if final is not None and final.state is ApprovalState.EXECUTED:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.3)  # give the other replica every chance to (wrongly) run it too
    writes = [r for r in router.requests if r.method == "POST"]
    assert len(writes) == 1
    await a.aclose()
    await b.aclose()


async def test_only_one_replica_can_claim_an_approved_write(tmp_path: Any, router: Router) -> None:
    from tests.test_state import replica

    a, b = replica(tmp_path, router), replica(tmp_path, router)
    await a.start()
    await b.start()
    # An approval nobody is already watching, so the two claims below really race.
    pending = await a._start_write(comment("PAY-1"), KIM)  # pyright: ignore[reportPrivateUsage]
    assert isinstance(pending, WriteApproval)
    await a.approvals.approve(pending.id, "kim@x.test")
    copy_a = await a.approvals.load(pending.id)
    copy_b = await b.approvals.load(pending.id)
    assert copy_a is not None and copy_b is not None
    assert copy_a.state is ApprovalState.APPROVED and copy_b.state is ApprovalState.APPROVED
    claims = await asyncio.gather(a.approvals.claim(copy_a), b.approvals.claim(copy_b))
    assert sorted(claims) == [False, True]
    await a.aclose()
    await b.aclose()
