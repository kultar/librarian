"""What a request body holds: read from an API's description, kept with the operation,
and told to agents.

An agent that is told only that an operation takes a body has to guess what goes in it:
from what it knows of a public API, and from nothing at all for an internal one. The
description usually says, so librarian reads it and tells: the names of the fields where
an API's operations are listed, and everything about them where an agent asks about the
operation it is going to call. Nothing is checked against it.
"""

from __future__ import annotations

import copy
import re
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from librarian.calls import Caller, Limits
from librarian.capabilities import BODY_NAMES, size
from librarian.drift import diff, proposed_update, told
from librarian.executor import PolicyDenied
from librarian.openapi import entry_from_spec
from librarian.registry.models import (
    MAX_BODY_FIELDS,
    ApiEntry,
    BearerAuth,
    Body,
    BodyField,
    EntryStatus,
    Operation,
)
from librarian.registry.store import dump_entry, load_yaml
from librarian.service import Librarian, LibrarianError
from tests.conftest import RecordingNotifier, Router

HOST = "https://forge.example.test"
SPEC_URL = f"{HOST}/openapi.json"
WRITER = Caller(agent="agent-a", write_apis=("forge",), owner="ops@x.test")


def json_body(schema: dict[str, Any], kind: str = "application/json") -> dict[str, Any]:
    return {"content": {kind: {"schema": schema}}}


def spec_with(paths: dict[str, Any], **more: Any) -> dict[str, Any]:
    return {
        "openapi": "3.0.3",
        "info": {"title": "Forge"},
        "servers": [{"url": HOST}],
        "paths": paths,
        **more,
    }


PULL = {
    "type": "object",
    "required": ["head", "base"],
    "properties": {
        "title": {"type": "string", "description": "The title of the\n  new   pull request."},
        "head": {"type": "string", "description": "The branch with your changes."},
        "base": {"type": "string"},
        "draft": {"type": "boolean"},
        "state": {"type": "string", "enum": ["open", "closed"]},
        "labels": {"type": "array", "items": {"type": "string"}},
        "due": {"type": "string", "format": "date-time"},
        "milestone": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
        "author": {"$ref": "#/components/schemas/Person"},
        "reviews": {"type": "array", "items": {"$ref": "#/components/schemas/Review"}},
        "id": {"type": "integer", "readOnly": True},
        "old_name": {"type": "string", "deprecated": True},
    },
}
COMPONENTS = {
    "schemas": {
        "Person": {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string"},
                "email": {"type": "string", "format": "email"},
                "team": {
                    "type": "object",
                    "properties": {"slug": {"type": "string"}, "org": {"type": "object"}},
                },
            },
        },
        "Review": {
            "type": "object",
            "properties": {"reviewer": {"type": "string"}, "event": {"enum": ["APPROVE"]}},
        },
    }
}
FORGE = spec_with(
    {
        "/pulls": {
            "get": {"operationId": "listPulls"},
            "post": {"operationId": "createPull", "requestBody": json_body(PULL)},
        },
        "/pulls/{number}": {
            "patch": {
                "operationId": "updatePull",
                "parameters": [{"name": "number", "in": "path"}],
                "requestBody": json_body(
                    {"type": "object", "properties": {"title": {"type": "string"}}}
                ),
            },
            "delete": {"operationId": "deletePull", "requestBody": json_body(PULL)},
        },
        "/labels": {"post": {"operationId": "createLabel"}},
    },
    components=COMPONENTS,
)


def body_of(spec: dict[str, Any], operation: str) -> Body | None:
    entry = entry_from_spec(spec, api_id="forge", spec_url=SPEC_URL)
    op = entry.operation(operation)
    assert op is not None
    return op.body


def names(body: Body | None) -> list[str]:
    assert body is not None
    return [field.name if field.required else f"{field.name}?" for field in body.fields]


# -- reading a description ---------------------------------------------------------------


