"""A request body sent as a form, where the API's description says it takes one.

An agent gives a body as an object, whatever the API takes. Stripe's whole API, and
others of its age, take a form (`application/x-www-form-urlencoded`) and refuse JSON, so
a write that was read, approved and sent as JSON failed there. The description says how
an operation takes its body and how a form writes a list; librarian sends it that way.
The checks on what goes over the wire use a real HTTP client and a real server.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from librarian import forms
from librarian.approvals import ApprovalState, WriteApproval
from librarian.audit import MemoryAuditSink
from librarian.calls import Caller, CallRequest, WriteRequest
from librarian.capabilities import described
from librarian.config import Settings
from librarian.drift import diff, proposed_update, told
from librarian.executor import PolicyDenied
from librarian.openapi import entry_from_spec
from librarian.registry.models import Body, Operation
from librarian.registry.store import dump_entry
from librarian.secrets import FileSecretStore
from librarian.service import Librarian
from librarian.web import approval_routes
from tests.conftest import RecordingNotifier, write_entries

WRITER = Caller(agent="billing-bot", principal="kim@x.test", write_apis=("pay",))
REFUND = {
    "amount": 1000,
    "charge": "ch_123",
    "reverse_transfer": False,
    "note": None,
    "metadata": {"order_id": "6735", "by": "kim & co"},
    "expand": ["charge", "customer"],
    "items": [{"price": "p1", "quantity": 2}, {"price": "p2", "tags": ["a", "b"]}],
}


# -- an object written as a form ---------------------------------------------------------


def test_fields_inside_others_go_in_brackets_and_a_list_as_the_api_writes_it() -> None:
    brackets = forms.pairs(REFUND, "brackets")
    assert brackets == [
        ("amount", "1000"),
        ("charge", "ch_123"),
        ("reverse_transfer", "false"),  # as a form says it, not Python's False
        ("note", ""),  # nothing: the empty value, which clears a field
        ("metadata[order_id]", "6735"),
        ("metadata[by]", "kim & co"),
        ("expand[0]", "charge"),
        ("expand[1]", "customer"),
        ("items[0][price]", "p1"),
        ("items[0][quantity]", "2"),
        ("items[1][price]", "p2"),
        ("items[1][tags][0]", "a"),
        ("items[1][tags][1]", "b"),
    ]
    repeated = forms.pairs(REFUND, "repeated")
    assert ("expand", "charge") in repeated and ("expand", "customer") in repeated
    assert ("items[1][tags]", "a") in repeated and ("items[1][tags]", "b") in repeated
    # Which fields belong to which item can only be said with numbers, whatever the style.
    assert ("items[0][price]", "p1") in repeated and ("items[1][price]", "p2") in repeated
    commas = forms.pairs(REFUND, "commas")
    assert ("expand", "charge,customer") in commas and ("items[1][tags]", "a,b") in commas
    assert forms.pairs({"tags": [], "metadata": {}, "n": 1.5, "on": True}) == [
        ("tags", ""),
        ("metadata", ""),
        ("n", "1.5"),
        ("on", "true"),
    ]
    assert forms.pairs({}) == []


def test_what_goes_over_the_wire_reads_back_as_the_same_fields() -> None:
    odd = {"q": "a&b=c d", "name": "Zoë / 東京", "metadata": {"k[x]": "1+1"}}
    sent = forms.encoded(odd, "brackets")
    assert sent.isascii() and b" " not in sent
    assert parse_qsl(sent.decode(), keep_blank_values=True) == [
        ("q", "a&b=c d"),
        ("name", "Zoë / 東京"),
        ("metadata[k[x]]", "1+1"),
    ]


# -- read from the description -----------------------------------------------------------


def form_spec(base: str, encoding: dict[str, Any] | None = None) -> dict[str, Any]:
    schema = {
        "type": "object",
        "required": ["charge"],
        "properties": {
            "charge": {"type": "string"},
            "amount": {"type": "integer"},
            "metadata": {"type": "object"},
            "expand": {"type": "array", "items": {"type": "string"}},
        },
    }
    media: dict[str, Any] = {"schema": schema}
    if encoding is not None:
        media["encoding"] = encoding
    return {
        "openapi": "3.0.3",
        "info": {"title": "Pay"},
        "servers": [{"url": base}],
        "paths": {
            "/refunds": {
                "post": {
                    "operationId": "createRefund",
                    "requestBody": {"content": {"application/x-www-form-urlencoded": media}},
                }
            },
            "/notes": {
                "post": {
                    "operationId": "createNote",
                    "requestBody": {"content": {"application/json": {"schema": schema}}},
                }
            },
            "/search": {
                "post": {
                    "operationId": "searchRefunds",
                    "requestBody": {"content": {"application/x-www-form-urlencoded": media}},
                }
            },
        },
    }


DEEP = {"metadata": {"style": "deepObject", "explode": True}, "expand": {"style": "deepObject"}}


def body(spec: dict[str, Any], operation: str) -> Body:
    op = entry_from_spec(spec, api_id="pay", spec_url="https://x.test/o.json").operation(operation)
    assert op is not None and op.body is not None
    return op.body


def test_how_a_form_writes_its_lists_is_read_from_the_description() -> None:
    base = "https://pay.example.test"
    stripe_like = body(form_spec(base, DEEP), "createRefund")
    assert (stripe_like.encoding, stripe_like.lists) == ("form", "brackets")
    plain = body(form_spec(base), "createRefund")
    assert (plain.encoding, plain.lists) == ("form", "repeated")  # what OpenAPI means by default
    as_json = body(form_spec(base, DEEP), "createNote")
    assert (as_json.encoding, as_json.lists) == ("json", "repeated")
    written = dump_entry(entry_from_spec(form_spec(base, DEEP), api_id="pay", spec_url=base))
    assert written.count("lists: brackets") == 2 and "lists: repeated" not in written

    def swagger(**array: Any) -> dict[str, Any]:
        return {
            "swagger": "2.0",
            "info": {"title": "Old"},
            "host": "old.example.test",
            "paths": {
                "/sms": {
                    "post": {
                        "operationId": "createRefund",
                        "parameters": [
                            {"name": "To", "in": "formData", "type": "string", "required": True},
                            {"name": "Tags", "in": "formData", "type": "array", **array},
                        ],
                    }
                }
            },
        }

    assert body(swagger(), "createRefund").lists == "commas"  # Swagger's own default
    assert body(swagger(collectionFormat="csv"), "createRefund").lists == "commas"
    assert body(swagger(collectionFormat="multi"), "createRefund").lists == "repeated"


# -- sent that way -----------------------------------------------------------------------


class Recording:
    """A real server that keeps what it was sent and answers with a small JSON object."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str], bytes]] = []
        self._server: asyncio.Server | None = None

    async def start(self) -> str:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return f"http://127.0.0.1:{self._server.sockets[0].getsockname()[1]}"

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
            first, *lines = head.strip().split("\r\n")
            headers = {k.strip().lower(): v.strip() for k, v in (ln.split(":", 1) for ln in lines)}
            sent = await reader.readexactly(int(headers.get("content-length", "0")))
            self.requests.append((first, headers, sent))
            answer = b'{"id": "re_1"}'
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
                + f"Content-Length: {len(answer)}\r\n\r\n".encode()
                + answer
            )
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()


