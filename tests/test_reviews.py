"""The approve-or-deny step for learned APIs: the card, the page, and a decision that is
in effect the moment it is made, with GitHub only told about it afterwards."""

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
from tests.conftest import RecordingNotifier, RecordPublisher
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


async def learn(librarian: Librarian, monkeypatch: pytest.MonkeyPatch) -> Any:
    fake_spec(monkeypatch)
    return await librarian.onboard(
        "jira", spec_url="https://jira.example.test/openapi.json", caller=AGENT
    )


async def test_learning_opens_a_review_card_with_a_link(
    librarian: Librarian, notifier: RecordingNotifier, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = RecordPublisher()
    librarian.publisher = record
    outcome = await learn(librarian, monkeypatch)
    kind, review, link = notifier.reviews[-1]
    assert outcome.review == review.id
    assert kind == "opened" and review.api == "jira" and review.learned_by == "kim@x.test"
    assert review.kind == "learned" and review.review_url == ""  # no pull request anywhere
    assert "3 operations" in review.summary and "1 need review" in review.summary
    assert review.auth.startswith("bearer (jira/readonly)")
    assert review.needs_review == ["searchIssues"]
    assert link and link.startswith("https://librarian.example.test/reviews/")
    blocks = review_blocks(review, link)
    button = next(b for b in blocks if "accessory" in b)
    assert button["accessory"]["url"] == link
    assert "Approve makes the API verified, at once" in button["text"]["text"]
    assert "pull request" not in str(blocks).lower()
    assert librarian.reviews.pending_for("jira") is review
    assert record.commits == []  # nothing is written down before a person approves


async def test_review_page_needs_a_valid_link_and_an_approver(
    librarian: Librarian, notifier: RecordingNotifier, monkeypatch: pytest.MonkeyPatch
) -> None:
    await learn(librarian, monkeypatch)
    _, review, link = notifier.reviews[-1]
    assert link
    path = link.removeprefix("https://librarian.example.test")
    c = pages(librarian)
    assert c.get(f"/reviews/{review.id}?t=nope").status_code == 404
    ok = c.get(path)  # token mode: the link is enough
    assert ok.status_code == 200 and "<button>Approve</button>" in ok.text
    assert "searchIssues" in ok.text and "pull request" not in ok.text.lower()
    themed(ok)
    librarian.settings.approvers = ApproverSettings(mode="trusted_header", group="api-approvers")
    assert c.get(path).status_code == 403
    assert c.get(path, headers={"X-Auth-Request-Email": "eve@x.test"}).status_code == 403
    sam = {"X-Auth-Request-Email": "sam@x.test", "X-Auth-Request-Groups": "api-approvers"}
    assert c.get(path, headers=sam).status_code == 200
    assert "Review <b>Jira</b>" in c.get("/", headers=sam).text  # waiting on a person


async def test_approving_verifies_the_api_at_once_and_writes_it_down(
    librarian: Librarian,
    notifier: RecordingNotifier,
    audit: MemoryAuditSink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = RecordPublisher()
    librarian.publisher = record
    await learn(librarian, monkeypatch)
    _, review, link = notifier.reviews[-1]
    assert link
    token = link.split("t=")[1]
    done = pages(librarian).post(
        f"/reviews/{review.id}",
        data={"t": token, "action": "approve", "decide_searchIssues": "read"},
    )
    assert done.status_code == 200 and "Approved" in done.text
    assert "verified; in effect now; being written to the record" in done.text
    # the database decided: agents get the verified entry on their very next call
    entry = librarian.registry.get("jira")
    assert entry is not None and entry.status is EntryStatus.VERIFIED
    search = entry.operation("searchIssues")
    assert search and search.access is Access.READ and search.read_basis is ReadBasis.REVIEWED
    assert "Approved by link holder." in entry.notes
    # GitHub is told afterwards: one commit, with the decisions made on the page in it
    assert [api for api, _ in record.commits] == ["jira"]
    message = record.commits[0][1]
    assert message.startswith("jira: approved (Jira)\n")
    assert "Decided by link holder in librarian." in message
    written = record.files["jira"]
    assert written.status is EntryStatus.VERIFIED
    assert written.operation("searchIssues").access is Access.READ  # type: ignore[union-attr]
    assert review.state is ReviewState.APPROVED and review.decided_by == "link holder"
    assert notifier.reviews[-1][0] == "closed"
    events = [ev.event for ev in audit.events]
    assert "api_approved" in events and "review_approved" in events and "recorded" in events
    assert librarian.reviews.pending() == []
    # the one-time link is spent
    assert pages(librarian).get(f"/reviews/{review.id}?t={token}").status_code == 404


async def test_github_refusing_the_record_does_not_undo_the_approval(
    librarian: Librarian,
    notifier: RecordingNotifier,
    audit: MemoryAuditSink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = RecordPublisher()
    record.fail = "acme/registry takes no direct commits to main"
    librarian.publisher = record
    await learn(librarian, monkeypatch)
    _, review, link = notifier.reviews[-1]
    assert link
    token = link.split("t=")[1]
    page = pages(librarian).post(f"/reviews/{review.id}", data={"t": token, "action": "approve"})
    assert page.status_code == 200 and "Approved" in page.text
    entry = librarian.registry.get("jira")
    assert entry is not None and entry.status is EntryStatus.VERIFIED  # the decision stands
    assert review.state is ReviewState.APPROVED
    assert any(ev.event == "record_failed" for ev in audit.events)
    # the console says the copy is behind, and offers to try again
    librarian.settings.approvers.console = "open"
    c = pages(librarian)
    assert "Not written to the record" in c.get("/").text
    api_page = c.get("/apis/jira").text
    assert "Not in the record" in api_page and "takes no direct commits" in api_page
    t = librarian.approvals.page_token("console", "someone (console open)")
    again = c.post("/apis/jira/record", data={"t": t})
    assert again.status_code == 502 and "decision itself is in effect" in again.text
    record.fail = None
    written = c.post("/apis/jira/record", data={"t": t})
    assert written.status_code == 200 and "Written to the record" in written.text
    assert record.files["jira"].status is EntryStatus.VERIFIED
    assert "Not written to the record" not in c.get("/").text
    assert "Not in the record" not in c.get("/apis/jira").text


async def test_a_learned_api_can_be_approved_from_its_console_page(
    librarian: Librarian, notifier: RecordingNotifier, monkeypatch: pytest.MonkeyPatch
) -> None:
    await learn(librarian, monkeypatch)
    _, review, _ = notifier.reviews[-1]
    librarian.settings.approvers.console = "open"
    c = pages(librarian)
    page = c.get("/apis/jira").text
    assert "Approve this API" in page and "1 operation still need a decision" in page
    t = librarian.approvals.page_token("console", "someone (console open)")
    done = c.post("/apis/jira/approve", data={"t": t})
    assert done.status_code == 200 and "Jira is verified" in done.text
    entry = librarian.registry.get("jira")
    assert entry is not None and entry.status is EntryStatus.VERIFIED
    undecided = entry.operation("searchIssues")
    assert undecided is not None and undecided.access is Access.UNKNOWN  # still refused
    assert review.state is ReviewState.APPROVED  # the card is settled too
    assert "Approve this API" not in c.get("/apis/jira").text
    # a blocked API is not approved by accident
    await librarian.block("status", "sam")
    refused = c.post("/apis/status/approve", data={"t": t})
    assert refused.status_code == 400 and "unblock it first" in refused.text


async def test_deny_forgets_the_api(
    librarian: Librarian,
    notifier: RecordingNotifier,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    record = RecordPublisher()
    librarian.publisher = record
    librarian.registry.overlay = tmp_path / "overlay"
    await learn(librarian, monkeypatch)
    _, review, link = notifier.reviews[-1]
    assert link
    token = link.split("t=")[1]
    done = pages(librarian).post(f"/reviews/{review.id}", data={"t": token, "action": "deny"})
    assert done.status_code == 200 and "forgotten" in done.text
    assert librarian.registry.get("jira") is None
    assert review.state is ReviewState.DENIED
    assert record.commits == []  # it was never approved, so it was never in the record


async def test_forgetting_in_the_console_closes_the_review_too(
    librarian: Librarian,
    notifier: RecordingNotifier,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    librarian.registry.overlay = tmp_path / "overlay"
    await learn(librarian, monkeypatch)
    _, review, _ = notifier.reviews[-1]
    librarian.settings.approvers.console = "open"
    t = librarian.approvals.page_token("console", "someone (console open)")
    assert pages(librarian).post("/apis/jira/forget", data={"t": t}).status_code == 200
    assert review.state is ReviewState.DENIED and "forgotten" in (review.note or "")
    assert notifier.reviews[-1][0] == "closed"


async def test_verified_entries_are_not_forgotten_on_deny(
    librarian: Librarian, notifier: RecordingNotifier, monkeypatch: pytest.MonkeyPatch
) -> None:
    await learn(librarian, monkeypatch)
    _, review, link = notifier.reviews[-1]
    entry = librarian.registry.get("jira")
    assert entry is not None
    await librarian.registry.save(entry.model_copy(update={"status": EntryStatus.VERIFIED}))
    assert link
    token = link.split("t=")[1]
    done = pages(librarian).post(f"/reviews/{review.id}", data={"t": token, "action": "deny"})
    assert done.status_code == 200 and "forgotten" not in done.text
    assert librarian.registry.get("jira") is not None


async def test_learning_again_replaces_the_open_review(
    librarian: Librarian, notifier: RecordingNotifier, monkeypatch: pytest.MonkeyPatch
) -> None:
    await learn(librarian, monkeypatch)
    _, first, _ = notifier.reviews[-1]
    await learn(librarian, monkeypatch)
    opened = [r for kind, r, _ in notifier.reviews if kind == "opened"]
    assert len(opened) == 2 and librarian.reviews.pending() == [opened[1]]
    assert first.state is ReviewState.DENIED and "superseded" in (first.note or "")


def test_review_blocks_never_carry_secrets() -> None:
    from datetime import UTC, datetime, timedelta

    from librarian.reviews import ReviewRequest

    review = ReviewRequest(
        api="x",
        api_name="X",
        host="x.test",
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


def test_a_review_saved_before_0_20_still_loads() -> None:
    from datetime import UTC, datetime, timedelta

    from librarian.reviews import ReviewRequest

    old = {
        "api": "x",
        "api_name": "X",
        "host": "x.test",
        "review_url": "https://github.com/acme/registry/pull/7",
        "learned_by": "a",
        "summary": "s",
        "auth": "none",
        "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    }
    review = ReviewRequest.model_validate(old)
    assert review.kind == "learned" and review.review_url.endswith("/pull/7")
    assert "pull/7" not in str(review_blocks(review, "https://l/reviews/1?t=abc"))
