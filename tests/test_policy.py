from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from librarian.audit import MemoryAuditSink
from librarian.calls import ArgValue, Caller, CallRequest, WriteRequest
from librarian.executor import PolicyDenied
from librarian.policy import PolicyEngine, RateLimiter, takes
from librarian.registry import ApiEntry, Operation, Parameter, ParamLocation, Registry
from librarian.secrets import FileSecretStore
from librarian.service import Librarian
from tests.conftest import BASE, OPEN, TRACKER, Router, write_entries

CALLER = Caller(agent="agent-a")


@pytest.fixture
def engine(tmp_path: Path) -> PolicyEngine:
    write_entries(tmp_path, TRACKER, OPEN, {**TRACKER, "id": "blocked", "status": "blocked"})
    registry = Registry(tmp_path)
    registry.load()
    return PolicyEngine(registry, rate_limit_per_minute=1000)


def req(api: str = "tracker", op: str = "getIssue", **kwargs: Any) -> CallRequest:
    args: dict[str, ArgValue] = kwargs.pop("args", {"key": "ABC-1"})
    return CallRequest(api=api, operation=op, args=args, **kwargs)


def test_allows_declared_read(engine: PolicyEngine) -> None:
    decision = engine.check(req(), CALLER)
    assert decision.allowed, decision.reason


@pytest.mark.parametrize(
    ("request_", "reason"),
    [
        (req(api="nope"), "unknown API"),
        (req(op="nope"), "unknown operation"),
        (req(op="deleteIssue"), "not a read"),
        (req(api="blocked"), "blocked"),
        (req(args={}), "missing required"),
        (req(args={"key": "A", "extra": "x"}), "unknown arguments"),
        (req(args={"key": "../admin"}), "path segment"),
        (req(args={"key": ".."}), "path segment"),
        (req(args={"key": ["a", "b"]}), "single value"),
        (req(args={"key": "a\r\nX-Injected: 1"}), "control characters"),
        (req(op="listIssues", args={"X-Trace": "1", "team": "a"}), "allowed"),
        (req(body={"q": 1}), "does not accept a request body"),
    ],
)
def test_denials(engine: PolicyEngine, request_: CallRequest, reason: str) -> None:
    decision = engine.check(request_, CALLER)
    if reason == "allowed":
        assert decision.allowed, decision.reason
    else:
        assert not decision.allowed
        assert reason in decision.reason


@pytest.mark.parametrize(
    ("named", "body", "said"),
    [
        ([("key", True)], False, "It takes: key."),
        ([("key", True)], True, "It takes: key and a body."),
        (
            [("key", True), ("team", False)],
            False,
            "It takes: key, team? (a ? marks one that may be left out).",
        ),
        (
            [("key", True), ("team", False)],
            True,
            "It takes: key, team? and a body (a ? marks one that may be left out).",
        ),
        ([], True, "It takes: a body."),
        ([], False, "It takes no arguments."),
    ],
)
def test_what_an_operation_takes_is_said_as_it_is(
    named: list[tuple[str, bool]], body: bool, said: str
) -> None:
    """The sentence a refused agent reads next. It explains the ? only where there is
    one, and an operation that takes nothing is not said to take "no arguments (a ? ...)"."""
    op = Operation(
        id="op",
        method="POST" if body else "GET",
        path="/things",
        parameters=[
            Parameter.model_validate(
                {"name": name, "in": ParamLocation.QUERY, "required": required}
            )
            for name, required in named
        ],
        accepts_body=body,
    )
    assert takes(op) == said


def test_an_api_named_another_way_is_answered_with_the_one_that_exists(
    engine: PolicyEngine,
) -> None:
    """...among the APIs that caller may use anyway: one it may not use, or that is
    blocked, is never the answer."""

    def refused(api: str, caller: Caller = CALLER) -> str:
        decision = engine.check(req(api=api), caller)
        assert not decision.allowed
        return decision.reason

    assert refused("Tracker") == "unknown API 'Tracker'. Did you mean 'tracker'?"
    assert refused("TRACKER") == "unknown API 'TRACKER'. Did you mean 'tracker'?"
    assert refused("track") == "unknown API 'track'"  # another name, not another spelling
    assert refused("Blocked") == "unknown API 'Blocked'"
    elsewhere = Caller(agent="agent-b", apis=("status",))
    assert refused("Tracker", elsewhere) == ("caller 'agent-b' is not allowed to use API 'Tracker'")
    assert refused("STATUS", elsewhere) == (
        "caller 'agent-b' is not allowed to use API 'STATUS'. Did you mean 'status'?"
    )
    # The same where a write names the API: among the ones that caller may write to.
    write = WriteRequest(api="Tracker", operation="addComment", args={"key": "A-1"}, reason="x")
    writer = Caller(agent="agent-w", write_apis=("tracker",))
    assert engine.check_write(write, writer).reason == (
        "caller 'agent-w' may not write to API 'Tracker'. Did you mean 'tracker'?"
    )
    anywhere = Caller(agent="agent-x", write_apis=("*",))
    assert engine.check_write(write, anywhere).reason == (
        "unknown API 'Tracker'. Did you mean 'tracker'?"
    )
    assert engine.check_write(write, CALLER).reason == (
        "caller 'agent-a' may not write to API 'Tracker'"  # it may write nowhere
    )


