"""Write approvals and grants.

Every write is either approved by a human for that exact request, or matches a
*grant*: a bounded, expiring, revocable permission (same agent, same API and
operation, pinned argument values). Grants come from "approve and allow similar"
on the approval page, or from operator config (for autonomous agents).

Agents never approve anything: approvals come only from the approval page.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from librarian import telemetry
from librarian.audit import AuditEvent, AuditSink
from librarian.calls import ArgValue, Caller, WriteRequest
from librarian.registry.models import ApiEntry, Operation

log = logging.getLogger(__name__)

POLL_SECONDS = 2.0

ApproverMode = Literal["requester", "group", "requester_or_group"]


class ApprovalState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    EXECUTED = "executed"
    FAILED = "failed"


class WriteApproval(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    fingerprint: str
    api: str
    api_name: str
    host: str
    operation: str
    method: str
    path: str  # the operation's path template
    summary: str = ""
    request: WriteRequest
    agent: str
    principal: str | None = None
    requesters: list[str] = Field(default_factory=list[str])  # principal and/or owner
    rememberable: bool = True
    state: ApprovalState = ApprovalState.PENDING
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime
    decided_by: str | None = None
    decision: Literal["once", "grant"] | None = None
    grant_id: str | None = None
    note: str | None = None
    result: dict[str, Any] | None = None  # CallResult as JSON once executed
    channel: str | None = None
    approver_group: str | None = None
    notifier_ref: dict[str, str] = Field(default_factory=dict[str, str])


class Grant(BaseModel):
    """Standing permission for one agent to make one kind of write."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    agent: str
    principal: str | None = None  # None = any principal (or none)
    api: str
    operation: str
    # Argument values that must match exactly; unpinned arguments are free.
    pinned_args: dict[str, str] = Field(default_factory=dict[str, str])
    expires_at: datetime | None = None
    max_uses: int | None = None
    max_uses_per_day: int | None = None
    uses: int = 0
    uses_today: int = 0
    day: str = ""
    created_by: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source: Literal["approval", "config"] = "approval"
    revoked: bool = False
    notify_owner: bool = False

    def usable(self, now: datetime) -> bool:
        if self.revoked:
            return False
        if self.expires_at is not None and now >= self.expires_at:
            return False
        if self.max_uses is not None and self.uses >= self.max_uses:
            return False
        today = now.date().isoformat()
        used_today = self.uses_today if self.day == today else 0
        return not (self.max_uses_per_day is not None and used_today >= self.max_uses_per_day)

    def matches(self, request: WriteRequest, caller: Caller, now: datetime) -> bool:
        if not self.usable(now):
            return False
        if (self.agent, self.api, self.operation) != (caller.agent, request.api, request.operation):
            return False
        if (
            self.principal is not None
            and self.principal.lower() != (caller.principal or "").lower()
        ):
            return False
        return all(
            _arg_text(request.args.get(name)) == value for name, value in self.pinned_args.items()
        )


