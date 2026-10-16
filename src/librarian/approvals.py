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
    EXECUTING = "executing"  # claimed by one replica; never run twice
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
    executed_at: datetime | None = None  # when the write ran (the result is as of then)
    # Requested by a caller that does not wait (e.g. MCP): librarian runs the write
    # itself once approved, and the caller polls for the outcome.
    detached: bool = False
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
    notify_owner: bool = True  # post a notice each time the grant is used

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
    async def transition_doc(
        self, kind: str, doc_id: str, from_state: str, to_state: str, data: str
    ) -> bool: ...
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
        deciding_group: str | None = None,
        max_grant_days: int | None = None,
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
        # Who gets pinged (a Slack user group, for the card)...
        self.default_approver_group = default_approver_group
        # ...and who may decide: the identity provider's group (`approvers.group`), as the
        # auth proxy sends it. None: no group can approve; only requesters.
        self.deciding_group = deciding_group
        # Ceiling on grant length (`writes.max_grant_days`); None: until revoked is allowed.
        self.max_grant_days = max_grant_days
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
        for state in (ApprovalState.PENDING, ApprovalState.APPROVED):
            for data in await self.store.list_docs("approval", state.value):
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

    def grant_expiry(self, days: int | None) -> datetime | None:
        """When a grant of `days` (None: until revoked) expires, within the ceiling."""
        if days is not None and days < 1:
            raise ApprovalError("a grant lasts at least a day")
        if self.max_grant_days is not None and (days is None or days > self.max_grant_days):
            raise ApprovalError(
                f"grants here last at most {self.max_grant_days} days (writes.max_grant_days)"
            )
        return None if days is None else datetime.now(UTC) + timedelta(days=days)

    async def _grant(self, grant_id: str) -> Grant:
        grant = self._grants.get(grant_id)
        if grant is None and self.store is not None:
            data = await self.store.get_doc("grant", grant_id)
            grant = Grant.model_validate_json(data) if data else None
        if grant is None:
            raise ApprovalError(f"unknown grant {grant_id!r}")
        return grant

    async def adjust_grant(self, grant_id: str, by: str, *, days: int | None) -> Grant:
        """Give an active grant a new expiry: `days` from now, or None for until revoked."""
        grant = await self._grant(grant_id)
        if grant.source == "config":
            raise ApprovalError("this grant comes from the config file; change it there")
        if not grant.usable(datetime.now(UTC)):
            raise ApprovalError("this grant is no longer active")
        grant.expires_at = self.grant_expiry(days)
        self._grants[grant.id] = grant
        await self._save_grant(grant)
        await self.audit.record(
            AuditEvent(
                event="grant_adjusted",
                api=grant.api,
                operation=grant.operation,
                caller=by,
                detail=f"grant {grant.id} for {grant.agent}: "
                + (f"expires in {days} days" if days is not None else "until revoked"),
                extra={"days": days},
            )
        )
        return grant

    async def set_grant_notices(self, grant_id: str, by: str, *, on: bool) -> Grant:
        """Whether each use of this grant is posted to the channel (writes.announce_grants
        must be on too)."""
        grant = await self._grant(grant_id)
        if grant.source == "config":
            raise ApprovalError("this grant comes from the config file; change it there")
        grant.notify_owner = on
        self._grants[grant.id] = grant
        await self._save_grant(grant)
        await self.audit.record(
            AuditEvent(
                event="grant_adjusted",
                api=grant.api,
                operation=grant.operation,
                caller=by,
                detail=f"grant {grant.id} for {grant.agent}: notices " + ("on" if on else "off"),
                extra={"notify_owner": on},
            )
        )
        return grant

    async def revoke_grant(self, grant_id: str, by: str) -> Grant:
        grant = await self._grant(grant_id)
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

    def pending(self) -> list[WriteApproval]:
        return sorted(
            (a for a in self._approvals.values() if a.state is ApprovalState.PENDING),
            key=lambda a: a.created_at,
        )

    def detached_unfinished(self) -> list[WriteApproval]:
        return [
            a
            for a in self._approvals.values()
            if a.detached and a.state in (ApprovalState.PENDING, ApprovalState.APPROVED)
        ]

    @staticmethod
    def _key_doc(agent: str, idempotency_key: str) -> str:
        return hashlib.sha256(f"{agent}\0{idempotency_key}".encode()).hexdigest()[:32]

    async def _remember_key(self, approval: WriteApproval) -> None:
        """Index the approval by (agent, idempotency key) so every replica finds it."""
        key = approval.request.idempotency_key
        if key and self.store is not None:
            await self.store.save_doc(
                "idempotency", self._key_doc(approval.agent, key), approval.state.value, approval.id
            )

    async def find_by_key(self, caller: Caller, idempotency_key: str) -> WriteApproval | None:
        """The live or executed write this agent already made with this key, if any
        (pending, approved, running or executed; on any replica)."""
        live = (
            ApprovalState.PENDING,
            ApprovalState.APPROVED,
            ApprovalState.EXECUTING,
            ApprovalState.EXECUTED,
        )
        found = next(
            (
                a
                for a in self._approvals.values()
                if a.agent == caller.agent
                and a.request.idempotency_key == idempotency_key
                and a.state in live
            ),
            None,
        )
        if found is None and self.store is not None:
            approval_id = await self.store.get_doc(
                "idempotency", self._key_doc(caller.agent, idempotency_key)
            )
            if approval_id:
                stored = await self.load(approval_id)
                if stored is not None and stored.agent == caller.agent and stored.state in live:
                    found = stored
        return found

    async def record_executed(
        self, notice: WriteApproval, result: dict[str, Any], note: str
    ) -> WriteApproval:
        """Keep a write that ran without a decision (a grant, automatic approval) like any
        other executed write: `write_status` finds it and a repeat with the same
        idempotency key returns this result."""
        notice.state = ApprovalState.EXECUTED
        notice.result = result
        notice.note = note
        notice.executed_at = datetime.now(UTC)
        self._approvals[notice.id] = notice
        self._events.setdefault(notice.id, asyncio.Event())
        await self._save(notice)
        await self._remember_key(notice)
        return notice

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
            await self._remember_key(approval)
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
        await self._save_notifier_ref(approval)
        return approval

    async def _save_notifier_ref(self, approval: WriteApproval) -> None:
        """Store where the notifier posted, so the replica that closes the approval can
        update that message. Only while still pending: never overwrites a decision."""
        if self.store is None or not approval.notifier_ref:
            return
        data = await self.store.get_doc("approval", approval.id)
        stored = WriteApproval.model_validate_json(data) if data else approval
        stored.notifier_ref = dict(approval.notifier_ref)
        pending = ApprovalState.PENDING.value
        await self.store.transition_doc(
            "approval", approval.id, pending, pending, stored.model_dump_json()
        )

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

    def approval_route(self, requesters: set[str], *, link_holders: bool = False) -> str | None:
        """Why no human could approve such a write, or None if someone can.

        Checked before opening an approval so a write nobody can approve is refused
        at once instead of waiting until it expires (e.g. an autonomous agent with no
        owner when only requesters may approve). `link_holders`: approver pages are
        in token mode, so whoever holds the link decides and a group is not needed.
        """
        mode = self.approver_mode
        has_requester = bool(requesters)
        has_group = self.deciding_group is not None or link_holders
        if mode == "requester" and not has_requester:
            return "no one can approve: this agent has no owner and acts for no person"
        if mode == "group" and not has_group:
            return "no one can approve: no approver group is configured (approvers.group)"
        if mode == "requester_or_group" and not (has_requester or has_group):
            return (
                "no one can approve: set an owner for this agent or configure an approver "
                "group (approvers.group)"
            )
        return None

    def may_approve(
        self, approval: WriteApproval, identity: str | None, groups: set[str]
    ) -> str | None:
        """Return None if `identity` may approve, else the reason it may not."""
        if identity is None:  # token mode: link possession (local use only)
            return None
        is_requester = identity.lower() in approval.requesters
        in_group = self.deciding_group is not None and self.deciding_group in groups
        mode = self.approver_mode
        if mode == "requester" and is_requester:
            return None
        if mode == "group" and in_group:
            return None
        if mode == "requester_or_group" and (is_requester or in_group):
            return None
        requester = "the requester" + (
            f" ({', '.join(approval.requesters)})" if approval.requesters else ""
        )
        group = (
            f"members of {self.deciding_group}"
            if self.deciding_group is not None
            else "the approver group"
        )
        who = {
            "requester": requester,
            "group": group,
            "requester_or_group": f"{requester} or {group}",
        }[mode]
        why = f"Only {who} can approve this write."
        if mode == "requester" and not approval.requesters:
            why = "No one can approve this write: the agent has no owner and acts for no person."
        elif mode != "requester" and self.deciding_group is None:
            why += " No approver group is configured (approvers.group)."
        return why

    async def approve(
        self,
        approval_id: str,
        approver: str,
        *,
        remember: bool = False,
        pin: list[str] | None = None,
        grant_days: int | None = 7,
        grant_uses: int | None = None,
    ) -> WriteApproval:
        """`grant_days`: how long the grant lasts; None means until revoked."""
        approval = await self._require_pending(approval_id)
        if remember:
            if not approval.rememberable:
                raise ApprovalError("this operation can only be approved once")
            expires_at = self.grant_expiry(grant_days)
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
                expires_at=expires_at,
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

    async def mark_detached(self, approval: WriteApproval) -> None:
        approval.detached = True
        await self._save(approval)

    async def claim(self, approval: WriteApproval) -> bool:
        """Move an approved write to EXECUTING, exactly once across all replicas."""
        if approval.state is not ApprovalState.APPROVED:
            return False
        approval.state = ApprovalState.EXECUTING
        if self.store is None:
            return True
        claimed = await self.store.transition_doc(
            "approval",
            approval.id,
            ApprovalState.APPROVED.value,
            ApprovalState.EXECUTING.value,
            approval.model_dump_json(),
        )
        if not claimed:
            await self.load(approval.id)  # someone else claimed it; refresh our copy
        return claimed

    async def finish(
        self, approval: WriteApproval, ok: bool, result: dict[str, Any] | None, note: str
    ) -> None:
        approval.state = ApprovalState.EXECUTED if ok else ApprovalState.FAILED
        approval.result = result
        approval.note = note
        approval.executed_at = datetime.now(UTC)
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
        # Store first, then update the shared copy: a waiter that wakes in between must
        # not try to claim a write the database still shows as pending.
        decided = approval.model_copy(update={"state": state, "decided_by": by, "note": note})
        await self._save(decided)
        approval.state, approval.decided_by, approval.note = state, by, note
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

    def page_token(self, purpose: str, subject: str) -> str:
        """A token for a form on a page (e.g. the console), bound to the signed-in person,
        so a request from another site cannot act for them."""
        message = f"{purpose}:{subject}".encode()
        return hmac.new(self._signing_key, message, hashlib.sha256).hexdigest()[:40]

    def token_for(self, approval: WriteApproval) -> str:
        message = f"write:{approval.id}:{approval.fingerprint}".encode()
        return hmac.new(self._signing_key, message, hashlib.sha256).hexdigest()[:40]

    def check_token(self, approval: WriteApproval, token: str) -> bool:
        if not token or approval.state is not ApprovalState.PENDING:
            return False
        if datetime.now(UTC) >= approval.expires_at:
            return False
        return hmac.compare_digest(self.token_for(approval), token)
