from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
import yaml

from librarian import service as service_module
from librarian.openapi import entry_from_spec
from librarian.publish import GitHubPublisher, PublishError, review_body
from librarian.service import Librarian
from tests.test_openapi import SPEC


class FakeGitHub:
    def __init__(self, file_exists: bool = False, existing_status: str = "provisional") -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.file_exists = file_exists
        self.existing_status = existing_status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path.removeprefix("/repos/acme/registry")
        self.calls.append((request.method, path, body))
        assert request.headers["authorization"] == "Bearer ghp-test"
        if request.method == "GET" and path == "/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "base-sha"}})
        if request.method == "POST" and path == "/git/refs":
            return httpx.Response(201, json={})
        if request.method == "GET" and path.startswith("/contents/"):
            if self.file_exists:
                content = base64.b64encode(f"id: jira\nstatus: {self.existing_status}\n".encode())
                return httpx.Response(200, json={"sha": "old-sha", "content": content.decode()})
            return httpx.Response(404, json={})
        if request.method == "PUT" and path.startswith("/contents/"):
            return httpx.Response(201, json={})
        if request.method == "POST" and path == "/pulls":
            return httpx.Response(201, json={"html_url": "https://github.com/acme/registry/pull/7"})
        return httpx.Response(500)


def publisher(fake: FakeGitHub) -> GitHubPublisher:
    return GitHubPublisher(
        "acme/registry",
        "ghp-test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(fake)),
    )


async def test_publish_opens_pull_request() -> None:
    fake = FakeGitHub()
    entry = entry_from_spec(SPEC, api_id="jira", spec_url="https://x.example/spec.json")
    url = await publisher(fake).publish(entry, "release-notes-agent")
    assert url == "https://github.com/acme/registry/pull/7"
    methods = [(m, p) for m, p, _ in fake.calls]
    # The existing file is checked first, so a refusal leaves no branch behind.
    assert methods[:3] == [
        ("GET", "/contents/registry/jira.yaml"),
        ("GET", "/git/ref/heads/main"),
        ("POST", "/git/refs"),
    ]
    branch_ref = fake.calls[2][2]["ref"]
    assert branch_ref.startswith("refs/heads/librarian/onboard-jira-")
    put = next(body for m, _, body in fake.calls if m == "PUT")
    assert put["branch"] == branch_ref.removeprefix("refs/heads/")
    assert "sha" not in put  # new file
    content = yaml.safe_load(base64.b64decode(put["content"]))
    assert content["id"] == "jira" and content["status"] == "provisional"
    pr = fake.calls[-1][2]
    assert pr["base"] == "main" and "release-notes-agent" in pr["body"]


async def test_publish_updates_existing_file() -> None:
    fake = FakeGitHub(file_exists=True)
    entry = entry_from_spec(SPEC, api_id="jira")
    await publisher(fake).publish(entry, "agent")
    put = next(body for m, _, body in fake.calls if m == "PUT")
    assert put["sha"] == "old-sha"


async def test_publish_errors_are_raised() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Resource not accessible"})

    pub = GitHubPublisher(
        "acme/registry", "t", client=httpx.AsyncClient(transport=httpx.MockTransport(broken))
    )
    with pytest.raises(PublishError, match="403"):
        await pub.publish(entry_from_spec(SPEC, api_id="jira"), "agent")


def test_review_body_lists_decisions() -> None:
    body = review_body(entry_from_spec(SPEC, api_id="jira"), "agent-a")
    assert "searchForIssuesUsingJqlPost" in body  # needs a human decision
    assert "status: verified" in body
    assert "get__api_3_myself" in body  # key test


async def test_onboard_publishes_and_survives_publish_failure(
    librarian: Librarian, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_fetch(url: str, client: httpx.AsyncClient, *, allow_private: bool) -> Any:
        return SPEC

    monkeypatch.setattr(service_module, "fetch_spec", fake_fetch)
    fake = FakeGitHub()
    librarian.publisher = publisher(fake)
    outcome = await librarian.onboard("jira", spec_url="https://x.example/spec.json")
    assert outcome.review_url == "https://github.com/acme/registry/pull/7"
    assert librarian.registry.get("jira") is not None

    class Failing:
        async def publish(self, entry: Any, learned_by: str) -> str:
            raise PublishError("GitHub is down")

        async def propose_update(self, entry: Any, title: str, body: str) -> str:
            raise PublishError("GitHub is down")

    librarian.publisher = Failing()
    outcome = await librarian.onboard("jira", spec_url="https://x.example/spec.json")
    assert outcome.review_url is None
    assert outcome.review_error is not None and "GitHub is down" in outcome.review_error


async def test_never_proposes_replacing_a_verified_entry() -> None:
    fake = FakeGitHub(file_exists=True, existing_status="verified")
    with pytest.raises(PublishError, match="already verified"):
        await publisher(fake).publish(entry_from_spec(SPEC, api_id="jira"), "agent")
    assert not [c for c in fake.calls if c[0] in ("POST", "PUT")]  # no branch, no PR


async def test_updates_to_verified_entries_are_proposed_not_refused() -> None:
    fake = FakeGitHub(file_exists=True, existing_status="verified")
    entry = entry_from_spec(SPEC, api_id="jira", spec_url="https://x.example/spec.json")
    url = await publisher(fake).propose_update(entry, "registry: `jira` changed", "body")
    assert url == "https://github.com/acme/registry/pull/7"
    put = next(body for method, _, body in fake.calls if method == "PUT")
    assert put["sha"] == "old-sha" and put["branch"].startswith("librarian/update-jira-")
    pr = next(body for _, path, body in fake.calls if path == "/pulls")
    assert pr["title"] == "registry: `jira` changed"
