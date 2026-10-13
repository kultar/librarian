"""Durable state: restarts, several replicas, and internal-secret protection."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from librarian import credentials
from librarian.audit import MemoryAuditSink
from librarian.calls import Caller, CallRequest
from librarian.config import CallerKey, Settings
from librarian.credentials import RequestState
from librarian.registry import ApiEntry
from librarian.secrets import FileSecretStore
from librarian.service import Librarian
from tests.conftest import BASE, TRACKER, RecordingNotifier, Router, write_entries

A = Caller(agent="agent-a")


def replica(tmp_path: Path, router: Router) -> Librarian:
    """A librarian sharing registry, secrets and database with its siblings."""
    write_entries(tmp_path / "registry", TRACKER)
    settings = Settings(
        registry_dir=tmp_path / "registry",
        secrets_dir=tmp_path / "secrets",
        database_url=f"sqlite+aiosqlite:///{tmp_path}/state.db",
        public_url="https://librarian.example.test",
        allow_private_networks=True,
    )
    return Librarian(
        settings,
        secrets_store=FileSecretStore(tmp_path / "secrets"),
        audit=MemoryAuditSink(),
        notifier=RecordingNotifier(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(router)),
    )


async def test_pending_requests_and_links_survive_restart(tmp_path: Path, router: Router) -> None:
    first = replica(tmp_path, router)
    await first.start()
    entry = first.registry.get("tracker")
    assert entry is not None
    request = await first.broker.open(entry, A, "missing")
    link = first.broker.link(request)
    await first.aclose()

    second = replica(tmp_path, router)
    await second.start()
    restored = await second.broker.load(request.id)
    assert restored is not None and restored.state is RequestState.PENDING
    # Same signing key (persisted in the secret store) → the old Slack link still works.
    assert second.broker.link(restored) == link
    assert second.broker.check_token(restored, link.rsplit("t=", 1)[1])
    await second.aclose()


async def test_approval_on_another_replica_resumes_waiter(
    tmp_path: Path, router: Router, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(credentials, "POLL_SECONDS", 0.05)
    router.add("GET", f"{BASE}/v1/me", lambda r: httpx.Response(200, json={}))
    a, b = replica(tmp_path, router), replica(tmp_path, router)
    await a.start()
    await b.start()
    entry = a.registry.get("tracker")
    assert entry is not None
    request = await a.broker.open(entry, A, "missing")
    waiter = asyncio.create_task(a.broker.wait(request.id, max_wait=5))
    await asyncio.sleep(0.1)

    # The approver's browser lands on replica B.
    assert await b.broker.provide_value(request.id, "tok", "sam") is None
    resolved = await asyncio.wait_for(waiter, 5)
    assert resolved.state is RequestState.FULFILLED
    await a.aclose()
    await b.aclose()


async def test_open_dedupes_across_replicas(tmp_path: Path, router: Router) -> None:
    a, b = replica(tmp_path, router), replica(tmp_path, router)
    await a.start()
    await b.start()
    entry = a.registry.get("tracker")
    assert entry is not None
    first = await a.broker.open(entry, A, "missing")
    second = await b.broker.open(entry, Caller(agent="agent-b"), "missing")
    assert first.id == second.id
    await a.aclose()
    await b.aclose()


def test_entries_cannot_reference_internal_secrets() -> None:
    for ref in ("librarian/signing-key", "librarian/callers/agent-a"):
        with pytest.raises(ValidationError, match="reserved"):
            ApiEntry.model_validate({**TRACKER, "auth": {"type": "bearer", "secret_ref": ref}})


def test_internal_secret_settings_must_use_reserved_prefix() -> None:
    with pytest.raises(ValidationError):
        CallerKey(agent="a", key_ref="callers/a")
    with pytest.raises(ValidationError):
        Settings(signing_key_ref="signing-key")
    assert CallerKey(agent="a", key_ref="librarian/callers/a").key_ref


async def test_approvers_cannot_pick_internal_secrets(librarian: Librarian) -> None:
    await librarian.secrets.put("librarian/callers/agent-a", "caller-key")
    await librarian.secrets.put("shared/bot", "bot")
    assert await librarian.broker.offerable_secrets() == ["shared/bot"]
    entry = librarian.registry.get("tracker")
    assert entry is not None
    request = await librarian.broker.open(entry, A, "missing")
    error = await librarian.broker.use_existing(request.id, "librarian/callers/agent-a", "sam")
    assert error is not None
    assert librarian.registry.get("tracker").secret_ref == "tracker/readonly"  # type: ignore[union-attr]


async def test_replicas_starting_together_share_one_signing_key(
    tmp_path: Path, router: Router
) -> None:
    a, b = replica(tmp_path, router), replica(tmp_path, router)
    await asyncio.gather(a.start(), b.start())
    entry = a.registry.get("tracker")
    assert entry is not None
    request = await a.broker.open(entry, A, "missing")
    loaded = await b.broker.load(request.id)
    assert loaded is not None
    assert b.broker.link(loaded) == a.broker.link(request)
    await a.aclose()
    await b.aclose()


class PostingNotifier(RecordingNotifier):
    """Like Slack: remembers where each card was posted, and needs it to update the card."""

    def __init__(self) -> None:
        super().__init__()
        self.closed_refs: list[dict[str, str]] = []

    async def opened(self, request: Any, link: str) -> None:
        await super().opened(request, link)
        request.notifier_ref = {"channel": "C1", "ts": f"cred-{request.id}"}

    async def closed(self, request: Any) -> None:
        await super().closed(request)
        self.closed_refs.append(dict(request.notifier_ref))

    async def write_opened(self, approval: Any, link: str) -> None:
        await super().write_opened(approval, link)
        approval.notifier_ref = {"channel": "C1", "ts": f"write-{approval.id}"}

    async def write_closed(self, approval: Any) -> None:
        await super().write_closed(approval)
        self.closed_refs.append(dict(approval.notifier_ref))


async def test_another_replica_can_update_the_posted_message(
    tmp_path: Path, router: Router
) -> None:
    from librarian.calls import WriteRequest

    first, second = replica(tmp_path, router), replica(tmp_path, router)
    posting = PostingNotifier()
    first.broker.notifier = first.approvals.notifier = posting
    second.broker.notifier = second.approvals.notifier = posting
    await first.start()
    await second.start()
    entry = first.registry.get("tracker")
    assert entry is not None

    # Credential request: opened (and posted) on one replica, resolved on the other.
    router.add("GET", f"{BASE}/v1/me", lambda r: httpx.Response(200, json={}))
    request = await first.broker.open(entry, A, "missing")
    assert await second.broker.provide_value(request.id, "read-key", "kim@x.test") is None
    assert posting.closed_refs[-1] == {"channel": "C1", "ts": f"cred-{request.id}"}

    # Write approval: the same.
    await first.secrets.put("tracker/write", "w")
    kim = Caller(agent="notes-bot", principal="kim@x.test", write_apis=("tracker",))
    # The A2A path (a caller waiting on the approval), which does not save it again.
    approval = await first._start_write(  # pyright: ignore[reportPrivateUsage]
        WriteRequest(api="tracker", operation="addComment", args={"key": "PAY-1"}, body={}), kim
    )
    from librarian.approvals import WriteApproval

    assert isinstance(approval, WriteApproval)
    await second.approvals.deny(approval.id, "kim@x.test")
    assert posting.closed_refs[-1] == {"channel": "C1", "ts": f"write-{approval.id}"}
    await first.aclose()
    await second.aclose()


async def test_audit_events_are_kept_in_the_database_for_the_console(
    tmp_path: Path, router: Router, secrets_store: FileSecretStore
) -> None:
    from datetime import UTC, datetime, timedelta

    from librarian.audit import AuditEvent, DatabaseAuditSink, FanoutAuditSink

    settings = Settings(
        registry_dir=tmp_path / "registry",
        secrets_dir=tmp_path / "secrets",
        database_url=f"sqlite+aiosqlite:///{tmp_path}/state.db",
        public_url="https://librarian.example.test",
        allow_private_networks=True,
        audit_file=tmp_path / "audit.jsonl",
    )
    write_entries(tmp_path / "registry", TRACKER)
    first = Librarian(
        settings,
        secrets_store=FileSecretStore(tmp_path / "secrets"),
        notifier=RecordingNotifier(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(router)),
    )
    await first.start()
    assert first.state is not None
    # Without a sink given, the JSONL stream stays and the database gets a copy.
    assert isinstance(first.audit, FanoutAuditSink)
    assert any(isinstance(s, DatabaseAuditSink) for s in first.audit.sinks)
    await first.secrets.put("tracker/readonly", "k")
    router.add("GET", f"{BASE}/v1/me", lambda r: httpx.Response(200, json={"login": "a"}))
    assert (await first.executor.execute(CallRequest(api="tracker", operation="whoami"), A)).ok
    with pytest.raises(Exception, match="unknown operation"):
        await first.executor.execute(CallRequest(api="tracker", operation="nope"), A)
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert sum('"event": "call"' in line for line in lines) == 1  # the stream still gets it

    second = replica(tmp_path, router)  # another replica sees the same activity
    await second.start()
    assert second.state is not None
    events = await second.state.recent_audit("tracker")
    assert [ev.event for ev in events] == ["denied", "call"]  # newest first
    assert events[1].caller == "agent-a" and events[1].status_code == 200 and events[1].bytes
    assert events[1].ts.tzinfo is not None
    assert await second.state.recent_audit("weather") == []
    await second.state.record_audit(AuditEvent(event="fetch", caller="agent-a", detail="recipe"))
    await second.state.record_audit(AuditEvent(event="fetch", caller="agent-a", detail="planner"))
    await second.state.record_audit(AuditEvent(event="fetch", caller="agent-a", detail="recipe"))
    summary = await second.state.audit_summary(datetime.now(UTC) - timedelta(days=7))
    assert summary["calls"] == 1 and summary["denied"] == 1 and summary["writes"] == 0
    assert summary["fetch_recipe"] == 2 and summary["fetch_planner"] == 1
    assert summary["bytes"] == events[1].bytes and summary["by_api"] == [("tracker", 1)]
    assert await second.state.audit_summary(datetime.now(UTC) + timedelta(days=1)) == {
        "calls": 0,
        "denied": 0,
        "writes": 0,
        "fetch_planner": 0,
        "fetch_recipe": 0,
        "bytes": 0,
        "by_api": [],
    }
    # Retention: an hourly sweep drops database rows older than the setting.
    second.settings.audit_retention_days = 1
    assert await second.prune_audit() == 0
    await second.state.record_audit(
        AuditEvent(ts=datetime.now(UTC) - timedelta(days=2), event="call", api="tracker")
    )
    assert await second.prune_audit() == 1
    assert len(await second.state.recent_audit()) == 5
    await first.aclose()
    await second.aclose()


async def test_a_failing_database_sink_never_breaks_the_call() -> None:
    from librarian.audit import AuditEvent, DatabaseAuditSink

    class Broken:
        async def record_audit(self, event: AuditEvent) -> None:
            raise RuntimeError("db down")

    await DatabaseAuditSink(Broken()).record(AuditEvent(event="call"))  # logged, not raised


def test_psql_style_sslmode_is_translated_for_asyncpg() -> None:
    from librarian.state import normalize_database_url

    pg = "postgresql+asyncpg://u:p@db.example.test:5432/librarian"
    assert normalize_database_url(f"{pg}?sslmode=require") == f"{pg}?ssl=require"
    assert normalize_database_url(f"{pg}?a=1&sslmode=verify-full") == f"{pg}?a=1&ssl=verify-full"
    assert normalize_database_url(f"{pg}?ssl=require") == f"{pg}?ssl=require"
    other = "postgresql+psycopg://u:p@h/db?sslmode=require"  # not asyncpg: left alone
    assert normalize_database_url(other) == other
