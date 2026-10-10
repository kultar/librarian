from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from librarian.calls import Caller, CallRequest
from librarian.registry import Pagination
from librarian.secrets import FileSecretStore
from librarian.service import Librarian
from librarian.shaping import get_path, select, set_path
from tests.conftest import BASE, Router

DOC: dict[str, Any] = {
    "total": 2,
    "issues": [
        {"key": "A-1", "fields": {"summary": "one", "status": {"name": "Open"}, "big": "x" * 50}},
        {"key": "A-2", "fields": {"summary": "two", "status": {"name": "Done"}, "big": "y"}},
    ],
}


def test_select_traverses_lists() -> None:
    assert select(DOC, ["issues.key", "issues.fields.status.name"]) == {
        "issues": [
            {"key": "A-1", "fields": {"status": {"name": "Open"}}},
            {"key": "A-2", "fields": {"status": {"name": "Done"}}},
        ]
    }
    assert select(DOC, ["issues[].key"]) == select(DOC, ["issues.key"])
    assert select(DOC, ["issues", "issues.key"]) == {"issues": DOC["issues"]}  # parent wins
    assert select([{"a": 1, "b": 2}], ["a"]) == [{"a": 1}]
    assert select(DOC, ["missing.path"]) == {}
    assert select(DOC, []) is DOC


def test_get_and_set_path() -> None:
    assert get_path(DOC, "total") == 2
    assert get_path(DOC, "issues.key") is None  # get_path does not traverse lists
    replaced = set_path(DOC, "issues", [])
    assert replaced["issues"] == [] and DOC["issues"]  # original untouched


def test_pagination_requires_style_fields() -> None:
    with pytest.raises(ValidationError, match="cursor_param"):
        Pagination(style="cursor")
    with pytest.raises(ValidationError, match="items_path"):
        Pagination(style="offset", offset_param="startAt")


@pytest.fixture(autouse=True)
async def secret(secrets_store: FileSecretStore) -> None:
    await secrets_store.put("tracker/readonly", "tok")


async def paginate(librarian: Librarian, pagination: dict[str, Any]) -> None:
    entry = librarian.registry.get("tracker")
    assert entry is not None
    await librarian.registry.save(
        entry.model_copy(update={"pagination": Pagination.model_validate(pagination)})
    )


async def test_cursor_pagination_merges_items(librarian: Librarian, router: Router) -> None:
    await paginate(
        librarian,
        {
            "style": "cursor",
            "cursor_param": "nextPageToken",
            "next_cursor_path": "nextPageToken",
            "items_path": "issues",
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("nextPageToken")
        assert request.url.params["team"] == "pay"  # original args kept on every page
        if token is None:
            return httpx.Response(200, json={"issues": [{"key": "A-1"}], "nextPageToken": "t2"})
        if token == "t2":
            return httpx.Response(200, json={"issues": [{"key": "A-2"}], "nextPageToken": "t3"})
        return httpx.Response(200, json={"issues": [{"key": "A-3"}]})  # no token: last page

    router.add("GET", f"{BASE}/v1/issues", handler)
    result = await librarian.executor.execute(
        CallRequest(
            api="tracker",
            operation="listIssues",
            args={"team": "pay"},
            max_pages=10,
            select=["issues.key"],
        ),
        Caller(agent="a"),
    )
    assert result.data == {"issues": [{"key": "A-1"}, {"key": "A-2"}, {"key": "A-3"}]}
    assert result.provenance.pages == 3


async def test_offset_pagination_stops_at_total(librarian: Librarian, router: Router) -> None:
    await paginate(
        librarian,
        {
            "style": "offset",
            "offset_param": "startAt",
            "items_path": "values",
            "total_path": "total",
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("startAt", "0"))
        return httpx.Response(
            200, json={"total": 3, "values": list(range(start, min(start + 2, 3)))}
        )

    router.add("GET", f"{BASE}/v1/issues", handler)
    result = await librarian.executor.execute(
        CallRequest(api="tracker", operation="listIssues", max_pages=10), Caller(agent="a")
    )
    assert result.data["values"] == [0, 1, 2]
    assert len(router.requests) == 2  # stopped at total, no empty third request


async def test_page_pagination_stops_on_empty_page(librarian: Librarian, router: Router) -> None:
    await paginate(librarian, {"style": "page", "page_param": "page", "items_path": "items"})

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(200, json={"items": [page] if page <= 2 else []})

    router.add("GET", f"{BASE}/v1/issues", handler)
    result = await librarian.executor.execute(
        CallRequest(api="tracker", operation="listIssues", max_pages=10), Caller(agent="a")
    )
    assert result.data == {"items": [1, 2]}


async def test_max_pages_is_respected(librarian: Librarian, router: Router) -> None:
    await paginate(
        librarian,
        {"style": "cursor", "cursor_param": "c", "next_cursor_path": "next", "items_path": "items"},
    )
    router.add(
        "GET", f"{BASE}/v1/issues", lambda r: httpx.Response(200, json={"items": [1], "next": "x"})
    )
    result = await librarian.executor.execute(
        CallRequest(api="tracker", operation="listIssues", max_pages=3), Caller(agent="a")
    )
    assert result.data == {"items": [1, 1, 1], "next": "x"}
    assert len(router.requests) == 3
