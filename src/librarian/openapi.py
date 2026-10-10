"""Learning an API from its OpenAPI (3.x) or Swagger (2.0) description.

Classification is deliberately conservative:
- GET/HEAD                         -> read (by method)
- POST that looks like a search    -> unknown, flagged for human review
- everything else                  -> write
Nothing here can mark a non-GET operation as a read; only a human can.
"""

from __future__ import annotations

import contextlib
import json
import re
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import urljoin, urlsplit

import httpx
import yaml

from librarian.net import BlockedAddress, check_url, pin
from librarian.registry.models import (
    Access,
    ApiEntry,
    ApiKeyAuth,
    BasicAuth,
    BearerAuth,
    CredentialGuidance,
    EntryStatus,
    NoAuth,
    Operation,
    Parameter,
    ParamLocation,
    SpecSource,
)

MAX_SPEC_BYTES = 25_000_000
MAX_OPERATIONS = 3000
SPEC_PATHS = (
    "/openapi.json",
    "/openapi.yaml",
    "/swagger.json",
    "/swagger/v1/swagger.json",
    "/v3/api-docs",
    "/api-docs",
    "/api-docs-json",
    "/api/openapi.json",
    "/docs/openapi.json",
    "/.well-known/openapi.json",
)
_SEARCH_HINT = re.compile(r"(search|query|find|lookup|list|filter|graphql|_bulk_get)", re.I)
_NON_ID = re.compile(r"[^A-Za-z0-9_.\-]+")

Json = dict[str, Any]


class SpecError(Exception):
    pass


async def fetch_spec(url: str, client: httpx.AsyncClient, *, allow_private: bool = False) -> Json:
    """Download and parse an OpenAPI/Swagger document (JSON or YAML), size-capped.

    Redirects are followed manually so every hop passes the SSRF check.
    """
    current = url
    for _ in range(6):
        target, headers, extensions = current, {}, None
        try:
            await check_url(current, allow_private=True)  # scheme check
            if not allow_private:
                pinned = await pin(current)
                target = pinned.url
                # Never reuse this connection for another hostname on the same IP.
                headers = {"Host": pinned.host_header, "Connection": "close"}
                extensions = pinned.extensions
        except BlockedAddress as exc:
            raise SpecError(str(exc)) from exc
        request = client.build_request("GET", target, headers=headers, extensions=extensions)
        response = await client.send(request, stream=True)
        async with contextlib.AsyncExitStack() as stack:
            stack.push_async_callback(response.aclose)
            if response.is_redirect:
                current = urljoin(current, response.headers.get("location", ""))
                continue
            if response.status_code != 200:
                raise SpecError(f"HTTP {response.status_code} fetching {current}")
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_SPEC_BYTES:
                    raise SpecError("spec is larger than the 25 MB limit")
                chunks.append(chunk)
        return parse_spec(b"".join(chunks).decode("utf-8", errors="replace"))
    raise SpecError("too many redirects fetching the spec")


