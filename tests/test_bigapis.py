"""Big APIs: operation search, discovery/glossary, and recipes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from librarian.audit import MemoryAuditSink
from librarian.calls import Caller
from librarian.opsearch import rank, tokens
from librarian.planner import Planner
from librarian.registry import Access, ApiEntry
from librarian.secrets import FileSecretStore
from librarian.service import Librarian
from tests.conftest import BASE, TRACKER, Router
from tests.test_planner import scripted, tool_results

A = Caller(agent="agent-a")


def big_entry() -> ApiEntry:
    ops: list[dict[str, Any]] = [
        {
            "id": f"getThing{i}",
            "method": "GET",
            "path": f"/things/{i}",
            "access": "read",
            "summary": f"Thing number {i}",
            "tags": ["Things"],
        }
        for i in range(300)
    ]
    ops += [
        {
            "id": "getIssueWatchers",
            "method": "GET",
            "path": "/issue/{key}/watchers",
            "access": "read",
            "summary": "Returns the watchers for an issue.",
            "tags": ["Issue watchers"],
            "parameters": [{"name": "key", "in": "path"}],
        },
        {
            "id": "getComments",
            "method": "GET",
            "path": "/issue/{key}/comment",
            "access": "read",
            "summary": "Returns all comments for an issue.",
            "tags": ["Issue comments"],
            "parameters": [{"name": "key", "in": "path"}],
        },
    ]
    return ApiEntry.model_validate(
        {**TRACKER, "id": "big", "operations": ops, "credential": {}, "write_auth": None}
    )


def test_tokens_split_ids_and_stem() -> None:
    assert tokens("getIssueWatchers") == ["issue", "watch"]
    assert tokens("who is watching an issue") == ["who", "watch", "issue"]
    assert tokens("/rest/api/3/issue/{issueIdOrKey}") == [
        "rest",
        "api",
        "issue",
        "issue",
        "id",
        "key",
    ]


def test_ranking_finds_the_needle_in_300_operations() -> None:
    entry = big_entry()
    reads = [op for op in entry.operations if op.access is Access.READ]
    assert rank(entry, "who is watching an issue", reads, limit=1)[0].id == "getIssueWatchers"
    assert rank(entry, "comments on PAY-1", reads, limit=1)[0].id == "getComments"
    assert len(rank(entry, "", reads, limit=15)) == 15  # no query: first N


async def test_describe_api_ranks_and_reports_totals(librarian: Librarian) -> None:
    await librarian.registry.save(big_entry())
    model, seen = scripted(
        [
            ToolCallPart("describe_api", {"api": "big"}),
            ToolCallPart("describe_api", {"api": "big", "filter_text": "issue watchers"}),
        ]
    )
    await Planner(model).fetch("x", librarian.registry, librarian.executor, A)
    unfiltered, filtered = tool_results(seen[-1])[:2]
    unfiltered_content: Any = unfiltered.content  # type: ignore[attr-defined]
    filtered_content: Any = filtered.content  # type: ignore[attr-defined]
    assert unfiltered_content["total_operations"] == 302 and "hint" in unfiltered_content
    assert filtered_content["operations"][0]["operation"] == "getIssueWatchers"


async def test_capabilities_query_ranks_operations(librarian: Librarian) -> None:
    await librarian.registry.save(big_entry())
    caps = librarian.capabilities("issue comments")
    big = next(c for c in caps if c["api"] == "big")
    assert big["read_operations"][0]["operation"] == "getComments"
    assert big["total_read_operations"] == 302


# -- discovery / glossary --------------------------------------------------------------------


def test_discovery_must_use_a_read() -> None:
    with pytest.raises(ValidationError, match="discovery operation"):
        ApiEntry.model_validate({**TRACKER, "discovery": [{"operation": "deleteIssue"}]})


async def with_discovery(librarian: Librarian, router: Router) -> None:
    entry = librarian.registry.get("tracker")
    assert entry is not None
    data = entry.model_dump(mode="json", by_alias=True)
    data["operations"].append(
        {"id": "getFields", "method": "GET", "path": "/field", "access": "read"}
    )
    data["discovery"] = [
        {"operation": "getFields", "key": "id", "value": "name", "key_prefix": "customfield_"}
    ]
    await librarian.registry.save(ApiEntry.model_validate(data))
    router.add(
        "GET",
        f"{BASE}/v1/field",
        lambda r: httpx.Response(
            200,
            json=[
                {"id": "summary", "name": "Summary"},
                {"id": "customfield_10020", "name": "Sprint"},
                {"id": "customfield_10016", "name": "Story point estimate"},
            ],
        ),
    )


async def test_discovery_builds_a_glossary_the_planner_sees(
    librarian: Librarian, router: Router, secrets_store: FileSecretStore
) -> None:
    await secrets_store.put("tracker/readonly", "tok")
    await with_discovery(librarian, router)
    assert await librarian.discover("tracker") == 2
    entry = librarian.registry.get("tracker")
    assert entry is not None and entry.glossary == {
        "customfield_10020": "Sprint",
        "customfield_10016": "Story point estimate",
    }
    model, seen = scripted(
        [ToolCallPart("describe_api", {"api": "tracker", "filter_text": "sprint"})]
    )
    await Planner(model).fetch("x", librarian.registry, librarian.executor, A)
    content: Any = tool_results(seen[-1])[0].content  # type: ignore[attr-defined]
    assert content["glossary"] == {"customfield_10020": "Sprint"}


async def test_discovery_runs_after_a_credential_is_approved(
    librarian: Librarian, router: Router
) -> None:
    await with_discovery(librarian, router)
    router.add("GET", f"{BASE}/v1/me", lambda r: httpx.Response(200, json={}))
    entry = librarian.registry.get("tracker")
    assert entry is not None
    request = await librarian.broker.open(entry, A, "missing")
    assert await librarian.broker.provide_value(request.id, "tok", "sam") is None
    for _ in range(100):
        updated = librarian.registry.get("tracker")
        if updated is not None and updated.glossary:
            break
        await asyncio.sleep(0.02)
    assert librarian.registry.get("tracker").glossary  # type: ignore[union-attr]


# -- recipes ------------------------------------------------------------------------------------


@pytest.fixture
async def issue_api(secrets_store: FileSecretStore, router: Router) -> None:
    await secrets_store.put("tracker/readonly", "tok")
    for key in ("PAY-1", "PAY-7"):
        router.add(
            "GET",
            f"{BASE}/v1/issues/{key}",
            lambda r, k=key: httpx.Response(200, json={"key": k, "state": "open"}),
        )


async def test_successful_plan_becomes_a_recipe_replayed_without_llm(
    librarian: Librarian, router: Router, issue_api: None, audit: MemoryAuditSink
) -> None:
    model, _ = scripted(
        [
            ToolCallPart(
                "call_api", {"api": "tracker", "operation": "getIssue", "args": {"key": "PAY-1"}}
            )
        ]
    )
    librarian.planner = Planner(model)
    first = await librarian.fetch("Is PAY-1 open?", A)
    assert first.recipe is None and len(librarian.recipes) == 1

    def no_llm(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise AssertionError("the model must not be called for a recipe")

    librarian.planner = Planner(FunctionModel(no_llm))
    again = await librarian.fetch("is PAY-7 open", A)
    assert again.recipe is not None and again.results[0].data == {"key": "PAY-7", "state": "open"}
    assert next(iter(librarian.recipes.values())).uses == 1
    # Each plain-language request leaves one audit event saying whether a model was involved.
    fetches = [ev for ev in audit.events if ev.event == "fetch"]
    assert [ev.detail for ev in fetches] == ["planner", "recipe"]
    assert fetches[0].extra["calls"] == 1 and fetches[1].extra["recipe"] == again.recipe

    # Replays still obey the caller's permissions: this agent may not use the tracker.
    with pytest.raises(AssertionError, match="must not be called"):
        await librarian.fetch("is PAY-7 open", Caller(agent="other", apis=("weather",)))


async def test_plans_using_earlier_results_are_not_saved(
    librarian: Librarian, router: Router, issue_api: None
) -> None:
    router.add("GET", f"{BASE}/v1/issues", lambda r: httpx.Response(200, json=[]))
    model, _ = scripted(
        [
            ToolCallPart(
                "call_api", {"api": "tracker", "operation": "getIssue", "args": {"key": "PAY-1"}}
            ),
            # "open" came from the first result, not from the request.
            ToolCallPart(
                "call_api", {"api": "tracker", "operation": "listIssues", "args": {"state": "open"}}
            ),
        ]
    )
    librarian.planner = Planner(model)
    await librarian.fetch("related to PAY-1", A)
    assert librarian.recipes == {}


async def test_recipes_survive_restart(tmp_path: Path, router: Router) -> None:
    from tests.test_state import replica

    router.add("GET", f"{BASE}/v1/issues/PAY-1", lambda r: httpx.Response(200, json={}))
    first = replica(tmp_path, router)
    await first.start()
    await first.secrets.put("tracker/readonly", "tok")
    model, _ = scripted(
        [
            ToolCallPart(
                "call_api", {"api": "tracker", "operation": "getIssue", "args": {"key": "PAY-1"}}
            )
        ]
    )
    first.planner = Planner(model)
    await first.fetch("Is PAY-1 open?", A)
    await first.aclose()
    second = replica(tmp_path, router)
    await second.start()
    assert [r.template for r in second.recipes.values()] == ["Is {p0} open"]
    await second.delete_recipe(next(iter(second.recipes)))
    await second.aclose()
    third = replica(tmp_path, router)
    await third.start()
    assert third.recipes == {}
    await third.aclose()


async def test_planner_running_out_of_steps_is_a_clear_error(
    librarian: Librarian, router: Router, issue_api: None
) -> None:
    from librarian.service import LibrarianError

    def loop_forever(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart("search_apis", {"query": "issues"})])

    librarian.planner = Planner(FunctionModel(loop_forever), max_calls=2)
    with pytest.raises(LibrarianError, match="step budget"):
        await librarian.fetch("anything at all", A)
