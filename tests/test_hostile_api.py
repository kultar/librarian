"""Every misbehaviour in `tests/hostile.py`, through every way an agent reaches librarian.

Whatever an API does, three things must hold:

1. **The agent is told something it can act on.** A result, a refusal with a reason, or
   "the API did not answer": never "Internal error in the librarian", which is what any
   failure nobody thought of turns into.
2. **No credential leaves.** Not in the answer, the audit trail, the log, the notice to
   approvers or the stored result of a write.
3. **A write ends.** Its approval reaches a final state that says what happened, and
   says whether the request can have reached the API.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text as sql

from librarian import plans
from librarian.approvals import ApprovalState
from librarian.audit import MemoryAuditSink
from librarian.calls import Caller, CallRequest, WriteRequest
from librarian.config import Settings
from librarian.doctor import examine
from librarian.executor import PolicyDenied
from librarian.secrets import FileSecretStore
from librarian.service import Librarian, LibrarianError
from librarian.state import make_engine
from tests.conftest import BASE, TRACKER, RecordingNotifier, Router, write_entries
from tests.hostile import BEHAVIOURS, KEYS, READ_KEY, WRITE_KEY, Behaviour
from tests.surfaces import (
    APPROVER,
    ENDED,
    PLAN,
    READ,
    WRITE,
    Reply,
    a2a_plan,
    a2a_read,
    a2a_served,
    a2a_task,
    a2a_write,
    approve_the_write,
    mcp_at,
    mcp_plan,
    mcp_read,
    mcp_write,
    mcp_write_in,
    served,
)

AGENT = Caller(agent="agent-a", write_apis=("tracker",), owner=APPROVER)
# What an agent is told when something failed that nobody had thought of: librarian's own
# catch-all over A2A, and the MCP library's (a tool error with no reason after its name).
UNEXPLAINED = ("Internal error", "Traceback", "object has no attribute")
MASKED = re.compile(r"Error executing tool \w+\s*$")


def strict_json(text: str) -> Any:
    """Parse as a client in any language would: NaN and Infinity are not JSON."""

    def refuse(token: str) -> None:
        raise ValueError(f"{token} is not JSON")

    return json.loads(text, parse_constant=refuse)


async def service_read(librarian: Librarian, **more: Any) -> Reply:
    """Straight into the service: only errors the protocols know how to word may come out."""
    try:
        result = await librarian.execute(
            CallRequest.model_validate({**READ, **more}), AGENT, wait_for_credential=False
        )
    except (PolicyDenied, LibrarianError) as exc:
        return Reply("failed", str(exc), None, str(exc))
    return Reply("completed", result.error or "", result.model_dump(mode="json"), _json(result))


async def service_write(librarian: Librarian) -> Reply:
    task = asyncio.create_task(librarian.write(WriteRequest.model_validate(WRITE), AGENT))
    await approve_the_write(librarian)
    try:
        outcome = await asyncio.wait_for(task, 10)
    except (PolicyDenied, LibrarianError) as exc:
        return Reply("failed", str(exc), None, str(exc))
    result = outcome.result
    return Reply("completed", result.error or "", result.model_dump(mode="json"), _json(result))


async def service_plan(librarian: Librarian) -> Reply:
    task = asyncio.create_task(librarian.plan(plans.parse(PLAN), AGENT))
    await approve_the_write(librarian)
    try:
        outcome = await asyncio.wait_for(task, 10)
    except (PolicyDenied, LibrarianError) as exc:
        return Reply("failed", str(exc), None, str(exc))
    given = {"steps": outcome.steps, "result": outcome.result.model_dump(mode="json")}
    return Reply("completed", outcome.message, given, json.dumps(given))


def _json(result: Any) -> str:
    return str(result.model_dump_json())


Surface = Callable[..., Awaitable[Reply]]
READS: dict[str, Surface] = {"service": service_read, "a2a": a2a_read, "mcp": mcp_read}
WRITES: dict[str, Surface] = {"service": service_write, "a2a": a2a_write, "mcp": mcp_write}
PLANS: dict[str, Surface] = {"service": service_plan, "a2a": a2a_plan, "mcp": mcp_plan}


@pytest.fixture
def hostile(
    librarian: Librarian, router: Router, caplog: pytest.LogCaptureFixture
) -> Callable[[Behaviour], None]:
    caplog.set_level(logging.DEBUG, logger="librarian")
    librarian.settings.credential_wait_seconds = 0.2  # a read that needs a key gives up soon

    def install(behaviour: Behaviour) -> None:
        router.add("GET", f"{BASE}/v1/me", behaviour.handler)
        router.add("POST", f"{BASE}/v1/issues/PAY-1/comments", behaviour.handler)
        if behaviour.limit is not None:
            librarian.executor.max_response_bytes = behaviour.limit

    return install


@pytest.fixture(autouse=True)
async def keys(secrets_store: FileSecretStore) -> None:
    await secrets_store.put("tracker/readonly", READ_KEY)
    await secrets_store.put("tracker/write", WRITE_KEY)


def check(
    reply: Reply,
    behaviour: Behaviour,
    librarian: Librarian,
    audit: MemoryAuditSink,
    notifier: RecordingNotifier,
    caplog: pytest.LogCaptureFixture,
    *,
    write: bool,
) -> None:
    # 1. The agent is told something it can act on.
    assert reply.state in ("completed", "failed", "rejected"), reply
    for phrase in UNEXPLAINED:
        assert phrase not in reply.text, reply.text
    assert not MASKED.search(reply.text), reply.text
    if reply.data is not None:
        strict_json(reply.raw)  # any client can parse what it was given
    events = [(event.event, event.detail or "", event.status_code) for event in audit.events]
    attempts = [e for e in events if e[0] == ("write" if write else "call")]
    if behaviour.silent:
        assert reply.state == "failed", reply
        assert "Issue Tracker" in reply.text, reply.text  # which API
        if not write:
            assert "retry" in reply.text.lower(), reply.text
        elif behaviour.reached:
            assert "may have" in reply.text, reply.text  # it could have reached the API
        else:
            assert "was not sent" in reply.text, reply.text
        # ...and the attempt is on record, saying it got no answer.
        assert attempts and attempts[-1][1].startswith("no answer: "), events
    elif write:
        assert attempts and attempts[-1][2] is not None, events  # the write, with its status
    # 2. No credential leaves, whole or in a recognisable part.
    approvals = librarian.approvals._approvals  # pyright: ignore[reportPrivateUsage]
    places = {
        "the agent's answer": reply.raw + reply.text,
        "the audit trail": "".join(event.model_dump_json() for event in audit.events),
        "the log": caplog.text,
        "the approvers' notices": "".join(a.model_dump_json() for _, a in notifier.write_events),
        "the stored approval": "".join(a.model_dump_json() for a in approvals.values()),
    }
    for place, text in places.items():
        for key in KEYS:
            assert key not in text, f"{place} holds a key"
            assert key[:10] not in text, f"{place} holds the start of a key"
    assert "\x1b" not in caplog.text, "the log holds a terminal escape an API sent"
    # 3. A write ends.
    if write:
        states = {approval.state for approval in approvals.values()}
        assert states <= {ApprovalState.EXECUTED, ApprovalState.FAILED}, states
        for approval in approvals.values():
            json.loads(approval.model_dump_json())  # what is stored can be read back


@pytest.mark.parametrize("surface", READS)
@pytest.mark.parametrize("name", BEHAVIOURS)
async def test_a_read_survives(
    name: str,
    surface: str,
    hostile: Callable[[Behaviour], None],
    librarian: Librarian,
    audit: MemoryAuditSink,
    notifier: RecordingNotifier,
    caplog: pytest.LogCaptureFixture,
) -> None:
    behaviour = BEHAVIOURS[name]
    hostile(behaviour)
    more = {"max_pages": 20} if name == "endless_pages" else {}
    reply = await READS[surface](librarian, **more)
    check(reply, behaviour, librarian, audit, notifier, caplog, write=False)


@pytest.mark.parametrize("surface", WRITES)
@pytest.mark.parametrize("name", BEHAVIOURS)
async def test_a_write_survives(
    name: str,
    surface: str,
    hostile: Callable[[Behaviour], None],
    librarian: Librarian,
    audit: MemoryAuditSink,
    notifier: RecordingNotifier,
    caplog: pytest.LogCaptureFixture,
) -> None:
    behaviour = BEHAVIOURS[name]
    hostile(behaviour)
    reply = await WRITES[surface](librarian)
    check(reply, behaviour, librarian, audit, notifier, caplog, write=True)


@pytest.mark.parametrize("surface", PLANS)
@pytest.mark.parametrize("name", BEHAVIOURS)
async def test_a_plan_survives(
    name: str,
    surface: str,
    hostile: Callable[[Behaviour], None],
    librarian: Librarian,
    router: Router,
    audit: MemoryAuditSink,
    notifier: RecordingNotifier,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The same in the middle of a plan of three writes. Beyond what holds for one write:
    the write after it is only sent when it succeeded and answered what that write needs,
    and the plan ends saying how far it got."""
    behaviour = BEHAVIOURS[name]
    hostile(behaviour)  # what the middle step meets
    received: dict[str, Any] = {}

    def as_usual(request: httpx.Request) -> httpx.Response:
        received[request.url.path.split("/")[-2]] = json.loads(request.content)
        return httpx.Response(201, json={"id": 9})

    router.add("POST", f"{BASE}/v1/issues/PAY-0/comments", as_usual)
    router.add("POST", f"{BASE}/v1/issues/PAY-2/comments", as_usual)
    reply = await PLANS[surface](librarian)
    check(reply, behaviour, librarian, audit, notifier, caplog, write=True)
    [approval] = librarian.approvals._approvals.values()  # pyright: ignore[reportPrivateUsage]
    assert "PAY-0" in received and len(approval.steps) == 3
    answers = approval.step_results
    middle = answers[1] if len(answers) > 1 else None
    if "PAY-2" in received:
        # It went on: so the middle step succeeded, and what the last one was given from
        # its answer is what the agent was shown of it (never a key).
        assert middle is not None and middle["ok"], middle
        assert received["PAY-2"]["after"] == middle["data"]["id"]
        assert approval.state is ApprovalState.EXECUTED and len(answers) == 3
    else:
        assert approval.state is ApprovalState.FAILED and len(answers) <= 2
        where = "step 2 of 3 (middle)" if middle is None or not middle["ok"] else "step 3 of 3"
        assert where in (approval.note or ""), approval.note
        if behaviour.silent:
            assert middle is None  # no answer: none is on record
    for key in KEYS:
        assert key not in json.dumps(received), "a later step was sent a key"


