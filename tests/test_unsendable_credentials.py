"""A stored key the HTTP client cannot send must not end up in an error message.

A key stored from a file often ends with a line break. The HTTP client refuses to put
such a value into a header, and its error quotes the header it refused: the key. That
error would travel to the calling agent (a failed write says why), to the approvers
and into the logs. So librarian sends a key without the whitespace around it, refuses
a key that cannot be sent at all with a reason that does not name it, and never passes
the client's own message on.
"""

from __future__ import annotations

import asyncio
import base64
import http.server
import json
import threading
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from librarian.audit import MemoryAuditSink
from librarian.calls import Caller, CallRequest, WriteRequest
from librarian.config import Settings
from librarian.errors import ApiUnavailable
from librarian.executor import CredentialMissing
from librarian.registry import BasicAuth
from librarian.secrets import FileSecretStore
from librarian.service import Librarian
from tests.conftest import BASE, PUBLIC, TRACKER, RecordingNotifier, Router, write_entries

CALLER = Caller(agent="agent-a", principal="kim")
WRITER = Caller(agent="notes-bot", principal="kim@x.test", write_apis=("tracker",))
KEY = "r3ad-key-0123456789"
WHOAMI = CallRequest(api="tracker", operation="whoami")
COMMENT = WriteRequest(
    api="tracker", operation="addComment", args={"key": "PAY-1"}, body={"body": "hello"}
)


def as_stored(
    monkeypatch: pytest.MonkeyPatch, store: FileSecretStore, values: dict[str, str]
) -> None:
    """Hand values back exactly as they were stored, as the environment and AWS stores do
    (the file store trims them when it reads)."""

    async def get(ref: str) -> str | None:
        return values.get(ref)

    monkeypatch.setattr(store, "get", get)


async def write(librarian: Librarian) -> Any:
    entry = librarian.registry.get("tracker")
    assert entry is not None
    op = entry.operation("addComment")
    assert op is not None
    return await librarian.executor.execute_write(entry, op, COMMENT, WRITER)


@pytest.mark.parametrize(
    "stored",
    [f"{KEY}\n", f"{KEY}\r\n", f"  {KEY}\t", f"﻿{KEY}\n"],
    ids=["line break", "windows line break", "spaces and a tab", "byte-order mark"],
)
async def test_whitespace_around_a_stored_key_is_not_sent(
    librarian: Librarian,
    router: Router,
    monkeypatch: pytest.MonkeyPatch,
    secrets_store: FileSecretStore,
    stored: str,
) -> None:
    as_stored(monkeypatch, secrets_store, {"tracker/readonly": stored, "tracker/write": stored})
    router.add("GET", f"{BASE}/v1/me", lambda r: httpx.Response(200, json={}))
    router.add("POST", f"{BASE}/v1/issues/PAY-1/comments", lambda r: httpx.Response(201, json={}))
    assert (await librarian.executor.execute(WHOAMI, CALLER)).ok
    assert (await write(librarian)).ok
    assert [r.headers["authorization"] for r in router.requests] == [f"Bearer {KEY}"] * 2


@pytest.mark.parametrize(
    "stored",
    [f"{KEY[:8]}\n{KEY[8:]}", f"{KEY[:8]}\r{KEY[8:]}", f"{KEY[:8]}\x00{KEY[8:]}", f"{KEY}-é"],
    ids=["line break inside", "carriage return inside", "NUL inside", "not ASCII"],
)
async def test_a_key_that_cannot_be_sent_is_refused_without_naming_it(
    librarian: Librarian,
    router: Router,
    audit: MemoryAuditSink,
    monkeypatch: pytest.MonkeyPatch,
    secrets_store: FileSecretStore,
    stored: str,
) -> None:
    as_stored(monkeypatch, secrets_store, {"tracker/readonly": stored, "tracker/write": stored})
    with pytest.raises(CredentialMissing) as read:
        await librarian.executor.execute(WHOAMI, CALLER)
    with pytest.raises(CredentialMissing) as written:
        await write(librarian)
    assert router.requests == []  # nothing was sent
    assert "the stored credential contains a line break" in read.value.reason
    assert "the write credential contains a line break" in written.value.reason
    said = str(read.value) + str(written.value)
    said += "".join(event.model_dump_json() for event in audit.events)
    assert KEY[:8] not in said and KEY[8:] not in said


async def test_only_a_key_that_travels_in_a_header_is_checked(
    librarian: Librarian,
    router: Router,
    monkeypatch: pytest.MonkeyPatch,
    secrets_store: FileSecretStore,
) -> None:
    """Basic auth is encoded and a key in the query is escaped: both can carry anything."""
    entry = librarian.registry.get("tracker")
    assert entry is not None
    await librarian.registry.save(
        entry.model_copy(update={"auth": BasicAuth(secret_ref="tracker/readonly")})
    )
    librarian.registry.load()
    pair, odd = "me@example.test:pass\nword-0123", "we\nather-key-0123"
    as_stored(monkeypatch, secrets_store, {"tracker/readonly": f"{pair}\n", "weather/key": odd})
    router.add("GET", f"{BASE}/v1/me", lambda r: httpx.Response(200, json={}))
    router.add(
        "GET", "https://weather.example.test/forecast", lambda r: httpx.Response(200, json=[])
    )
    await librarian.executor.execute(WHOAMI, CALLER)
    await librarian.executor.execute(
        CallRequest(api="weather", operation="forecast", args={"city": "Oslo"}), CALLER
    )
    basic, in_query = router.requests
    assert basic.headers["authorization"] == "Basic " + base64.b64encode(pair.encode()).decode()
    assert in_query.url.params["appid"] == odd


