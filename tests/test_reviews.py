"""The approve-or-deny step for learned APIs: the card, the page, merge and deny."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from librarian.audit import MemoryAuditSink
from librarian.calls import Caller
from librarian.config import ApproverSettings
from librarian.console import console_routes, review_routes
from librarian.notifiers import review_blocks
from librarian.registry.models import Access, EntryStatus, ReadBasis
from librarian.reviews import ReviewState
from librarian.service import Librarian
from tests.conftest import RecordingNotifier
from tests.test_publish import FakeGitHub, publisher
from tests.test_web import themed

AGENT = Caller(agent="agent-a", authenticated=True, principal="kim@x.test")
SPEC: dict[str, Any] = {
    "openapi": "3.0.0",
    "info": {"title": "Jira"},
    "servers": [{"url": "https://jira.example.test"}],
    "components": {"securitySchemes": {"b": {"type": "http", "scheme": "bearer"}}},
    "security": [{"b": []}],
    "paths": {
        "/me": {"get": {"operationId": "me"}},
        "/issues": {"get": {"operationId": "listIssues"}},
        "/search": {"post": {"operationId": "searchIssues", "summary": "Search issues"}},
    },
}


def fake_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    from librarian import service as service_module

    async def fetch(url: str, client: Any, *, net: Any) -> Any:
        return SPEC

    monkeypatch.setattr(service_module, "fetch_spec", fetch)


def pages(librarian: Librarian) -> TestClient:
    return TestClient(Starlette(routes=[*review_routes(librarian), *console_routes(librarian)]))


async def learn(librarian: Librarian, fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> Any:
    fake_spec(monkeypatch)
    fake.open_pulls[0]["head"]["ref"] = "librarian/onboard-jira-20261012"
    librarian.publisher = publisher(fake)
    return await librarian.onboard(
        "jira", spec_url="https://jira.example.test/openapi.json", caller=AGENT
    )


async def test_learning_opens_a_review_card_with_a_link(
    librarian: Librarian, notifier: RecordingNotifier, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = await learn(librarian, FakeGitHub(), monkeypatch)
    assert outcome.review_url == "https://github.com/acme/registry/pull/7"
    kind, review, link = notifier.reviews[-1]
    assert kind == "opened" and review.api == "jira" and review.learned_by == "kim@x.test"
    assert "3 operations" in review.summary and "1 need review" in review.summary
    assert review.auth.startswith("bearer (jira/readonly)")
    assert review.needs_review == ["searchIssues"]
    assert link and link.startswith("https://librarian.example.test/reviews/")
    assert notifier.learned == []  # the card replaces the plain notice
    blocks = review_blocks(review, link)
    button = next(b for b in blocks if "accessory" in b)
    assert button["accessory"]["url"] == link and "Approve merges" in button["text"]["text"]
    assert any("pull/7" in str(b) for b in blocks)
    assert librarian.reviews.pending_for("jira") is review


async def test_review_page_needs_a_valid_link_and_an_approver(
    librarian: Librarian, notifier: RecordingNotifier, monkeypatch: pytest.MonkeyPatch
) -> None:
    await learn(librarian, FakeGitHub(), monkeypatch)
    _, review, link = notifier.reviews[-1]
    assert link
    path = link.removeprefix("https://librarian.example.test")
    c = pages(librarian)
    assert c.get(f"/reviews/{review.id}?t=nope").status_code == 404
    ok = c.get(path)  # token mode: the link is enough
    assert ok.status_code == 200 and "Approve and merge" in ok.text and "searchIssues" in ok.text
    themed(ok)
    librarian.settings.approvers = ApproverSettings(mode="trusted_header", group="api-approvers")
    assert c.get(path).status_code == 403
    assert c.get(path, headers={"X-Auth-Request-Email": "eve@x.test"}).status_code == 403
    sam = {"X-Auth-Request-Email": "sam@x.test", "X-Auth-Request-Groups": "api-approvers"}
    assert c.get(path, headers=sam).status_code == 200
    assert "Review <b>Jira</b>" in c.get("/", headers=sam).text  # waiting on a person


async def test_approve_applies_decisions_and_merges_as_the_app(
    librarian: Librarian,
    notifier: RecordingNotifier,
    audit: MemoryAuditSink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeGitHub(file_exists=True)
    await learn(librarian, fake, monkeypatch)
    _, review, link = notifier.reviews[-1]
    assert link
    token = link.split("t=")[1]
    done = pages(librarian).post(
        f"/reviews/{review.id}",
        data={"t": token, "action": "approve", "decide_searchIssues": "read"},
    )
    assert done.status_code == 200 and "Approved" in done.text and "merged" in done.text
    entry = librarian.registry.get("jira")
    assert entry is not None
    search = entry.operation("searchIssues")
    assert search and search.access is Access.READ and search.read_basis is ReadBasis.REVIEWED
    calls = [(m, p) for m, p, _ in fake.calls]
    assert ("PUT", "/contents/registry/jira.yaml") in calls  # the decision, on the branch
    assert ("PUT", "/pulls/7/merge") in calls and (
        "DELETE",
        "/git/refs/heads/librarian/onboard-jira-20261012",
    ) in calls
    assert review.state is ReviewState.APPROVED and review.decided_by == "link holder"
    assert notifier.reviews[-1][0] == "closed"
    assert any(ev.event == "review_approved" for ev in audit.events)
    assert librarian.reviews.pending() == []
    # the one-time link is spent
    assert pages(librarian).get(f"/reviews/{review.id}?t={token}").status_code == 404


async def test_merge_refused_by_a_rule_keeps_the_review_open(
    librarian: Librarian,
    notifier: RecordingNotifier,
    audit: MemoryAuditSink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeGitHub()
    fake.merge_blocked = True
    await learn(librarian, fake, monkeypatch)
    _, review, link = notifier.reviews[-1]
    assert link
    token = link.split("t=")[1]
    page = pages(librarian).post(f"/reviews/{review.id}", data={"t": token, "action": "approve"})
    assert page.status_code == 502
    assert "refused to merge" in page.text and "merge it on GitHub" in page.text
    assert review.state is ReviewState.PENDING and audit.events[-1].event == "review_merge_failed"
    assert (
        pages(librarian).get(f"/reviews/{review.id}?t={token}").status_code == 200
    )  # still usable


async def test_deny_closes_the_proposal_and_forgets_the_api(
    librarian: Librarian,
    notifier: RecordingNotifier,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    fake = FakeGitHub()
    librarian.registry.overlay = tmp_path / "overlay"
    await learn(librarian, fake, monkeypatch)
    _, review, link = notifier.reviews[-1]
    assert link
    token = link.split("t=")[1]
    done = pages(librarian).post(f"/reviews/{review.id}", data={"t": token, "action": "deny"})
    assert done.status_code == 200 and "forgotten" in done.text
    assert librarian.registry.get("jira") is None
    assert review.state is ReviewState.DENIED
    assert [p["number"] for p in fake.open_pulls] == [8]  # jira's proposal closed, the other stays
    assert ("PATCH", "/pulls/7") in [(m, p) for m, p, _ in fake.calls]


async def test_forgetting_in_the_console_closes_the_review_too(
    librarian: Librarian,
    notifier: RecordingNotifier,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    librarian.registry.overlay = tmp_path / "overlay"
    await learn(librarian, FakeGitHub(), monkeypatch)
    _, review, _ = notifier.reviews[-1]
    librarian.settings.approvers.console = "open"
    t = librarian.approvals.page_token("console", "someone (console open)")
    assert pages(librarian).post("/apis/jira/forget", data={"t": t}).status_code == 200
    assert review.state is ReviewState.DENIED and "forgotten" in (review.note or "")
    assert notifier.reviews[-1][0] == "closed"


async def test_verified_entries_are_not_forgotten_on_deny(
    librarian: Librarian, notifier: RecordingNotifier, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGitHub()
    await learn(librarian, fake, monkeypatch)
    _, review, link = notifier.reviews[-1]
    entry = librarian.registry.get("jira")
    assert entry is not None
    await librarian.registry.save(entry.model_copy(update={"status": EntryStatus.VERIFIED}))
    assert link
    token = link.split("t=")[1]
    done = pages(librarian).post(f"/reviews/{review.id}", data={"t": token, "action": "deny"})
    assert done.status_code == 200 and "forgotten" not in done.text
    assert librarian.registry.get("jira") is not None


def test_review_blocks_never_carry_secrets() -> None:
    from datetime import UTC, datetime, timedelta

    from librarian.reviews import ReviewRequest

    review = ReviewRequest(
        api="x",
        api_name="X",
        host="x.test",
        review_url="https://g/pr/1",
        learned_by="a",
        summary="s",
        auth="bearer (x/readonly)",
        warnings=["w"],
        needs_review=["op"],
        expires_at=datetime.now(UTC) + timedelta(days=1),
        approver_group="S0GROUP",
    )
    text = str(review_blocks(review, "https://l/reviews/1?t=abc"))
    assert "<!subteam^S0GROUP>" in text and "x/readonly" in text and ":warning: w" in text
    review.state = ReviewState.DENIED
    review.decided_by = "sam"
    assert "denied by sam" in str(review_blocks(review, None))
