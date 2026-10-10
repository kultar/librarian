from __future__ import annotations

from typing import Any

import httpx
import pytest

from librarian.openapi import SpecError, entry_from_spec, fetch_spec, parse_spec
from librarian.registry import Access, ApiKeyAuth, BasicAuth, BearerAuth, EntryStatus

SPEC: dict[str, Any] = {
    "openapi": "3.0.1",
    "info": {"title": "Jira-ish", "description": "Issue tracking"},
    "servers": [
        {"url": "https://{site}.atlassian.example/rest", "variables": {"site": {"default": "acme"}}}
    ],
    "components": {
        "securitySchemes": {
            "basicAuth": {"type": "http", "scheme": "basic"},
            "oauth": {
                "type": "oauth2",
                "flows": {
                    "authorizationCode": {"scopes": {"read:jira-work": "", "write:jira-work": ""}}
                },
            },
        },
        "parameters": {"Key": {"name": "issueIdOrKey", "in": "path", "required": True}},
    },
    "security": [{"basicAuth": []}],
    "paths": {
        "/api/3/issue/{issueIdOrKey}": {
            "parameters": [{"$ref": "#/components/parameters/Key"}],
            "get": {
                "operationId": "getIssue",
                "summary": "Get issue",
                "parameters": [{"name": "fields", "in": "query"}, {"name": "c", "in": "cookie"}],
            },
            "delete": {"operationId": "deleteIssue"},
        },
        "/api/3/search": {
            "post": {"operationId": "searchForIssuesUsingJqlPost", "summary": "Search for issues"},
        },
        "/api/3/issue": {"post": {"operationId": "createIssue"}},
        "/api/3/myself": {"get": {"summary": "Current user"}},
    },
}


def test_classification_and_params() -> None:
    entry = entry_from_spec(SPEC, api_id="jira", spec_url="https://x.example/spec.json")
    assert entry.status is EntryStatus.PROVISIONAL
    assert entry.base_url == "https://acme.atlassian.example/rest"
    ops = {op.id: op for op in entry.operations}
    assert ops["getIssue"].access is Access.READ
    assert {p.name for p in ops["getIssue"].parameters} == {"issueIdOrKey", "fields"}  # no cookie
    assert ops["deleteIssue"].access is Access.WRITE
    assert ops["createIssue"].access is Access.WRITE
    assert ops["searchForIssuesUsingJqlPost"].access is Access.UNKNOWN  # needs human review
    assert ops["searchForIssuesUsingJqlPost"].accepts_body
    assert "get__api_3_myself" in ops  # synthesized id
    assert "searchForIssuesUsingJqlPost" in entry.notes


def test_preferred_auth_and_scopes() -> None:
    entry = entry_from_spec(SPEC, api_id="jira")
    assert isinstance(entry.auth, BasicAuth)
    assert entry.auth.secret_ref == "jira/readonly"

    oauth_first = {**SPEC, "security": [{"oauth": []}]}
    entry = entry_from_spec(oauth_first, api_id="jira")
    assert isinstance(entry.auth, BearerAuth)
    assert entry.credential.scopes == ["read:jira-work"]


def test_swagger2_api_key() -> None:
    spec = {
        "swagger": "2.0",
        "info": {"title": "Old"},
        "host": "old.example",
        "basePath": "/api",
        "schemes": ["https"],
        "securityDefinitions": {"k": {"type": "apiKey", "in": "header", "name": "X-Key"}},
        "paths": {"/things": {"get": {"operationId": "things"}}},
    }
    entry = entry_from_spec(parse_spec(__import__("json").dumps(spec)), api_id="old")
    assert entry.base_url == "https://old.example/api"
    assert isinstance(entry.auth, ApiKeyAuth) and entry.auth.name == "X-Key"


def test_yaml_spec_and_garbage() -> None:
    assert parse_spec("openapi: 3.1.0\npaths: {}\n")["openapi"] == "3.1.0"
    with pytest.raises(SpecError):
        parse_spec("not: a spec")
    with pytest.raises(SpecError):
        parse_spec("[1, 2]")


async def test_fetch_spec_blocks_private_addresses() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as c:
        with pytest.raises(SpecError, match="non-public"):
            await fetch_spec("http://169.254.169.254/latest/meta-data", c)
        with pytest.raises(SpecError, match="non-public"):
            await fetch_spec("http://127.0.0.1/openapi.json", c)


async def test_fetch_spec_checks_redirect_hops() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://10.0.0.5/spec.json"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(SpecError, match="non-public"):
            await fetch_spec("http://93.184.215.14/spec.json", c)


def test_validation_operation_is_guessed() -> None:
    entry = entry_from_spec(SPEC, api_id="jira")
    assert entry.credential.validation_operation == "get__api_3_myself"
    no_auth = {k: v for k, v in SPEC.items() if k not in ("components", "security")}
    assert entry_from_spec(no_auth, api_id="x").credential.validation_operation is None
