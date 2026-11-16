"""Describing an API's authentication in full, in the console: the kinds the simple
choice does not cover (a credential made for each call, each person's own sign-in,
different authentication for writes)."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from librarian.audit import MemoryAuditSink
from librarian.console import console_routes
from librarian.registry.models import (
    ApiEntry,
    AwsSigV4Auth,
    BearerAuth,
    EntryStatus,
    GitHubAppTokenAuth,
    NoAuth,
    OAuth2ClientAuth,
)
from librarian.service import Librarian, LibrarianError
from tests.conftest import RecordingNotifier

APP = "type: github_app\nsecret_ref: tracker/app-key\napp_id: 12345\n"
CLIENT = (
    "type: oauth2_client\n"
    "secret_ref: tracker/client\n"
    "token_url: https://login.example.test/oauth/token\n"
    "client_id: librarian\n"
    "scopes: [issues.write]\n"
)
SIGNED = "type: aws_sigv4\nservice: execute-api\nregion: eu-west-1\n"


async def test_a_person_describes_the_authentication_and_it_is_in_effect_at_once(
    librarian: Librarian, audit: MemoryAuditSink
) -> None:
    updated, applied = await librarian.set_auth_spec("tracker", "auth", APP, "sam@x.test")
    assert isinstance(updated.auth, GitHubAppTokenAuth) and updated.auth.app_id == "12345"
    assert applied.note.startswith("in effect now")
    stored = librarian.registry.get("tracker")
    assert stored is not None and stored.auth == updated.auth
    assert stored.status is EntryStatus.VERIFIED  # still approved: an approver did this
    event = audit.events[-1]
    assert (event.event, event.principal, event.detail) == (
        "auth_set",
        "sam@x.test",
        "reads: github_app",
    )
    # Writes can be authenticated differently from reads...
    updated, _ = await librarian.set_auth_spec("tracker", "write_auth", CLIENT, "sam@x.test")
    assert isinstance(updated.write_auth, OAuth2ClientAuth)
    assert updated.write_auth.scopes == ["issues.write"]
    assert isinstance(updated.auth, GitHubAppTokenAuth)  # reads are untouched
    updated, _ = await librarian.set_auth_spec("tracker", "write_auth", SIGNED, "sam@x.test")
    assert isinstance(updated.write_auth, AwsSigV4Auth)
    # ...or not at all: an empty description takes the write credential away.
    updated, _ = await librarian.set_auth_spec("tracker", "write_auth", "  \n", "sam@x.test")
    assert updated.write_auth is None
    updated, _ = await librarian.set_auth_spec("tracker", "auth", "", "sam@x.test")
    assert isinstance(updated.auth, NoAuth)


@pytest.mark.parametrize(
    ("spec", "problem"),
    [
        ("type: [unclosed", "not valid YAML"),
        ("- a list", "as `key: value` lines"),
        ("just words", "as `key: value` lines"),
        ("type: magic\nsecret_ref: tracker/x", "not valid"),
        ("type: bearer", "secret_ref: Field required"),
        ("type: bearer\nsecret_ref: tracker/x\ncolour: blue", "Extra inputs are not permitted"),
        ("type: bearer\nsecret_ref: librarian/signing-key", "reserved prefix"),
        # One API may not be pointed at another's key (nor at one nobody named for it).
        ("type: bearer\nsecret_ref: weather/key", "not under this API's own name"),
        ("type: bearer\nsecret_ref: prod-admin-token", "not under this API's own name"),
        (
            CLIENT.replace("https://login.example.test", "http://login.example.test"),
            "must be an https URL",
        ),
        (SIGNED + "role_arn: arn:aws:iam::12:role/x", "role_arn"),
    ],
)
async def test_what_cannot_be_set_is_refused_with_the_reason(
    librarian: Librarian, spec: str, problem: str
) -> None:
    before = librarian.registry.get("tracker")
    with pytest.raises(LibrarianError, match=problem):
        await librarian.set_auth_spec("tracker", "auth", spec, "sam@x.test")
    assert librarian.registry.get("tracker") == before


async def test_a_secret_the_entry_already_uses_may_be_kept(librarian: Librarian) -> None:
    """Deployments name secrets their own way (one JSON secret per service, set in a
    registry file); changing the kind of authentication must not force a rename."""
    entry = librarian.registry.get("tracker")
    assert entry is not None
    elsewhere = BearerAuth.model_validate({"type": "bearer", "secret_ref": "service/tracker#key"})
    await librarian.registry.save(entry.model_copy(update={"auth": elsewhere}))
    spec = "type: basic\nsecret_ref: service/tracker#key"
    updated, _ = await librarian.set_auth_spec("tracker", "auth", spec, "sam@x.test")
    assert updated.auth.type == "basic" and updated.secret_ref == "service/tracker#key"
    with pytest.raises(LibrarianError, match="unknown API"):
        await librarian.set_auth_spec("nope", "auth", spec, "sam@x.test")
    with pytest.raises(LibrarianError, match="choose reads or writes"):
        await librarian.set_auth_spec("tracker", "operations", spec, "sam@x.test")


def console(librarian: Librarian) -> tuple[TestClient, str]:
    librarian.settings.approvers.console = "open"
    client = TestClient(Starlette(routes=console_routes(librarian)))
    return client, librarian.approvals.page_token("console", "someone (console open)")


async def test_the_console_shows_it_and_takes_it(librarian: Librarian) -> None:
    c, t = console(librarian)
    page = c.get("/apis/tracker").text
    assert "Describe it in full" in page and "name=spec" in page
    # What is there now is shown as it would be written: references, never a secret.
    assert "type: bearer\nsecret_ref: tracker/readonly" in page
    done = c.post("/apis/tracker/auth-spec", data={"t": t, "which": "auth", "spec": APP})
    assert done.status_code == 200 and "Reads of tracker now use github_app" in done.text
    page = c.get("/apis/tracker").text
    assert "a GitHub App token made for each call" in page
    assert "type: github_app\nsecret_ref: tracker/app-key\napp_id: &#x27;12345&#x27;" in page
    # The simple choice does not sit on "none" now: one click must not remove all this.
    assert "<option value='' selected disabled>github_app (described in full below)" in page
    assert "<option value=none selected>" not in page
    assert c.post("/apis/tracker/auth", data={"t": t, "scheme": ""}).status_code == 400
    wrong: dict[str, Any] = {"t": t, "which": "auth", "spec": "type: bearer\nsecret_ref: a/b"}
    refused = c.post("/apis/tracker/auth-spec", data=wrong)
    assert refused.status_code == 400 and "not under this API" in refused.text
    assert c.post("/apis/tracker/auth-spec", data={"which": "auth", "spec": APP}).status_code == 403


async def test_enabling_writes_asks_nobody_for_a_key_when_none_is_stored(
    librarian: Librarian, notifier: RecordingNotifier
) -> None:
    """With a credential made per call there is no write key to add: writes are
    authenticated the way reads are, and a read still never gets a token that can write."""
    entry = librarian.registry.get("tracker")
    assert entry is not None
    await librarian.registry.save(
        ApiEntry.model_validate(
            {
                **entry.model_dump(mode="json", by_alias=True),
                "auth": {"type": "github_app", "secret_ref": "tracker/app-key", "app_id": "1"},
                "write_auth": None,
            }
        )
    )
    c, _ = console(librarian)
    asked = await librarian.enable_operation("tracker", "renameProject", "sam@x.test")
    assert asked.key_request is None  # no key page follows
    assert [kind for kind, _ in notifier.events] == []
    updated = librarian.registry.get("tracker")
    assert updated is not None and updated.write_auth == updated.auth
    op = updated.operation("renameProject")
    assert op is not None and op.writable
    assert "authenticated like reads (github_app)" in updated.notes
    page = c.get("/apis/tracker").text
    assert "a GitHub App token made for each call" in page and "writes the same way" in page
