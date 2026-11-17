"""The policy engine: deterministic rules that approve or reject every call.

No LLM is involved here. The planner may *propose* calls, but only this module
decides whether they run, so no wording of a request can talk its way past it.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Protocol

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
    # A refused write that approvers could enable (the operation is not writable, or the
    # entry has no write credential): the service turns it into a request instead of a
    # bare denial.
    askable: bool = False

    @classmethod
    def deny(
        cls,
        reason: str,
        *,
        askable: bool = False,
        entry: ApiEntry | None = None,
        operation: Operation | None = None,
    ) -> Decision:
        return cls(False, reason, entry, operation, askable)


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


class RateLimitStore(Protocol):
    """Counts hits per key and window across replicas (see `librarian.state.StateStore`)."""

    async def count_hit(self, key: str, window: int) -> int: ...
    async def prune_rate_limits(self, before_window: int) -> None: ...


class SharedRateLimiter:
    """A fixed one-minute window per (caller, api), counted in the shared database, so
    several replicas enforce one limit together instead of one each."""

    def __init__(self, per_minute: int, store: RateLimitStore, kind: str) -> None:
        self.per_minute = per_minute
        self.store = store
        self.kind = kind
        self._hits = 0

    async def allow(self, caller: str, api: str, now: float | None = None) -> bool:
        if self.per_minute <= 0:
            return True
        now = time.time() if now is None else now
        window = int(now // 60)
        count = await self.store.count_hit(f"{self.kind}:{caller}:{api}"[:200], window)
        self._hits += 1
        if self._hits % 500 == 0:  # old windows are never read again
            await self.store.prune_rate_limits(window - 2)
        return count <= self.per_minute


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
        shared_limits: RateLimitStore | None = None,
    ) -> None:
        self.registry = registry
        # Runtime facts about operations, including suspensions after an API changed.
        self.observations = observations or Observations()
        # With a shared database the limits are counted there, so N replicas enforce one
        # limit; without one, each process counts on its own.
        self.rate_limiter: RateLimiter | SharedRateLimiter = (
            SharedRateLimiter(rate_limit_per_minute, shared_limits, "read")
            if shared_limits is not None
            else RateLimiter(rate_limit_per_minute)
        )
        self.write_rate_limiter: RateLimiter | SharedRateLimiter = (
            SharedRateLimiter(write_rate_limit_per_minute, shared_limits, "write")
            if shared_limits is not None
            else RateLimiter(write_rate_limit_per_minute)
        )
        self.allow_provisional = allow_provisional
        # Optional extra guard: when set, entries outside these hosts are refused
        # even if they are in the registry.
        self.allowed_hosts = allowed_hosts

    def listed(self, entry: ApiEntry, caller: Caller, access: Access) -> list[Operation]:
        """The operations of an API a caller is told about: the reads it can make, or
        (asked for `Access.WRITE`) the changes it may ask for, where it may ask for any.
        Its limits leave out what they cannot hold, and suspended operations are left out."""
        if access is not Access.READ and not caller.may_write(entry.id):
            return []
        limits = caller.limits_for(entry.id)
        return [
            op
            for op in entry.operations
            if (op.access is Access.READ) == (access is Access.READ)
            and not self.observations.suspended(entry, op)
            and (limits is None or limits.reaches(op))
        ]

    def api(self, api_id: str, caller: Caller) -> Decision:
        """The API a caller names, where it may use it: for a read, and for `capabilities`
        asked about one API. One named another way (`GitHub` for `github`) is answered
        with the one that exists, among the APIs that caller may use anyway."""
        if not caller.may_use(api_id):
            return Decision.deny(
                f"caller {caller.agent!r} is not allowed to use API {api_id!r}"
                + _meant(api_id, self._usable(caller))
            )
        entry = self.registry.get(api_id)
        if entry is None:
            return Decision.deny(f"unknown API {api_id!r}" + _meant(api_id, self._usable(caller)))
        if entry.status is EntryStatus.BLOCKED:
            return Decision.deny(f"API {entry.id!r} is blocked")
        return Decision(True, "known", entry)

    def _usable(self, caller: Caller) -> list[str]:
        """The APIs a caller is told about: the ones it may use that are not blocked."""
        return [
            entry.id
            for entry in self.registry.all()
            if entry.status is not EntryStatus.BLOCKED and caller.may_use(entry.id)
        ]

    def shown(self, entry: ApiEntry, caller: Caller, operation: str) -> Decision:
        """The operation a caller asks to be told about, where it is one it is shown
        anyway (`listed`). Any other is not there for that caller."""
        reads = self.listed(entry, caller, Access.READ)
        writes = self.listed(entry, caller, Access.WRITE)
        op = next((op for op in [*reads, *writes] if op.id == operation), None)
        if op is None:
            return Decision.deny(
                f"unknown operation {operation!r} on {entry.id!r}"
                + _meant(operation, [known.id for known in [*reads, *writes]])
            )
        return Decision(True, "shown", entry, op)

    def check(self, request: CallRequest, caller: Caller) -> Decision:
        known = self.api(request.api, caller)
        entry = known.entry
        if not known.allowed or entry is None:
            return known
        if entry.status is EntryStatus.PROVISIONAL and not self.allow_provisional:
            return Decision.deny(f"API {entry.id!r} is provisional and provisional use is off")
        if self.allowed_hosts is not None and entry.host.lower() not in self.allowed_hosts:
            return Decision.deny(f"host {entry.host!r} is not on the allowlist")

        op = entry.operation(request.operation)
        if op is None:
            return Decision.deny(
                f"unknown operation {request.operation!r} on {entry.id!r}"
                + _meant(
                    request.operation,
                    [shown.id for shown in self.listed(entry, caller, Access.READ)],
                )
            )
        outside = caller.outside_limits(entry.id, op, request.args)
        if outside:
            return Decision.deny(outside)
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
        return Decision(True, "allowed", entry, op)

    async def rate_limited(
        self, caller: Caller, entry: ApiEntry, *, write: bool = False
    ) -> str | None:
        """The rate-limit part of the decision, taken after `check`/`check_write` allowed
        the call (it may need the shared database). None when within the limit, else why."""
        limiter = self.write_rate_limiter if write else self.rate_limiter
        allowed = (
            await limiter.allow(caller.agent, entry.id)
            if isinstance(limiter, SharedRateLimiter)
            else limiter.allow(caller.agent, entry.id)
        )
        if allowed:
            return None
        what = "write rate limit" if write else "rate limit"
        return f"{what} exceeded for {caller.agent!r} on {entry.id!r}"

    def check_write(
        self,
        request: WriteRequest,
        caller: Caller,
        *,
        unresolved: frozenset[str] = frozenset(),
        refers: bool = False,
    ) -> Decision:
        """Whether a write may be *requested*. Approval is still required to run it.

        `unresolved`: arguments whose value is not known yet (a step of a plan that takes
        it from an earlier step's answer). The step is checked again, with every value,
        before it is sent. `refers`: the step takes something from an earlier step, in an
        argument or in its body.
        """
        if not caller.may_write(request.api):
            writable = [name for name in self._usable(caller) if caller.may_write(name)]
            return Decision.deny(
                f"caller {caller.agent!r} may not write to API {request.api!r}"
                + _meant(request.api, writable)
            )
        return self._write(request, caller, unresolved, refers or bool(unresolved))

    def recheck_write(self, request: WriteRequest) -> Decision:
        """Whether an approved write may still be sent: the API and the operation as the
        registry has them now, and the arguments as they are now (a plan's step has just
        been given what an earlier step answered, which nobody has checked yet).

        Who may ask for it was settled when it was requested, and a person approved it;
        that is not asked again here. Librarian may be finishing this without the agent,
        after a restart, and has to come to the same answer then.
        """
        return self._write(request, None, frozenset(), False)

    def _write(
        self,
        request: WriteRequest,
        caller: Caller | None,
        unresolved: frozenset[str],
        refers: bool,
    ) -> Decision:
        entry = self.registry.get(request.api)
        if entry is None:
            usable = self._usable(caller) if caller is not None else []
            return Decision.deny(f"unknown API {request.api!r}" + _meant(request.api, usable))
        if entry.status is not EntryStatus.VERIFIED:
            return Decision.deny(f"API {entry.id!r} is not verified; writes need a reviewed entry")
        if self.allowed_hosts is not None and entry.host.lower() not in self.allowed_hosts:
            return Decision.deny(f"host {entry.host!r} is not on the allowlist")
        op = entry.operation(request.operation)
        if op is None:
            known = self.listed(entry, caller, Access.WRITE) if caller is not None else []
            return Decision.deny(
                f"unknown operation {request.operation!r} on {entry.id!r}"
                + _meant(request.operation, [op.id for op in known])
            )
        if caller is not None:
            # Before anything an approver could be asked about: a call outside the agent's
            # limits is refused, not turned into a request to enable it.
            limits = caller.limits_for(entry.id)
            later = sorted(unresolved & set(limits.arguments)) if limits is not None else []
            if later:
                # A limited argument has to be in the plan, for this check and for the
                # approver to see: the agent's limits are not looked at again when the
                # step is sent.
                return Decision.deny(
                    f"caller {caller.agent!r} is limited on API {entry.id!r}: the argument "
                    f"{later[0]!r} must be given in the plan itself, not taken from an "
                    "earlier step"
                )
            outside = caller.outside_limits(entry.id, op, request.args)
            if outside:
                return Decision.deny(outside)
        if refers and op.method.upper() == "DELETE":
            # The approver has to see what is removed. A DELETE is never approved for
            # more than the one request shown; here part of the request would not be.
            return Decision.deny(
                f"operation {op.id!r} is a DELETE: what it deletes has to be written in the "
                "plan itself, not taken from an earlier step"
            )
        if not op.writable:
            return Decision.deny(
                f"operation {op.id!r} is not enabled for writes",
                askable=op.access is not Access.READ,
                entry=entry,
                operation=op,
            )
        suspended = self.observations.suspended(entry, op)
        if suspended:
            return Decision.deny(f"operation {op.id!r} is suspended until reviewed: {suspended}")
        if entry.write_auth is None:
            return Decision.deny(
                f"API {entry.id!r} has no write credential configured",
                askable=True,
                entry=entry,
                operation=op,
            )
        if request.body is not None and not op.accepts_body:
            return Decision.deny(f"operation {op.id!r} does not accept a request body")
        if _not_a_form(op, request.body):
            return Decision.deny(_not_a_form(op, request.body))
        problem = _check_args(op, request.args, unresolved)
        if problem:
            return Decision.deny(problem)
        return Decision(True, "write may be requested", entry, op)


def _not_a_form(op: Operation, body: object) -> str:
    """Why a body cannot be sent to an operation that takes a form; empty where it can."""
    if body is None or op.body is None or op.body.encoding != "form" or isinstance(body, dict):
        return ""
    return f"operation {op.id!r} takes its body as a form: give an object of its fields, not a list"


def arguments(op: Operation) -> list[str]:
    """An operation's arguments as an agent is shown them: `key`, and `team?` for one
    that may be left out."""
    return [p.name if p.required else f"{p.name}?" for p in op.parameters]


def takes(op: Operation) -> str:
    """What an operation takes, said where an agent got it wrong."""
    named = arguments(op)
    parts = ([", ".join(named)] if named else []) + (["a body"] if op.accepts_body else [])
    if not parts:
        return "It takes no arguments."
    optional = any(not p.required for p in op.parameters)
    note = " (a ? marks one that may be left out)" if optional else ""
    return f"It takes: {' and '.join(parts)}{note}."


def _plain(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _meant(asked: str, known: list[str]) -> str:
    """The operation or API an agent probably meant: the same name written another way
    (`add_comment` for `addComment`, `GitHub` for `github`), among the ones it is shown
    anyway."""
    same = [name for name in known if _plain(name) == _plain(asked)]
    return f". Did you mean {same[0]!r}?" if same else ""


def _check_args(
    op: Operation, args: dict[str, ArgValue], unresolved: frozenset[str] = frozenset()
) -> str | None:
    declared = {p.name: p for p in op.parameters}
    unknown = sorted((set(args) | unresolved) - set(declared))
    if unknown:
        return f"unknown arguments for {op.id!r}: {', '.join(unknown)}. {takes(op)}"
    for param in op.parameters:
        if param.required and param.name not in args and param.name not in unresolved:
            return f"missing required argument {param.name!r} for {op.id!r}. {takes(op)}"
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
            if param.allow_slash:
                problem = path_value_problem(text)
                if problem:
                    return f"path argument {name!r} {problem}"
            elif not text or text in (".", "..") or "/" in text:
                hint = (
                    "; this parameter does not allow '/' (an approver can allow slashes in "
                    "path-like parameters from the console)"
                    if "/" in text
                    else ""
                )
                return f"path argument {name!r} is not a valid path segment{hint}"
        if param.location is ParamLocation.HEADER and name.lower() in _FORBIDDEN_HEADERS:
            return f"header argument {name!r} is not allowed"
    return None


def path_value_problem(text: str) -> str | None:
    """Why a multi-segment path value (a file path, a git ref) is unsafe, or None."""
    if not text:
        return "is empty"
    if text.startswith("/"):
        return "must not start with '/'"
    if text.endswith("/"):
        return "must not end with '/'"
    if "\\" in text:
        return "contains a backslash"
    if "%2f" in text.lower():
        return "contains an encoded slash"
    if any(segment in ("", ".", "..") for segment in text.split("/")):
        return "contains an empty, '.' or '..' segment"
    return None


_FORBIDDEN_HEADERS = frozenset(
    {"authorization", "cookie", "host", "proxy-authorization", "x-api-key"}
)