def test_a_body_is_read_field_by_field_the_top_and_the_required_first() -> None:
    body = body_of(FORGE, "createPull")
    assert names(body) == [
        "head", "base", "title?", "draft?", "state?", "labels[]?", "due?", "milestone?",
        "author?", "reviews[]?",              # the top of the body, what is needed first
        "author.name", "author.email?", "author.team?",       # then what is inside it
        "reviews[].reviewer?", "reviews[].event?",
        "author.team.slug?", "author.team.org?",              # three deep, and no further
    ]  # fmt: skip
    assert body is not None and body.more == 0 and body.encoding == "json"
    by_name = {field.name: field for field in body.fields}
    assert by_name["title"].description == "The title of the new pull request."  # one line
    assert by_name["state"].values == ["open", "closed"]
    assert by_name["labels[]"].type == "string"  # a list, of these
    assert by_name["due"].type == "string (date-time)"
    assert by_name["milestone"].type == "string or integer"
    assert by_name["author"].type == "object" and by_name["reviews[]"].type == "object"
    assert by_name["author.name"].required  # whenever an author is given
    # What is not for sending is not told: a field the API sets, one it has retired.
    assert "id" not in by_name and "old_name" not in by_name


def test_only_where_a_body_is_taken_and_the_description_says_what_is_in_it() -> None:
    assert names(body_of(FORGE, "updatePull")) == ["title?"]
    assert body_of(FORGE, "createLabel") is None  # nothing said: it is not made up
    assert body_of(FORGE, "deletePull") is None  # described, but a DELETE takes none here
    assert body_of(FORGE, "listPulls") is None
    entry = entry_from_spec(FORGE, api_id="forge", spec_url=SPEC_URL)
    label = entry.operation("createLabel")
    assert label is not None and label.accepts_body and label.body is None


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        (  # allOf: all of its parts
            {
                "allOf": [
                    {"$ref": "#/components/schemas/Person"},
                    {"type": "object", "required": ["role"], "properties": {"role": {}}},
                ]
            },
            ["name", "role", "email?", "team?", "team.slug?", "team.org?"],
        ),
        (  # oneOf: any of them, and needed only where every one of them needs it
            {
                "oneOf": [
                    {
                        "type": "object",
                        "required": ["id", "sha"],
                        "properties": {"id": {}, "sha": {}},
                    },
                    {"type": "object", "required": ["id"], "properties": {"id": {}, "ref": {}}},
                ]
            },
            ["id", "sha?", "ref?"],
        ),
        (  # anyOf beside properties of its own
            {
                "type": "object",
                "properties": {"note": {"type": "string"}},
                "anyOf": [{"properties": {"a": {}}}, {"properties": {"b": {}}}],
            },
            ["note?", "a?", "b?"],
        ),
    ],
)
def test_a_body_put_together_from_parts(schema: dict[str, Any], expected: list[str]) -> None:
    spec = spec_with(
        {"/things": {"post": {"operationId": "make", "requestBody": json_body(schema)}}},
        components=COMPONENTS,
    )
    assert names(body_of(spec, "make")) == expected


@pytest.mark.parametrize(
    "request_body",
    [
        json_body({"type": "array", "items": PULL}),  # a list: not fields of one thing
        json_body({"type": "string"}),
        json_body({"type": "object"}),  # an object, and not a word about what is in it
        json_body({"type": "string", "format": "binary"}, "application/octet-stream"),
        json_body(PULL, "text/plain"),
        {"content": {}},
        {"description": "the body"},
        {"$ref": "#/components/requestBodies/Nowhere"},
    ],
)
def test_what_cannot_be_told_as_fields_is_not_told(request_body: dict[str, Any]) -> None:
    spec = spec_with({"/things": {"post": {"operationId": "make", "requestBody": request_body}}})
    entry = entry_from_spec(spec, api_id="forge", spec_url=SPEC_URL)
    op = entry.operation("make")
    assert op is not None and op.accepts_body and op.body is None


def test_json_is_read_where_it_is_offered_and_another_form_is_named() -> None:
    form = {"type": "object", "properties": {"amount": {"type": "integer"}}}
    both = {
        "content": {
            "application/x-www-form-urlencoded": {"schema": form},
            "application/json; charset=utf-8": {"schema": PULL},
        }
    }
    paths = {
        "/a": {"post": {"operationId": "both", "requestBody": both}},
        "/b": {
            "post": {
                "operationId": "form",
                "requestBody": json_body(form, "application/x-www-form-urlencoded"),
            }
        },
        "/c": {
            "post": {
                "operationId": "upload",
                "requestBody": json_body(
                    {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "file": {"type": "string", "format": "binary"},
                        },
                    },
                    "multipart/form-data",
                ),
            }
        },
        "/d": {
            "post": {
                "operationId": "patchy",
                "requestBody": json_body(form, "application/merge-patch+json"),
            }
        },
    }
    spec = spec_with(paths, components=COMPONENTS)
    both_body, form_body = body_of(spec, "both"), body_of(spec, "form")
    assert both_body is not None and both_body.encoding == "json"  # what librarian sends
    assert names(both_body)[:2] == ["head", "base"]
    assert form_body is not None and form_body.encoding == "form"
    assert names(form_body) == ["amount?"]
    upload = body_of(spec, "upload")
    assert upload is not None and upload.encoding == "multipart"
    assert [(f.name, f.type) for f in upload.fields] == [
        ("name", "string"),
        ("file", "string (binary)"),
    ]
    patchy = body_of(spec, "patchy")
    assert patchy is not None and patchy.encoding == "json"