# -- what is kept -----------------------------------------------------------------------
#
# An answer does not only travel to the agent: the task it belongs to and the result of
# a write are stored, and another replica reads them later. SQLite takes anything; a
# real Postgres is stricter about what text and JSON may hold, so the same checks run
# against one when LIBRARIAN_TEST_POSTGRES_URL names a database that may be emptied.

KEPT = [
    "echo_json",
    "echo_in_keys",
    "not_a_number",
    "nul_character",
    "lone_surrogate",
    "huge_integer",
    "deep_json",
    "binary",
    "terminal_escapes",
    "hangs_up",
    "rate_limit_overflow",
]
POSTGRES = os.environ.get("LIBRARIAN_TEST_POSTGRES_URL")
DATABASES = [
    pytest.param(None, id="sqlite"),
    pytest.param(
        POSTGRES,
        id="postgres",
        marks=pytest.mark.skipif(
            not POSTGRES, reason="set LIBRARIAN_TEST_POSTGRES_URL to an empty test database"
        ),
    ),
]


def on_database(tmp_path: Path, router: Router, url: str) -> Librarian:
    """A replica: its own process state, the shared database, secrets and audit file."""
    write_entries(tmp_path / "registry", TRACKER)
    settings = Settings(
        registry_dir=tmp_path / "registry",
        secrets_dir=tmp_path / "secrets",
        database_url=url,
        audit_file=tmp_path / "audit.jsonl",
        public_url="https://librarian.example.test",
        allow_private_networks=True,
        rate_limit_per_minute=1000,
    )
    return Librarian(
        settings,
        secrets_store=FileSecretStore(tmp_path / "secrets"),
        notifier=RecordingNotifier(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(router)),
    )


