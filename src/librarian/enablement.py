"""Enable-write requests: a refused write becomes a question for the approvers.

A write operation on a verified entry is refused until a reviewer enables it and the
entry has a write credential. Before, that refusal was silent: the agent saw a bare
denial and nobody learned that it wanted the write. Now the refusal opens one request
per API and operation (deduplicated), tells the approvers (a Slack card and a console
to-do item with Enable or Decline), and the agent gets a clear status. Enable carries
the change into the entry's review proposal and, when the entry has no write
credential, asks for one through the usual secure page. Decline keeps the same
operation from re-asking for a while. Nobody edits YAML.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field, model_validator

from librarian.audit import AuditEvent, AuditSink
from librarian.registry.models import ApiEntry, Operation

MAX_REASONS = 5


class EnableState(StrEnum):
    PENDING = "pending"
    ENABLED = "enabled"
    DECLINED = "declined"
    EXPIRED = "expired"


class EnableWriteRequest(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    api: str
    api_name: str
    host: str
    operation: str  # the first operation asked for
    method: str
    path: str
    summary: str = ""
    # Every operation on this request (the first one and the rest of its flow); one
    # approver action enables them all, into one proposal.
    operations: list[str] = Field(default_factory=list[str])
    # The operations the approver actually enabled (a subset of `operations`).
    enabled: list[str] = Field(default_factory=list[str])
    needs_enabling: bool  # the operation is not marked writable
    needs_credential: bool  # the entry has no write credential
    requested_by: list[str] = Field(default_factory=list[str])  # "agent (for principal)"
    reasons: list[str] = Field(default_factory=list[str])  # what the agents said they wanted
    asks: int = 1
    state: EnableState = EnableState.PENDING
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # Pending: when the request lapses. Declined: until when the same operation stays
    # refused without asking again.
    expires_at: datetime
    decided_by: str | None = None
    note: str | None = None
    review_url: str | None = None  # the proposal carrying the change, once enabled
    key_request: str | None = None  # the credential request for the write key, if asked
    channel: str | None = None
    approver_group: str | None = None
    notifier_ref: dict[str, str] = Field(default_factory=dict[str, str])

    @model_validator(mode="after")
    def _operations_include_the_first(self) -> EnableWriteRequest:
        if self.operation not in self.operations:
            self.operations = [self.operation, *self.operations]
        return self

    @property
    def what(self) -> str:
        return f"{self.method} {self.path}"

    @property
    def others(self) -> list[str]:
        return [o for o in self.operations if o != self.operation]


class EnableNotifier(Protocol):
    async def enable_opened(self, request: EnableWriteRequest, link: str) -> None: ...
    async def enable_updated(self, request: EnableWriteRequest) -> None: ...
    async def enable_closed(self, request: EnableWriteRequest) -> None: ...


class EnableStore(Protocol):
    async def save_doc(self, kind: str, doc_id: str, state: str, data: str) -> None: ...
    async def get_doc(self, kind: str, doc_id: str) -> str | None: ...
    async def list_docs(self, kind: str, state: str | None = None) -> list[str]: ...


KIND = "enable_write"


class EnableWriteBroker:
    def __init__(
        self,
        notifier: Any,
        audit: AuditSink,
        *,
        public_url: str,
        signing_key: bytes,
        expire_after: timedelta = timedelta(days=7),
        decline_for: timedelta = timedelta(days=7),
        default_channel: str | None = None,
        default_approver_group: str | None = None,
        store: EnableStore | None = None,
    ) -> None:
        self.notifier: EnableNotifier = notifier
        self.audit = audit
        self.public_url = public_url.rstrip("/")
        self._signing_key = signing_key
        self.expire_after = expire_after
        self.decline_for = decline_for
        self.default_channel = default_channel
        self.default_approver_group = default_approver_group
        self.store = store
        self._requests: dict[str, EnableWriteRequest] = {}

    def set_signing_key(self, key: bytes) -> None:
        self._signing_key = key

    async def restore(self) -> None:
        """Reload open requests and live declines after a restart."""
        await self.refresh()

    async def refresh(self) -> None:
        """Pick up requests opened or decided on other replicas."""
        if self.store is None:
            return
        for state in (EnableState.PENDING, EnableState.DECLINED):
            for data in await self.store.list_docs(KIND, state.value):
                request = EnableWriteRequest.model_validate_json(data)
                self._requests[request.id] = request

    async def _save(self, request: EnableWriteRequest) -> None:
        if self.store is not None:
            await self.store.save_doc(
                KIND, request.id, request.state.value, request.model_dump_json()
            )

    async def load(self, request_id: str) -> EnableWriteRequest | None:
        if self.store is not None:
            data = await self.store.get_doc(KIND, request_id)
            if data is not None:
                request = EnableWriteRequest.model_validate_json(data)
                self._requests[request.id] = request
                return request
        return self._requests.get(request_id)

    def get(self, request_id: str) -> EnableWriteRequest | None:
        return self._requests.get(request_id)

    def pending(self) -> list[EnableWriteRequest]:
        return sorted(
            (r for r in self._requests.values() if r.state is EnableState.PENDING),
            key=lambda r: r.created_at,
        )

    def pending_for(self, api: str, operation: str | None = None) -> EnableWriteRequest | None:
        return next(
            (
                r
                for r in self.pending()
                if r.api == api and (operation is None or operation in r.operations)
            ),
            None,
        )

    async def enabled_for(self, api: str) -> list[EnableWriteRequest]:
        """Requests enabled for this API (here and on other replicas), oldest first."""
        found = {
            r.id: r
            for r in self._requests.values()
            if r.state is EnableState.ENABLED and r.api == api
        }
        if self.store is not None:
            for data in await self.store.list_docs(KIND, EnableState.ENABLED.value):
                request = EnableWriteRequest.model_validate_json(data)
                if request.api == api:
                    found[request.id] = request
        return sorted(found.values(), key=lambda r: r.created_at)

    def declined_for(self, api: str, operation: str) -> EnableWriteRequest | None:
        """A recent decline of this operation: the agent is told no without asking again."""
        now = datetime.now(UTC)
        return next(
            (
                r
                for r in self._requests.values()
                if r.state is EnableState.DECLINED
                and r.api == api
                and operation in r.operations
                and r.expires_at > now
            ),
            None,
        )

    # -- links ---------------------------------------------------------------------

    def token_for(self, request: EnableWriteRequest) -> str:
        message = f"enable:{request.id}:{request.created_at.isoformat()}".encode()
        return hmac.new(self._signing_key, message, hashlib.sha256).hexdigest()[:40]

    def check_token(self, request: EnableWriteRequest, token: str) -> bool:
        if request.state is not EnableState.PENDING or datetime.now(UTC) > request.expires_at:
            return False
        return hmac.compare_digest(self.token_for(request), token)

    def link(self, request: EnableWriteRequest) -> str:
        return f"{self.public_url}/enable/{request.id}?t={self.token_for(request)}"

    # -- lifecycle -----------------------------------------------------------------

    async def open(
        self,
        entry: ApiEntry,
        ops: list[Operation],
        *,
        who: str,
        reason: str,
        notify: bool = True,
        join: bool = True,
    ) -> EnableWriteRequest:
        """Open (or join) the request to enable these operations of one API. `who` is the
        agent (and the person it acts for), or the approver who started it from the
        console. With `join`, an open request for the same API takes the operations in;
        one approver action then unblocks the whole flow."""
        await self.refresh()
        op = ops[0]
        existing = self.pending_for(entry.id, op.id)
        if existing is None and join:
            existing = self.pending_for(entry.id)
        reason = reason.strip()[:500]
        if existing is not None:
            existing.asks += 1
            if who not in existing.requested_by:
                existing.requested_by.append(who)
            if reason and reason not in existing.reasons and len(existing.reasons) < MAX_REASONS:
                existing.reasons.append(reason)
            for extra in ops:
                if extra.id not in existing.operations and len(existing.operations) < 50:
                    existing.operations.append(extra.id)
            await self._save(existing)
            if notify:
                await self.notifier.enable_updated(existing)
            return existing
        request = EnableWriteRequest(
            api=entry.id,
            api_name=entry.name,
            host=entry.host,
            operation=op.id,
            method=op.method,
            path=op.path,
            summary=op.summary[:200],
            operations=[o.id for o in ops],
            needs_enabling=not op.writable,
            needs_credential=entry.write_auth is None,
            requested_by=[who],
            reasons=[reason] if reason else [],
            expires_at=datetime.now(UTC) + self.expire_after,
            channel=entry.owners.channel or self.default_channel,
            approver_group=entry.owners.approver_group or self.default_approver_group,
        )
        self._requests[request.id] = request
        await self._save(request)
        if notify:
            await self.notifier.enable_opened(request, self.link(request))
            await self._save(request)  # the notifier may have recorded where the card is
        return request

    async def decide(
        self,
        request_id: str,
        state: EnableState,
        by: str | None,
        note: str,
        *,
        review_url: str | None = None,
        key_request: str | None = None,
        enabled: list[str] | None = None,
    ) -> EnableWriteRequest:
        request = await self.load(request_id)
        if request is None or request.state is not EnableState.PENDING:
            raise LookupError(f"enable-write request {request_id!r} is not pending")
        request.state = state
        request.decided_by = by
        request.note = note
        request.review_url = review_url
        request.key_request = key_request
        request.enabled = list(enabled or [])
        if state is EnableState.DECLINED:
            request.expires_at = datetime.now(UTC) + self.decline_for
        await self._save(request)
        await self.audit.record(
            AuditEvent(
                event=f"enable_write_{state.value}",
                caller="console",
                principal=by,
                api=request.api,
                operation=request.operation,
                detail=note,
                extra={"review_url": review_url, "key_request": key_request},
            )
        )
        await self.notifier.enable_closed(request)
        return request

    async def sweep(self) -> None:
        """Expire stale requests and forget lapsed declines; call periodically."""
        await self.refresh()
        now = datetime.now(UTC)
        for request in list(self._requests.values()):
            if request.state is EnableState.PENDING and now >= request.expires_at:
                await self.decide(request.id, EnableState.EXPIRED, None, "no one decided in time")
            elif request.state is EnableState.DECLINED and now >= request.expires_at:
                del self._requests[request.id]