async def test_the_http_clients_own_message_is_not_passed_on(
    librarian: Librarian,
    router: Router,
    notifier: RecordingNotifier,
    secrets_store: FileSecretStore,
) -> None:
    """Whatever else the client may refuse to send: its message quotes the header."""
    await secrets_store.put("tracker/readonly", KEY)
    await secrets_store.put("tracker/write", KEY)

    def refuse(request: httpx.Request) -> httpx.Response:
        header = request.headers["authorization"]
        raise httpx.LocalProtocolError(f"Illegal header value b'{header}'")

    router.add("GET", f"{BASE}/v1/me", refuse)
    router.add("POST", f"{BASE}/v1/issues/PAY-1/comments", refuse)
    with pytest.raises(ApiUnavailable) as read:
        await librarian.executor.execute(WHOAMI, CALLER)
    assert "a header that is not valid HTTP" in str(read.value) and KEY not in str(read.value)
    assert read.value.detail == ""  # the client's own words are not even kept for the log
    assert read.value.__suppress_context__  # nor shown by a logged traceback

    # A write: its failure is stored with the approval, shown to the approvers and
    # returned to the agent.
    task = asyncio.create_task(librarian.write(COMMENT, WRITER))
    approvals = librarian.approvals._approvals  # pyright: ignore[reportPrivateUsage]
    while not approvals:
        await asyncio.sleep(0.01)
    approval_id = next(iter(approvals))
    await librarian.approvals.approve(approval_id, "kim@x.test")
    with pytest.raises(ApiUnavailable) as failed:
        await asyncio.wait_for(task, 5)
    assert "The write was not sent" in str(failed.value) and KEY not in str(failed.value)
    assert KEY not in approvals[approval_id].model_dump_json()
    assert KEY not in json.dumps(librarian.write_status(approvals[approval_id]), default=str)
    assert KEY not in "".join(a.model_dump_json() for _, a in notifier.write_events)


class Recorder(http.server.BaseHTTPRequestHandler):
    """A real HTTP server, so requests go through the real HTTP client."""

    seen: list[str | None]

    def do_GET(self) -> None:
        self.seen.append(self.headers.get("Authorization"))
        body = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def real_server() -> Iterator[tuple[str, list[str | None]]]:
    seen: list[str | None] = []
    handler = type("Handler", (Recorder,), {"seen": seen})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", seen
    server.shutdown()
    server.server_close()
    thread.join(5)


# What the real client does with each value: librarian must refuse exactly the ones the
# client would refuse (and so quote), and send the rest untouched.
REFUSED = {
    "line break": "abc\ndef-0123456789",
    "carriage return": "abc\rdef-0123456789",
    "NUL": "abc\x00def-0123456789",
    "vertical tab": "abc\x0bdef-0123456789",
    "form feed": "abc\x0cdef-0123456789",
    "not ASCII": "abcédef-0123456789",
}
SENT = {
    "plain": "abc-DEF_0123456789",
    "space inside": "abc def-0123456789",
    "tab inside": "abc\tdef-0123456789",
    "punctuation": "a+b/c=d.e~f:g;h,i!0123456789",
}


async def test_the_real_http_client_never_gets_a_key_it_would_refuse(
    settings: Settings,
    real_server: tuple[str, list[str | None]],
    monkeypatch: pytest.MonkeyPatch,
    secrets_store: FileSecretStore,
    audit: MemoryAuditSink,
    notifier: RecordingNotifier,
) -> None:
    base, seen = real_server
    write_entries(settings.registry_dir, {**TRACKER, "base_url": f"{base}/v1"}, PUBLIC)
    librarian = Librarian(
        settings,
        secrets_store=secrets_store,
        audit=audit,
        notifier=notifier,
        http_client=httpx.AsyncClient(),  # the real thing, not a fake transport
    )
    librarian.load()
    try:
        for name, value in SENT.items():
            as_stored(monkeypatch, secrets_store, {"tracker/readonly": f"{value}\n"})
            result = await librarian.executor.execute(WHOAMI, CALLER)
            assert result.ok, name
            assert seen[-1] == f"Bearer {value}", name
        arrived = len(seen)
        for name, value in REFUSED.items():
            as_stored(monkeypatch, secrets_store, {"tracker/readonly": value})
            with pytest.raises(CredentialMissing) as refused:  # not the client's own error
                await librarian.executor.execute(WHOAMI, CALLER)
            assert "abc" not in str(refused.value) and "0123456789" not in str(refused.value), name
        assert len(seen) == arrived  # none of them left the process

        # And without the check, the client's error does quote the key: this is the
        # message that must never be passed on.
        async with httpx.AsyncClient() as client:
            with pytest.raises(httpx.LocalProtocolError) as quoted:
                await client.get(f"{base}/v1/me", headers={"Authorization": "Bearer abc\ndef"})
        assert "abc" in str(quoted.value)
    finally:
        await librarian.aclose()