def test_a_large_or_a_strange_body_is_kept_within_bounds() -> None:
    wide: dict[str, Any] = {
        "type": "object",
        "properties": {
            **{f"field{n:02}": {"type": "string"} for n in range(45)},
            "has space": {"type": "string"},  # not a name an agent could be told safely
            "dotted.name": {"type": "string"},
            "x" * 65: {"type": "string"},
            "choice": {"type": "string", "enum": [f"v{n}" for n in range(13)]},  # too many
            "odd": {"type": "string", "enum": ["fine", "two\nlines", "x" * 70]},
            "text": {"type": "string", "description": "long " * 100},
        },
    }
    deep = {
        "type": "object",
        "properties": {
            "a": {
                "type": "object",
                "properties": {
                    "b": {
                        "type": "object",
                        "properties": {
                            "c": {"type": "object", "properties": {"d": {"type": "string"}}}
                        },
                    }
                },
            }
        },
    }
    loop: dict[str, Any] = {"$ref": "#/components/schemas/Node"}
    selfish: dict[str, Any] = {"$ref": "#/components/schemas/Selfish"}
    spec = spec_with(
        {
            "/wide": {"post": {"operationId": "wide", "requestBody": json_body(wide)}},
            "/deep": {"post": {"operationId": "deep", "requestBody": json_body(deep)}},
            "/loop": {"post": {"operationId": "loop", "requestBody": json_body(loop)}},
            "/self": {"post": {"operationId": "selfish", "requestBody": json_body(selfish)}},
        },
        components={
            "schemas": {
                # One that is "all of" itself, directly and through another.
                "Selfish": {"allOf": [selfish, {"$ref": "#/components/schemas/Other"}]},
                "Other": {"allOf": [selfish], "properties": {"x": {"type": "string"}}},
                "Node": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "next": loop,
                        "all": {"allOf": [loop]},
                    },
                },
            }
        },
    )
    body = body_of(spec, "wide")
    assert body is not None and len(body.fields) == MAX_BODY_FIELDS
    assert body.more == 45 + 6 - MAX_BODY_FIELDS  # the rest, and the three with no usable name
    assert all(f.name.startswith("field") for f in body.fields)
    properties: dict[str, Any] = wide["properties"]
    small = {**wide, "properties": {k: v for k, v in properties.items() if "field" not in k}}
    spec["paths"]["/wide"]["post"]["requestBody"] = json_body(small)
    kept = {f.name: f for f in (body_of(spec, "wide") or Body()).fields}
    assert set(kept) == {"choice", "odd", "text"}
    assert kept["choice"].values == []  # a list too long to name all of is not half named
    assert kept["odd"].values == ["fine", "two lines", "x" * 60]
    assert 150 < len(kept["text"].description) <= 160

    assert names(body_of(spec, "deep")) == ["a?", "a.b?", "a.b.c?"]  # three deep
    assert names(body_of(spec, "selfish")) == ["x?"]  # and it comes to an end
    # One that refers to itself comes to an end.
    assert names(body_of(spec, "loop")) == [
        "name?", "next?", "all?", "next.name?", "next.next?", "next.all?", "all.name?",
        "all.next?", "all.all?", "next.next.name?", "next.next.next?", "next.next.all?",
        "next.all.name?", "next.all.next?", "next.all.all?", "all.next.name?",
        "all.next.next?", "all.next.all?", "all.all.name?", "all.all.next?", "all.all.all?",
    ]  # fmt: skip


