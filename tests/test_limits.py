"""Limits inside an API: an agent that may use an API may be held to some of its
operations, and to some values of its arguments ("GitHub, but only acme's payments
repositories"). Decided by the policy engine, for reads, writes and planned calls."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from pydantic_ai.messages import RetryPromptPart, ToolCallPart, ToolReturnPart

from librarian.audit import MemoryAuditSink
from librarian.calls import Caller, CallRequest, Limits, WriteRequest
from librarian.config import ActorMatch, ActorRule, CallerKey, Settings
from librarian.executor import PolicyDenied
from librarian.planner import Planner
from librarian.secrets import FileSecretStore
from librarian.server import create_app
from librarian.service import Librarian
from tests.conftest import BASE, RecordingNotifier, Router
from tests.test_actors import FakeSubstrate, authenticator
from tests.test_planner import scripted, tool_results
from tests.test_server import KEYS, send

PAYMENTS = Limits(arguments={"key": ("PAY-*",), "team": ("payments", "billing")})


def agent(limits: Limits = PAYMENTS, **more: Any) -> Caller:
    return Caller(agent="pay-bot", write_apis=("tracker",), limits={"tracker": limits}, **more)


def read(operation: str, **args: Any) -> CallRequest:
    return CallRequest(api="tracker", operation=operation, args=args)


def write(operation: str, **args: Any) -> WriteRequest:
    return WriteRequest(api="tracker", operation=operation, args=args, body=None)


@pytest.fixture(autouse=True)
async def tracker(secrets_store: FileSecretStore, router: Router) -> None:
    await secrets_store.put("tracker/readonly", "read-key-0123456789")
    await secrets_store.put("tracker/write", "write-key-0123456789")
    for path in ("/v1/issues/PAY-1", "/v1/issues", "/v1/me"):
        router.add("GET", BASE + path, lambda r: httpx.Response(200, json={"ok": True}))
    router.add(
        "POST", f"{BASE}/v1/issues/PAY-1/comments", lambda r: httpx.Response(201, json={"id": 7})
    )


def why(librarian: Librarian, request: CallRequest, caller: Caller) -> str:
    decision = librarian.policy.check(request, caller)
    return "" if decision.allowed else decision.reason


def test_an_argument_may_only_take_the_values_the_limit_names(librarian: Librarian) -> None:
    pay = agent()
    assert why(librarian, read("getIssue", key="PAY-1"), pay) == ""
    assert why(librarian, read("getIssue", key="OPS-9"), pay) == (
        "caller 'pay-bot' is limited on API 'tracker': key='OPS-9' is outside what it may "
        "use (PAY-*)"
    )
    assert "outside what it may use" in why(librarian, read("getIssue", key="pay-1"), pay)
    assert why(librarian, read("listIssues", team="billing"), pay) == ""
    assert "team='growth' is outside" in why(librarian, read("listIssues", team="growth"), pay)
    # A list of values: every one of them has to be allowed.
    assert why(librarian, read("listIssues", team=["payments", "billing"]), pay) == ""
    assert "team='growth'" in why(librarian, read("listIssues", team=["payments", "growth"]), pay)
    # Another agent, and another API, are not touched by it.
    assert why(librarian, read("getIssue", key="OPS-9"), Caller(agent="ops-bot")) == ""
    weather = CallRequest(api="weather", operation="forecast", args={"city": "Oslo"})
    assert librarian.policy.check(weather, pay).allowed


def test_a_limited_argument_cannot_be_left_out(librarian: Librarian) -> None:
    """`team` is optional for the API: without it the answer covers every team."""
    assert why(librarian, read("listIssues"), agent()) == (
        "caller 'pay-bot' is limited on API 'tracker': the argument 'team' must be given: "
        "its value is limited"
    )
    assert why(librarian, read("listIssues", state="open"), agent()).endswith("is limited")


def test_an_operation_that_takes_no_limited_argument_is_refused_unless_named(
    librarian: Librarian,
) -> None:
    """`whoami` takes neither `key` nor `team`: there is nothing to hold it to."""
    assert why(librarian, read("whoami"), agent()) == (
        "caller 'pay-bot' is limited on API 'tracker': it is limited there to calls that "
        "name 'key' or 'team', and the operation 'whoami' takes no such argument"
    )
    named = Limits(arguments={"key": ("PAY-*",)}, allow_without=("who*",))
    assert why(librarian, read("whoami"), agent(named)) == ""
    assert "takes no such argument" in why(librarian, read("listIssues", team="x"), agent(named))


def test_an_agent_may_be_held_to_some_operations(librarian: Librarian) -> None:
    only = Limits(operations=("get*", "whoami"))
    assert why(librarian, read("getIssue", key="OPS-9"), agent(only)) == ""
    assert why(librarian, read("whoami"), agent(only)) == ""
    assert why(librarian, read("listIssues"), agent(only)) == (
        "caller 'pay-bot' is limited on API 'tracker': it may not use the operation "
        "'listIssues' there"
    )
    both = Limits(operations=("get*",), arguments={"key": ("PAY-*",)})
    assert why(librarian, read("getIssue", key="PAY-1"), agent(both)) == ""
    assert "outside what it may use" in why(librarian, read("getIssue", key="OPS-9"), agent(both))
    # Limits are found by API id, as a pattern; the first that matches counts.
    patterned = Caller(agent="pay-bot", limits={"track*": only, "*": PAYMENTS})
    assert why(librarian, read("getIssue", key="OPS-9"), patterned) == ""


async def test_a_refused_read_sends_nothing_and_is_on_record(
    librarian: Librarian, router: Router, audit: MemoryAuditSink
) -> None:
    with pytest.raises(PolicyDenied, match="key='OPS-9' is outside"):
        await librarian.execute(read("getIssue", key="OPS-9"), agent())
    assert router.requests == []
    assert (audit.events[-1].event, audit.events[-1].caller) == ("denied", "pay-bot")
    assert "limited on API 'tracker'" in (audit.events[-1].detail or "")
    result = await librarian.execute(read("getIssue", key="PAY-1"), agent())
    assert result.ok and len(router.requests) == 1


async def test_a_write_outside_the_limits_is_never_put_to_a_person(
    librarian: Librarian, notifier: RecordingNotifier
) -> None:
    pay = agent(owner="leads@x.test")
    with pytest.raises(PolicyDenied, match="key='OPS-1' is outside"):
        await librarian.write(write("addComment", key="OPS-1"), pay)
    assert librarian.approvals.pending() == [] and notifier.write_events == []
    # An operation nobody has enabled yet would normally be put to the approvers: not
    # when the agent could not use it anyway.
    with pytest.raises(PolicyDenied, match="'renameProject' takes no such argument"):
        await librarian.write(write("renameProject", p="x"), pay)
    assert librarian.enablement.pending() == [] and notifier.enables == []
    # Inside the limits, the write is asked for as always.
    task = asyncio.create_task(librarian.write(write("addComment", key="PAY-1"), pay))
    while not librarian.approvals.pending():
        await asyncio.sleep(0.01)
    assert librarian.approvals.pending()[0].request.args == {"key": "PAY-1"}
    task.cancel()


async def test_a_standing_grant_does_not_reach_past_the_limits(librarian: Librarian) -> None:
    free = Caller(agent="pay-bot", write_apis=("tracker",), owner="leads@x.test")
    task = asyncio.create_task(librarian.write(write("addComment", key="PAY-1"), free))
    while not librarian.approvals.pending():
        await asyncio.sleep(0.01)
    approval = librarian.approvals.pending()[0]
    await librarian.approvals.approve(approval.id, "leads@x.test", remember=True)  # any key
    await asyncio.wait_for(task, 5)
    assert librarian.approvals.find_grant(write("addComment", key="OPS-1"), free) is not None
    with pytest.raises(PolicyDenied, match="is outside what it may use"):
        await librarian.write(write("addComment", key="OPS-1"), agent(owner="leads@x.test"))


def test_an_agent_is_shown_what_it_can_reach_and_told_its_limits(librarian: Librarian) -> None:
    [seen] = [api for api in librarian.capabilities(None, agent()) if api["api"] == "tracker"]
    assert [op["operation"] for op in seen["read_operations"]] == ["getIssue", "listIssues"]
    assert seen["total_read_operations"] == 2  # not whoami, not the search
    assert seen["limits"]["arguments"] == {"key": ["PAY-*"], "team": ["payments", "billing"]}
    # The same for what it may ask to change: not the operation its limits cannot hold.
    assert [op["operation"] for op in seen["write_operations"]] == ["deleteIssue", "addComment"]
    assert seen["total_write_operations"] == 2
    [free] = [a for a in librarian.capabilities(None, Caller(agent="x")) if a["api"] == "tracker"]
    assert "limits" not in free and free["total_read_operations"] == 4
    assert "write_operations" not in free  # it may not ask for writes there at all
    writer = Caller(agent="y", write_apis=("tracker",))
    [all_of_it] = [a for a in librarian.capabilities(None, writer) if a["api"] == "tracker"]
    assert {op["operation"]: op["enabled"] for op in all_of_it["write_operations"]} == {
        "deleteIssue": True,
        "addComment": True,
        "renameProject": False,  # nobody has allowed writes through it yet
    }


async def test_the_planner_sees_the_limits_and_cannot_plan_around_them(
    librarian: Librarian, router: Router
) -> None:
    model, seen = scripted(
        [
            ToolCallPart("describe_api", {"api": "tracker"}),
            ToolCallPart(
                "call_api", {"api": "tracker", "operation": "getIssue", "args": {"key": "OPS-9"}}
            ),
            ToolCallPart(
                "call_api", {"api": "tracker", "operation": "getIssue", "args": {"key": "PAY-1"}}
            ),
        ]
    )
    outcome = await Planner(model).fetch(
        "anything", librarian.registry, librarian.executor, agent()
    )
    described, refused = (tool_results(messages)[-1] for messages in seen[1:3])
    assert isinstance(described, ToolReturnPart)
    listed: Any = described.content
    assert [op["operation"] for op in listed["operations"]] == [
        "getIssue",
        "listIssues",
        "deleteIssue",
        "addComment",
    ]
    assert listed["limits"]["arguments"]["key"] == ["PAY-*"]
    assert isinstance(refused, RetryPromptPart)
    assert "refused by policy: caller 'pay-bot' is limited" in str(refused.content)
    assert [r.provenance.operation for r in outcome.results] == ["getIssue"]
    assert [str(r.url) for r in router.requests] == [f"{BASE}/v1/issues/PAY-1"]


def test_what_a_limit_may_say() -> None:
    for bad in (
        {},
        {"allow_without": ["whoami"]},
        {"arguments": {"key": []}},
        {"arguments": {"": ["x"]}},
        {"operations": [""]},
        {"arguments": {"key": ["PAY-*"]}, "colour": "blue"},
    ):
        with pytest.raises(ValidationError):
            Limits.model_validate(bad)
    said = Limits.model_validate({"operations": ["get*"], "arguments": {"key": ["PAY-*"]}})
    assert said.told()["operations"] == ["get*"] and "note" in said.told()


async def test_limits_come_from_the_configuration_of_each_agent(
    librarian: Librarian, router: Router, tmp_path: Any
) -> None:
    config = tmp_path / "librarian.yaml"
    config.write_text(
        "callers:\n"
        "  - agent: agent-a\n"
        "    key_ref: librarian/callers/agent-a\n"
        "    limits:\n"
        "      tracker:\n"
        "        arguments: {key: ['PAY-*']}\n"
        "        allow_without: [whoami]\n",
        "utf-8",
    )
    librarian.settings.callers = Settings.load(config).callers
    assert librarian.settings.callers[0].limits["tracker"].arguments == {"key": ("PAY-*",)}
    with pytest.raises(ValidationError, match="names `operations` or `arguments`"):
        CallerKey(agent="x", key_ref="librarian/callers/x", limits={"tracker": {}})  # type: ignore[dict-item]

    app = create_app(librarian, caller_keys=KEYS)
    app.state.caller_keys.update(KEYS)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=librarian.settings.public_url,
        headers={"Authorization": "Bearer key-a"},
    ) as http:
        from a2a.helpers.proto_helpers import new_data_part
        from a2a.types import TaskState

        def command(key: str) -> list[Any]:
            call = {"skill": "execute", "api": "tracker", "operation": "getIssue"}
            return [new_data_part({**call, "args": {"key": key}})]

        inside = await send(librarian, http, command("PAY-1"))
        assert inside.final == TaskState.TASK_STATE_COMPLETED
        outside = await send(librarian, http, command("OPS-9"))
        assert outside.final == TaskState.TASK_STATE_REJECTED
        assert "Refused: caller 'agent-a' is limited on API 'tracker'" in outside.texts[-1]
    assert [str(r.url) for r in router.requests] == [f"{BASE}/v1/issues/PAY-1"]


async def test_limits_come_from_the_rule_an_agent_on_substrate_matches() -> None:
    substrate = FakeSubstrate()
    rule = ActorRule(
        match=ActorMatch(namespace="payments"),
        apis=["tracker"],
        limits={"tracker": Limits(arguments={"key": ("PAY-*",)})},
    )
    caller = await authenticator(substrate, rules=[rule]).caller(substrate.token())
    assert caller.agent == "payments/release-notes"
    assert caller.limits_for("tracker") == Limits(arguments={"key": ("PAY-*",)})
    assert caller.limits_for("weather") is None