@pytest.fixture
async def api() -> AsyncIterator[Recording]:
    recording = Recording()
    yield recording
    await recording.stop()


async def serving(base: str, settings: Settings, encoding: dict[str, Any] | None) -> Librarian:
    """A librarian with a real HTTP client and the Pay API, learned from its description."""
    assert settings.secrets_dir is not None
    entry = entry_from_spec(form_spec(base, encoding), api_id="pay", spec_url=f"{base}/o.json")
    data = entry.model_dump(mode="json", by_alias=True)
    data |= {"status": "verified", "write_auth": {"type": "bearer", "secret_ref": "pay/write"}}
    for op in data["operations"]:
        if op["id"] == "searchRefunds":  # a person decided this POST only reads
            op |= {"access": "read", "read_basis": "reviewed"}
        else:
            op["writable"] = True
    write_entries(settings.registry_dir, data)
    secrets = FileSecretStore(settings.secrets_dir)
    librarian = Librarian(
        settings,
        secrets_store=secrets,
        audit=MemoryAuditSink(),
        notifier=RecordingNotifier(),
        http_client=httpx.AsyncClient(timeout=5),
    )
    librarian.load()
    await secrets.put("pay/write", "wr1te-key-0123456789")
    return librarian


async def sent(librarian: Librarian, operation: str, body: Any) -> Any:
    entry = librarian.registry.get("pay")
    assert entry is not None
    op = entry.operation(operation)
    assert op is not None
    request = WriteRequest(api="pay", operation=operation, body=body, reason="test")
    return await librarian.executor.execute_write(entry, op, request, WRITER)


