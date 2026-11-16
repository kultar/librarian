"""The external record on GitHub: commits to the base branch, a pull request merged at
once where a rule requires one, and reading it back only to restore a database."""

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
from librarian.publish import GitHubPublisher, PublishError, blob_sha
from librarian.registry.models import ApiEntry, EntryStatus
from librarian.registry.store import dump_entry
from librarian.service import Librarian
from tests.test_openapi import SPEC

PROTECTED = "Repository rule violations found\n\nChanges must be made through a pull request."


class FakeGitHub:
    """A repository: files per branch, pull requests, and optionally a rule that the
    base branch only changes through pull requests."""

    def __init__(self, protected: bool = False) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.branches: dict[str, dict[str, str]] = {"main": {}}
        self.protected = protected
        self.merge_blocked = False
        self.dirty: set[int] = set()  # pull requests that conflict with the base branch
        self.pulls: list[dict[str, Any]] = []
        self.commits: list[tuple[str, str, str]] = []  # (branch, path, message)
        self.comments: list[tuple[int, str]] = []
        self.before_write: list[tuple[str, str]] = []  # (path, text) committed just before

    def file(self, api_id: str, branch: str = "main") -> ApiEntry | None:
        text = self.branches[branch].get(f"registry/{api_id}.yaml")
        return None if text is None else ApiEntry.model_validate(yaml.safe_load(text))

    def open(self, number: int, branch: str) -> None:
        self.branches.setdefault(branch, dict(self.branches["main"]))
        self.pulls.append(
            {
                "number": number,
                "state": "open",
                "html_url": f"https://github.com/acme/registry/pull/{number}",
                "head": {"ref": branch},
            }
        )

    def open_pulls(self) -> list[dict[str, Any]]:
        return [p for p in self.pulls if p["state"] == "open"]

    def _contents(self, request: httpx.Request, path: str) -> httpx.Response:
        branch = request.url.params.get("ref", "main")
        files = self.branches.get(branch)
        if files is None:
            return httpx.Response(404, json={"message": "No commit found for the ref"})
        if path in files:
            if "raw" in request.headers.get("accept", ""):
                return httpx.Response(200, text=files[path])
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "path": path,
                    "sha": blob_sha(files[path]),
                    "html_url": f"https://github.com/acme/registry/blob/{branch}/{path}",
                },
            )
        listing = [
            {"type": "file", "name": p.rsplit("/", 1)[-1], "path": p, "sha": blob_sha(t)}
            for p, t in sorted(files.items())
            if p.startswith(path.rstrip("/") + "/")
        ]
        if listing:
            return httpx.Response(200, json=listing)
        return httpx.Response(404, json={"message": "Not Found"})

    def _write(self, method: str, path: str, body: dict[str, Any]) -> httpx.Response:
        branch = str(body["branch"])
        files = self.branches[branch]
        if branch == "main" and self.protected:
            return httpx.Response(409, json={"message": PROTECTED})
        if branch == "main" and self.before_write:
            other_path, other_text = self.before_write.pop(0)
            files[other_path] = other_text  # another replica got there first
        current = blob_sha(files[path]) if path in files else None
        if body.get("sha") != current:
            said = f"{path} does not match {body.get('sha')}"
            if current is not None and "sha" not in body:
                return httpx.Response(422, json={"message": '"sha" wasn\'t supplied.'})
            return httpx.Response(409, json={"message": said})
        if method == "DELETE":
            del files[path]
        else:
            files[path] = base64.b64decode(body["content"]).decode()
        self.commits.append((branch, path, str(body["message"])))
        number = len(self.commits)
        return httpx.Response(
            200 if current else 201,
            json={"commit": {"html_url": f"https://github.com/acme/registry/commit/{number}"}},
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path.removeprefix("/repos/acme/registry")
        self.calls.append((request.method, path, body))
        assert request.headers["authorization"] == "Bearer ghp-test"
        method = request.method
        if path.startswith("/contents/"):
            file_path = path.removeprefix("/contents/")
            if method == "GET":
                return self._contents(request, file_path)
            assert body is not None
            return self._write(method, file_path, body)
        if method == "GET" and path == "/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "base-sha"}})
        if method == "POST" and path == "/git/refs":
            assert body is not None
            self.branches[str(body["ref"]).removeprefix("refs/heads/")] = dict(
                self.branches["main"]
            )
            return httpx.Response(201, json={})
        if method == "DELETE" and path.startswith("/git/refs/heads/"):
            self.branches.pop(path.removeprefix("/git/refs/heads/"), None)
            return httpx.Response(204)
        if method == "POST" and path == "/pulls":
            assert body is not None
            number = 100 + len(self.pulls)
            self.open(number, str(body["head"]))
            self.pulls[-1]["title"] = body["title"]
            return httpx.Response(201, json=self.pulls[-1])
        if method == "GET" and path == "/pulls":
            return httpx.Response(200, json=self.open_pulls())
        if path.startswith("/pulls/"):
            number = int(path.split("/")[2])
            pr = next(p for p in self.pulls if p["number"] == number)
            if method == "PUT" and path.endswith("/merge"):
                if self.merge_blocked or number in self.dirty:
                    return httpx.Response(405, json={"message": "Pull Request is not mergeable"})
                self.branches["main"] = dict(self.branches[str(pr["head"]["ref"])])
                pr["state"] = "closed"
                return httpx.Response(200, json={"merged": True})
            if method == "GET":
                state = "dirty" if number in self.dirty else "blocked"
                return httpx.Response(200, json={**pr, "mergeable_state": state})
            if method == "PATCH":
                pr["state"] = "closed"
                return httpx.Response(200, json=pr)
        if method == "POST" and path.startswith("/issues/"):
            assert body is not None
            self.comments.append((int(path.split("/")[2]), str(body["body"])))
            return httpx.Response(201, json={})
        return httpx.Response(500, json={"message": f"unhandled {method} {path}"})


def publisher(fake: FakeGitHub) -> GitHubPublisher:
    return GitHubPublisher(
        "acme/registry",
        "ghp-test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(fake)),
    )


# As it comes back from YAML, so entries read from the record compare equal.
LEARNED = ApiEntry.model_validate(
    yaml.safe_load(
        dump_entry(entry_from_spec(SPEC, api_id="jira", spec_url="https://x.example/spec.json"))
    )
)


def jira(**update: Any) -> ApiEntry:
    return LEARNED.model_copy(update={"status": EntryStatus.VERIFIED, **update})


async def test_a_decision_is_one_commit_on_the_base_branch() -> None:
    fake = FakeGitHub()
    message = "jira: approved (Jira)\n\nsam approved Jira.\n\nDecided by sam in librarian."
    url = await publisher(fake).record(jira(), message)
    assert url == "https://github.com/acme/registry/commit/1"
    assert fake.commits == [("main", "registry/jira.yaml", message)]
    put = next(body for method, _, body in fake.calls if method == "PUT")
    assert "sha" not in put  # a new file
    recorded = fake.file("jira")
    assert recorded is not None and recorded.status is EntryStatus.VERIFIED
    assert not fake.pulls  # nothing to review on GitHub: the decision was made in librarian

    # The next decision replaces the file, naming the version it replaces.
    changed = jira(description="Now with comments")
    assert await publisher(fake).record(changed, "jira: second") == (
        "https://github.com/acme/registry/commit/2"
    )
    put = [body for method, _, body in fake.calls if method == "PUT"][-1]
    assert put["sha"] == blob_sha(dump_entry(jira()))
    assert fake.file("jira") == changed


async def test_recording_what_the_record_already_says_commits_nothing() -> None:
    fake = FakeGitHub()
    await publisher(fake).record(jira(), "first")
    url = await publisher(fake).record(jira(), "again")
    assert url == "https://github.com/acme/registry/blob/main/registry/jira.yaml"
    assert len(fake.commits) == 1
    assert [m for m, _, _ in fake.calls].count("PUT") == 1


async def test_a_commit_made_in_between_is_seen_and_the_write_tried_again() -> None:
    fake = FakeGitHub()
    await publisher(fake).record(jira(), "first")
    # Another replica records an older decision between this one's read and its write.
    older = dump_entry(jira(description="An older decision"))
    fake.before_write.append(("registry/jira.yaml", older))
    latest = jira(description="The latest decision")
    await publisher(fake).record(latest, "latest")
    assert fake.file("jira") == latest  # the latest state wins, whatever order they landed in
    assert [m for m, _, _ in fake.calls].count("PUT") == 3  # first, refused, retried


async def test_a_branch_that_only_takes_pull_requests_gets_one_merged_at_once() -> None:
    fake = FakeGitHub(protected=True)
    url = await publisher(fake).record(jira(), "jira: approved (Jira)\n\nsam approved Jira.")
    assert url == "https://github.com/acme/registry/pull/100"
    pr = fake.pulls[0]
    assert pr["state"] == "closed" and pr["title"] == "jira: approved (Jira)"
    branch = str(pr["head"]["ref"])
    assert branch.startswith("librarian/record-jira-")
    assert branch not in fake.branches  # merged and cleaned up
    assert fake.file("jira") == jira()
    opened = next(body for method, path, body in fake.calls if (method, path) == ("POST", "/pulls"))
    assert "already in effect" in opened["body"]  # nobody is asked to approve anything here


async def test_a_refused_merge_says_what_to_do_and_never_piles_up_pull_requests() -> None:
    fake = FakeGitHub(protected=True)
    fake.merge_blocked = True
    with pytest.raises(PublishError) as info:
        await publisher(fake).record(jira(), "jira: approved")
    said = str(info.value)
    assert "takes no direct commits to main" in said and "pull request" in said
    assert "https://github.com/acme/registry/pull/100" in said and "bypass the rule" in said
    assert len(fake.open_pulls()) == 1

    # A later decision goes onto the same pull request rather than opening another.
    later = jira(description="A later decision")
    with pytest.raises(PublishError):
        await publisher(fake).record(later, "jira: later")
    assert len(fake.pulls) == 1
    assert fake.file("jira", str(fake.pulls[0]["head"]["ref"])) == later

    # Once the rule lets the App through (or a person merges it), the record catches up.
    fake.merge_blocked = False
    assert await publisher(fake).record(later, "jira: later") == (
        "https://github.com/acme/registry/pull/100"
    )
    assert fake.file("jira") == later and not fake.open_pulls()


async def test_a_pull_request_that_conflicts_is_replaced_by_a_fresh_one() -> None:
    fake = FakeGitHub(protected=True)
    fake.open(7, "librarian/record-jira-20261101000000")
    fake.dirty.add(7)
    url = await publisher(fake).record(jira(), "jira: approved")
    assert url == "https://github.com/acme/registry/pull/101"
    assert [p["state"] for p in fake.pulls] == ["closed", "closed"]
    assert "librarian/record-jira-20261101000000" not in fake.branches
    assert fake.file("jira") == jira()


async def test_forgetting_removes_the_file() -> None:
    fake = FakeGitHub()
    await publisher(fake).record(jira(), "first")
    url = await publisher(fake).remove("jira", "jira: forgotten")
    assert url == "https://github.com/acme/registry/commit/2"
    assert fake.file("jira") is None and fake.commits[-1][2] == "jira: forgotten"
    assert await publisher(fake).remove("jira", "again") is None  # never there: nothing to do
    assert len(fake.commits) == 2

    protected = FakeGitHub(protected=True)
    protected.branches["main"]["registry/jira.yaml"] = dump_entry(jira())
    assert await publisher(protected).remove("jira", "jira: forgotten") == (
        "https://github.com/acme/registry/pull/100"
    )
    assert protected.file("jira") is None


async def test_errors_carry_what_github_said() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "/contents/" in request.url.path:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(403, json={"message": "Resource not accessible by integration"})

    pub = GitHubPublisher(
        "acme/registry", "t", client=httpx.AsyncClient(transport=httpx.MockTransport(broken))
    )
    with pytest.raises(PublishError, match="403: Resource not accessible by integration"):
        await pub.record(jira(), "jira: approved")


async def test_the_record_is_read_back_only_to_restore() -> None:
    fake = FakeGitHub()
    await publisher(fake).record(jira(), "first")
    fake.branches["main"]["registry/broken.yaml"] = "id: broken\nname: [\n"
    fake.branches["main"]["registry/misnamed.yaml"] = dump_entry(jira())
    fake.branches["main"]["registry/README.md"] = "not an entry"
    entries, unreadable = await publisher(fake).read_all()
    assert entries == [jira()]
    assert set(unreadable) == {"broken.yaml", "misnamed.yaml"}
    assert "file name must match id 'jira'" in unreadable["misnamed.yaml"]
    assert await publisher(FakeGitHub()).read_all() == ([], {})  # an empty repository


async def test_pull_requests_from_before_0_20_are_closed_with_an_explanation() -> None:
    fake = FakeGitHub()
    fake.open(7, "librarian/onboard-jira-20261012")
    fake.open(8, "librarian/update-github-20261013")
    fake.open(9, "librarian/record-jira-20261101")  # this version's own: left alone
    fake.open(10, "someone/else")
    closed = await publisher(fake).close_proposals("librarian approves in its database now.")
    assert closed == [
        "https://github.com/acme/registry/pull/7",
        "https://github.com/acme/registry/pull/8",
    ]
    assert [p["number"] for p in fake.open_pulls()] == [9, 10]
    assert fake.comments == [
        (7, "librarian approves in its database now."),
        (8, "librarian approves in its database now."),
    ]
    assert "librarian/onboard-jira-20261012" not in fake.branches
    assert await publisher(fake).close_proposals("again") == []


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
    assert note in result.entry.notes  # the approver sees it on the review page
    onboarded = next(ev for ev in audit.events if ev.event == "onboard" and ev.api == "inside")
    assert note in str(onboarded.extra.get("internal"))
    outside = await librarian.onboard(
        "outside",
        spec_url="https://api.example.test/openapi.json",
        base_url="https://api.example.test",
    )
    assert "internal_hosts" not in outside.entry.notes