def test_a_swagger_description_says_it_in_its_parameters() -> None:
    spec: dict[str, Any] = {
        "swagger": "2.0",
        "info": {"title": "Old forge"},
        "host": "forge.example.test",
        "paths": {
            "/pulls": {
                "post": {
                    "operationId": "createPull",
                    "parameters": [
                        {"name": "dry", "in": "query", "type": "boolean"},
                        {"name": "pull", "in": "body", "schema": {"$ref": "#/definitions/Pull"}},
                    ],
                }
            },
            "/sms": {
                "post": {
                    "operationId": "send",
                    "parameters": [
                        {"name": "To", "in": "formData", "type": "string", "required": True},
                        {"name": "Body", "in": "formData", "type": "string", "enum": ["a", "b"]},
                    ],
                }
            },
            "/files": {
                "post": {
                    "operationId": "upload",
                    "parameters": [
                        {"name": "file", "in": "formData", "type": "file"},
                        {"name": "name", "in": "formData", "type": "string"},
                    ],
                }
            },
        },
        "definitions": {
            "Pull": {
                "type": "object",
                "required": ["head", "base"],
                "properties": {
                    "title": {"type": "string"},
                    "head": {"type": "string"},
                    "base": {"type": "string"},
                },
            }
        },
    }
    assert names(body_of(spec, "createPull"))[:3] == ["head", "base", "title?"]
    sms = body_of(spec, "send")
    assert sms is not None and sms.encoding == "form" and names(sms) == ["To", "Body?"]
    assert sms.fields[1].values == ["a", "b"]
    upload = body_of(spec, "upload")
    assert upload is not None and upload.encoding == "multipart" and names(upload) == ["name?"]
    entry = entry_from_spec(spec, api_id="forge", spec_url=SPEC_URL)
    create = entry.operation("createPull")
    assert create is not None and [p.name for p in create.parameters] == ["dry"]  # as before


# -- kept with the operation -------------------------------------------------------------


def test_an_entry_without_bodies_is_written_as_it_always_was() -> None:
    """Versions before this one refuse a field they do not know. An entry that has no
    body described must not carry the field, or a replica not yet upgraded loses the API."""
    bare = spec_with({"/labels": {"post": {"operationId": "createLabel"}}})
    written = dump_entry(entry_from_spec(bare, api_id="forge", spec_url=SPEC_URL))
    assert not re.search(r"^\s*body:", written, re.M) and "accepts_body: true" in written
    # With one, it is there, as little of it as says it, and reads back the same.
    entry = entry_from_spec(FORGE, api_id="forge", spec_url=SPEC_URL)
    again = ApiEntry.model_validate(load_yaml(dump_entry(entry)))
    assert again == entry
    create = next(
        op for op in load_yaml(dump_entry(entry))["operations"] if op["id"] == "createPull"
    )
    head, base, title = create["body"]["fields"][:3]
    assert head == {
        "name": "head",
        "type": "string",
        "required": True,
        "description": "The branch with your changes.",
    }
    assert base == {"name": "base", "type": "string", "required": True}
    assert title["description"] == "The title of the new pull request." and "required" not in title
    assert set(create["body"]) == {"fields"}  # nothing left out, and JSON: not said


