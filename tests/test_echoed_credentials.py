"""An API that sends the credential back must not hand it to the agent.

Some APIs answer with the request they received: an echo or debugging endpoint, an
error page that quotes the URL, a message that names the rejected key. librarian
removes the credential it sent from every answer before the answer goes anywhere:
to the calling agent, to a model, into the audit trail, or into the stored result of
a write.
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

import httpx
import pytest

from librarian.audit import MemoryAuditSink
from librarian.calls import Caller, CallRequest, WriteRequest
from librarian.executor import _echoes, _scrub  # pyright: ignore[reportPrivateUsage]
from librarian.registry import BasicAuth
from librarian.registry.models import ApiKeyAuth, BearerAuth, NoAuth
from librarian.secrets import FileSecretStore
from librarian.service import Librarian
from tests.conftest import BASE, Router

CALLER = Caller(agent="agent-a", principal="kim")
WRITER = Caller(agent="notes-bot", principal="kim@x.test", write_apis=("tracker",))
READ_KEY = "r3ad-key-0123456789"
WRITE_KEY = "wr1te-key-9876543210"
ODD_KEY = "k+e/y=w 1&2<3"  # everything a URL, a JSON string or an HTML page escapes
WHOAMI = CallRequest(api="tracker", operation="whoami")


@pytest.fixture(autouse=True)
async def keys(secrets_store: FileSecretStore) -> None:
    await secrets_store.put("tracker/readonly", READ_KEY)
    await secrets_store.put("tracker/write", WRITE_KEY)
    await secrets_store.put("weather/key", ODD_KEY)


def echo(request: httpx.Request) -> httpx.Response:
    """Answer with the request that arrived, as an echo or debugging endpoint does."""
    return httpx.Response(
        200,
        json={
            "headers": dict(request.headers),
            "url": str(request.url),
            "args": dict(request.url.params),
        },
    )


async def tracker_auth(librarian: Librarian, auth: Any, secret: str) -> None:
    entry = librarian.registry.get("tracker")
    assert entry is not None
    await librarian.registry.save(entry.model_copy(update={"auth": auth}))
    librarian.registry.load()
    await librarian.secrets.put("tracker/readonly", secret)


def database_files(folder: Path) -> dict[str, bytes]:
    return {file.name: file.read_bytes() for file in folder.glob("state.db*")}


def everything(result: Any, audit: MemoryAuditSink) -> str:
    """All that leaves the call: what the caller gets and what the audit trail keeps."""
    return result.model_dump_json() + "".join(event.model_dump_json() for event in audit.events)


async def test_a_read_does_not_return_the_key_the_api_echoed(
    librarian: Librarian, router: Router, audit: MemoryAuditSink
) -> None:
    router.add("GET", f"{BASE}/v1/me", echo)
    result = await librarian.executor.execute(WHOAMI, CALLER)
    assert router.requests[0].headers["authorization"] == f"Bearer {READ_KEY}"  # it was sent
    assert result.ok and result.data["headers"]["authorization"] == "Bearer [redacted]"
    assert result.data["headers"]["accept"] == "application/json"  # the rest is untouched
    assert READ_KEY not in everything(result, audit)


async def test_an_error_that_names_the_key_is_returned_without_it(
    librarian: Librarian, router: Router, audit: MemoryAuditSink
) -> None:
    said = {"message": f"token {READ_KEY} lacks the scope read:user", "token": READ_KEY}
    router.add("GET", f"{BASE}/v1/me", lambda r: httpx.Response(403, json=said))
    result = await librarian.executor.execute(WHOAMI, CALLER)
    assert result.status_code == 403 and result.error and "lacks the scope" in result.error
    assert result.data == {
        "message": "token [redacted] lacks the scope read:user",
        "token": "[redacted]",
    }
    assert READ_KEY not in everything(result, audit)


async def test_basic_auth_is_removed_encoded_and_decoded(
    librarian: Librarian, router: Router, audit: MemoryAuditSink
) -> None:
    pair = "me@example.test:p4ssw0rd-0123"
    await tracker_auth(librarian, BasicAuth(secret_ref="tracker/readonly"), pair)
    encoded = base64.b64encode(pair.encode()).decode()
    router.add(
        "GET",
        f"{BASE}/v1/me",
        lambda r: httpx.Response(
            200,
            json={
                "authorization": r.headers["authorization"],
                "credentials": pair,
                "user": "me@example.test",
                "password": "p4ssw0rd-0123",
            },
        ),
    )
    result = await librarian.executor.execute(WHOAMI, CALLER)
    assert router.requests[0].headers["authorization"] == f"Basic {encoded}"
    assert result.data == {
        "authorization": "Basic [redacted]",
        "credentials": "[redacted]",
        "user": "me@example.test",  # who is calling is not the secret
        "password": "[redacted]",
    }
    assert encoded not in everything(result, audit)


async def test_a_basic_key_sent_as_the_user_name_is_removed(
    librarian: Librarian, router: Router
) -> None:
    key = "key-abcdef0123456789"  # sent as "key:" with an empty password, as some APIs want
    await tracker_auth(librarian, BasicAuth(secret_ref="tracker/readonly"), f"{key}:")
    router.add("GET", f"{BASE}/v1/me", lambda r: httpx.Response(200, json={"user": key}))
    result = await librarian.executor.execute(WHOAMI, CALLER)
    assert result.data == {"user": "[redacted]"}


async def test_a_key_sent_after_a_scheme_word_is_removed_with_and_without_it(
    librarian: Librarian, router: Router
) -> None:
    auth = ApiKeyAuth.model_validate(
        {"type": "api_key", "secret_ref": "tracker/readonly", "in": "header", "name": "X-Api-Key"}
    )
    await tracker_auth(librarian, auth, "Token abcdef0123456789")
    router.add(
        "GET",
        f"{BASE}/v1/me",
        lambda r: httpx.Response(
            200,
            json={
                "header": r.headers["x-api-key"],
                "message": "the token abcdef0123456789 may not do this",
            },
        ),
    )
    result = await librarian.executor.execute(WHOAMI, CALLER)
    assert router.requests[0].headers["x-api-key"] == "Token abcdef0123456789"
    assert result.data == {
        "header": "[redacted]",
        "message": "the token [redacted] may not do this",
    }


async def test_a_key_in_the_query_is_removed_however_the_url_encodes_it(
    librarian: Librarian, router: Router, audit: MemoryAuditSink
) -> None:
    router.add("GET", "https://weather.example.test/forecast", echo)
    result = await librarian.executor.execute(
        CallRequest(api="weather", operation="forecast", args={"city": "Oslo"}), CALLER
    )
    sent = router.requests[0].url
    assert sent.params["appid"] == ODD_KEY  # it was sent
    on_the_wire = sent.query.decode().split("appid=")[1].split("&")[0]
    assert result.data["args"] == {"city": "Oslo", "appid": "[redacted]"}
    assert "city=Oslo" in result.data["url"] and "appid=[redacted]" in result.data["url"]
    left = everything(result, audit)
    for form in (ODD_KEY, on_the_wire, quote(ODD_KEY, safe=""), quote_plus(ODD_KEY)):
        assert form not in left, form


async def test_an_error_page_that_quotes_the_request_is_returned_without_the_key(
    librarian: Librarian, router: Router, audit: MemoryAuditSink
) -> None:
    def not_found(request: httpx.Request) -> httpx.Response:
        page = (
            f"<h1>Not Found</h1><p>The requested URL {html.escape(str(request.url))} was not "
            f"found.</p><p>Key given: {html.escape(request.url.params['appid'])}</p>"
        )
        return httpx.Response(404, text=page, headers={"content-type": "text/html"})

    router.add("GET", "https://weather.example.test/forecast", not_found)
    result = await librarian.executor.execute(
        CallRequest(api="weather", operation="forecast", args={"city": "Oslo"}), CALLER
    )
    on_the_wire = router.requests[0].url.query.decode().split("appid=")[1].split("&")[0]
    assert result.status_code == 404 and isinstance(result.data, str)
    assert "Key given: [redacted]</p>" in result.data and "appid=[redacted]" in result.data
    left = everything(result, audit)
    for form in (ODD_KEY, on_the_wire, html.escape(ODD_KEY)):
        assert form not in left, form


async def test_an_answer_cut_off_inside_the_key_does_not_keep_its_start(
    librarian: Librarian, router: Router
) -> None:
    body = "x" * 80 + f" Authorization: Bearer {READ_KEY}"
    librarian.executor.max_response_bytes = len(body) - 6  # the limit falls inside the key
    router.add("GET", f"{BASE}/v1/me", lambda r: httpx.Response(200, text=body))
    result = await librarian.executor.execute(WHOAMI, CALLER)
    assert result.truncated and isinstance(result.data, str)
    assert result.data.endswith("Authorization: Bearer [redacted]")
    assert READ_KEY[:6] not in result.data


async def test_every_page_of_a_paged_read_is_cleaned(librarian: Librarian, router: Router) -> None:
    def page(request: httpx.Request) -> httpx.Response:
        item = [{"sent": request.headers["authorization"]}]
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=item)
        link = f'<{BASE}/v1/issues?page=2>; rel="next"'
        return httpx.Response(200, json=item, headers={"link": link})

    router.add("GET", f"{BASE}/v1/issues", page)
    result = await librarian.executor.execute(
        CallRequest(api="tracker", operation="listIssues", args={"team": "a"}, max_pages=5), CALLER
    )
    assert result.provenance.pages == 2
    assert result.data == [[{"sent": "Bearer [redacted]"}], [{"sent": "Bearer [redacted]"}]]


async def test_a_write_keeps_no_copy_of_the_key_the_api_echoed(
    tmp_path: Path, router: Router
) -> None:
    """The result of a write is stored with its approval: the key must not be in it."""
    from tests.test_state import replica

    librarian = replica(tmp_path, router)  # with a database, as a deployment has
    await librarian.start()
    await librarian.secrets.put("tracker/write", WRITE_KEY)
    router.add("POST", f"{BASE}/v1/issues/PAY-1/comments", echo)
    request = WriteRequest(
        api="tracker", operation="addComment", args={"key": "PAY-1"}, body={"body": "hello"}
    )
    task = asyncio.create_task(librarian.write(request, WRITER))
    approvals = librarian.approvals._approvals  # pyright: ignore[reportPrivateUsage]
    while not approvals:
        await asyncio.sleep(0.01)
    approval_id = next(iter(approvals))
    await librarian.approvals.approve(approval_id, "kim@x.test")
    outcome = await asyncio.wait_for(task, 5)

    sent = [r for r in router.requests if r.method == "POST"]
    assert len(sent) == 1 and sent[0].headers["authorization"] == f"Bearer {WRITE_KEY}"
    assert outcome.how == "approved" and outcome.result.ok
    assert outcome.result.data["headers"]["authorization"] == "Bearer [redacted]"
    assert WRITE_KEY not in outcome.result.model_dump_json()  # what the agent gets
    status = librarian.write_status(approvals[approval_id])  # what write_status answers later
    assert status["result"]["data"]["headers"]["authorization"] == "Bearer [redacted]"
    assert WRITE_KEY not in json.dumps(status, default=str)
    await librarian.aclose()
    kept = database_files(tmp_path)  # and what the database keeps
    assert kept
    for name, content in kept.items():
        assert WRITE_KEY.encode() not in content, name


async def test_a_value_too_short_to_be_a_key_is_left_alone(
    librarian: Librarian, router: Router, secrets_store: FileSecretStore
) -> None:
    """Replacing a few characters wherever they occur would mangle ordinary data, and a
    value that short protects nothing."""
    await secrets_store.put("tracker/readonly", "ab12")
    answer = {"id": "tab12-7", "note": "cab12"}
    router.add("GET", f"{BASE}/v1/me", lambda r: httpx.Response(200, json=answer))
    result = await librarian.executor.execute(WHOAMI, CALLER)
    assert result.data == answer


def test_the_forms_a_key_can_come_back_in() -> None:
    bearer = BearerAuth(secret_ref="x/y")
    assert _echoes(None, bearer) == ()
    assert _echoes("abcdef0123456789", NoAuth()) == ()
    assert _echoes("abcdef0123456789", bearer) == ("abcdef0123456789",)  # nothing to encode
    in_query = ApiKeyAuth.model_validate(
        {"type": "api_key", "secret_ref": "x/y", "in": "query", "name": "key"}
    )
    forms = _echoes(ODD_KEY, in_query)
    for form in (
        ODD_KEY,
        quote(ODD_KEY, safe=""),
        quote_plus(ODD_KEY),
        html.escape(ODD_KEY),
        json.dumps(ODD_KEY)[1:-1],
    ):
        assert form in forms, form
    # Longest first, so a form that contains another is replaced whole.
    assert [len(form) for form in forms] == sorted((len(form) for form in forms), reverse=True)
    pair = "me@example.test:p4ssw0rd-0123"
    basic = _echoes(pair, BasicAuth(secret_ref="x/y"))
    assert base64.b64encode(pair.encode()).decode() in basic
    assert pair in basic and "p4ssw0rd-0123" in basic
    assert "me@example.test" not in basic


def test_cleaning_reaches_every_string_however_deep() -> None:
    key = "abcdef0123456789"
    deep: Any = f"Bearer {key}"
    for _ in range(5000):  # deeper than a function may call itself
        deep = [deep]
    answer: dict[str, Any] = {
        "deep": deep,
        key: 1,
        "count": 7,
        "nothing": None,
        "list": [f"x {key} y", {"inner": key}, 3.5, True],
    }
    cleaned = _scrub(answer, (key,))
    node = cleaned["deep"]
    while isinstance(node, list):
        node = node[0]  # pyright: ignore[reportUnknownVariableType]
    assert node == "Bearer [redacted]"
    assert cleaned["[redacted]"] == 1 and key not in cleaned
    assert cleaned["count"] == 7 and cleaned["nothing"] is None
    assert cleaned["list"] == ["x [redacted] y", {"inner": "[redacted]"}, 3.5, True]
    assert _scrub(answer, ()) is answer  # no credential was sent: nothing to do
