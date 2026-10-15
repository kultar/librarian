"""Executes approved calls against real APIs.

Security properties enforced here (in addition to the policy engine):
- the URL is built from the registry entry; callers can never supply a host
- only same-origin redirects are followed, and credentials are re-attached only there
- responses are size-capped before parsing
- secret values are injected at the last moment and never returned or logged
"""

from __future__ import annotations

import base64
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast
from urllib.parse import quote, urlencode, urljoin, urlsplit

import httpx

from librarian import __version__, telemetry
from librarian.audit import AuditEvent, AuditSink
from librarian.calls import ArgValue, Caller, CallRequest, CallResult, Provenance, WriteRequest
from librarian.net import PUBLIC, BlockedAddress, NetworkPolicy, pin
from librarian.oauth import ConsentBroker, principal_key
from librarian.observations import Observations
from librarian.policy import PolicyEngine
from librarian.registry.models import (
    ApiEntry,
    ApiKeyAuth,
    AuthSpec,
    BasicAuth,
    BearerAuth,
    EntryStatus,
    NoAuth,
    OAuth2UserAuth,
    Operation,
    ParamLocation,
    is_reserved_secret,
)
from librarian.secrets import SecretStore
from librarian.shaping import get_path, select, set_path

log = logging.getLogger(__name__)

MAX_REDIRECTS = 3
USER_AGENT = f"librarian/{__version__} (+https://github.com/kultar/librarian)"


