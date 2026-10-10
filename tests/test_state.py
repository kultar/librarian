"""Durable state: restarts, several replicas, and internal-secret protection."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from librarian import credentials
from librarian.audit import MemoryAuditSink
from librarian.calls import Caller
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
