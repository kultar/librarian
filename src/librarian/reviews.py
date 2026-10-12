"""Reviews of learned APIs: the approve-or-deny step, from Slack or the console.

When librarian learns an API and opens a review pull request, it also opens a
*review request*: a card for approvers with a one-time link to a page where they
see what librarian proposes (the operations, the authentication it will use, the
operations that still need a decision) and approve or deny. Approve merges the
pull request as the App; deny closes it and drops the API. Nobody edits YAML.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field

from librarian.audit import AuditEvent, AuditSink


class ReviewState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


class ReviewRequest(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    api: str
    api_name: str
    host: str
    review_url: str  # the pull request
    learned_by: str
    summary: str  # "12 operations (10 reads, 2 need review) from an OpenAPI spec"
    auth: str  # "bearer (github/readonly), inferred from the model's knowledge" or "none"
    warnings: list[str] = Field(default_factory=list[str])
    needs_review: list[str] = Field(default_factory=list[str])  # operation ids
    state: ReviewState = ReviewState.PENDING
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime
    decided_by: str | None = None
    note: str | None = None  # what happened ("merged", "closed and forgotten", ...)
    channel: str | None = None
    approver_group: str | None = None
    notifier_ref: dict[str, str] = Field(default_factory=dict[str, str])


class ReviewNotifier(Protocol):
    async def review_opened(self, review: ReviewRequest, link: str) -> None: ...
    async def review_closed(self, review: ReviewRequest) -> None: ...


class ReviewStore(Protocol):
    async def save_doc(self, kind: str, doc_id: str, state: str, data: str) -> None: ...
    async def get_doc(self, kind: str, doc_id: str) -> str | None: ...
    async def list_docs(self, kind: str, state: str | None = None) -> list[str]: ...


class ReviewBroker:
    def __init__(
        self,
        notifier: Any,
        audit: AuditSink,
        *,
        public_url: str,
        signing_key: bytes,
        expire_after: timedelta = timedelta(days=7),
        default_channel: str | None = None,
        default_approver_group: str | None = None,
        store: ReviewStore | None = None,
    ) -> None:
        self.notifier: ReviewNotifier = notifier
        self.audit = audit
        self.public_url = public_url.rstrip("/")
        self._signing_key = signing_key
        self.expire_after = expire_after
        self.default_channel = default_channel
        self.default_approver_group = default_approver_group
        self.store = store
        self._reviews: dict[str, ReviewRequest] = {}

    def set_signing_key(self, key: bytes) -> None:
        self._signing_key = key

    async def restore(self) -> None:
        if self.store is None:
            return
        for data in await self.store.list_docs("review", ReviewState.PENDING.value):
            review = ReviewRequest.model_validate_json(data)
            self._reviews[review.id] = review

    async def _save(self, review: ReviewRequest) -> None:
        if self.store is not None:
            await self.store.save_doc(
                "review", review.id, review.state.value, review.model_dump_json()
            )

    async def load(self, review_id: str) -> ReviewRequest | None:
        if self.store is not None:
            data = await self.store.get_doc("review", review_id)
            if data is not None:
                review = ReviewRequest.model_validate_json(data)
                self._reviews[review.id] = review
                return review
        return self._reviews.get(review_id)

    def pending(self) -> list[ReviewRequest]:
        return sorted(
            (r for r in self._reviews.values() if r.state is ReviewState.PENDING),
            key=lambda r: r.created_at,
        )

    def pending_for(self, api: str) -> ReviewRequest | None:
        return next((r for r in self.pending() if r.api == api), None)

    # -- links ---------------------------------------------------------------------

    def token_for(self, review: ReviewRequest) -> str:
        message = f"review:{review.id}:{review.created_at.isoformat()}".encode()
        return hmac.new(self._signing_key, message, hashlib.sha256).hexdigest()[:40]

    def check_token(self, review: ReviewRequest, token: str) -> bool:
        if review.state is not ReviewState.PENDING or datetime.now(UTC) > review.expires_at:
            return False
        return hmac.compare_digest(self.token_for(review), token)

    def link(self, review: ReviewRequest) -> str:
        return f"{self.public_url}/reviews/{review.id}?t={self.token_for(review)}"

    # -- lifecycle -----------------------------------------------------------------

    async def open(
        self,
        *,
        api: str,
        api_name: str,
        host: str,
        review_url: str,
        learned_by: str,
        summary: str,
        auth: str,
        warnings: list[str],
        needs_review: list[str],
        channel: str | None = None,
        approver_group: str | None = None,
    ) -> ReviewRequest:
        """Open the approve-or-deny step for a learned API and tell the approvers."""
        existing = self.pending_for(api)
        if existing is not None:  # re-learned while a review was open: supersede it
            existing.state = ReviewState.DENIED
            existing.note = "superseded by a newer proposal"
            await self._save(existing)
        review = ReviewRequest(
            api=api,
            api_name=api_name,
            host=host,
            review_url=review_url,
            learned_by=learned_by,
            summary=summary,
            auth=auth,
            warnings=warnings,
            needs_review=needs_review,
            expires_at=datetime.now(UTC) + self.expire_after,
            channel=channel or self.default_channel,
            approver_group=approver_group or self.default_approver_group,
        )
        self._reviews[review.id] = review
        await self._save(review)
        await self.notifier.review_opened(review, self.link(review))
        await self._save(review)  # the notifier may have recorded where the card is
        return review

    async def decide(self, review_id: str, state: ReviewState, by: str, note: str) -> ReviewRequest:
        review = await self.load(review_id)
        if review is None or review.state is not ReviewState.PENDING:
            raise LookupError(f"review {review_id!r} is not pending")
        review.state = state
        review.decided_by = by
        review.note = note
        await self._save(review)
        await self.audit.record(
            AuditEvent(
                event=f"review_{state.value}",
                caller="console",
                principal=by,
                api=review.api,
                detail=note,
                extra={"review_url": review.review_url},
            )
        )
        await self.notifier.review_closed(review)
        return review