async def asked(librarian: Librarian, request: WriteRequest) -> WriteApproval:
    """A write an agent asked for, now waiting for a person."""
    approval = await librarian.request_write(request, WRITER)
    assert isinstance(approval, WriteApproval) and approval.state is ApprovalState.PENDING
    return approval


async def test_a_write_to_an_api_that_takes_forms_is_sent_as_one(
    api: Recording, settings: Settings
) -> None:
    librarian = await serving(await api.start(), settings, DEEP)
    result = await sent(librarian, "createRefund", REFUND)
    assert result.ok and result.data == {"id": "re_1"}
    first, headers, wire = api.requests[-1]
    assert first == "POST /refunds HTTP/1.1"
    assert headers["content-type"] == "application/x-www-form-urlencoded"
    assert headers["authorization"] == "Bearer wr1te-key-0123456789"
    assert int(headers["content-length"]) == len(wire)
    assert parse_qsl(wire.decode(), keep_blank_values=True) == forms.pairs(REFUND, "brackets")
    assert b"expand%5B0%5D=charge" in wire and b"metadata%5Border_id%5D=6735" in wire
    assert b"{" not in wire  # nothing of it went as JSON

    # An operation of the same API that takes JSON gets JSON, as it always did.
    await sent(librarian, "createNote", REFUND)
    _, headers, wire = api.requests[-1]
    assert headers["content-type"] == "application/json"
    assert json.loads(wire) == REFUND
    await librarian.aclose()


async def test_a_list_goes_the_way_that_api_writes_lists(
    api: Recording, settings: Settings
) -> None:
    librarian = await serving(await api.start(), settings, None)  # no brackets said
    await sent(librarian, "createRefund", {"charge": "ch_1", "expand": ["a", "b"]})
    assert api.requests[-1][2] == b"charge=ch_1&expand=a&expand=b"
    # A read that takes a body (a search a person marked as a read) is sent the same way.
    request = CallRequest(api="pay", operation="searchRefunds", body={"charge": "ch_1"})
    read = await librarian.execute(request, Caller(agent="reader"))
    assert read.data == {"id": "re_1"}
    first, headers, wire = api.requests[-1]
    assert first == "POST /search HTTP/1.1" and wire == b"charge=ch_1"
    assert headers["content-type"] == "application/x-www-form-urlencoded"
    await librarian.aclose()


async def test_a_body_that_cannot_be_a_form_is_refused_before_anyone_is_asked(
    api: Recording, settings: Settings
) -> None:
    librarian = await serving(await api.start(), settings, DEEP)
    as_list = WriteRequest(api="pay", operation="createRefund", body=[{"charge": "ch_1"}])
    with pytest.raises(PolicyDenied) as refused:
        await librarian.request_write(as_list, WRITER)
    assert refused.value.reason == (
        "operation 'createRefund' takes its body as a form: give an object of its fields, "
        "not a list"
    )
    assert not librarian.approvals.pending() and api.requests == []
    # The same body to one that takes JSON is the API's to judge: a person is asked.
    to_json = WriteRequest(api="pay", operation="createNote", body=[{"charge": "ch_1"}])
    await asked(librarian, to_json)
    await librarian.aclose()