@pytest.mark.parametrize(
    "field",
    [
        {"name": "has space"},
        {"name": "a..b"},
        {"name": "[]"},
        {"name": "a.b.c.d.e"},  # deeper than anything is followed
        {"name": "ok", "values": ["x" * 61]},
        {"name": "ok", "values": ["two\nlines"]},
        {"name": "ok", "values": [str(n) for n in range(13)]},
        {"name": "ok", "type": ""},
        {"name": "ok", "description": "x" * 201},
        {"name": "ok", "unknown": 1},
    ],
)
def test_a_field_written_by_hand_is_held_to_the_same_shape(field: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        BodyField.model_validate(field)


def test_a_body_belongs_to_an_operation_that_takes_one() -> None:
    fields = [{"name": "title"}, {"name": "tree[].path", "type": "string", "required": True}]
    op = Operation(
        id="x",
        method="POST",
        path="/x",
        accepts_body=True,
        body=Body.model_validate({"fields": fields}),
    )
    assert op.body is not None and op.body.fields[1].name == "tree[].path"
    with pytest.raises(ValidationError, match="a body is described but not accepted"):
        Operation(id="x", method="DELETE", path="/x", body=Body.model_validate({"fields": fields}))
    with pytest.raises(ValidationError):
        Body.model_validate({"fields": [{"name": f"f{n}"} for n in range(MAX_BODY_FIELDS + 1)]})
    with pytest.raises(ValidationError):
        Body.model_validate({"fields": fields, "encoding": "xml"})


# -- told to agents ----------------------------------------------------------------------


async def saved(librarian: Librarian, spec: dict[str, Any], status: EntryStatus) -> ApiEntry:
    entry = entry_from_spec(spec, api_id="forge", spec_url=SPEC_URL)
    entry = entry.model_copy(
        update={
            "status": status,
            "write_auth": BearerAuth(secret_ref="forge/write"),
            "operations": [
                op.model_copy(update={"writable": True}) if op.id == "createPull" else op
                for op in entry.operations
            ],
        }
    )
    await librarian.registry.save(entry)
    return entry


async def test_an_agent_is_told_what_a_body_holds_where_it_asks_about_the_api(
    librarian: Librarian,
) -> None:
    await saved(librarian, FORGE, EntryStatus.VERIFIED)
    # Among all the APIs there is no room for it: an operation says that it takes one.
    everything = librarian.capabilities(None, WRITER)["apis"]
    forge = next(a for a in everything if a["api"] == "forge")
    writes = {op["operation"]: op for op in forge["write_operations"]}
    assert writes["createPull"]["body"] is True and "body_more" not in writes["createPull"]

    # Asked about this API, its operations name what goes in.
    one = librarian.capabilities(None, WRITER, api="forge")["apis"][0]
    writes = {op["operation"]: op for op in one["write_operations"]}
    assert writes["createPull"]["body"] == [
        "head", "base", "title?", "draft?", "state?", "labels[]?", "due?", "milestone?",
        "author?", "reviews[]?",
    ]  # fmt: skip
    assert (
        len(writes["createPull"]["body"]) == BODY_NAMES and writes["createPull"]["body_more"] == 7
    )
    assert writes["updatePull"]["body"] == ["title?"] and "body_more" not in writes["updatePull"]
    assert writes["createLabel"]["body"] is True  # it takes one; what is in it is not known
    assert "body" not in writes["deletePull"]
    assert writes["createPull"]["args"] == [] and writes["createPull"]["enabled"] is True
    # The same with words to match.
    found = librarian.capabilities("create a pull", WRITER, api="forge")["apis"][0]
    assert found["write_operations"][0]["body"][:2] == ["head", "base"]


async def test_an_agent_about_to_call_an_operation_is_told_all_of_it(librarian: Librarian) -> None:
    await saved(librarian, FORGE, EntryStatus.VERIFIED)
    told_ = librarian.capabilities(None, WRITER, api="forge", operation="updatePull")
    assert told_ == {
        "api": "forge",
        "operation": "updatePull",
        "summary": "",
        "method": "PATCH",
        "path": "/pulls/{number}",
        "kind": "write",
        "enabled": False,  # nobody has allowed writes through it yet
        "args": [{"name": "number", "in": "path", "type": "string", "required": True}],
        "body": {"fields": [{"name": "title", "type": "string", "required": False}]},
    }
    create = librarian.capabilities("ignored", WRITER, api="forge", operation="createPull")
    assert create["kind"] == "write" and create["enabled"] is True
    fields = {f["name"]: f for f in create["body"]["fields"]}
    assert len(fields) == 17 and "more" not in create["body"] and "sent_as" not in create["body"]
    assert fields["head"] == {
        "name": "head",
        "type": "string",
        "required": True,
        "description": "The branch with your changes.",
    }
    assert fields["state"]["values"] == ["open", "closed"]
    assert fields["author.name"]["required"] is True
    # One that reads: no word about writing. One whose body is not described: it says so.
    read = librarian.capabilities(None, WRITER, api="forge", operation="listPulls")
    assert read["kind"] == "read" and "enabled" not in read and "body" not in read
    label = librarian.capabilities(None, WRITER, api="forge", operation="createLabel")
    assert label["body"] is True


async def test_a_body_the_api_takes_another_way_says_so(librarian: Librarian) -> None:
    form = {"type": "object", "properties": {"amount": {"type": "integer"}}}
    spec = spec_with(
        {
            "/refunds": {
                "post": {
                    "operationId": "createPull",
                    "requestBody": json_body(form, "application/x-www-form-urlencoded"),
                }
            }
        }
    )
    await saved(librarian, spec, EntryStatus.VERIFIED)
    told_ = librarian.capabilities(None, WRITER, api="forge", operation="createPull")
    assert told_["body"] == {
        "fields": [{"name": "amount", "type": "integer", "required": False}],
        "sent_as": "form",
    }


async def test_only_an_operation_the_agent_is_shown_is_told_in_full(librarian: Librarian) -> None:
    await saved(librarian, FORGE, EntryStatus.VERIFIED)

    def refused(operation: str, caller: Caller = WRITER, api: str = "forge") -> str:
        with pytest.raises(PolicyDenied) as denied:
            librarian.capabilities(None, caller, api=api, operation=operation)
        return denied.value.reason

    assert refused("nope") == "unknown operation 'nope' on 'forge'"
    assert (
        refused("create_pull")
        == "unknown operation 'create_pull' on 'forge'. Did you mean 'createPull'?"
    )
    assert refused("createPull", api="Forge") == "unknown API 'Forge'. Did you mean 'forge'?"
    # One that writes, asked by an agent that may not ask for writes there: not there.
    reader = Caller(agent="agent-r")
    assert refused("createPull", reader) == "unknown operation 'createPull' on 'forge'"
    assert (
        librarian.capabilities(None, reader, api="forge", operation="listPulls")["kind"] == "read"
    )
    # One outside an agent's limits: not there either.
    held = Caller(
        agent="agent-h", write_apis=("forge",), limits={"forge": Limits(operations=("list*",))}
    )
    assert refused("createPull", held) == "unknown operation 'createPull' on 'forge'"
    with pytest.raises(LibrarianError, match="name its API too"):
        librarian.capabilities(None, WRITER, operation="createPull")


async def test_an_operation_in_full_stays_within_the_size_too(librarian: Librarian) -> None:
    many = {
        "type": "object",
        "properties": {
            f"field{n:02}": {"type": "string", "description": "what it is " * 20} for n in range(40)
        },
    }
    spec = spec_with(
        {"/pulls": {"post": {"operationId": "createPull", "requestBody": json_body(many)}}}
    )
    await saved(librarian, spec, EntryStatus.VERIFIED)
    whole = librarian.capabilities(None, WRITER, api="forge", operation="createPull")
    assert "note" not in whole and len(whole["body"]["fields"][0]["description"]) == 160
    librarian.settings.capabilities_max_bytes = 6_000
    cut = librarian.capabilities(None, WRITER, api="forge", operation="createPull")
    assert size(cut) <= 6_000 and len(cut["body"]["fields"]) == 40  # every field is still there
    assert len(cut["body"]["fields"][0]["description"]) == 60
    assert cut["note"] == "Shortened to keep this answer small: the descriptions are cut."
    librarian.settings.capabilities_max_bytes = 3_000
    bare = librarian.capabilities(None, WRITER, api="forge", operation="createPull")
    assert size(bare) <= 3_000 and "description" not in bare["body"]["fields"][0]


# -- when the description changes, and for an API learned before ---------------------------


def without_bodies(entry: ApiEntry) -> ApiEntry:
    """The entry as a version before this one learned it."""
    return entry.model_copy(
        update={"operations": [op.model_copy(update={"body": None}) for op in entry.operations]}
    )


def test_a_change_in_a_body_is_listed_and_holds_nothing_back() -> None:
    old = entry_from_spec(FORGE, api_id="forge", spec_url=SPEC_URL)
    changed = copy.deepcopy(FORGE)
    properties = changed["paths"]["/pulls"]["post"]["requestBody"]["content"]["application/json"][
        "schema"
    ]["properties"]
    del properties["draft"], properties["labels"]
    properties["squash"] = {"type": "boolean"}
    changed["paths"]["/pulls"]["post"]["requestBody"]["content"]["application/json"]["schema"][
        "required"
    ] = ["head", "base", "title"]
    new = entry_from_spec(changed, api_id="forge", spec_url=SPEC_URL)
    [change] = diff(old, new)
    assert change.operation == "createPull" and change.severity == "additive"  # never more
    assert change.detail == (
        "createPull: its request body now needs 'title'; now also takes 'squash'; "
        "no longer takes 'draft', 'labels[]'"
    )
    assert diff(old, old) == []
    # An approver who takes the update gets the new body, and keeps what people decided.
    decided = old.model_copy(
        update={
            "operations": [
                op.model_copy(update={"writable": True}) if op.id == "createPull" else op
                for op in old.operations
            ]
        }
    )
    proposal = proposed_update(decided, new, diff(decided, new))
    create = proposal.operation("createPull")
    assert create is not None and create.writable and "squash?" in names(create.body)


def test_a_body_that_is_new_touches_nothing_a_person_decided() -> None:
    """Taking an update rebuilds a changed operation from the source and keeps the main
    decisions. Where only its body is new, the operation is what it was: how it is
    approved, the slashes allowed in its path, what was seen of it all stay."""
    new = entry_from_spec(FORGE, api_id="forge", spec_url=SPEC_URL)

    def decided(op: Operation) -> Operation:
        if op.id != "updatePull":
            return op
        number = op.parameters[0].model_copy(update={"allow_slash": True})
        return op.model_copy(
            update={
                "writable": True,
                "approval": "auto",
                "auto_limit_per_day": 7,
                "parameters": [number],
                "warning": "seen to be slow",
                "response_shape": ["id", "title"],
            }
        )

    old = without_bodies(new)
    old = old.model_copy(update={"operations": [decided(op) for op in old.operations]})
    changes = diff(old, new)
    assert {c.operation for c in changes} == {"createPull", "updatePull"}
    updated = proposed_update(old, new, changes).operation("updatePull")
    before = old.operation("updatePull")
    assert updated is not None and before is not None
    assert names(updated.body) == ["title?"]
    assert updated.model_copy(update={"body": None}) == before  # and nothing else differs
    assert updated.approval == "auto" and updated.parameters[0].allow_slash

    # A decision that no body is taken there is kept as well: none is described then.
    refused = old.model_copy(
        update={
            "operations": [
                op.model_copy(update={"accepts_body": False}) if op.id == "createPull" else op
                for op in old.operations
            ]
        }
    )
    create = proposed_update(refused, new, diff(refused, new)).operation("createPull")
    assert create is not None and not create.accepts_body and create.body is None


def test_bodies_described_for_the_first_time_are_one_line_however_many() -> None:
    new = entry_from_spec(FORGE, api_id="forge", spec_url=SPEC_URL)
    old = without_bodies(new)
    changes = diff(old, new)
    assert sorted(c.operation for c in changes) == ["createPull", "updatePull"]
    assert told(changes) == [
        "[additive] createPull: its request body is now described",
        "[additive] updatePull: its request body is now described",
    ]
    paths = {
        f"/things{n}": {"post": {"operationId": f"make{n}", "requestBody": json_body(PULL)}}
        for n in range(30)
    }
    paths["/gone"] = {"get": {"operationId": "gone"}}
    new = entry_from_spec(
        spec_with(paths, components=COMPONENTS), api_id="forge", spec_url=SPEC_URL
    )
    old = without_bodies(new)
    fewer = new.model_copy(update={"operations": [op for op in new.operations if op.id != "gone"]})
    assert told(diff(old, fewer)) == [
        "[breaking] GET /gone is no longer in the source",  # what matters stays first
        "[additive] the request bodies of 30 operations are now described",
    ]
    # ...and every one of them gets its body when the update is taken.
    proposal = proposed_update(old, fewer, diff(old, fewer))
    assert all(op.body is not None for op in proposal.operations if op.id != "gone")


@pytest.fixture
def source(router: Router, librarian: Librarian) -> dict[str, Any]:
    served = {"spec": FORGE}
    router.add("GET", SPEC_URL, lambda request: httpx.Response(200, json=served["spec"]))
    librarian.source_client_factory = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(router)
    )
    return served


