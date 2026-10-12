from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
import yaml

from librarian import service as service_module
from librarian.audit import MemoryAuditSink
from librarian.openapi import entry_from_spec
from librarian.publish import GitHubPublisher, PublishError, review_body
from librarian.service import Librarian
from tests.test_openapi import SPEC


class FakeGitHub:
    def __init__(self, file_exists: bool = False, existing_status: str = "provisional") -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.file_exists = file_exists
        self.existing_status = existing_status
        self.merge_blocked = False
        self.open_pulls: list[dict[str, Any]] = [
            {
                "number": 7,
                "html_url": "https://github.com/acme/registry/pull/7",
                "head": {"ref": "librarian/onboard-jira-20261012"},
            },
            {
                "number": 8,
                "html_url": "https://github.com/acme/registry/pull/8",
                "head": {"ref": "librarian/onboard-other-20261012"},
            },
        ]

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
        if request.method == "GET" and path == "/pulls":
            return httpx.Response(200, json=self.open_pulls)
        if request.method == "POST" and path.startswith("/issues/"):
            return httpx.Response(201, json={})
        if request.method == "PUT" and path.endswith("/merge"):
            if self.merge_blocked:
                return httpx.Response(405, json={"message": "Base branch requires a review"})
            number = int(path.split("/")[2])
            self.open_pulls = [p for p in self.open_pulls if p["number"] != number]
            return httpx.Response(200, json={"merged": True})
        if request.method == "PATCH" and path.startswith("/pulls/"):
            self.open_pulls = [p for p in self.open_pulls if f"/pulls/{p['number']}" != path]
            return httpx.Response(200, json={})
        if request.method == "DELETE" and path.startswith("/git/refs/heads/"):
            return httpx.Response(204)
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
    # the merge is the approval: the proposal already says verified (librarian keeps
    # using its provisional copy until then; unknown-access operations stay unknown)
    assert content["id"] == "jira" and content["status"] == "verified"
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
    async def fake_fetch(url: str, client: httpx.AsyncClient, *, net: Any) -> Any:
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

        async def withdraw(self, api_id: str, reason: str) -> list[str]:
            raise PublishError("GitHub is down")

        async def comment(self, api_id: str, text: str) -> list[str]:
            raise PublishError("GitHub is down")

        async def refresh(self, entry: Any, note: str) -> str | None:
            raise PublishError("GitHub is down")

        async def merge(self, api_id: str, by: str) -> str | None:
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


async def test_onboarding_says_when_an_internal_host_pattern_allowed_it(
    librarian: Librarian, monkeypatch: pytest.MonkeyPatch, audit: MemoryAuditSink
) -> None:
    async def fake_fetch(url: str, client: httpx.AsyncClient, *, net: Any) -> Any:
        return SPEC

    monkeypatch.setattr(service_module, "fetch_spec", fake_fetch)
    librarian.settings = librarian.settings.model_copy(
        update={"allow_private_networks": False, "internal_hosts": ["*.internal.example.test"]}
    )
    result = await librarian.onboard(
        "inside",
        spec_url="https://jira.internal.example.test/openapi.json",
        base_url="https://jira.internal.example.test/api",
    )
    note = "matches internal_hosts '*.internal.example.test'"
    assert note in result.entry.notes  # the reviewer of the PR sees it
    assert note in str(audit.events[-1].extra.get("internal"))
    outside = await librarian.onboard(
        "outside",
        spec_url="https://api.example.test/openapi.json",
        base_url="https://api.example.test",
    )
    assert "internal_hosts" not in outside.entry.notes


async def test_withdraw_closes_only_the_apis_open_proposals() -> None:
    fake = FakeGitHub()
    closed = await publisher(fake).withdraw("jira", "Forgotten in the console by sam.")
    assert closed == ["https://github.com/acme/registry/pull/7"]
    calls = [(m, p) for m, p, _ in fake.calls]
    assert ("POST", "/issues/7/comments") in calls
    assert ("PATCH", "/pulls/7") in calls
    assert ("DELETE", "/git/refs/heads/librarian/onboard-jira-20261012") in calls
    assert not any(p.startswith(("/pulls/8", "/issues/8")) for _, p in calls)  # other API
    assert [p["number"] for p in fake.open_pulls] == [8]
    assert await publisher(fake).comment("jira", "note") == []  # nothing open any more
    assert await publisher(fake).comment("other", "note") == [
        "https://github.com/acme/registry/pull/8"
    ]


async def test_refresh_commits_onto_the_open_proposal() -> None:
    fake = FakeGitHub(file_exists=True)
    entry = entry_from_spec(SPEC, api_id="jira")
    url = await publisher(fake).refresh(entry, "sam decided searchIssues is read")
    assert url == "https://github.com/acme/registry/pull/7"
    put = next(c for c in fake.calls if c[0] == "PUT")
    assert put[2]["branch"] == "librarian/onboard-jira-20261012" and put[2]["sha"] == "old-sha"
    assert ("POST", "/issues/7/comments") in [(m, p) for m, p, _ in fake.calls]
    assert not any(p == "/pulls" and m == "POST" for m, p, _ in fake.calls)  # no new PR
    other = entry_from_spec(SPEC, api_id="nothing-open")
    assert await publisher(fake).refresh(other, "note") is None