async def test_the_approver_sees_what_goes_over_the_wire(
    api: Recording, settings: Settings
) -> None:
    librarian = await serving(await api.start(), settings, DEEP)
    request = WriteRequest(
        api="pay",
        operation="createRefund",
        body={"charge": "ch_1", "metadata": {"order": "<b>7</b>"}, "expand": ["charge"]},
        reason="refund the duplicate",
    )
    approval = await asked(librarian, request)
    assert librarian.as_form(approval) == [
        ("charge", "ch_1"),
        ("metadata[order]", "<b>7</b>"),
        ("expand[0]", "charge"),
    ]
    routes = approval_routes(librarian.approvals, librarian.settings.approvers, librarian.as_form)
    page = TestClient(Starlette(routes=routes)).get(librarian.approvals.link(approval)).text
    assert "This API takes it as a form. Sent as:" in page
    assert "metadata[order] = &lt;b&gt;7&lt;/b&gt;" in page and "<b>7</b>" not in page
    assert "expand[0] = charge" in page
    # One that goes as JSON is shown as JSON, with no such line.
    note = await asked(librarian, request.model_copy(update={"operation": "createNote"}))
    assert librarian.as_form(note) is None
    assert "takes it as a form" not in (
        TestClient(Starlette(routes=routes)).get(librarian.approvals.link(note)).text
    )
    await librarian.aclose()


async def test_an_agent_is_told_how_the_body_is_sent(api: Recording, settings: Settings) -> None:
    librarian = await serving(await api.start(), settings, DEEP)
    told = librarian.capabilities(None, WRITER, api="pay", operation="createRefund")
    assert told["body"]["sent_as"] == "form"
    # A file upload is not something librarian sends, and it does not say that it does.
    upload = Operation(
        id="upload",
        method="POST",
        path="/files",
        accepts_body=True,
        body=Body.model_validate({"fields": [{"name": "name"}], "encoding": "multipart"}),
    )
    about = described("pay", upload, enabled=True)["body"]
    assert "sent_as" not in about and "librarian sends JSON" in about["note"]
    assert (
        "sent_as"
        not in librarian.capabilities(None, WRITER, api="pay", operation="createNote")["body"]
    )
    await librarian.aclose()


async def test_an_api_learned_before_gets_the_way_its_lists_are_written(
    api: Recording, settings: Settings
) -> None:
    """0.23.0 read that an operation takes a form, and not how that form writes a list.
    A re-read sees the difference, although no field changed, and it touches nothing
    else about the operation."""
    base = await api.start()
    fresh = entry_from_spec(form_spec(base, DEEP), api_id="pay", spec_url=f"{base}/o.json")

    def as_before(op: Operation) -> Operation:
        if op.body is None:
            return op
        return op.model_copy(
            update={"body": op.body.model_copy(update={"lists": "repeated"}), "writable": True}
        )

    old = fresh.model_copy(update={"operations": [as_before(op) for op in fresh.operations]})
    changes = diff(old, fresh)
    assert [(c.operation, c.severity, c.body) for c in changes] == [
        ("createRefund", "additive", True),
        ("searchRefunds", "additive", True),
    ]
    assert changes[0].detail == (
        "createRefund: its request body is now taken as a form, its lists in brackets"
    )
    updated = proposed_update(old, fresh, changes).operation("createRefund")
    assert updated is not None and updated.body is not None
    assert updated.body.lists == "brackets" and updated.writable  # and what was decided stays
    assert diff(fresh, fresh) == []
    # Said of many operations alike, it is one line for the person who reads it.
    assert told(changes) == [
        "[additive] createRefund: its request body is now taken as a form, its lists in brackets",
        "[additive] searchRefunds: its request body is now taken as a form, its lists in brackets",
    ]
    many = [
        change.model_copy(update={"operation": f"op{n}"})
        for n in range(40)
        for change in changes[:1]
    ]
    many = [
        c.model_copy(update={"detail": c.detail.replace("createRefund", c.operation)}) for c in many
    ]
    assert told(many) == [
        "[additive] 40 operations, each: its request body is now taken as a form, its lists in "
        "brackets"
    ]
