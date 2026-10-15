"""Models for API calls flowing through the librarian."""

from __future__ import annotations

from datetime import UTC, datetime
from fnmatch import fnmatchcase
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from librarian.registry.models import EntryStatus

ArgValue = str | int | float | bool | list[str] | list[int]


class Caller(BaseModel):
    """Who is asking.

    `agent` is authenticated by the server (or `anonymous` when caller auth is
    off). `principal` is who the agent acts for; in v0.1 it is *asserted* by the
    calling agent and recorded for audit, not verified.
    """

    model_config = ConfigDict(frozen=True)

    agent: str = "anonymous"
    authenticated: bool = False
    principal: str | None = None
    # True when `principal` came from a verified identity-provider token.
    principal_verified: bool = False
    # Whether an unverified (merely claimed) principal may act as requester.
    trust_claimed_principal: bool = True
    # Glob patterns of API ids this caller may use; None = every API.
    apis: tuple[str, ...] | None = None
    can_onboard: bool = True
    # Glob patterns of API ids this caller may *request writes* on. Empty = none.
    write_apis: tuple[str, ...] = ()
    # The human (email) or team responsible for this agent. For autonomous agents
    # with no principal, the owner stands in as the requester for approvals.
    owner: str | None = None
    # Seconds to wait for a write approval before returning "pending" (0 = don't wait).
    approval_wait_seconds: float | None = None

    def requesters(self) -> set[str]:
        """Humans who count as 'the requester' when approving this caller's writes.

        A claimed principal only counts when it was verified, or when principal
        verification is off. The agent's configured owner always counts.
        """
        people = {self.owner} if self.owner else set[str]()
        if self.principal and (self.principal_verified or self.trust_claimed_principal):
            people.add(self.principal)
        return {who.lower() for who in people}

    def may_use(self, api: str) -> bool:
        return self.apis is None or any(fnmatchcase(api, pattern) for pattern in self.apis)

    def may_write(self, api: str) -> bool:
        return self.may_use(api) and any(fnmatchcase(api, p) for p in self.write_apis)


class CallRequest(BaseModel):
    """A fully specified call: which API, which operation, which arguments."""

    model_config = ConfigDict(extra="forbid")

    api: str = Field(min_length=1, max_length=64)
    operation: str = Field(min_length=1, max_length=128)
    args: dict[str, ArgValue] = Field(default_factory=dict[str, ArgValue])
    body: dict[str, Any] | None = None
    max_pages: int = Field(default=1, ge=1, le=20)
    # Dotted paths to keep, e.g. ["issues.key", "issues.fields.summary"].
    select: list[str] = Field(default_factory=list[str], max_length=50)

    @field_validator("select")
    @classmethod
    def _check_select(cls, paths: list[str]) -> list[str]:
        for path in paths:
            if not path.strip() or len(path) > 200:
                raise ValueError(f"invalid select path {path!r}")
        return paths


class Provenance(BaseModel):
    """Where a result came from, so callers can cite and auditors can trace it."""

    api: str
    api_status: EntryStatus
    operation: str
    method: str
    url: str  # never contains secrets: query-string API keys are redacted
    requested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    caller: str
    principal: str | None = None
    principal_verified: bool = False
    credential_ref: str | None = None
    pages: int = 1
    # Where the operation's description came from (spec, docs, model, manual) and
    # whether a real call has confirmed it works.
    origin: str = "manual"
    confirmed: bool = True


class WriteRequest(BaseModel):
    """A request to change data. Always approved by a human or a matching grant."""

    model_config = ConfigDict(extra="forbid")

    api: str = Field(min_length=1, max_length=64)
    operation: str = Field(min_length=1, max_length=128)
    args: dict[str, ArgValue] = Field(default_factory=dict[str, ArgValue])
    body: dict[str, Any] | list[Any] | None = None
    # Lets a caller retry safely: a second request with the same key returns the
    # first result instead of writing again.
    idempotency_key: str | None = Field(default=None, max_length=128)
    reason: str = Field(default="", max_length=2000)  # shown to the approver
    # Other operations of the same API this flow will need (e.g. the rest of a
    # create-blob, create-tree, create-commit, create-ref chain). When the write is not
    # enabled, they go on the same enable request so one approver action unblocks them all.
    also_needs: list[str] = Field(default_factory=list[str], max_length=20)

    def fingerprint(self, caller: Caller) -> str:
        """Identity of exactly this change by this caller (binds approval to request)."""
        import hashlib

        payload = self.model_dump_json(exclude={"reason", "idempotency_key", "also_needs"})
        return hashlib.sha256(f"{caller.agent}|{caller.principal}|{payload}".encode()).hexdigest()


class CallResult(BaseModel):
    status_code: int
    ok: bool
    data: Any = None
    truncated: bool = False
    content_type: str = ""
    # Set when the API refused the call for a reason the caller should see
    # (e.g. 403: the credential works but lacks permission for this resource).
    error: str | None = None
    # Things the caller should know about this operation: unconfirmed, deprecated by
    # the API, or changed since it was reviewed.
    warnings: list[str] = Field(default_factory=list[str])
    provenance: Provenance