def parse_spec(text: str) -> Json:
    try:
        data: Any = json.loads(text)
    except ValueError:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise SpecError(f"not valid JSON or YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise SpecError("spec is not a JSON/YAML object")
    spec = cast(Json, data)
    if not (str(spec.get("openapi", "")).startswith("3") or spec.get("swagger") == "2.0"):
        raise SpecError("not an OpenAPI 3.x or Swagger 2.0 document")
    if not isinstance(spec.get("paths"), dict):
        raise SpecError("spec has no paths")
    return spec


async def discover_spec(
    base_url: str, client: httpx.AsyncClient, *, allow_private: bool = False
) -> tuple[str, Json]:
    """Try well-known spec locations under an API's origin."""
    try:
        await check_url(base_url, allow_private)
    except BlockedAddress as exc:
        raise SpecError(str(exc)) from exc
    parts = urlsplit(base_url)
    roots = {f"{parts.scheme}://{parts.netloc}", base_url.rstrip("/")}
    for root in sorted(roots, key=len, reverse=True):
        for path in SPEC_PATHS:
            url = root + path
            try:
                return url, await fetch_spec(url, client, allow_private=allow_private)
            except (SpecError, httpx.HTTPError):
                continue
    raise SpecError("no OpenAPI document found at well-known paths")


def entry_from_spec(
    spec: Json,
    *,
    api_id: str,
    spec_url: str | None = None,
    base_url: str | None = None,
    name: str | None = None,
    secret_ref: str | None = None,
) -> ApiEntry:
    """Build a provisional registry entry from a parsed spec."""
    info = _dict(spec.get("info"))
    resolved_base = base_url or _base_url(spec, spec_url)
    if not resolved_base:
        raise SpecError("could not determine the API base URL; pass base_url explicitly")
    auth, scopes, auth_note = _auth(spec, secret_ref or f"{api_id}/readonly")
    operations = _operations(spec)
    if not operations:
        raise SpecError("spec contains no usable operations")
    review = [op.id for op in operations if op.access is Access.UNKNOWN]
    notes = [f"Learned automatically from {spec_url or 'a provided spec'}."]
    if review:
        notes.append(
            "Needs review: these POST operations look like reads but are blocked until a "
            "human marks them `access: read` + `read_basis: reviewed`: " + ", ".join(review[:50])
        )
    if auth_note:
        notes.append(auth_note)
    validation = None if isinstance(auth, NoAuth) else guess_validation_operation(operations)
    if validation is not None:
        notes.append(f"New credentials are tested with `{validation}`.")
    return ApiEntry(
        id=api_id,
        name=(name or str(info.get("title") or api_id))[:200],
        description=str(info.get("description") or "")[:4000],
        base_url=resolved_base,
        status=EntryStatus.PROVISIONAL,
        auth=auth,
        credential=CredentialGuidance(scopes=scopes[:50], validation_operation=validation),
        operations=operations,
        source=SpecSource(kind="openapi", url=spec_url, fetched_at=datetime.now(UTC)),
        notes="\n".join(notes)[:8000],
    )


# Paths that usually return the identity behind a credential: cheap, always
# allowed for a valid key, and a 401 when the key is bad.
_WHOAMI = re.compile(
    r"/(me|myself|whoami|self|viewer|current[_-]?user|user|account|session|auth/test|"
    r"users/(me|self|current))/?$",
    re.I,
)


def guess_validation_operation(operations: list[Operation]) -> str | None:
    """Pick a cheap read that proves a credential works, if the spec has one."""
    candidates = [
        op
        for op in operations
        if op.method == "GET"
        and op.access is Access.READ
        and not any(p.required for p in op.parameters)
        and _WHOAMI.search(op.path)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda op: (len(op.path), op.path)).id


def _dict(value: Any) -> Json:
    return cast(Json, value) if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return cast(list[Any], value) if isinstance(value, list) else []


def _base_url(spec: Json, spec_url: str | None) -> str | None:
    if spec.get("swagger") == "2.0":
        host = spec.get("host") or (urlsplit(spec_url).netloc if spec_url else None)
        if not host:
            return None
        schemes = _list(spec.get("schemes"))
        scheme = "https" if "https" in schemes or not schemes else str(schemes[0])
        return f"{scheme}://{host}{spec.get('basePath', '')}".rstrip("/")
    for server in _list(spec.get("servers")):
        url = str(_dict(server).get("url", ""))
        for var, definition in _dict(_dict(server).get("variables")).items():
            url = url.replace("{" + var + "}", str(_dict(definition).get("default", "")))
        if spec_url:
            url = urljoin(spec_url, url)
        if urlsplit(url).scheme in ("http", "https") and "{" not in url:
            return url.rstrip("/")
    if spec_url:
        parts = urlsplit(spec_url)
        return f"{parts.scheme}://{parts.netloc}"
    return None


def _resolve(spec: Json, node: Any) -> Json:
    node_dict = _dict(node)
    ref = node_dict.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return node_dict
    target: Any = spec
    for part in ref[2:].split("/"):
        target = _dict(target).get(part.replace("~1", "/").replace("~0", "~"))
    return _dict(target)


def _operations(spec: Json) -> list[Operation]:
    operations: list[Operation] = []
    seen: set[str] = set()
    for path, item in _dict(spec.get("paths")).items():
        if not path.startswith("/"):
            continue
        path_item = _resolve(spec, item)
        shared = _list(path_item.get("parameters"))
        for method in ("get", "head", "post", "put", "patch", "delete"):
            raw = path_item.get(method)
            if not isinstance(raw, dict):
                continue
            op = _dict(raw)
            op_id = _operation_id(op.get("operationId"), method, path, seen)
            params = _parameters(spec, [*shared, *_list(op.get("parameters"))])
            summary = str(op.get("summary") or op.get("description") or "").strip()
            if op.get("deprecated"):
                summary = f"(deprecated) {summary}"
            access, accepts_body = _classify(method.upper(), op_id, path, summary)
            try:
                operations.append(
                    Operation(
                        id=op_id,
                        method=method.upper(),
                        path=path,
                        summary=summary[:2000],
                        parameters=params,
                        access=access,
                        accepts_body=accepts_body,
                    )
                )
            except ValueError:
                continue  # skip operations we cannot represent safely
            seen.add(op_id)
            if len(operations) >= MAX_OPERATIONS:
                return operations
    return operations


def _operation_id(raw: Any, method: str, path: str, seen: set[str]) -> str:
    base = str(raw) if isinstance(raw, str) and raw else f"{method}_{path}"
    candidate = _NON_ID.sub("_", base).strip("_")[:120] or method
    unique, n = candidate, 2
    while unique in seen:
        unique, n = f"{candidate}_{n}", n + 1
    return unique


def _parameters(spec: Json, raw: list[Any]) -> list[Parameter]:
    merged: dict[tuple[str, str], Parameter] = {}
    for item in raw:
        param = _resolve(spec, item)
        location = str(param.get("in", ""))
        name = param.get("name")
        if location not in ("path", "query", "header") or not isinstance(name, str) or not name:
            continue
        schema = _resolve(spec, param.get("schema")) or param
        merged[(location, name)] = Parameter.model_validate(
            {
                "name": name[:128],
                "in": ParamLocation(location),
                "required": bool(param.get("required")) or location == "path",
                "type": str(schema.get("type") or "string"),
                "description": str(param.get("description") or "")[:2000],
            }
        )
    return list(merged.values())


def _classify(method: str, op_id: str, path: str, summary: str) -> tuple[Access, bool]:
    if method in ("GET", "HEAD"):
        return Access.READ, False
    if method == "POST" and _SEARCH_HINT.search(f"{op_id} {path} {summary[:200]}"):
        return Access.UNKNOWN, True
    return Access.WRITE, method in ("POST", "PUT", "PATCH")


def _auth(
    spec: Json, secret_ref: str
) -> tuple[NoAuth | BearerAuth | ApiKeyAuth | BasicAuth, list[str], str | None]:
    if spec.get("swagger") == "2.0":
        schemes = _dict(spec.get("securityDefinitions"))
    else:
        schemes = _dict(_dict(spec.get("components")).get("securitySchemes"))
    if not schemes:
        return NoAuth(), [], None

    preferred = [name for requirement in _list(spec.get("security")) for name in _dict(requirement)]
    order = [n for n in preferred if n in schemes] + [n for n in schemes if n not in preferred]
    for name in order:
        scheme = _resolve(spec, schemes[name])
        kind = str(scheme.get("type", "")).lower()
        if kind == "http" and str(scheme.get("scheme", "")).lower() == "bearer":
            return BearerAuth(secret_ref=secret_ref), [], None
        if (kind == "http" and str(scheme.get("scheme", "")).lower() == "basic") or kind == "basic":
            return BasicAuth(secret_ref=secret_ref), [], "Basic auth: store `username:password`."
        if kind == "apikey" and scheme.get("in") in ("header", "query") and scheme.get("name"):
            return (
                ApiKeyAuth.model_validate(
                    {"secret_ref": secret_ref, "in": scheme["in"], "name": str(scheme["name"])}
                ),
                [],
                None,
            )
        if kind in ("oauth2", "openidconnect"):
            scopes = _read_scopes(scheme)
            return (
                BearerAuth(secret_ref=secret_ref),
                scopes,
                "OAuth2 API: v0.1 uses a pre-issued bearer token (per-user OAuth comes later).",
            )
    return NoAuth(), [], "Spec declares an unsupported auth scheme; set auth manually."


def _read_scopes(scheme: Json) -> list[str]:
    scopes: set[str] = set()
    for flow in _dict(scheme.get("flows")).values():
        for scope in _dict(_dict(flow).get("scopes")):
            if re.search(r"read|view|get|list", scope, re.I):
                scopes.add(scope)
    return sorted(scopes)
