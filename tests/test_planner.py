"""The fetch planner, driven by a scripted model (no real LLM)."""

from __future__ import annotations

import httpx
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from librarian.calls import Caller
from librarian.planner import Planner
from librarian.secrets import FileSecretStore
from librarian.service import Librarian
from tests.conftest import BASE, Router


def scripted(steps: list[ToolCallPart]) -> tuple[FunctionModel, list[list[ModelMessage]]]:
    seen: list[list[ModelMessage]] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(list(messages))
        index = len(seen) - 1
        if index < len(steps):
            return ModelResponse(parts=[steps[index]])
        output_tool = info.output_tools[0].name
        return ModelResponse(parts=[ToolCallPart(output_tool, {"summary": "Issue ABC-1 is open."})])

    return FunctionModel(respond), seen


def tool_results(messages: list[ModelMessage]) -> list[object]:
    out: list[object] = []
    for message in messages:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, ToolReturnPart | RetryPromptPart):
                    out.append(part)
    return out


async def test_planner_chains_tools_and_policy_still_applies(
    librarian: Librarian, router: Router, secrets_store: FileSecretStore
) -> None:
    await secrets_store.put("tracker/readonly", "tok")
    router.add(
        "GET", f"{BASE}/v1/issues/ABC-1", lambda r: httpx.Response(200, json={"state": "open"})
    )
    model, seen = scripted(
        [
            ToolCallPart("search_apis", {"query": "issue tracker"}),
            ToolCallPart("describe_api", {"api": "tracker", "filter_text": "issue"}),
            # The model tries a write: the policy engine refuses it.
            ToolCallPart(
                "call_api", {"api": "tracker", "operation": "deleteIssue", "args": {"key": "ABC-1"}}
            ),
            ToolCallPart(
                "call_api", {"api": "tracker", "operation": "getIssue", "args": {"key": "ABC-1"}}
            ),
        ]
    )
    planner = Planner(model)
    outcome = await planner.fetch(
        "Is ABC-1 open?", librarian.registry, librarian.executor, Caller(agent="agent-a")
    )

    assert outcome.answer.summary == "Issue ABC-1 is open."
    assert [r.provenance.operation for r in outcome.results] == ["getIssue"]
    assert outcome.results[0].data == {"state": "open"}
    # Only the read reached the network; the delete never left the librarian.
    assert [(r.method, r.url.path) for r in router.requests] == [("GET", "/v1/issues/ABC-1")]

    results = tool_results(seen[-1])
    search = results[0]
    assert isinstance(search, ToolReturnPart)
    assert "tracker" in str(search.content)
    describe = results[1]
    assert isinstance(describe, ToolReturnPart)
    assert "deleteIssue" not in str(describe.content)  # writes are never even shown
    refused = results[2]
    assert isinstance(refused, RetryPromptPart)
    assert "refused by policy" in str(refused.content)


async def test_service_fetch_uses_configured_model(
    librarian: Librarian, router: Router, secrets_store: FileSecretStore
) -> None:
    await secrets_store.put("tracker/readonly", "tok")
    router.add(
        "GET", f"{BASE}/v1/issues/ABC-1", lambda r: httpx.Response(200, json={"state": "open"})
    )
    model, _ = scripted(
        [
            ToolCallPart(
                "call_api", {"api": "tracker", "operation": "getIssue", "args": {"key": "ABC-1"}}
            )
        ]
    )
    librarian.planner = Planner(model)
    outcome = await librarian.fetch("Is ABC-1 open?", Caller(agent="agent-a"))
    assert outcome.results[0].ok


async def test_identical_calls_are_made_once(
    librarian: Librarian, router: Router, secrets_store: FileSecretStore
) -> None:
    await secrets_store.put("tracker/readonly", "tok")
    router.add(
        "GET", f"{BASE}/v1/issues/ABC-1", lambda r: httpx.Response(200, json={"state": "open"})
    )
    call = {"api": "tracker", "operation": "getIssue", "args": {"key": "ABC-1"}}
    model, _ = scripted([ToolCallPart("call_api", call), ToolCallPart("call_api", call)])
    outcome = await Planner(model).fetch(
        "Is ABC-1 open?", librarian.registry, librarian.executor, Caller(agent="agent-a")
    )
    assert len(router.requests) == 1
    assert len(outcome.results) == 1
