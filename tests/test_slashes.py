"""Slashes in path arguments: file paths and git refs, with their segments still checked."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from librarian.audit import MemoryAuditSink
from librarian.calls import Caller, CallRequest
from librarian.executor import PolicyDenied
from librarian.openapi import entry_from_spec
from librarian.registry.models import ApiEntry, Parameter, looks_like_path
from librarian.service import Librarian
from tests.conftest import BASE, Router
from tests.test_console import console, token
from tests.test_enable_writes import ProposalPublisher

FILES: dict[str, Any] = {
    "id": "files",
    "name": "Files",
    "base_url": BASE,
    "status": "verified",
    "operations": [
        {
            "id": "getContent",
            "method": "GET",
            "path": "/repos/{repo}/contents/{path}",
            "access": "read",
            "parameters": [
                {"name": "repo", "in": "path"},
                {"name": "path", "in": "path", "allow_slash": True},
            ],
        },
        {
            "id": "getRef",
            "method": "GET",
            "path": "/repos/{repo}/git/ref/{ref}",
            "access": "read",
            "parameters": [
                {"name": "repo", "in": "path"},
                {"name": "ref", "in": "path", "description": "The Git reference, e.g. heads/main"},
            ],
        },
    ],
}
A = Caller(agent="agent-a")


@pytest.fixture
async def files(librarian: Librarian) -> ApiEntry:
    entry = ApiEntry.model_validate(FILES)
    await librarian.registry.save(entry)
    return entry


def test_allow_slash_only_on_path_parameters() -> None:
    with pytest.raises(ValidationError, match="allow_slash"):
        Parameter.model_validate({"name": "q", "in": "query", "allow_slash": True})
    assert Parameter.model_validate({"name": "p", "in": "path", "allow_slash": True}).required


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("src/main.py", True),
        ("docs/a b/c.md", True),
        ("README", True),
        ("../etc/passwd", False),
        ("a/../b", False),
        ("a/./b", False),
        ("a//b", False),
        ("/abs", False),
        ("dir/", False),
        ("a%2Fb", False),
        ("a%2fb", False),
        ("a\\b", False),
        ("", False),
    ],
)
async def test_path_values_are_checked_segment_by_segment(
    librarian: Librarian, files: ApiEntry, value: str, ok: bool
) -> None:
    request = CallRequest(api="files", operation="getContent", args={"repo": "r", "path": value})
    decision = librarian.policy.check(request, A)
    assert decision.allowed is ok, decision.reason


async def test_slashes_reach_the_api_unencoded_and_other_parameters_still_refuse_them(
    librarian: Librarian, files: ApiEntry, router: Router
) -> None:
    router.add(
        "GET", f"{BASE}/repos/r/contents/src/main.py", lambda r: httpx.Response(200, json={"ok": 1})
    )
    result = await librarian.execute(
        CallRequest(api="files", operation="getContent", args={"repo": "r", "path": "src/main.py"}),
        A,
    )
    assert result.ok and router.requests[-1].url.path == "/repos/r/contents/src/main.py"
    with pytest.raises(PolicyDenied, match="does not allow '/'"):
        await librarian.execute(
            CallRequest(api="files", operation="getRef", args={"repo": "r", "ref": "heads/main"}), A
        )
    with pytest.raises(PolicyDenied, match="path segment"):
        await librarian.execute(
            CallRequest(api="files", operation="getContent", args={"repo": "a/b", "path": "x"}), A
        )
    assert len(router.requests) == 1


def param(name: str, description: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "in": "path",
        "required": True,
        "schema": {"type": "string"},
        "description": description,
    }


def test_path_like_parameters_are_inferred_from_a_spec() -> None:
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Hub", "version": "1"},
        "servers": [{"url": BASE}],
        "paths": {
            "/repos/{owner}/{repo}/contents/{path}": {
                "get": {
                    "operationId": "repos/get-content",
                    "parameters": [param("owner"), param("repo"), param("path", "path parameter")],
                }
            },
            "/repos/{owner}/{repo}/git/ref/{ref}": {
                "get": {
                    "operationId": "git/get-ref",
                    "parameters": [
                        param("owner"),
                        param("repo"),
                        param("ref", "The Git reference. For more information, see ..."),
                    ],
                }
            },
            "/repos/{owner}/{repo}/branches/{branch}": {
                "get": {
                    "operationId": "repos/get-branch",
                    "parameters": [param("owner"), param("repo"), param("branch", "The branch.")],
                }
            },
            "/issues/{issueIdOrKey}": {
                "get": {
                    "operationId": "getIssue",
                    "parameters": [param("issueIdOrKey", "The issue key")],
                }
            },
        },
    }
    entry = entry_from_spec(
        spec, api_id="hub", spec_url="https://hub.test/spec.json", base_url=BASE
    )
    flags = {(op.id, p.name): p.allow_slash for op in entry.operations for p in op.path_params}
    assert flags[("repos_get-content", "path")] and flags[("git_get-ref", "ref")]
    assert flags[("repos_get-branch", "branch")]
    assert not flags[("repos_get-content", "owner")] and not flags[("repos_get-content", "repo")]
    assert not flags[("getIssue", "issueIdOrKey")]
    assert looks_like_path("name", "Path to the file in the repository")
    assert looks_like_path("object_path") and not looks_like_path("key", "The issue key")


async def test_console_allows_slashes_in_path_like_parameters(
    librarian: Librarian, files: ApiEntry, audit: MemoryAuditSink
) -> None:
    librarian.settings.approvers.console = "open"
    pub = ProposalPublisher()
    librarian.publisher = pub
    c = console(librarian)
    t = token(librarian)
    page = c.get("/apis/files").text
    assert "Slashes in path arguments" in page and "getRef.ref" in page
    assert "getContent.path" not in page  # already allowed
    done = c.post("/apis/files/slashes", data={"t": t})
    assert (
        done.status_code == 200 and "getRef.ref" in done.text and "Proposed for review" in done.text
    )
    proposed, title = pub.entries[-1]
    assert title == "Files: allow slashes in 1 path argument(s)"
    ref = next(p for p in proposed.operation("getRef").parameters if p.name == "ref")
    assert ref.allow_slash and "allowed '/' in path arguments" in proposed.notes
    live = librarian.registry.get("files")
    assert live is not None
    assert not next(p for p in live.operation("getRef").parameters if p.name == "ref").allow_slash  # type: ignore[union-attr]
    assert audit.events[-1].event == "slashes_allowed" and audit.events[-1].detail == "getRef.ref"
    nothing = c.post("/apis/tracker/slashes", data={"t": t})
    assert nothing.status_code == 400 and "no path parameter looks like" in nothing.text
    # a provisional entry changes at once
    await librarian.registry.save(
        ApiEntry.model_validate({**FILES, "id": "files2", "status": "provisional"})
    )
    applied = c.post("/apis/files2/slashes", data={"t": t})
    assert applied.status_code == 200 and "Applied now" in applied.text
    changed = librarian.registry.get("files2")
    assert changed is not None
    assert next(p for p in changed.operation("getRef").parameters if p.name == "ref").allow_slash  # type: ignore[union-attr]
    decision = librarian.policy.check(
        CallRequest(api="files2", operation="getRef", args={"repo": "r", "ref": "heads/main"}), A
    )
    assert decision.allowed, decision.reason