def _arg_text(value: ArgValue | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        return ",".join(str(v) for v in value)
    return str(value)


class ApprovalStore(Protocol):
    async def save_doc(self, kind: str, doc_id: str, state: str, data: str) -> None: ...
    async def get_doc(self, kind: str, doc_id: str) -> str | None: ...
    async def list_docs(self, kind: str, state: str | None = None) -> list[str]: ...


class WriteNotifier(Protocol):
    async def write_opened(self, approval: WriteApproval, link: str) -> None: ...
    async def write_closed(self, approval: WriteApproval) -> None: ...
    async def grant_used(self, grant: Grant, approval_like: WriteApproval) -> None: ...


class ApprovalError(Exception):
    pass


class ApprovalBroker:
    def __init__(
        self,
        notifier: WriteNotifier,
        audit: AuditSink,
        *,
        public_url: str,
        signing_key: bytes,
        approver_mode: ApproverMode = "requester_or_group",
        expire_after: timedelta = timedelta(hours=24),
        default_channel: str | None = None,
        default_approver_group: str | None = None,
        store: ApprovalStore | None = None,
        config_grants: list[Grant] | None = None,
    ) -> None:
        self.notifier = notifier
        self.audit = audit
        self.public_url = public_url.rstrip("/")
        self._signing_key = signing_key
        self.approver_mode: ApproverMode = approver_mode
        self.expire_after = expire_after
        self.default_channel = default_channel
        self.default_approver_group = default_approver_group
        self.store = store
        self._approvals: dict[str, WriteApproval] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._grants: dict[str, Grant] = {g.id: g for g in (config_grants or [])}
        self._lock = asyncio.Lock()

    def set_signing_key(self, key: bytes) -> None:
        self._signing_key = key

    # -- persistence -------------------------------------------------------------

    async def restore(self) -> None:
        if self.store is None:
            return
        for data in await self.store.list_docs("approval", ApprovalState.PENDING.value):
            approval = WriteApproval.model_validate_json(data)
            self._approvals[approval.id] = approval
            self._events.setdefault(approval.id, asyncio.Event())
        for data in await self.store.list_docs("grant"):
            grant = Grant.model_validate_json(data)
            self._grants.setdefault(grant.id, grant)

    async def _save(self, approval: WriteApproval) -> None:
        if self.store is not None:
            await self.store.save_doc(
                "approval", approval.id, approval.state.value, approval.model_dump_json()
            )

    async def _save_grant(self, grant: Grant) -> None:
        if self.store is not None and grant.source == "approval":
            state = "revoked" if grant.revoked else "active"
            await self.store.save_doc("grant", grant.id, state, grant.model_dump_json())

    async def load(self, approval_id: str) -> WriteApproval | None:
        if self.store is not None:
            data = await self.store.get_doc("approval", approval_id)
            if data is not None:
                stored = WriteApproval.model_validate_json(data)
                current = self._approvals.get(approval_id)
                if current is None:
                    self._approvals[approval_id] = stored
                    self._events.setdefault(approval_id, asyncio.Event())
                    return stored
                for name in WriteApproval.model_fields:
                    setattr(current, name, getattr(stored, name))
                return current
        return self._approvals.get(approval_id)

    # -- grants ----------------------------------------------------------------------

    def grants(self, include_inactive: bool = False) -> list[Grant]:
        now = datetime.now(UTC)
        return sorted(
            (g for g in self._grants.values() if include_inactive or g.usable(now)),
            key=lambda g: g.created_at,
        )

    def find_grant(self, request: WriteRequest, caller: Caller) -> Grant | None:
        now = datetime.now(UTC)
        return next((g for g in self.grants() if g.matches(request, caller, now)), None)

    async def use_grant(self, grant: Grant) -> None:
        today = date.today().isoformat()
        if grant.day != today:
            grant.day, grant.uses_today = today, 0
        grant.uses += 1
        grant.uses_today += 1
        await self._save_grant(grant)

    async def revoke_grant(self, grant_id: str, by: str) -> Grant:
        grant = self._grants.get(grant_id)
        if grant is None and self.store is not None:
            data = await self.store.get_doc("grant", grant_id)
            grant = Grant.model_validate_json(data) if data else None
        if grant is None:
            raise ApprovalError(f"unknown grant {grant_id!r}")
        grant.revoked = True
        self._grants[grant.id] = grant
        await self._save_grant(grant)
        await self.audit.record(
            AuditEvent(
                event="grant_revoked",
                api=grant.api,
                operation=grant.operation,
                caller=by,
                detail=f"grant {grant.id} for {grant.agent}",
            )
        )
        return grant

    # -- approvals -----------------------------------------------------------------

    def find_executed(self, caller: Caller, idempotency_key: str) -> WriteApproval | None:
        return next(
            (
                a
                for a in self._approvals.values()
                if a.agent == caller.agent
                and a.request.idempotency_key == idempotency_key
                and a.state is ApprovalState.EXECUTED
            ),
            None,
        )

    async def open(
        self, entry: ApiEntry, op: Operation, request: WriteRequest, caller: Caller
    ) -> WriteApproval:
        fingerprint = request.fingerprint(caller)
        async with self._lock:
            existing = next(
                (
                    a
                    for a in self._approvals.values()
                    if a.fingerprint == fingerprint and a.state is ApprovalState.PENDING
                ),
                None,
            )
            if existing is not None:
                return existing
            approval = WriteApproval(
                fingerprint=fingerprint,
                api=entry.id,
                api_name=entry.name,
                host=entry.host,
                operation=op.id,
                method=op.method,
                path=op.path,
                summary=op.summary,
                request=request,
                agent=caller.agent,
                principal=caller.principal,
                requesters=sorted(caller.requesters()),
                rememberable=bool(op.rememberable),
                expires_at=datetime.now(UTC) + self.expire_after,
                channel=entry.owners.channel or self.default_channel,
                approver_group=entry.owners.approver_group or self.default_approver_group,
            )
            self._approvals[approval.id] = approval
            self._events[approval.id] = asyncio.Event()
            await self._save(approval)
        telemetry.write_requests.add(
            1, {"librarian.api": entry.id, "outcome": "approval_requested"}
        )
        await self.audit.record(
            AuditEvent(
                event="write_requested",
                caller=caller.agent,
                principal=caller.principal,
                api=entry.id,
                operation=op.id,
                detail=request.reason or None,
                extra={"approval": approval.id, "args": request.args},
            )
        )
        await self.notifier.write_opened(approval, self.link(approval))
        return approval

    async def wait(self, approval_id: str, max_wait: float | None = None) -> WriteApproval:
        approval = self._approvals[approval_id]
        event = self._events[approval_id]
        loop = asyncio.get_running_loop()
        remaining = (approval.expires_at - datetime.now(UTC)).total_seconds()
        limit = remaining if max_wait is None else min(max_wait, remaining)
        deadline = loop.time() + max(limit, 0)
        while approval.state is ApprovalState.PENDING:
            left = deadline - loop.time()
            if left <= 0:
                if datetime.now(UTC) >= approval.expires_at:
                    await self._decide(
                        approval, ApprovalState.EXPIRED, None, "no one approved in time"
                    )
                break
            try:
                await asyncio.wait_for(event.wait(), timeout=min(left, POLL_SECONDS))
            except TimeoutError:
                await self.load(approval_id)
        return approval

    def approval_route(self, requesters: set[str], approver_group: str | None) -> str | None:
        """Why no human could approve such a write, or None if someone can.

        Checked before opening an approval so a write nobody can approve is refused
        at once instead of waiting until it expires (e.g. an autonomous agent with no
        owner when only requesters may approve).
        """
        mode = self.approver_mode
        has_requester = bool(requesters)
        has_group = approver_group is not None
        if mode == "requester" and not has_requester:
            return "no one can approve: this agent has no owner and acts for no person"
        if mode == "group" and not has_group:
            return "no one can approve: no approver group is configured for this API"
        if mode == "requester_or_group" and not (has_requester or has_group):
            return "no one can approve: set an owner for this agent or configure an approver group"
        return None

    def may_approve(
        self, approval: WriteApproval, identity: str | None, groups: set[str]
    ) -> str | None:
        """Return None if `identity` may approve, else the reason it may not."""
        if identity is None:  # token mode: link possession (local use only)
            return None
        is_requester = identity.lower() in approval.requesters
        in_group = approval.approver_group is not None and approval.approver_group in groups
        mode = self.approver_mode
        if mode == "requester" and is_requester:
            return None
        if mode == "group" and in_group:
            return None
        if mode == "requester_or_group" and (is_requester or in_group):
            return None
        who = {
            "requester": "the requester",
            "group": f"members of {approval.approver_group}",
            "requester_or_group": f"the requester or members of {approval.approver_group}",
        }[mode]
        return f"Only {who} can approve this write."

    async def approve(
        self,
        approval_id: str,
        approver: str,
        *,
        remember: bool = False,
        pin: list[str] | None = None,
        grant_days: int = 7,
        grant_uses: int | None = None,
    ) -> WriteApproval:
        approval = await self._require_pending(approval_id)
        if remember:
            if not approval.rememberable:
                raise ApprovalError("this operation can only be approved once")
            pinned = {
                name: text
                for name in (pin or [])
                if (text := _arg_text(approval.request.args.get(name))) is not None
            }
            grant = Grant(
                agent=approval.agent,
                principal=approval.principal,
                api=approval.api,
                operation=approval.operation,
                pinned_args=pinned,
                expires_at=datetime.now(UTC) + timedelta(days=max(1, min(grant_days, 90))),
                max_uses=grant_uses,
                created_by=approver,
            )
            self._grants[grant.id] = grant
            await self._save_grant(grant)
            approval.grant_id = grant.id
            await self.audit.record(
                AuditEvent(
                    event="grant_created",
                    api=grant.api,
                    operation=grant.operation,
                    caller=approver,
                    detail=f"grant {grant.id} for {grant.agent}",
                    extra={"pinned": pinned, "days": grant_days, "max_uses": grant_uses},
                )
            )
        approval.decision = "grant" if remember else "once"
        await self._decide(approval, ApprovalState.APPROVED, approver, "approved")
        return approval

    async def deny(self, approval_id: str, approver: str) -> WriteApproval:
        approval = await self._require_pending(approval_id)
        await self._decide(approval, ApprovalState.DENIED, approver, "denied by approver")
        return approval

    async def expire(self, approval_id: str, note: str) -> None:
        approval = self._approvals.get(approval_id)
        if approval is not None and approval.state is ApprovalState.PENDING:
            await self._decide(approval, ApprovalState.EXPIRED, None, note)

    async def finish(
        self, approval: WriteApproval, ok: bool, result: dict[str, Any] | None, note: str
    ) -> None:
        approval.state = ApprovalState.EXECUTED if ok else ApprovalState.FAILED
        approval.result = result
        approval.note = note
        await self._save(approval)
        await self.notifier.write_closed(approval)

    async def _require_pending(self, approval_id: str) -> WriteApproval:
        approval = await self.load(approval_id)
        if approval is None or approval.state is not ApprovalState.PENDING:
            raise LookupError("this approval is no longer pending")
        if datetime.now(UTC) >= approval.expires_at:
            raise LookupError("this approval has expired")
        return approval

    async def _decide(
        self, approval: WriteApproval, state: ApprovalState, by: str | None, note: str
    ) -> None:
        approval.state = state
        approval.decided_by = by
        approval.note = note
        await self._save(approval)
        self._events.setdefault(approval.id, asyncio.Event()).set()
        await self.audit.record(
            AuditEvent(
                event="write_decided",
                api=approval.api,
                operation=approval.operation,
                caller=by,
                detail=f"{state}: {note}",
                extra={"approval": approval.id},
            )
        )
        if state is not ApprovalState.APPROVED:
            await self.notifier.write_closed(approval)

    # -- one-time links --------------------------------------------------------------

    def link(self, approval: WriteApproval) -> str:
        return f"{self.public_url}/approvals/{approval.id}?t={self.token_for(approval)}"

    def token_for(self, approval: WriteApproval) -> str:
        message = f"write:{approval.id}:{approval.fingerprint}".encode()
        return hmac.new(self._signing_key, message, hashlib.sha256).hexdigest()[:40]

    def check_token(self, approval: WriteApproval, token: str) -> bool:
        if not token or approval.state is not ApprovalState.PENDING:
            return False
        if datetime.now(UTC) >= approval.expires_at:
            return False
        return hmac.compare_digest(self.token_for(approval), token)