async def emptied(url: str) -> None:
    engine = make_engine(url)
    async with engine.begin() as connection:
        await connection.execute(sql("DROP SCHEMA public CASCADE"))
        await connection.execute(sql("CREATE SCHEMA public"))
    await engine.dispose()


@pytest.mark.parametrize("database", DATABASES)
@pytest.mark.parametrize("name", KEPT)
async def test_what_it_answers_is_kept_and_read_back_by_another_replica(
    name: str, database: str | None, tmp_path: Path, router: Router
) -> None:
    behaviour = BEHAVIOURS[name]
    if database:
        await emptied(database)
    url = database or f"sqlite+aiosqlite:///{tmp_path}/state.db"
    router.add("GET", f"{BASE}/v1/me", behaviour.handler)
    router.add("POST", f"{BASE}/v1/issues/PAY-1/comments", behaviour.handler)

    first = on_database(tmp_path, router, url)
    async with served(first) as base:  # started, and stopped at the end, as a pod is
        read = await a2a_served(first, base, write=False)
        written = await a2a_served(first, base, write=True)
        async with mcp_at(base) as session:
            polled = await mcp_write_in(first, session)
    assert read.state in ("completed", "failed") and written.state in ("completed", "failed")

    second = on_database(tmp_path, router, url)
    async with served(second) as base:
        for before in (read, written):
            after = await a2a_task(second, base, before.ref)
            assert (after.state, after.text) == (before.state, before.text)
            strict_json(after.raw)
        stored = await second.approvals.load(polled.ref)
        assert stored is not None and stored.state.value in ENDED
        assert second.write_status(stored)["state"] == stored.state.value
        report = await examine(second)
    # No credential anywhere in the database or the audit file, by librarian's own check.
    assert report.findings == [] and report.rows > 0 and report.secrets >= 2
