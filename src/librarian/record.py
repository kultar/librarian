"""Writing decisions to the external record, off the request path.

A decision is in effect the moment it is saved to the database. Writing it to the
record (see `publish.py`) happens afterwards, in the background, so a slow or
unreachable GitHub never delays or undoes an approval: a write that fails is kept,
shown in the console and tried again.

What is written is always the entry as the database has it now, so writes can be
repeated, merged or arrive late without the record ever saying something the database
does not.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

import httpx
from pydantic import BaseModel, Field

from librarian.audit import AuditEvent, AuditSink
from librarian.publish import Publisher, PublishError
from librarian.registry.store import Registry, recordable

log = logging.getLogger(__name__)

KIND = "record"
STALE_SECONDS = 120  # a pending write this old belongs to a replica that went away


class RecordStatus(BaseModel):
    api: str
    state: Literal["pending", "recorded", "failed"] = "pending"
    messages: list[str] = Field(default_factory=list[str])  # decisions not written yet
    url: str | None = None  # the last write
    error: str | None = None
    attempts: int = 0
    removed: bool = False  # the last write deleted the file (the API was forgotten)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def retry_at(self) -> datetime:
        return self.updated_at + timedelta(seconds=min(3600, 60 * 2 ** min(self.attempts, 6)))


class RecordStore(Protocol):
    async def save_doc(self, kind: str, doc_id: str, state: str, data: str) -> None: ...
    async def get_doc(self, kind: str, doc_id: str) -> str | None: ...
    async def list_docs(self, kind: str, state: str | None = None) -> list[str]: ...
    async def transition_doc(
        self, kind: str, doc_id: str, from_state: str, to_state: str, data: str
    ) -> bool: ...


def commit_message(api: str, messages: list[str]) -> str:
    if len(messages) == 1:
        return messages[0]
    if not messages:
        return f"{api}: bring the record up to date with librarian"
    return f"{api}: {len(messages)} decisions\n\n" + "\n\n".join(messages)


class Recorder:
    def __init__(
        self,
        registry: Registry,
        audit: AuditSink,
        *,
        publisher: Publisher | None = None,
        store: RecordStore | None = None,
    ) -> None:
        self.registry = registry
        self.audit = audit
        self.publisher = publisher
        self.store = store
        # False: a decision waits for its write to the record (one-shot commands, tests).
        self.background = True
        self._statuses: dict[str, RecordStatus] = {}
        self._queued: dict[str, list[str]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def enabled(self) -> bool:
        return self.publisher is not None

    async def status(self, api: str) -> RecordStatus | None:
        if self.store is not None:
            data = await self.store.get_doc(KIND, api)
            if data is not None:
                self._statuses[api] = RecordStatus.model_validate_json(data)
        return self._statuses.get(api)

    async def unwritten(self) -> list[RecordStatus]:
        """Decisions that are not in the record (yet): failed writes, and pending ones."""
        if self.store is not None:
            found = [
                RecordStatus.model_validate_json(data)
                for state in ("failed", "pending")
                for data in await self.store.list_docs(KIND, state)
            ]
        else:
            found = [s for s in self._statuses.values() if s.state != "recorded"]
        return sorted(found, key=lambda s: s.api)

    async def _save(self, status: RecordStatus) -> None:
        status.updated_at = datetime.now(UTC)
        self._statuses[status.api] = status
        if self.store is not None:
            await self.store.save_doc(KIND, status.api, status.state, status.model_dump_json())

    async def note(self, api: str, message: str) -> bool:
        """A decision about `api` is in effect: write it down, in the background. False
        when no record is configured."""
        if self.publisher is None:
            return False
        self._queued.setdefault(api, []).append(message)
        status = await self.status(api) or RecordStatus(api=api)
        status.state = "pending"
        status.messages = [*status.messages, message][-50:]
        try:
            await self._save(status)
        except Exception:  # the decision stands; the write below still happens
            log.exception("could not note the pending record of %s", api)
        if self.background:
            self._start(api)
        else:
            await self._flush(api)
        return True

    def _start(self, api: str) -> None:
        task = asyncio.create_task(self._flush(api))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for the writes in flight (tests, and a clean shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def write_now(self, api: str) -> RecordStatus | None:
        """Write the API's entry to the record now and wait for it (the console's
        "record now", and retries)."""
        if self.publisher is None:
            return None
        await self._flush(api, force=True)
        return await self.status(api)

    async def _flush(self, api: str, force: bool = False) -> None:
        publisher = self.publisher
        if publisher is None:
            return
        async with self._locks.setdefault(api, asyncio.Lock()):
            queued = self._queued.pop(api, [])
            try:
                status = await self.status(api) or RecordStatus(api=api)
            except Exception:
                log.exception("could not read the record status of %s", api)
                status = RecordStatus(api=api)
            messages = list(status.messages)
            messages += [m for m in queued if m not in messages]
            if not messages and status.state == "recorded" and not force:
                return
            try:
                await self.registry.refresh_stored()
                entry = self.registry.get(api)
                if entry is None:
                    url = await publisher.remove(api, commit_message(api, messages))
                elif not self.registry.approved(api):
                    url = status.url  # nothing approved to write down
                else:
                    url = await publisher.record(recordable(entry), commit_message(api, messages))
            except (PublishError, httpx.HTTPError, KeyError, ValueError) as exc:
                status.state, status.messages = "failed", messages[-50:]
                status.error, status.attempts = str(exc)[:1000], status.attempts + 1
                log.warning("could not write %s to the record: %s", api, exc)
                await self._settle(status, "record_failed", status.error)
                return
            status.state, status.messages, status.error, status.attempts = "recorded", [], None, 0
            status.url, status.removed = (url or status.url), entry is None
            await self._settle(status, "recorded", url or "nothing to write")

    async def _settle(self, status: RecordStatus, event: str, detail: str) -> None:
        try:
            await self._save(status)
            await self.audit.record(AuditEvent(event=event, api=status.api, detail=detail))
        except Exception:
            log.exception("could not save the record status of %s", status.api)

    def _busy(self, api: str) -> bool:
        lock = self._locks.get(api)
        return lock is not None and lock.locked()

    async def retry_due(self) -> int:
        """Try failed writes again (with a growing pause), and pick up pending ones
        left by a replica that went away. Returns how many were tried."""
        if self.publisher is None:
            return 0
        now = datetime.now(UTC)
        tried = 0
        for status in await self.unwritten():
            age = (now - status.updated_at).total_seconds()
            if status.state == "failed":
                if now < status.retry_at:
                    continue
                if self.store is not None:
                    status.state = "pending"
                    status.updated_at = now
                    # One replica takes each retry.
                    if not await self.store.transition_doc(
                        KIND, status.api, "failed", "pending", status.model_dump_json()
                    ):
                        continue
            elif age < STALE_SECONDS or self._busy(status.api):
                continue
            await self._flush(status.api, force=True)
            tried += 1
        return tried