async def test_an_api_learned_before_gets_its_bodies_when_a_person_has_it_read_again(
    librarian: Librarian, source: dict[str, Any], notifier: RecordingNotifier
) -> None:
    """The description is byte for byte what it was, so the daily look at it finds
    nothing to do, and nobody is sent a card they did not ask for. A person who asks
    for a re-check has it read again."""
    old = without_bodies(await saved(librarian, FORGE, EntryStatus.PROVISIONAL))
    await librarian.registry.save(old)

    quiet = await librarian.recheck("forge")
    assert quiet.status == "unchanged" and quiet.detail is None
    entry = librarian.registry.get("forge")
    assert entry is not None and all(op.body is None for op in entry.operations)

    report = await librarian.recheck("forge", reread=True)
    assert report.status == "changed" and report.review is None
    entry = librarian.registry.get("forge")
    assert entry is not None
    create = entry.operation("createPull")
    assert create is not None and names(create.body)[:2] == ["head", "base"]
    assert create.writable  # everything else about the entry is as people had it
    assert report.actions == ["what its request bodies hold was added to the entry"]
    assert notifier.changes[-1]["details"][:2] == [
        "[additive] createPull: its request body is now described",
        "[additive] updatePull: its request body is now described",
    ]
    again = await librarian.recheck("forge", reread=True)
    assert again.status == "unchanged" and again.detail == "read again; nothing in it is new"