class PolicyDenied(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CredentialMissing(Exception):
    """The API needs a credential that is not in the secret store."""

    def __init__(
        self, entry: ApiEntry, reason: str = "missing", principal: str | None = None
    ) -> None:
        super().__init__(f"no usable credential for {entry.id!r} ({reason})")
        self.entry = entry
        self.reason = reason
        # Set when the credential is the person's own (per-person OAuth): that person,
        # not the approvers, has to act.
        self.principal = principal


class Executor:
    def __init__(
        self,
        policy: PolicyEngine,
        secrets: SecretStore,
        audit: AuditSink,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 30.0,
        max_response_bytes: int = 2_000_000,
        net: NetworkPolicy = PUBLIC,
        pinned_client_factory: Callable[[], httpx.AsyncClient] | None = None,
        observations: Observations | None = None,
    ) -> None:
        self.policy = policy
        self.observations = observations or policy.observations
        self.secrets = secrets
        self.audit = audit
        self.client = client or httpx.AsyncClient(
            timeout=timeout_seconds, follow_redirects=False, headers={"User-Agent": USER_AGENT}
        )
        self.max_response_bytes = max_response_bytes
        # Called when an API whose entry declares no authentication answers 401. May
        # return the entry with the authentication librarian worked out, in which case
        # the normal credential flow takes over.
        self.on_unexpected_auth: Callable[[ApiEntry, int], Awaitable[ApiEntry | None]] | None = None
        # Per-person sign-in (OAuth): where each person's tokens come from, and whether
        # the person must be verified by the identity provider (principals.mode != off).
        self.consents: ConsentBroker | None = None
        self.require_verified_principal = False
        # Provisional entries come from agent-supplied specs, so by default they
        # may only reach public addresses (plus listed internal hosts). Verified
        # entries were human-reviewed.
        self.net = net
        # Pinned requests get a fresh client: httpcore pools connections by IP and
        # port, so a pooled connection verified for one hostname must not be reused
        # for another hostname on the same address.
        self._pinned_client_factory = pinned_client_factory or (
            lambda: httpx.AsyncClient(
                timeout=timeout_seconds, follow_redirects=False, headers={"User-Agent": USER_AGENT}
            )
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    async def execute(
        self, request: CallRequest, caller: Caller, *, auth: AuthSpec | None = None
    ) -> CallResult:
        """Run one read. `auth` sends the call with another shared credential than the
        entry's (used to test a freshly added write key with a read operation)."""
        attributes: dict[str, str] = {
            "librarian.api": request.api,
            "librarian.operation": request.operation,
            "librarian.caller": caller.agent,
        }
        with telemetry.tracer.start_as_current_span(
            "librarian.call", attributes=attributes
        ) as span:
            started = time.monotonic()
            try:
                result = await self._execute(request, caller, auth)
            except PolicyDenied as exc:
                telemetry.denials.add(1, {"librarian.api": request.api})
                span.set_attribute("librarian.denied", exc.reason)
                raise
            except CredentialMissing as exc:
                span.set_attribute("librarian.credential", exc.reason)
                raise
            code = result.status_code
            span.set_attribute("http.response.status_code", code)
            span.set_attribute("url.full", result.provenance.url)
            labels = {**attributes, "http.status_class": telemetry.status_class(code)}
            telemetry.calls.add(1, labels)
            telemetry.call_duration.record((time.monotonic() - started) * 1000, attributes)
            return result

    async def _execute(
        self, request: CallRequest, caller: Caller, auth: AuthSpec | None = None
    ) -> CallResult:
        if self.policy.registry.get(request.api) is None:
            await self.policy.registry.refresh_stored()  # learned on another replica?
        decision = self.policy.check(request, caller)
        if not decision.allowed or decision.entry is None or decision.operation is None:
            await self.audit.record(
                AuditEvent(
                    event="denied",
                    caller=caller.agent,
                    principal=caller.principal,
                    api=request.api,
                    operation=request.operation,
                    detail=decision.reason,
                )
            )
            raise PolicyDenied(decision.reason)

        entry, op = decision.entry, decision.operation
        limited = await self.policy.rate_limited(caller, entry)
        if limited:
            await self.audit.record(
                AuditEvent(
                    event="denied",
                    caller=caller.agent,
                    principal=caller.principal,
                    api=request.api,
                    operation=request.operation,
                    detail=limited,
                )
            )
            raise PolicyDenied(limited)
        secret = (
            await self._resolve_secret(entry, caller)
            if auth is None
            else await self._shared_secret(entry, auth)
        )
        started = time.monotonic()
        url, headers, query = _build_request(entry, op, request.args)
        secret_query = _auth_query(entry, secret, auth)
        _apply_auth_headers(entry, secret, headers, auth)

        pages: list[Any] = []
        total_bytes = 0
        truncated = False
        status_code = 0
        content_type = ""
        paging = entry.pagination_for(op)
        next_url: str | None = url
        next_query: dict[str, ArgValue] | None = query
        page_count = 0
        offset = _int(query.get(paging.offset_param or "")) if paging.offset_param else 0
        page_number = (
            _int(query.get(paging.page_param or ""), paging.first_page)
            if paging.page_param
            else paging.first_page
        )

        last_headers: dict[str, str] = {}
        while next_url is not None and page_count < request.max_pages:
            response, body, cut, final_url = await self._send(
                entry, op, next_url, next_query, secret_query, headers, request.body
            )
            last_headers = {
                name: response.headers[name]
                for name in ("deprecation", "sunset")
                if name in response.headers
            }
            page_count += 1
            status_code = response.status_code
            content_type = response.headers.get("content-type", "")
            total_bytes += len(body)
            truncated = truncated or cut
            page = _decode(body, content_type, cut)
            pages.append(page)
            if not response.is_success or cut or page_count >= request.max_pages:
                break
            if paging.style == "link":
                next_url = _next_link(response, entry, final_url)
                next_query = None  # the next link already carries its own query
                continue
            items = get_path(page, paging.items_path) if paging.items_path else None
            if paging.style == "cursor":
                cursor = get_path(page, paging.next_cursor_path or "")
                if not isinstance(cursor, str | int) or not str(cursor) or len(str(cursor)) > 2048:
                    break
                next_query = {**query, paging.cursor_param or "": str(cursor)}
            elif paging.style == "offset":
                if not isinstance(items, list) or not items:
                    break
                offset += len(cast(list[Any], items))
                total = get_path(page, paging.total_path) if paging.total_path else None
                if isinstance(total, int) and offset >= total:
                    break
                next_query = {**query, paging.offset_param or "": offset}
            else:  # page
                if not isinstance(items, list) or not items:
                    break
                page_number += 1
                next_query = {**query, paging.page_param or "": page_number}
            next_url = url

        ok = 200 <= status_code < 300
        data = _merge_pages(pages, paging.items_path)
        # Record what happened before computing warnings, so a first success counts.
        try:
            await self.observations.observe(
                entry, op, status_code, data if not truncated else None, last_headers
            )
        except Exception:  # observing must never break a call
            log.exception("recording an observation for %s.%s failed", entry.id, op.id)
        provenance = Provenance(
            api=entry.id,
            api_status=entry.status,
            operation=op.id,
            method=op.method,
            url=_redact(url, query, entry),
            caller=caller.agent,
            principal=caller.principal,
            principal_verified=caller.principal_verified,
            credential_ref=entry.secret_ref
            or (
                principal_key(entry.id, caller.principal)
                if caller.principal and entry.per_person
                else None
            ),
            pages=page_count,
            origin=op.origin.value,
            confirmed=self.observations.confirmed(entry, op),
        )
        await self.audit.record(
            AuditEvent(
                event="call",
                caller=caller.agent,
                principal=caller.principal,
                api=entry.id,
                operation=op.id,
                status_code=status_code,
                bytes=total_bytes,
                duration_ms=int((time.monotonic() - started) * 1000),
                detail="permission denied by the API" if status_code == 403 else None,
                extra={"pages": page_count, "truncated": truncated},
            )
        )
        if status_code == 401 and auth is not None:
            raise CredentialMissing(entry, reason="rejected with HTTP 401")
        if status_code == 401 and isinstance(entry.auth, OAuth2UserAuth):
            # The person's token no longer works: drop it and ask them to sign in again.
            if self.consents is not None and caller.principal:
                await self.consents.forget_tokens(entry.id, caller.principal)
            raise CredentialMissing(
                entry, reason="the sign-in was rejected (HTTP 401)", principal=caller.principal
            )
        if status_code == 401 and not isinstance(entry.auth, NoAuth):
            # The credential itself is missing, expired or revoked: ask for a new one.
            raise CredentialMissing(entry, reason="rejected with HTTP 401")
        error = None
        if status_code == 401:
            # The entry says "no authentication" (the spec declared none), yet the API
            # wants some. Let the service work the scheme out; then ask for a key.
            settled = (
                await self.on_unexpected_auth(entry, status_code)
                if self.on_unexpected_auth is not None
                else None
            )
            if settled is not None:
                raise CredentialMissing(settled, reason="rejected with HTTP 401; auth inferred")
            error = (
                f"{entry.name} answered 401 (not authenticated), but its entry declares no "
                "authentication. It needs a credential: choose its authentication in the "
                "console (APIs → this API → Authentication), then a key can be added."
            )
        if status_code == 403:
            # The credential works but may not see this resource. A new key would not
            # help, so tell the caller instead of paging an approver.
            error = (
                f"{entry.name} refused this request (HTTP 403): the credential is valid but "
                "lacks permission for this resource"
            )
        if request.select:
            data = select(data, request.select)
        return CallResult(
            status_code=status_code,
            ok=ok,
            data=data,
            truncated=truncated,
            content_type=content_type,
            error=error,
            warnings=self.observations.warnings(entry, op),
            provenance=provenance,
        )

    async def execute_write(
        self, entry: ApiEntry, op: Operation, request: WriteRequest, caller: Caller
    ) -> CallResult:
        """Send one approved write. Callers must have checked policy and approval first.

        The request is sent exactly once and redirects are never followed: a 307/308
        would replay the write against another URL.
        """
        auth = entry.write_auth
        if isinstance(auth, OAuth2UserAuth):
            secret: str | None = await self._person_token(entry, auth, caller)
            ref = principal_key(entry.id, caller.principal or "")
        else:
            ref = entry.write_secret_ref
            if auth is None or ref is None or is_reserved_secret(ref):
                raise PolicyDenied(f"API {entry.id!r} has no usable write credential")
            secret = await self.secrets.get(ref)
            if secret is None:
                raise CredentialMissing(entry, reason=f"write credential {ref!r} is missing")
        url, headers, query = _build_request(entry, op, request.args)
        _apply_auth_headers(entry, secret, headers, auth)
        params: dict[str, ArgValue] = {**query, **_auth_query(entry, secret, auth)}
        if request.idempotency_key:
            headers.setdefault("Idempotency-Key", request.idempotency_key)
        attributes = {
            "librarian.api": entry.id,
            "librarian.operation": op.id,
            "librarian.caller": caller.agent,
            "librarian.write": "true",
        }
        started = time.monotonic()
        with telemetry.tracer.start_as_current_span(
            "librarian.write", attributes=attributes
        ) as span:
            _assert_same_origin(entry, url)
            http_request = self.client.build_request(
                op.method,
                url,
                params=params or None,
                headers=headers,
                json=request.body if request.body is not None else None,
            )
            response = await self.client.send(http_request, stream=True)
            try:
                body, cut = await _read_capped(response, self.max_response_bytes)
            finally:
                await response.aclose()
            span.set_attribute("http.response.status_code", response.status_code)
        content_type = response.headers.get("content-type", "")
        status_code = response.status_code
        excerpt = json.dumps(request.body, default=str)[:2000] if request.body is not None else None
        await self.audit.record(
            AuditEvent(
                event="write",
                caller=caller.agent,
                principal=caller.principal,
                api=entry.id,
                operation=op.id,
                status_code=status_code,
                bytes=len(body),
                duration_ms=int((time.monotonic() - started) * 1000),
                extra={"args": request.args, "body_excerpt": excerpt},
            )
        )
        error = None
        if status_code in (401, 403):
            error = (
                f"{entry.name} refused the write (HTTP {status_code}): check the write credential"
            )
        elif response.is_redirect:
            error = f"{entry.name} answered with a redirect; writes are never re-sent elsewhere"
        return CallResult(
            status_code=status_code,
            ok=200 <= status_code < 300,
            data=_decode(body, content_type, cut),
            truncated=cut,
            content_type=content_type,
            error=error,
            provenance=Provenance(
                api=entry.id,
                api_status=entry.status,
                operation=op.id,
                method=op.method,
                url=_redact(url, query, entry),
                caller=caller.agent,
                principal=caller.principal,
                principal_verified=caller.principal_verified,
                credential_ref=ref,
            ),
        )

    async def _shared_secret(self, entry: ApiEntry, auth: AuthSpec) -> str | None:
        """The secret behind a given shared-key authentication (not the entry's own)."""
        if isinstance(auth, NoAuth | OAuth2UserAuth):
            return None
        if is_reserved_secret(auth.secret_ref):
            raise PolicyDenied(f"API {entry.id!r} references an internal secret")
        value = await self.secrets.get(auth.secret_ref)
        if value is None:
            raise CredentialMissing(entry, reason=f"credential {auth.secret_ref!r} is missing")
        return value

    async def _resolve_secret(self, entry: ApiEntry, caller: Caller) -> str | None:
        if isinstance(entry.auth, OAuth2UserAuth):
            return await self._person_token(entry, entry.auth, caller)
        ref = entry.secret_ref
        if ref is None:
            return None
        if is_reserved_secret(ref):  # defense in depth; models already forbid this
            raise PolicyDenied(f"API {entry.id!r} references an internal secret")
        value = await self.secrets.get(ref)
        if value is None:
            raise CredentialMissing(entry)
        return value

    async def _person_token(self, entry: ApiEntry, auth: OAuth2UserAuth, caller: Caller) -> str:
        """The calling person's own access token for this API, or the reasons there is none."""
        if self.consents is None:
            raise PolicyDenied(f"API {entry.id!r} needs per-person sign-in, which is not set up")
        if entry.status is not EntryStatus.VERIFIED:
            raise PolicyDenied(
                f"API {entry.id!r} uses per-person sign-in, which needs a verified entry"
            )
        if not caller.principal:
            raise PolicyDenied(f"API {entry.id!r} acts as the person, and this request has none")
        if self.require_verified_principal and not caller.principal_verified:
            raise PolicyDenied(
                f"API {entry.id!r} acts as the person, so the person must be verified "
                "(send an X-Principal-Token)"
            )
        tokens = await self.consents.tokens_for(entry, auth, caller.principal)
        if tokens is None:
            raise CredentialMissing(entry, reason="sign-in needed", principal=caller.principal)
        return tokens.access_token

    async def _send(
        self,
        entry: ApiEntry,
        op: Operation,
        url: str,
        query: dict[str, ArgValue] | None,
        secret_query: dict[str, str],
        headers: dict[str, str],
        body: dict[str, Any] | None,
    ) -> tuple[httpx.Response, bytes, bool, str]:
        """Send with same-origin redirects. Returns (response, body, truncated, final URL)."""
        method = op.method
        current = url
        params: dict[str, ArgValue] = {**(query or {}), **secret_query}
        must_pin = entry.status is not EntryStatus.VERIFIED and not self.net.allow_private
        for _ in range(MAX_REDIRECTS + 1):
            _assert_same_origin(entry, current)
            target, send_headers, extensions = current, headers, None
            if must_pin:
                try:
                    pinned = await pin(current, self.net)
                except BlockedAddress as exc:
                    raise PolicyDenied(f"provisional API {entry.id!r}: {exc}") from exc
                target = pinned.url
                send_headers = {**headers, "Host": pinned.host_header}
                extensions = pinned.extensions
            client = self._pinned_client_factory() if must_pin else self.client
            try:
                request = client.build_request(
                    method,
                    target,
                    params=params or None,
                    headers=send_headers,
                    json=body if body is not None else None,
                    extensions=extensions,
                )
                response = await client.send(request, stream=True)
                try:
                    if response.is_redirect:
                        location = response.headers.get("location", "")
                        next_target = urljoin(current, location)
                        if not _same_origin(entry, next_target):
                            # Do not follow (and do not leak credentials) off-origin.
                            return response, b"", False, current
                        current = next_target
                        params = dict(secret_query)  # redirect target carries its own query
                        if response.status_code in (301, 302, 303):
                            method, body = "GET", None
                        continue
                    payload, cut = await _read_capped(response, self.max_response_bytes)
                    return response, payload, cut, current
                finally:
                    await response.aclose()
            finally:
                if client is not self.client:
                    await client.aclose()
        raise PolicyDenied(f"too many redirects calling {entry.id!r}")


def _build_request(
    entry: ApiEntry, op: Operation, args: dict[str, ArgValue]
) -> tuple[str, dict[str, str], dict[str, ArgValue]]:
    path = op.path
    query: dict[str, ArgValue] = {}
    headers: dict[str, str] = {"Accept": "application/json"}
    for param in op.parameters:
        if param.name not in args:
            continue
        value = args[param.name]
        if param.location is ParamLocation.PATH:
            path = path.replace("{" + param.name + "}", quote(str(value), safe=""))
        elif param.location is ParamLocation.QUERY:
            query[param.name] = value
        else:
            headers[param.name] = (
                ",".join(map(str, value)) if isinstance(value, list) else str(value)
            )
    url = entry.base_url + path
    _assert_same_origin(entry, url)
    return url, headers, query


def _apply_auth_headers(
    entry: ApiEntry, secret: str | None, headers: dict[str, str], auth: AuthSpec | None = None
) -> None:
    auth = auth if auth is not None else entry.auth
    if secret is None or isinstance(auth, NoAuth):
        return
    if isinstance(auth, BearerAuth | OAuth2UserAuth):
        headers["Authorization"] = f"Bearer {secret}"
    elif isinstance(auth, BasicAuth):
        headers["Authorization"] = "Basic " + base64.b64encode(secret.encode()).decode()
    elif auth.location == "header":
        headers[auth.name] = secret


def _auth_query(
    entry: ApiEntry, secret: str | None, auth: AuthSpec | None = None
) -> dict[str, str]:
    auth = auth if auth is not None else entry.auth
    if secret is not None and isinstance(auth, ApiKeyAuth) and auth.location == "query":
        return {auth.name: secret}
    return {}


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    return parts.scheme.lower(), (parts.hostname or "").lower(), parts.port


def _same_origin(entry: ApiEntry, url: str) -> bool:
    return _origin(url) == _origin(entry.base_url)


def _assert_same_origin(entry: ApiEntry, url: str) -> None:
    if not _same_origin(entry, url):
        raise PolicyDenied(f"refusing request outside {entry.base_url}")


async def _read_capped(response: httpx.Response, limit: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        remaining = limit - size
        if len(chunk) > remaining:
            chunks.append(chunk[:remaining])
            return b"".join(chunks), True
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks), False


def _decode(body: bytes, content_type: str, truncated: bool) -> Any:
    text = body.decode("utf-8", errors="replace")
    if "json" in content_type and not truncated and text:
        try:
            return json.loads(text)
        except ValueError:
            return text
    return text


def _next_link(response: httpx.Response, entry: ApiEntry, base_url: str) -> str | None:
    link = response.links.get("next")
    if not link:
        return None
    # Join against the real URL (the response URL may be a pinned IP address).
    target = urljoin(base_url, link.get("url", ""))
    return target if _same_origin(entry, target) else None


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _merge_pages(pages: list[Any], items_path: str | None) -> Any:
    """One page as-is; several pages merged into one list at `items_path`."""
    if len(pages) == 1:
        return pages[0]
    if items_path is None:
        return pages
    merged: list[Any] = []
    for page in pages:
        items = get_path(page, items_path)
        if isinstance(items, list):
            merged.extend(cast(list[Any], items))
    return set_path(pages[-1], items_path, merged)


def _redact(url: str, query: dict[str, ArgValue], entry: ApiEntry) -> str:
    if not query:
        return url
    flat: list[tuple[str, str]] = []
    for key, value in query.items():
        values = value if isinstance(value, list) else [value]
        flat.extend((key, str(v)) for v in values)
    auth = entry.auth
    if isinstance(auth, ApiKeyAuth) and auth.location == "query":
        flat = [(k, "REDACTED" if k == auth.name else v) for k, v in flat]
    return f"{url}?{urlencode(flat)}"
