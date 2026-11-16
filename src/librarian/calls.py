"""Models for API calls flowing through the librarian."""

from __future__ import annotations

from datetime import UTC, datetime
from fnmatch import fnmatchcase
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from librarian.registry.models import EntryStatus, Operation

ArgValue = str | int | float | bool | list[str] | list[int]


class Limits(BaseModel):
    """What an agent may do inside one API it is allowed to use.

    `operations` names the operations it may call (glob patterns of operation ids;
    empty: all of them).

    `arguments` names arguments and the values they may take (glob patterns):
    `{owner: [acme], repo: ["payments-*"]}` lets an agent use GitHub on those
    repositories only. An operation may be called when it takes at least one of the
    named arguments, and every one it takes is given with an allowed value. An operation
    that takes none of them (a search, "who am I") cannot be limited this way: it is
    refused, unless `allow_without` names it.

    Matching is exact and case-sensitive, and only arguments are looked at: what a
    request body says is not.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operations: tuple[str, ...] = ()
    arguments: dict[str, tuple[str, ...]] = Field(default_factory=dict[str, tuple[str, ...]])
    allow_without: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _says_something(self) -> Limits:
        if not self.operations and not self.arguments:
            raise ValueError("a limit names `operations` or `arguments` (or both)")
        if self.allow_without and not self.arguments:
            raise ValueError("`allow_without` is only meaningful together with `arguments`")
        patterns = [*self.operations, *self.allow_without]
        for name, values in self.arguments.items():
            if not name or not values:
                raise ValueError(f"argument {name!r} needs at least one allowed value")
            patterns += values
        if any(not pattern or len(pattern) > 500 for pattern in patterns):
            raise ValueError("a pattern is empty or longer than 500 characters")
        return self

    def reaches(self, op: Operation) -> bool:
        """Whether this operation can be called at all under these limits, with some
        arguments (what an agent is shown as available)."""
        if self.operations and not any(fnmatchcase(op.id, p) for p in self.operations):
            return False
        if not self.arguments:
            return True
        takes = {p.name for p in op.parameters}
        return any(name in takes for name in self.arguments) or any(
            fnmatchcase(op.id, p) for p in self.allow_without
        )

    def told(self) -> dict[str, Any]:
        """These limits as an agent is told them, so it asks for what it may have."""
        said: dict[str, Any] = {}
        if self.operations:
            said["operations"] = list(self.operations)
        if self.arguments:
            said["arguments"] = {name: list(values) for name, values in self.arguments.items()}
            said["note"] = (
                "Give these arguments with one of these values; operations that take none "
                "of them are not available to you."
            )
        return said

    def refusal(self, op: Operation, args: dict[str, ArgValue]) -> str | None:
        """Why this call is outside the limits, or None when it is inside."""
        if self.operations and not any(fnmatchcase(op.id, p) for p in self.operations):
            return f"it may not use the operation {op.id!r} there"
        if not self.arguments:
            return None
        takes = {p.name for p in op.parameters}
        limited = [name for name in self.arguments if name in takes]
        if not limited:
            if any(fnmatchcase(op.id, p) for p in self.allow_without):
                return None
            names = " or ".join(repr(name) for name in self.arguments)
            return (
                f"it is limited there to calls that name {names}, and the operation "
                f"{op.id!r} takes no such argument"
            )
        for name in limited:
            if name not in args:
                return f"the argument {name!r} must be given: its value is limited"
            value = args[name]
            for item in value if isinstance(value, list) else [value]:
                if not any(fnmatchcase(str(item), p) for p in self.arguments[name]):
                    allowed = ", ".join(self.arguments[name])
                    return f"{name}={str(item)[:80]!r} is outside what it may use ({allowed})"
        return None


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
    # How the agent proved who it is, when not by its own key: the Agent Substrate
    # actor (one agent session) behind this call. Recorded with each call for audit.
    via: str | None = None
    # What it may do inside an API it may use, by glob pattern of API id (the first
    # that matches counts). No entry for an API: nothing is limited inside it.
    limits: dict[str, Limits] = Field(default_factory=dict[str, Limits])

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

    def limits_for(self, api: str) -> Limits | None:
        return next(
            (limits for pattern, limits in self.limits.items() if fnmatchcase(api, pattern)), None
        )

    def outside_limits(self, api: str, op: Operation, args: dict[str, ArgValue]) -> str | None:
        """Why this call is outside what the agent may do inside this API, or None."""
        limits = self.limits_for(api)
        why = limits.refusal(op, args) if limits is not None else None
        return f"caller {self.agent!r} is limited on API {api!r}: {why}" if why else None


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