async def test_for_an_approved_api_the_bodies_wait_for_an_approver(
    librarian: Librarian, source: dict[str, Any], notifier: RecordingNotifier
) -> None:
    old = without_bodies(await saved(librarian, FORGE, EntryStatus.VERIFIED))
    await librarian.registry.save(old)

    report = await librarian.recheck("forge", reread=True)
    assert report.status == "changed" and report.review is not None
    assert report.actions == []  # nothing is suspended and nobody is warned over a body
    entry = librarian.registry.get("forge")
    assert entry is not None and all(op.body is None for op in entry.operations)  # not yet
    kind, review, _ = notifier.reviews[-1]
    assert kind == "opened" and review.kind == "update"
    assert (
        review.summary.startswith("2 change(s) at ") and "the worst is additive" in review.summary
    )
    assert review.changes == [
        "[additive] createPull: its request body is now described",
        "[additive] updatePull: its request body is now described",
    ]
    told_ = librarian.capabilities(None, WRITER, api="forge", operation="createPull")
    assert told_["body"] is True and told_["enabled"] is True  # as it was, and still usable

    await librarian.approve_review(review.id, "kim@x.test", {})
    entry = librarian.registry.get("forge")
    assert entry is not None and entry.status is EntryStatus.VERIFIED
    create = entry.operation("createPull")
    assert create is not None and create.writable and names(create.body)[:2] == ["head", "base"]
    told_ = librarian.capabilities(None, WRITER, api="forge", operation="createPull")
    assert [f["name"] for f in told_["body"]["fields"]][:2] == ["head", "base"]


