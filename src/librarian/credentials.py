"""Credential requests: asking a human for access, out of band.

When a call needs a credential that does not exist (or was rejected), the
broker opens one request per API (deduplicated), notifies approvers with a
one-time link, and lets waiting tasks block until someone acts. Secret values
enter only through the secure web page, straight into the secret store.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field

from librarian import telemetry
from librarian.audit import AuditEvent, AuditSink
from librarian.calls import Caller
from librarian.registry.models import ApiEntry, EntryStatus, NoAuth, is_reserved_secret
from librarian.registry.store import Registry
from librarian.secrets import SecretStore

log = logging.getLogger(__name__)


class RequestState(StrEnum):
    PENDING = "pending"
    FULFILLED = "fulfilled"
    DENIED = "denied"
    EXPIRED = "expired"


class CredentialRequest(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    api: str
    api_name: str
    host: str
    secret_ref: str
    auth_type: str
    reason: str  # "missing" or "rejected with HTTP 401"
    scopes: list[str] = Field(default_factory=list[str])
    create_url: str | None = None
    instructions: str = ""
    requested_by: list[str] = Field(default_factory=list[str])  # "agent (for principal)"
    waiting: int = 0
    state: RequestState = RequestState.PENDING
    resolved_by: str | None = None
    resolution_note: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime
    reminded: bool = False
    # Notifier bookkeeping (e.g. Slack channel + message ts), never secrets.
    notifier_ref: dict[str, str] = Field(default_factory=dict[str, str])
    # The API has write operations enabled, so the approver may also allow this key
    # to be used for (still individually approved) writes.
    writes_possible: bool = False
    writes_enabled: bool = False
    channel: str | None = None
    approver_group: str | None = None


class Notifier:
    """Base notifier: logs only. Subclasses deliver to Slack, webhooks, etc."""

    async def opened(self, request: CredentialRequest, link: str) -> None:
        log.warning(
            "credential needed for %s (%s); approve at %s", request.api, request.reason, link
        )

    async def updated(self, request: CredentialRequest) -> None:
        log.info(
            "credential request %s for %s: %d waiting", request.id, request.api, request.waiting
        )

    async def closed(self, request: CredentialRequest) -> None:
        log.info(
            "credential request %s for %s %s by %s",
            request.id,
            request.api,
            request.state,
            request.resolved_by,
        )

    # Write approvals (see librarian.approvals). Typed loosely to avoid an import cycle.
    async def write_opened(self, approval: Any, link: str) -> None:
        log.warning(
            "write approval needed: %s.%s by %s; approve at %s",
            approval.api,
            approval.operation,
            approval.agent,
            link,
        )

    async def write_closed(self, approval: Any) -> None:
        log.info(
            "write %s on %s.%s: %s", approval.id, approval.api, approval.operation, approval.state
        )

    async def grant_used(self, grant: Any, approval_like: Any) -> None:
        log.info("grant %s used by %s for %s.%s", grant.id, grant.agent, grant.api, grant.operation)

    async def api_changed(
        self,
        api: str,
        title: str,
        details: list[str],
        link: str | None,
        channel: str | None,
        mention_group: str | None = None,
    ) -> None:
        """An API changed or is misbehaving in a way a human should look at.
        `mention_group` is set when something broke or was suspended."""
        log.warning(
            "API change: %s: %s%s%s",
            api,
            title,
            "".join(f"\n  - {d}" for d in details[:20]),
            f"\n  review: {link}" if link else "",
        )

    async def api_learned(
        self,
        api: str,
        name: str,
        learned_by: str,
        summary: str,
        link: str | None,
        channel: str | None,
        warnings: list[str],
        error: str | None = None,
    ) -> None:
        """A new API was learned and (normally) a review pull request opened: tell the
        people who review, so the entry does not go unnoticed. `warnings` are things to
        check in the review (e.g. the spec declares no authentication); `error` says
        why no review could be opened."""
        log.warning(
            "API learned: %s (%s) by %s: %s%s%s%s",
            api,
            name,
            learned_by,
            summary,
            "".join(f"\n  - {w}" for w in warnings),
            f"\n  review: {link}" if link else "",
            f"\n  NO REVIEW OPENED: {error}" if error else "",
        )

    async def review_opened(self, review: Any, link: str) -> None:
        """A learned API awaits approval: merge its review as the App, or deny."""
        log.warning(
            "review needed for %s (%s): %s; auth %s; decide at %s (pull request %s)",
            review.api,
            review.api_name,
            review.summary,
            review.auth,
            link,
            review.review_url,
        )

    async def consent_opened(self, request: Any, link: str) -> None:
        """A person must sign in to an API themselves (per-person OAuth)."""
        log.warning(
            "%s must sign in to %s for %s; link: %s",
            request.principal,
            request.api,
            request.agent,
            link,
        )

    async def consent_closed(self, request: Any) -> None:
        log.info("sign-in of %s to %s: %s", request.principal, request.api, request.state)

    async def review_closed(self, review: Any) -> None:
        log.info(
            "review of %s %s by %s: %s", review.api, review.state, review.decided_by, review.note
        )


class RequestStore(Protocol):
    """Durable storage for requests (see `librarian.state.StateStore`)."""

    async def save_request(self, request: CredentialRequest) -> None: ...
    async def update_request_if_pending(self, request: CredentialRequest) -> bool: ...
    async def get_request(self, request_id: str) -> CredentialRequest | None: ...
    async def pending_for(self, api: str) -> CredentialRequest | None: ...
    async def list_requests(self, state: RequestState | None = None) -> list[CredentialRequest]: ...


POLL_SECONDS = 2.0

Validator = Callable[[ApiEntry], Awaitable[str | None]]
"""Checks a freshly provided credential; returns an error message or None."""


class CredentialBroker:
    def __init__(
        self,
        registry: Registry,
        secrets_store: SecretStore,
        notifier: Notifier,
        audit: AuditSink,
        *,
        public_url: str,
        signing_key: bytes,
        expire_after: timedelta = timedelta(hours=24),
        remind_after: timedelta = timedelta(hours=4),
        default_channel: str | None = None,
        default_approver_group: str | None = None,
        store: RequestStore | None = None,
    ) -> None:
        self.registry = registry
        self.store = store
        self.secrets = secrets_store
        self.notifier = notifier
        self.audit = audit
        self.public_url = public_url.rstrip("/")
        self._signing_key = signing_key
        self.expire_after = expire_after
        self.remind_after = remind_after
        self.default_channel = default_channel
        self.default_approver_group = default_approver_group
        self.validator: Validator | None = None
        # Called (in the background) after a credential works, e.g. to run discovery.
        self.after_fulfilled: Callable[[str], Coroutine[Any, Any, None]] | None = None
        self._background: set[asyncio.Task[None]] = set()
        self._requests: dict[str, CredentialRequest] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._used_tokens: set[str] = set()
        self._lock = asyncio.Lock()

    def set_signing_key(self, key: bytes) -> None:
        self._signing_key = key

    async def restore(self) -> int:
        """Reload pending requests after a restart. Returns how many were restored."""
        if self.store is None:
            return 0
        restored = await self.store.list_requests(RequestState.PENDING)
        for request in restored:
            request.waiting = 0  # the tasks that were waiting died with the old process
            self._remember(request)
        return len(restored)

    def _remember(self, request: CredentialRequest) -> CredentialRequest:
        self._requests[request.id] = request
        self._events.setdefault(request.id, asyncio.Event())
        return request

    async def _persist(self, request: CredentialRequest) -> None:
        if self.store is not None:
            await self.store.save_request(request)

    async def load(self, request_id: str) -> CredentialRequest | None:
        """Get a request, refreshing from the shared store (another replica may own it)."""
        if self.store is not None:
            stored = await self.store.get_request(request_id)
            if stored is not None:
                current = self._requests.get(request_id)
                if current is None:
                    return self._remember(stored)
                for name in CredentialRequest.model_fields:
                    if name != "waiting":  # local count of tasks waiting on this replica
                        setattr(current, name, getattr(stored, name))
                return current
        return self._requests.get(request_id)

    # -- opening and waiting -------------------------------------------------

    def pending_for(self, api_id: str) -> CredentialRequest | None:
        return next(
            (
                r
                for r in self._requests.values()
                if r.api == api_id and r.state is RequestState.PENDING
            ),
            None,
        )

    def get(self, request_id: str) -> CredentialRequest | None:
        return self._requests.get(request_id)

    def all(self) -> list[CredentialRequest]:
        return sorted(self._requests.values(), key=lambda r: r.created_at, reverse=True)

    async def open(self, entry: ApiEntry, caller: Caller, reason: str) -> CredentialRequest:
        """Open (or join) the pending request for this API."""
        if isinstance(entry.auth, NoAuth) or entry.secret_ref is None:
            raise ValueError(f"API {entry.id!r} does not use a credential")
        who = caller.agent + (f" (for {caller.principal})" if caller.principal else "")
        async with self._lock:
            existing = self.pending_for(entry.id)
            if existing is None and self.store is not None:
                stored = await self.store.pending_for(entry.id)
                existing = self._remember(stored) if stored is not None else None
            if existing is not None:
                existing.waiting += 1
                if who not in existing.requested_by:
                    existing.requested_by.append(who)
                request, is_new = existing, False
            else:
                request = CredentialRequest(
                    api=entry.id,
                    api_name=entry.name,
                    host=entry.host,
                    secret_ref=entry.secret_ref,
                    auth_type=entry.auth.type,
                    reason=reason,
                    scopes=list(entry.credential.scopes),
                    create_url=entry.credential.create_url,
                    instructions=entry.credential.instructions,
                    requested_by=[who],
                    writes_possible=entry.has_writable_operations,
                    waiting=1,
                    expires_at=datetime.now(UTC) + self.expire_after,
                    channel=entry.owners.channel or self.default_channel,
                    approver_group=entry.owners.approver_group or self.default_approver_group,
                )
                self._remember(request)
                is_new = True
            await self._persist(request)
        if is_new:
            telemetry.credential_requests.add(1, {"librarian.api": entry.id, "state": "pending"})
            await self.audit.record(
                AuditEvent(
                    event="credential_request",
                    caller=caller.agent,
                    principal=caller.principal,
                    api=entry.id,
                    detail=reason,
                )
            )
            await self.notifier.opened(request, self.link(request))
            await self._save_notifier_ref(request)
        else:
            await self.notifier.updated(request)
        return request

    async def _save_notifier_ref(self, request: CredentialRequest) -> None:
        """Store where the notifier posted (e.g. the Slack message), so whichever replica
        resolves the request can update that message. Never overwrites a resolution."""
        if self.store is None or not request.notifier_ref:
            return
        stored = await self.store.get_request(request.id) or request
        stored.notifier_ref = dict(request.notifier_ref)
        await self.store.update_request_if_pending(stored)

    async def wait(self, request_id: str, max_wait: float | None = None) -> CredentialRequest:
        """Block until the request is resolved, expired, or `max_wait` seconds pass."""
        request = self._requests[request_id]
        event = self._events[request_id]
        remaining = (request.expires_at - datetime.now(UTC)).total_seconds()
        limit = remaining if max_wait is None else min(max_wait, remaining)
        deadline = asyncio.get_running_loop().time() + max(limit, 0)
        try:
            while request.state is RequestState.PENDING:
                left = deadline - asyncio.get_running_loop().time()
                if left <= 0:
                    await self._expire_if_due(request)
                    break
                try:
                    await asyncio.wait_for(event.wait(), timeout=min(left, POLL_SECONDS))
                except TimeoutError:
                    # Another replica may have resolved it.
                    await self.load(request_id)
        finally:
            if request.waiting > 0:
                request.waiting -= 1
        return request

    async def _expire_if_due(self, request: CredentialRequest) -> None:
        if request.state is RequestState.PENDING and datetime.now(UTC) >= request.expires_at:
            await self._close(request, RequestState.EXPIRED, None, "no one responded in time")

    async def sweep(self) -> None:
        """Send reminders and expire stale requests; call periodically."""
        now = datetime.now(UTC)
        for request in list(self._requests.values()):
            if request.state is not RequestState.PENDING:
                continue
            if now >= request.expires_at:
                await self._close(request, RequestState.EXPIRED, None, "no one responded in time")
            elif not request.reminded and now - request.created_at >= self.remind_after:
                request.reminded = True
                await self._persist(request)
                await self.notifier.updated(request)

    # -- one-time links ------------------------------------------------------

    def link(self, request: CredentialRequest) -> str:
        return f"{self.public_url}/credentials/{request.id}?t={self.token_for(request)}"

    def token_for(self, request: CredentialRequest) -> str:
        message = f"{request.id}:{request.created_at.isoformat()}".encode()
        return hmac.new(self._signing_key, message, hashlib.sha256).hexdigest()[:40]

    def check_token(self, request: CredentialRequest, token: str) -> bool:
        if not token or token in self._used_tokens:
            return False
        if request.state is not RequestState.PENDING:
            return False
        if datetime.now(UTC) >= request.expires_at:
            return False
        return hmac.compare_digest(self.token_for(request), token)

    # -- resolving -------------------------------------------------------------

    async def provide_value(
        self, request_id: str, value: str, approver: str, allow_writes: bool = False
    ) -> str | None:
        """Store a pasted secret, validate it, and resolve. Returns an error or None."""
        request = await self._require_pending(request_id)
        value = value.strip()
        if not value:
            return "The key was empty."
        await self.secrets.put(request.secret_ref, value)
        error = await self._validate_and_resolve(request, approver, "new key added")
        if error is None and allow_writes:
            await self._enable_writes(request, approver)
        return error

    async def _enable_writes(self, request: CredentialRequest, approver: str) -> None:
        """Use the same key for writes. Each write still needs approval or a grant."""
        entry = self.registry.get(request.api)
        if entry is None or not entry.has_writable_operations:
            return
        data = entry.model_dump(mode="json", by_alias=True)
        data["write_auth"] = entry.auth.model_dump(mode="json", by_alias=True)
        await self.registry.save(ApiEntry.model_validate(data))
        request.writes_enabled = True
        await self._persist(request)
        await self.audit.record(
            AuditEvent(
                event="writes_enabled",
                api=request.api,
                caller=approver,
                detail=f"write credential set to {request.secret_ref}",
            )
        )

    async def use_existing(
        self, request_id: str, existing_ref: str, approver: str, allow_writes: bool = False
    ) -> str | None:
        """Point the API at an existing secret, validate it, and resolve."""
        request = await self._require_pending(request_id)
        if is_reserved_secret(existing_ref) or existing_ref not in await self.offerable_secrets():
            return f"No secret named {existing_ref!r} exists."
        entry = self.registry.get(request.api)
        if entry is None:
            return f"API {request.api!r} is no longer in the registry."
        if existing_ref != entry.secret_ref:
            auth = entry.auth.model_copy(update={"secret_ref": existing_ref})
            await self.registry.save(entry.model_copy(update={"auth": auth}))
            request.secret_ref = existing_ref
            await self._persist(request)
        error = await self._validate_and_resolve(
            request, approver, f"using existing {existing_ref}"
        )
        if error is None and allow_writes:
            await self._enable_writes(request, approver)
        return error

    async def offerable_secrets(self) -> list[str]:
        """Secret names an approver may pick; internal secrets are never offered."""
        return [n for n in await self.secrets.list_names() if not is_reserved_secret(n)]

    async def deny(self, request_id: str, approver: str) -> None:
        request = await self._require_pending(request_id)
        await self.registry.set_status(request.api, EntryStatus.BLOCKED)
        await self._close(request, RequestState.DENIED, approver, "access denied by approver")

    async def _validate_and_resolve(
        self, request: CredentialRequest, approver: str, note: str
    ) -> str | None:
        entry = self.registry.get(request.api)
        if entry is not None and self.validator is not None:
            problem = await self.validator(entry)
            if problem is not None:
                return f"The credential was saved but did not work: {problem}"
        validated = (
            entry is not None
            and self.validator is not None
            and (entry.credential.validation_operation is not None)
        )
        await self._close(
            request,
            RequestState.FULFILLED,
            approver,
            f"{note}; " + ("tested OK" if validated else "not tested (no validation operation)"),
        )
        if self.after_fulfilled is not None:
            task = asyncio.create_task(self.after_fulfilled(request.api))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
        return None

    async def _require_pending(self, request_id: str) -> CredentialRequest:
        request = await self.load(request_id)
        if request is None or request.state is not RequestState.PENDING:
            raise LookupError("this request is no longer pending")
        return request

    async def _close(
        self,
        request: CredentialRequest,
        state: RequestState,
        approver: str | None,
        note: str,
    ) -> None:
        request.state = state
        request.resolved_by = approver
        request.resolution_note = note
        self._used_tokens.add(self.token_for(request))
        await self._persist(request)
        self._events.setdefault(request.id, asyncio.Event()).set()
        telemetry.credential_requests.add(1, {"librarian.api": request.api, "state": state.value})
        await self.audit.record(
            AuditEvent(
                event="credential_resolved",
                api=request.api,
                caller=approver,
                detail=f"{state}: {note}",
            )
        )
        await self.notifier.closed(request)


def new_signing_key() -> bytes:
    return secrets.token_bytes(32)