def test_reviewed_read_requires_verified_entry(tmp_path: Path) -> None:
    write_entries(tmp_path, {**TRACKER, "status": "provisional"})
    registry = Registry(tmp_path)
    registry.load()
    engine = PolicyEngine(registry)
    decision = engine.check(req(op="searchIssues", args={}, body={"jql": "x"}), CALLER)
    assert not decision.allowed
    assert "not verified" in decision.reason


def test_reviewed_read_allowed_when_verified(engine: PolicyEngine) -> None:
    decision = engine.check(req(op="searchIssues", args={}, body={"jql": "x"}), CALLER)
    assert decision.allowed, decision.reason


def test_provisional_can_be_switched_off(tmp_path: Path) -> None:
    write_entries(tmp_path, OPEN)
    registry = Registry(tmp_path)
    registry.load()
    engine = PolicyEngine(registry, allow_provisional=False)
    decision = engine.check(CallRequest(api="status", operation="summary"), CALLER)
    assert not decision.allowed


def test_host_allowlist(engine: PolicyEngine) -> None:
    engine.allowed_hosts = frozenset({"other.test"})
    assert not engine.check(req(), CALLER).allowed


def test_forbidden_header_args(tmp_path: Path) -> None:
    ops = [
        {
            "id": "x",
            "method": "GET",
            "path": "/x",
            "access": "read",
            "parameters": [{"name": "Authorization", "in": "header"}],
        }
    ]
    write_entries(tmp_path, {**OPEN, "operations": ops})
    registry = Registry(tmp_path)
    registry.load()
    decision = PolicyEngine(registry).check(
        CallRequest(api="status", operation="x", args={"Authorization": "Bearer stolen"}), CALLER
    )
    assert not decision.allowed
    assert "not allowed" in decision.reason


def test_rate_limiter_window() -> None:
    limiter = RateLimiter(per_minute=2)
    assert limiter.allow("a", "api", now=0)
    assert limiter.allow("a", "api", now=1)
    assert not limiter.allow("a", "api", now=2)
    assert limiter.allow("b", "api", now=2)  # separate bucket per caller
    assert limiter.allow("a", "api", now=61)


def test_caller_allowlist_globs(engine: PolicyEngine) -> None:
    restricted = Caller(agent="notes-bot", apis=("status", "track*"))
    assert engine.check(req(), restricted).allowed
    other = Caller(agent="hr-bot", apis=("weather",))
    decision = engine.check(req(), other)
    assert not decision.allowed
    assert "not allowed to use API 'tracker'" in decision.reason
    assert not engine.check(req(), Caller(agent="nobody", apis=())).allowed


async def test_shared_rate_limit_is_one_limit_across_replicas(tmp_path: Path) -> None:
    from librarian.policy import SharedRateLimiter
    from librarian.state import StateStore, make_engine

    store = StateStore(make_engine(f"sqlite+aiosqlite:///{tmp_path}/shared.db"))
    await store.initialize()
    replica_a = SharedRateLimiter(2, store, "read")
    replica_b = SharedRateLimiter(2, store, "read")
    assert await replica_a.allow("agent", "api", now=0)
    assert await replica_b.allow("agent", "api", now=1)  # the other replica counts too
    assert not await replica_a.allow("agent", "api", now=2)  # 3rd hit in the minute, anywhere
    assert not await replica_b.allow("agent", "api", now=3)
    assert await replica_b.allow("other-agent", "api", now=3)  # separate bucket per caller
    assert await replica_a.allow("agent", "other-api", now=3)  # and per API
    assert await replica_a.allow("agent", "api", now=61)  # a new minute
    writes = SharedRateLimiter(1, store, "write")
    assert await writes.allow("agent", "api", now=61)  # reads and writes count apart
    assert not await writes.allow("agent", "api", now=62)
    await store.prune_rate_limits(before_window=1)  # drops the first minute's rows
    assert await replica_a.allow("agent", "api", now=62)  # still within 2 in minute 1
    assert not await replica_a.allow("agent", "api", now=63)
    engine = PolicyEngine(
        Registry(tmp_path / "registry"), rate_limit_per_minute=1, shared_limits=store
    )
    entry = ApiEntry.model_validate(
        {"id": "x", "name": "X", "base_url": "https://x.test", "operations": []}
    )
    assert await engine.rate_limited(CALLER, entry) is None
    denied = await engine.rate_limited(CALLER, entry)
    assert denied and "rate limit exceeded" in denied
    assert (
        await engine.rate_limited(CALLER, entry, write=True) is None
    )  # the write limit is its own
    await store.engine.dispose()


async def test_rate_limit_denial_is_audited(
    librarian: Librarian, audit: MemoryAuditSink, router: Router, secrets_store: FileSecretStore
) -> None:
    await secrets_store.put("tracker/readonly", "k")
    router.add("GET", f"{BASE}/v1/me", lambda r: httpx.Response(200, json={}))
    librarian.policy.rate_limiter = RateLimiter(1)
    request = CallRequest(api="tracker", operation="whoami")
    assert (await librarian.executor.execute(request, CALLER)).ok
    with pytest.raises(PolicyDenied, match="rate limit exceeded"):
        await librarian.executor.execute(request, CALLER)
    assert audit.events[-1].event == "denied" and "rate limit" in (audit.events[-1].detail or "")