async def test_the_consoles_re_check_has_the_description_read_again(
    librarian: Librarian, source: dict[str, Any]
) -> None:
    """The lever for an API learned before: a person presses Re-check now."""
    from tests.test_console import console, token

    librarian.settings.approvers.console = "open"
    old = without_bodies(await saved(librarian, FORGE, EntryStatus.PROVISIONAL))
    await librarian.registry.save(old)
    c = console(librarian)
    page = c.get("/apis/forge").text
    assert "what its request bodies hold" in page and "Body: " not in page
    done = c.post("/apis/forge/recheck", data={"t": token(librarian)})
    assert done.status_code == 200 and "Re-check: changed" in done.text, done.text
    entry = librarian.registry.get("forge")
    assert entry is not None
    create = entry.operation("createPull")
    assert create is not None and names(create.body)[:2] == ["head", "base"]
    # ...and the people who review the API see what agents are now told.
    assert "Body: head, base, title?, draft?" in c.get("/apis/forge").text


def test_the_body_in_the_format_reference_is_one_librarian_reads() -> None:
    """A person writes a body into an entry from the example in docs/registry.md. It has
    to be YAML (a name with brackets needs quotes there) and a body librarian accepts."""
    from pathlib import Path

    reference = (Path(__file__).parent.parent / "docs" / "registry.md").read_text("utf-8")
    blocks = [block for block in re.findall(r"```yaml\n(.*?)```", reference, flags=re.S)]
    described = [
        op["body"]
        for block in blocks
        if "body:" in block
        for op in (load_yaml(block) or {}).get("operations", [])
        if isinstance(op.get("body"), dict)
    ]
    assert len(described) == 1
    body = Body.model_validate(described[0])
    assert [field.name for field in body.fields] == [
        "title", "labels[]", "state", "assignee.login", "tree[].path",
    ]  # fmt: skip
    assert body.fields[0].required and body.fields[2].values == ["open", "closed"]
    assert (body.more, body.encoding) == (3, "form")
