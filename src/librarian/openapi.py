"""Learning an API from its OpenAPI (3.x) or Swagger (2.0) description.

Classification is deliberately conservative:
- GET/HEAD                         -> read (by method)
- POST that looks like a search    -> unknown, flagged for human review
- everything else                  -> write
Nothing here can mark a non-GET operation as a read; only a human can.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import urljoin, urlsplit

import httpx
import yaml

from librarian.net import PUBLIC, BlockedAddress, NetworkPolicy, check_url, pin
from librarian.registry.models import (
    BODY_FIELD,
    MAX_BODY_FIELDS,
    MAX_FIELD_VALUES,
    Access,
    ApiEntry,
    ApiKeyAuth,
    BasicAuth,
    BearerAuth,
    Body,
    CredentialGuidance,
    EntryStatus,
    NoAuth,
    Operation,
    Origin,
    Parameter,
    ParamLocation,
    SpecSource,
    looks_like_path,
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


async def fetch_spec(url: str, client: httpx.AsyncClient, *, net: NetworkPolicy = PUBLIC) -> Json:
    """Download and parse an OpenAPI/Swagger document (JSON or YAML), size-capped."""
    text, _ = await fetch_text(url, client, net=net)
    return parse_spec(text)


async def fetch_text(
    url: str,
    client: httpx.AsyncClient,
    *,
    net: NetworkPolicy = PUBLIC,
    max_bytes: int = MAX_SPEC_BYTES,
) -> tuple[str, str]:
    """Download a document (spec or docs page); returns (text, final URL).

    Redirects are followed manually so every hop passes the SSRF check.
    """
    current = url
    for _ in range(6):
        target, headers, extensions = current, {}, None
        try:
            await check_url(current, NetworkPolicy(allow_private=True))  # scheme check
            if not net.allow_private:
                pinned = await pin(current, net)
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
                if size > max_bytes:
                    raise SpecError(f"{current} is larger than the {max_bytes:,} byte limit")
                chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace"), current
    raise SpecError(f"too many redirects fetching {url}")


def spec_hash(spec: Json) -> str:
    """A stable fingerprint of a spec's content (key order and formatting ignored)."""
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


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
    base_url: str, client: httpx.AsyncClient, *, net: NetworkPolicy = PUBLIC
) -> tuple[str, Json]:
    """Try well-known spec locations under an API's origin."""
    try:
        await check_url(base_url, net)
    except BlockedAddress as exc:
        raise SpecError(str(exc)) from exc
    parts = urlsplit(base_url)
    roots = {f"{parts.scheme}://{parts.netloc}", base_url.rstrip("/")}
    for root in sorted(roots, key=len, reverse=True):
        for path in SPEC_PATHS:
            url = root + path
            try:
                return url, await fetch_spec(url, client, net=net)
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
            "person decides them (read, write or exclude) in the console: " + ", ".join(review[:50])
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
        source=SpecSource(
            kind="openapi",
            url=spec_url,
            fetched_at=datetime.now(UTC),
            content_hash=spec_hash(spec),
        ),
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
            access, accepts_body = classify(method.upper(), op_id, path, summary)
            takes = [*shared, *_list(op.get("parameters"))]
            try:
                operations.append(
                    Operation(
                        id=op_id,
                        method=method.upper(),
                        path=path,
                        summary=summary[:2000],
                        tags=[str(t)[:60] for t in _list(op.get("tags"))][:20],
                        parameters=params,
                        access=access,
                        accepts_body=accepts_body,
                        body=_body(spec, op, takes) if accepts_body else None,
                        origin=Origin.SPEC,
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
        description = str(param.get("description") or "")[:2000]
        merged[(location, name)] = Parameter.model_validate(
            {
                "name": name[:128],
                "in": ParamLocation(location),
                "required": bool(param.get("required")) or location == "path",
                "type": str(schema.get("type") or "string"),
                "description": description,
                "allow_slash": location == "path" and looks_like_path(name, description),
            }
        )
    return list(merged.values())


# How far into a body its fields are followed: `incident.service.id` is three deep.
BODY_DEPTH = 3
_FIELD_NAME = re.compile(r"^[A-Za-z0-9_$@:-]{1,64}$")


def _body(spec: Json, op: Json, parameters: list[Any]) -> Body | None:
    """What an operation's request body holds, as far as the description says: its
    fields by their path, the top of the body first. None where it does not say, or
    where the body is not an object (a list, a file)."""
    found = _body_schema(spec, op, parameters)
    if found is None:
        return None
    encoding, schema, lists = found
    fields, more = _fields(spec, schema)
    if not fields:  # nothing said of it, or not an object: a list, a file, plain text
        return None
    try:
        return Body.model_validate(
            {"fields": fields, "more": more, "encoding": encoding, "lists": lists}
        )
    except ValueError:
        return None  # nothing an agent could be told safely


def _body_schema(spec: Json, op: Json, parameters: list[Any]) -> tuple[str, Json, str] | None:
    """The schema of the request body, how the API takes it, and for a form how it
    writes a list. JSON where the description offers it: that needs no such choices."""
    if spec.get("swagger") == "2.0":
        resolved = [_resolve(spec, item) for item in parameters]
        in_body = next((p for p in resolved if p.get("in") == "body"), None)
        if in_body is not None:
            return "json", _resolve(spec, in_body.get("schema")), "repeated"
        form = [p for p in resolved if p.get("in") == "formData" and isinstance(p.get("name"), str)]
        if not form:
            return None
        files = any(p.get("type") == "file" for p in form)
        # Swagger joins a list by commas unless it says `collectionFormat: multi`.
        arrays = [p for p in form if p.get("type") == "array"]
        joined = any(p.get("collectionFormat", "csv") != "multi" for p in arrays)
        return (
            "multipart" if files else "form",
            {
                "type": "object",
                "properties": {str(p["name"]): p for p in form if p.get("type") != "file"},
                "required": [str(p["name"]) for p in form if p.get("required") is True],
            },
            "commas" if joined else "repeated",
        )
    content = _dict(_resolve(spec, op.get("requestBody")).get("content"))
    offered = sorted(
        (rank, encoding, kind)
        for kind in content
        for rank, encoding in [_taken_as(str(kind))]
        if encoding
    )
    if not offered:
        return None
    _, encoding, kind = offered[0]
    media = _dict(content[kind])
    # OpenAPI repeats a list's name (`tag=a&tag=b`) unless the description gives the
    # property the style `deepObject`, as an API does that wants brackets (Stripe).
    styles = {_dict(how).get("style") for how in _dict(media.get("encoding")).values()}
    lists = "brackets" if encoding == "form" and "deepObject" in styles else "repeated"
    return encoding, _resolve(spec, media.get("schema")), lists


def _taken_as(media_type: str) -> tuple[int, str]:
    """How a body of this media type is taken, and how much it is preferred (less is
    more); an empty word for one that has no fields to tell (a file, plain text)."""
    kind = media_type.lower().split(";")[0].strip()
    if kind == "application/json":
        return 0, "json"
    if "json" in kind:
        return 1, "json"
    if kind == "application/x-www-form-urlencoded":
        return 2, "form"
    if kind.startswith("multipart/"):
        return 3, "multipart"
    return 9, ""


def _merged(spec: Json, schema: Any, depth: int = 0) -> tuple[dict[str, Any], set[str]]:
    """The properties of an object schema and which of them are required, through
    `allOf` (all of its parts) and `oneOf` / `anyOf` (any of them: a field is required
    only where every alternative requires it)."""
    node = _resolve(spec, schema)
    properties = dict(_dict(node.get("properties")))
    required = {name for name in _list(node.get("required")) if isinstance(name, str)}
    if depth > 6:  # a description that refers to itself
        return properties, required
    for part in _list(node.get("allOf")):
        more, needed = _merged(spec, part, depth + 1)
        for name, value in more.items():
            properties.setdefault(name, value)
        required |= needed
    alternatives = [
        _merged(spec, one, depth + 1)
        for one in [*_list(node.get("oneOf")), *_list(node.get("anyOf"))]
    ]
    for more, _ in alternatives:
        for name, value in more.items():
            properties.setdefault(name, value)
    if alternatives and all(more for more, _ in alternatives):
        required |= set.intersection(*(needed for _, needed in alternatives))
    return properties, required


def _is_object(spec: Json, schema: Any) -> bool:
    node = _resolve(spec, schema)
    if node.get("type") == "array" or "items" in node:
        return False
    return node.get("type") == "object" or bool(_merged(spec, node)[0])


def _type_word(spec: Json, schema: Any) -> str:
    """A field's type in a word or two: `string`, `integer`, `string (date-time)`,
    `string or integer`, `object`."""
    node = _resolve(spec, schema)
    kind: Any = node.get("type")
    if isinstance(kind, list):  # OpenAPI 3.1: ["string", "null"]
        kind = " or ".join(str(k) for k in cast(list[Any], kind) if k != "null")
    if not kind:
        named = [
            _resolve(spec, one).get("type")
            for one in [*_list(node.get("oneOf")), *_list(node.get("anyOf"))]
        ]
        distinct = list(dict.fromkeys(str(k) for k in named if isinstance(k, str)))
        kind = " or ".join(distinct) or ("object" if _is_object(spec, node) else "any")
    form = node.get("format")
    if kind == "string" and isinstance(form, str) and form and form != "string":
        kind = f"string ({form})"
    return _plain(str(kind), 40) or "any"


def _plain(text: str, limit: int) -> str:
    """One line of printable text, cut to a length."""
    return " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())[:limit].strip()


def _fields(spec: Json, root: Json) -> tuple[list[dict[str, Any]], int]:
    """The fields of a body, the top ones first and inside each object the required
    ones first, and how many there were no room for."""
    fields: list[dict[str, Any]] = []
    left_out = 0
    queue: list[tuple[str, Any, int]] = [("", root, 1)]
    while queue:
        prefix, schema, depth = queue.pop(0)
        properties, required = _merged(spec, schema)
        ordered = sorted(properties.items(), key=lambda item: item[0] not in required)
        for name, raw in ordered:
            node = _resolve(spec, raw)
            if node.get("readOnly") is True or node.get("deprecated") is True:
                continue  # not something to send
            if not _FIELD_NAME.match(str(name)):
                left_out += 1
                continue
            path, inner = f"{prefix}{name}", node
            if node.get("type") == "array" or "items" in node:
                path, inner = f"{path}[]", _resolve(spec, node.get("items"))
            if not BODY_FIELD.match(path):
                left_out += 1
                continue
            if len(fields) >= MAX_BODY_FIELDS:
                left_out += 1
                continue
            listed = [_plain(str(v), 60) for v in _list(inner.get("enum"))]
            field: dict[str, Any] = {"name": path, "type": _type_word(spec, inner)}
            if name in required:
                field["required"] = True
            if 0 < len(listed) <= MAX_FIELD_VALUES and all(listed):
                field["values"] = listed
            said = _plain(str(node.get("description") or ""), 160)
            if said:
                field["description"] = said
            fields.append(field)
            if depth < BODY_DEPTH and _is_object(spec, inner):
                queue.append((f"{path}.", inner, depth + 1))
    return fields, left_out


def classify(method: str, op_id: str, path: str, summary: str) -> tuple[Access, bool]:
    """Read/write/unknown by HTTP method alone; descriptions never make a write a read."""
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
