"""The policy engine: deterministic rules that approve or reject every call.

No LLM is involved here. The planner may *propose* calls, but only this module
decides whether they run, so no wording of a request can talk its way past it.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass

from librarian.calls import ArgValue, Caller, CallRequest, WriteRequest
from librarian.observations import Observations
from librarian.registry.models import (
    Access,
    ApiEntry,
    EntryStatus,
    Operation,
    ParamLocation,
    ReadBasis,
)
from librarian.registry.store import Registry

MAX_ARG_LENGTH = 2048


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    entry: ApiEntry | None = None
    operation: Operation | None = None

    @classmethod
    def deny(cls, reason: str) -> Decision:
        return cls(False, reason)


class RateLimiter:
    """Sliding one-minute window per (caller, api)."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._hits: defaultdict[tuple[str, str], deque[float]] = defaultdict(deque)

    def allow(self, caller: str, api: str, now: float | None = None) -> bool:
        if self.per_minute <= 0:
            return True
        now = time.monotonic() if now is None else now
        window = self._hits[(caller, api)]
        while window and now - window[0] >= 60:
            window.popleft()
        if len(window) >= self.per_minute:
            return False
        window.append(now)
        return True


class PolicyEngine:
    def __init__(
        self,
        registry: Registry,
        *,
        rate_limit_per_minute: int = 60,
        write_rate_limit_per_minute: int = 10,
        allow_provisional: bool = True,
        allowed_hosts: frozenset[str] | None = None,
        observations: Observations | None = None,
    ) -> None:
        self.registry = registry
        # Runtime facts about operations, including suspensions after an API changed.
        self.observations = observations or Observations()
        self.rate_limiter = RateLimiter(rate_limit_per_minute)
        self.write_rate_limiter = RateLimiter(write_rate_limit_per_minute)
        self.allow_provisional = allow_provisional
        # Optional extra guard: when set, entries outside these hosts are refused
        # even if they are in the registry.
        self.allowed_hosts = allowed_hosts

    def check(self, request: CallRequest, caller: Caller) -> Decision:
        if not caller.may_use(request.api):
            return Decision.deny(
                f"caller {caller.agent!r} is not allowed to use API {request.api!r}"
            )
        entry = self.registry.get(request.api)
        if entry is None:
            return Decision.deny(f"unknown API {request.api!r}")
        if entry.status is EntryStatus.BLOCKED:
            return Decision.deny(f"API {entry.id!r} is blocked")
        if entry.status is EntryStatus.PROVISIONAL and not self.allow_provisional:
            return Decision.deny(f"API {entry.id!r} is provisional and provisional use is off")
        if self.allowed_hosts is not None and entry.host.lower() not in self.allowed_hosts:
            return Decision.deny(f"host {entry.host!r} is not on the allowlist")

        op = entry.operation(request.operation)
        if op is None:
            return Decision.deny(f"unknown operation {request.operation!r} on {entry.id!r}")
        if op.access is not Access.READ:
            return Decision.deny(f"operation {op.id!r} is not a read (access={op.access})")
        suspended = self.observations.suspended(entry, op)
        if suspended:
            return Decision.deny(f"operation {op.id!r} is suspended until reviewed: {suspended}")
        if op.read_basis is ReadBasis.REVIEWED and entry.status is not EntryStatus.VERIFIED:
            return Decision.deny(
                f"operation {op.id!r} is a reviewed read and API {entry.id!r} is not verified"
            )
        if request.body is not None and not op.accepts_body:
            return Decision.deny(f"operation {op.id!r} does not accept a request body")

        problem = _check_args(op, request.args)
        if problem:
            return Decision.deny(problem)

        if not self.rate_limiter.allow(caller.agent, entry.id):
            return Decision.deny(f"rate limit exceeded for {caller.agent!r} on {entry.id!r}")

        return Decision(True, "allowed", entry, op)

    def check_write(self, request: WriteRequest, caller: Caller) -> Decision:
        """Whether a write may be *requested*. Approval is still required to run it."""
        if not caller.may_write(request.api):
            return Decision.deny(f"caller {caller.agent!r} may not write to API {request.api!r}")
        entry = self.registry.get(request.api)
        if entry is None:
            return Decision.deny(f"unknown API {request.api!r}")
        if entry.status is not EntryStatus.VERIFIED:
            return Decision.deny(f"API {entry.id!r} is not verified; writes need a reviewed entry")
        if self.allowed_hosts is not None and entry.host.lower() not in self.allowed_hosts:
            return Decision.deny(f"host {entry.host!r} is not on the allowlist")
        op = entry.operation(request.operation)
        if op is None:
            return Decision.deny(f"unknown operation {request.operation!r} on {entry.id!r}")
        if op.access is not Access.WRITE or not op.writable:
            return Decision.deny(f"operation {op.id!r} is not enabled for writes")
        suspended = self.observations.suspended(entry, op)
        if suspended:
            return Decision.deny(f"operation {op.id!r} is suspended until reviewed: {suspended}")
        if entry.write_auth is None:
            return Decision.deny(f"API {entry.id!r} has no write credential configured")
        if request.body is not None and not op.accepts_body:
            return Decision.deny(f"operation {op.id!r} does not accept a request body")
        problem = _check_args(op, request.args)
        if problem:
            return Decision.deny(problem)
        if not self.write_rate_limiter.allow(caller.agent, entry.id):
            return Decision.deny(f"write rate limit exceeded for {caller.agent!r} on {entry.id!r}")
        return Decision(True, "write may be requested", entry, op)


def _check_args(op: Operation, args: dict[str, ArgValue]) -> str | None:
    declared = {p.name: p for p in op.parameters}
    unknown = sorted(set(args) - set(declared))
    if unknown:
        return f"unknown arguments for {op.id!r}: {', '.join(unknown)}"
    for param in op.parameters:
        if param.required and param.name not in args:
            return f"missing required argument {param.name!r} for {op.id!r}"
    for name, value in args.items():
        param = declared[name]
        values = value if isinstance(value, list) else [value]
        for item in values:
            text = str(item)
            if len(text) > MAX_ARG_LENGTH:
                return f"argument {name!r} is too long"
            if any(ch in text for ch in "\r\n\x00"):
                return f"argument {name!r} contains control characters"
        if param.location is ParamLocation.PATH:
            if isinstance(value, list):
                return f"path argument {name!r} must be a single value"
            text = str(value)
            if not text or text in (".", "..") or "/" in text:
                return f"path argument {name!r} is not a valid path segment"
        if param.location is ParamLocation.HEADER and name.lower() in _FORBIDDEN_HEADERS:
            return f"header argument {name!r} is not allowed"
    return None


_FORBIDDEN_HEADERS = frozenset(
    {"authorization", "cookie", "host", "proxy-authorization", "x-api-key"}
)
