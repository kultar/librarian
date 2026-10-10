from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from librarian.calls import ArgValue, Caller, CallRequest
from librarian.policy import PolicyEngine, RateLimiter
from librarian.registry import Registry
from tests.conftest import OPEN, TRACKER, write_entries

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
