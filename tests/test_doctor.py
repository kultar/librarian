"""`librarian doctor`: credentials that got into the database or the audit file are
found by the secret's name, never shown, and can be replaced where they sit."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import JSON, Column, MetaData, String, Table, insert, select
from typer.testing import CliRunner

from librarian.approvals import ApprovalState
from librarian.audit import AuditEvent
from librarian.calls import Caller, WriteRequest
from librarian.cli import app
from librarian.doctor import Report, examine
from librarian.oauth import UserTokens, principal_key
from librarian.registry import BasicAuth
from librarian.registry.models import BearerAuth
from librarian.secrets import AwsSecretsManagerStore
from librarian.service import Librarian
from tests.conftest import Router
from tests.test_aws_secrets import FakeSecretsManager
from tests.test_state import replica

KIM = Caller(agent="notes-bot", principal="kim@x.test", write_apis=("tracker",))
READ_PAIR = "me@example.test:r3ad-pass-0123456789"
WRITE_KEY = "wr1te-key-9876543210"
JSON_TOKEN = "json-token-0123456789"
PERSON_TOKEN = "pers0n-token-0123456789"
EVERY_VALUE = (WRITE_KEY, "r3ad-pass-0123456789", PERSON_TOKEN)
OLD_TASKS = Table(
    "old_tasks", MetaData(), Column("id", String(36), primary_key=True), Column("artifacts", JSON)
)


async def seeded(tmp_path: Path, router: Router) -> Librarian:
    """A database as a version before 0.20 could leave it: credentials in three places."""
    librarian = replica(tmp_path, router)
    await librarian.start()
    entry = librarian.registry.get("tracker")
    assert entry is not None and librarian.state is not None and librarian.engine is not None
    await librarian.registry.save(
        entry.model_copy(update={"auth": BasicAuth(secret_ref="tracker/readonly")})
    )
    await librarian.secrets.put("tracker/readonly", READ_PAIR)
    await librarian.secrets.put("tracker/write", f"{WRITE_KEY}\n")  # as stored from a file
    person = principal_key("drive", "kim@x.test")
    tokens = UserTokens(access_token=PERSON_TOKEN)  # stored with token_type "Bearer"
    await librarian.secrets.put(person, tokens.model_dump_json())

    # 1. A write whose failure quoted the key, and whose earlier answer echoed it.
    op = entry.operation("addComment")
    assert op is not None
    request = WriteRequest(api="tracker", operation="addComment", args={"key": "PAY-1"})
    approval = await librarian.approvals.open(entry, op, request, KIM)
    approval.state = ApprovalState.FAILED
    approval.note = f"approved write failed: Illegal header value b'Bearer {WRITE_KEY}\\n'"
    approval.result = {"data": {"headers": {"Authorization": [f"Bearer {WRITE_KEY}"]}}}
    await librarian.approvals._save(approval)  # pyright: ignore[reportPrivateUsage]
    # 2. An audit row quoting what an API said, with the Basic pair as it was sent.
    sent = base64.b64encode(READ_PAIR.encode()).decode()
    await librarian.state.record_audit(
        AuditEvent(event="call", api="tracker", extra={"upstream": f"bad header: Basic {sent}"})
    )
    # 3. A table librarian does not define itself (the A2A tasks), with a JSON column.
    async with librarian.engine.begin() as connection:
        await connection.run_sync(OLD_TASKS.metadata.create_all)
        artifacts = [{"parts": [{"data": {"token": PERSON_TOKEN, PERSON_TOKEN: "as a key"}}]}]
        await connection.execute(insert(OLD_TASKS).values(id="task-1", artifacts=artifacts))
    return librarian


def everything(report: Report) -> str:
    return repr(report) + "".join(repr(f) for f in report.findings)


async def test_stored_credentials_are_found_by_name_and_never_shown(
    tmp_path: Path, router: Router
) -> None:
    librarian = await seeded(tmp_path, router)
    report = await examine(librarian)
    await librarian.aclose()
    found = sorted((f.secret, f.where.split(" ")[0]) for f in report.findings)
    person = principal_key("drive", "kim@x.test")
    assert found == [
        (person, "old_tasks"),
        ("tracker/readonly", "audit_events"),
        ("tracker/write", "librarian_documents"),
    ]
    assert all(f.kind == "stored" and not f.redacted for f in report.findings)
    assert len(report.open) == 3 and report.secrets >= 3 and report.rows > 3 and report.listed
    for value in (*EVERY_VALUE, READ_PAIR):
        assert value not in everything(report)
    # Nothing was changed: looking is safe to do at any time.
    assert WRITE_KEY.encode() in (tmp_path / "state.db").read_bytes()


async def test_redacting_replaces_them_where_they_sit(tmp_path: Path, router: Router) -> None:
    librarian = await seeded(tmp_path, router)
    assert librarian.engine is not None
    report = await examine(librarian, redact=True)
    assert len(report.findings) == 3 and all(f.redacted for f in report.findings)
    assert report.open == []
    # What was there is still readable, without the credential.
    approval = next(iter(librarian.approvals._approvals.values()))  # pyright: ignore[reportPrivateUsage]
    stored = await librarian.approvals.load(approval.id)
    assert stored is not None and stored.state is ApprovalState.FAILED
    # Only the credential went: "Bearer" stays, though the person's sign-in (a JSON
    # document in the secret store) contains that word as its token type.
    assert stored.note == "approved write failed: Illegal header value b'Bearer [redacted]\\n'"
    assert stored.result == {"data": {"headers": {"Authorization": ["Bearer [redacted]"]}}}
    async with librarian.engine.connect() as connection:
        row = (await connection.execute(select(OLD_TASKS))).one()
    assert row.artifacts == [
        {"parts": [{"data": {"token": "[redacted]", "[redacted]": "as a key"}}]}
    ]
    again = await examine(librarian)
    assert again.findings == []
    await librarian.aclose()
    content = (tmp_path / "state.db").read_bytes()
    for value in EVERY_VALUE:
        assert value.encode() not in content


async def test_the_audit_file_is_read_but_never_rewritten(tmp_path: Path, router: Router) -> None:
    librarian = replica(tmp_path, router)
    await librarian.start()
    await librarian.secrets.put("tracker/write", WRITE_KEY)
    audit_file = tmp_path / "audit.jsonl"
    lines = ['{"audit": {"event": "call"}}', f'{{"audit": {{"detail": "Bearer {WRITE_KEY}"}}}}']
    audit_file.write_text("\n".join(lines) + "\n", "utf-8")
    librarian.settings.audit_file = audit_file
    report = await examine(librarian, redact=True)
    await librarian.aclose()
    assert [(f.secret, f.where, f.redacted) for f in report.findings] == [
        ("tracker/write", f"{audit_file}, line 2", False)
    ]
    assert len(report.open) == 1  # someone has to deal with the file by hand
    assert WRITE_KEY in audit_file.read_text("utf-8")


async def test_a_stored_key_that_cannot_be_used_is_reported(tmp_path: Path, router: Router) -> None:
    librarian = replica(tmp_path, router)
    await librarian.start()
    await librarian.secrets.put("tracker/readonly", "r3ad-key-\n0123456789")
    await librarian.secrets.put("tracker/write", f"  {WRITE_KEY}\n")  # only space around it
    report = await examine(librarian)
    await librarian.aclose()
    assert [(f.kind, f.secret) for f in report.findings] == [("unusable", "tracker/readonly")]
    assert "contains a line break" in report.findings[0].where
    assert "r3ad-key" not in everything(report)


async def test_a_store_that_will_not_list_its_secrets_is_still_checked(
    tmp_path: Path, router: Router, monkeypatch: pytest.MonkeyPatch
) -> None:
    librarian = await seeded(tmp_path, router)

    async def denied() -> list[str]:
        raise PermissionError("not allowed to list secrets")

    monkeypatch.setattr(librarian.secrets, "list_names", denied)
    report = await examine(librarian)
    await librarian.aclose()
    assert not report.listed
    # The keys the registry names are still looked for; the ones nothing names are not.
    assert sorted({f.secret for f in report.findings}) == ["tracker/readonly", "tracker/write"]


async def test_one_key_of_a_json_secret_is_found_under_the_name_the_registry_uses(
    tmp_path: Path, router: Router
) -> None:
    """A secret can hold several keys; the registry names the one it uses, and only that
    one is a credential librarian ever sent anywhere."""
    librarian = replica(tmp_path, router)
    await librarian.start()
    entry = librarian.registry.get("tracker")
    assert entry is not None and librarian.state is not None
    write_auth = BearerAuth.model_validate(
        {"type": "bearer", "secret_ref": "service/tracker#write_token"}
    )
    await librarian.registry.save(entry.model_copy(update={"write_auth": write_auth}))
    document = json.dumps({"write_token": JSON_TOKEN, "environment": "production"})
    librarian.secrets = AwsSecretsManagerStore(
        client=FakeSecretsManager({"service/tracker": document})
    )
    await librarian.state.record_audit(
        AuditEvent(event="write", extra={"upstream": f"{JSON_TOKEN} is not valid in production"})
    )
    report = await examine(librarian, redact=True)
    await librarian.aclose()
    assert [(f.secret, f.where) for f in report.findings] == [
        ("service/tracker#write_token", "audit_events 1 (extra)")
    ]
    kept = (tmp_path / "state.db").read_bytes()
    assert JSON_TOKEN.encode() not in kept and b"is not valid in production" in kept


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("LIBRARIAN_REGISTRY_DIR", str(tmp_path / "registry"))
    monkeypatch.setenv("LIBRARIAN_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setenv("LIBRARIAN_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/state.db")
    monkeypatch.setenv("LIBRARIAN_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    return tmp_path


def test_the_command_reports_redacts_and_says_what_to_do(state: Path, router: Router) -> None:
    async def seed() -> None:
        await (await seeded(state, router)).aclose()

    asyncio.run(seed())
    runner = CliRunner()
    looked = runner.invoke(app, ["doctor"])
    assert looked.exit_code == 1, looked.output
    assert "tracker/write is stored in librarian_documents approval/" in looked.output
    assert "replace it at the API" in looked.output and "--redact" in looked.output
    as_json: dict[str, Any] = json.loads(runner.invoke(app, ["doctor", "--json"]).output)
    assert len(as_json["findings"]) == 3 and as_json["listed"] is True
    fixed = runner.invoke(app, ["doctor", "--redact"])
    assert fixed.exit_code == 0, fixed.output
    assert fixed.output.count("[now redacted]") == 3
    clean = runner.invoke(app, ["doctor"])
    assert clean.exit_code == 0 and "Nothing found." in clean.output
    for output in (looked.output, fixed.output, clean.output, json.dumps(as_json)):
        for value in (*EVERY_VALUE, READ_PAIR):
            assert value not in output
