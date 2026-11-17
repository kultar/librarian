"""Executes approved calls against real APIs.

Security properties enforced here (in addition to the policy engine):
- the URL is built from the registry entry; callers can never supply a host
- only same-origin redirects are followed, and credentials are re-attached only there
- responses are size-capped before parsing
- secret values are injected at the last moment and never returned or logged: a
  credential the API sends back (an echo, an error quoting the request) is removed from
  the answer, and a credential that cannot be sent is refused without being named
- where the API allows it, what is sent is made for the call and expires by itself (an
  OAuth token, a GitHub App token for one repository, an AWS signature) instead of
  being a stored key (`librarian.minted`)
- every request has a time limit, and an API that cannot be reached or does not finish
  answering is reported as exactly that (`ApiUnavailable`), saying for a write whether
  the request can have reached it
- an answer is only passed on as data if everything downstream can handle it; a write
  that was sent is always recorded, whatever its answer looks like
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, cast
from urllib.parse import quote, quote_plus, urlencode, urljoin, urlsplit

import httpx

from librarian import __version__, telemetry
from librarian.audit import AuditEvent, AuditSink
from librarian.calls import ArgValue, Caller, CallRequest, CallResult, Provenance, WriteRequest
from librarian.errors import ApiUnavailable, CredentialSetupError, why_no_answer
from librarian.minted import AwsSigner, Minter, SecretRejected, SetupProblem
from librarian.net import PUBLIC, BlockedAddress, NetworkPolicy, pin
from librarian.oauth import ConsentBroker, principal_key
from librarian.observations import Observations
from librarian.policy import PolicyEngine
from librarian.registry.models import (
    KEYLESS,
    NEEDS_VERIFIED,
    ApiEntry,
    ApiKeyAuth,
    AuthSpec,
    AwsSigV4Auth,
    BasicAuth,
    BearerAuth,
    EntryStatus,
    GitHubAppTokenAuth,
    NoAuth,
    OAuth2ClientAuth,
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


@dataclass
class _Credential:
    """What a call is authenticated with, as it goes out."""

    secret: str | None  # the value sent: a stored key, or a token made for this call
    auth: AuthSpec  # how it travels (a made token travels as a bearer token)
    echoes: tuple[str, ...]  # every form of it that must not come back in an answer
    signer: AwsSigner | None = None  # signs each request instead (AWS)
    note: str = ""  # for the audit trail, when the credential was made for the call
    forget: Callable[[], None] | None = None  # drop a made token the API refused

    @property
    def made(self) -> bool:
        return self.forget is not None or self.signer is not None

    def not_to_come_back(self) -> tuple[str, ...]:
        if self.signer is None:
            return self.echoes
        signed = {form for value in self.signer.sent for form in credential_forms(value)}
        return tuple(sorted(set(self.echoes) | signed, key=lambda f: (-len(f), f)))


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
        deadline_seconds: float | None = None,
    ) -> None:
        self.policy = policy
        self.observations = observations or policy.observations
        self.secrets = secrets
        self.audit = audit
        self.client = client or httpx.AsyncClient(
            timeout=timeout_seconds, follow_redirects=False, headers={"User-Agent": USER_AGENT}
        )
        self.max_response_bytes = max_response_bytes
        # One request to an API (connecting, sending, and reading the whole answer) must
        # finish within this. The client's own timeouts apply to each step, so an API
        # that keeps sending a byte now and then would otherwise never time out.
        self.deadline_seconds = (
            deadline_seconds if deadline_seconds is not None else 2 * timeout_seconds
        )
        # Makes the credentials that are not stored: tokens and signatures for one call.
        self.minter = Minter(self.client, deadline_seconds=self.deadline_seconds)
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
            except ApiUnavailable as exc:
                span.set_attribute("librarian.unavailable", exc.reason)
                telemetry.calls.add(1, {**attributes, "http.status_class": "none"})
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
                    extra=_via(caller),
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
                    extra=_via(caller),
                )
            )
            raise PolicyDenied(limited)
        declared = auth if auth is not None else entry.auth
        stored = (
            await self._resolve_secret(entry, caller)
            if auth is None
            else await self._shared_secret(entry, auth)
        )
        cred = await self._credential(entry, op, request.args, declared, stored, caller)
        secret = cred.secret
        started = time.monotonic()
        url, headers, query = _build_request(entry, op, request.args)
        secret_query = _auth_query(entry, secret, cred.auth)
        _apply_auth_headers(entry, secret, headers, cred.auth)
        echoes = cred.echoes

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
        response_headers: Mapping[str, str] = {}
        while next_url is not None and page_count < request.max_pages:
            try:
                response, body, cut, final_url = await self._send(
                    entry,
                    op,
                    next_url,
                    next_query,
                    secret_query,
                    headers,
                    request.body,
                    cred.signer,
                )
            except ApiUnavailable as gone:
                await self._no_answer(entry, op, caller, gone, cred.not_to_come_back(), started)
                raise
            echoes = cred.not_to_come_back()  # a signature is new with every request
            response_headers = response.headers
            last_headers = {
                name: _scrub(response.headers[name], echoes)
                for name in ("deprecation", "sunset")
                if name in response.headers
            }
            page_count += 1
            status_code = response.status_code
            content_type = response.headers.get("content-type", "")
            total_bytes += len(body)
            truncated = truncated or cut
            # Nothing below (the caller, a model, the audit trail, what is remembered
            # about the API) may see the credential, even if the API sent it back.
            page = _scrub(_decode(body, content_type, cut), echoes, cut=cut)
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
        refusal, refusal_detail = _refusal(entry, status_code, response_headers, data, secret)
        extra: dict[str, Any] = {"pages": page_count, "truncated": truncated}
        if cred.note:
            extra["credential"] = cred.note
        if status_code >= 400:
            extra["upstream"] = _said(data, secret)
            log.info(
                "%s.%s answered HTTP %s for %s: %s",
                entry.id,
                op.id,
                status_code,
                caller.agent,
                refusal_detail or extra["upstream"] or "(no body)",
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
                detail=refusal_detail,
                extra=extra | _via(caller),
            )
        )
        if status_code == 401 and cred.made:
            # What the token or signature was made from was accepted a moment ago, so a
            # person storing a new secret would not help. Make a new token next time, and
            # say what is known.
            if cred.forget is not None:
                cred.forget()
            return CallResult(
                status_code=status_code,
                ok=False,
                data=data,
                truncated=truncated,
                content_type=content_type,
                error=_made_but_refused(entry, declared, data, secret),
                warnings=self.observations.warnings(entry, op),
                provenance=provenance,
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
        if refusal is not None:
            # Rate limited, SAML, or the credential works but may not see this resource.
            # A new key would not help, so tell the caller instead of paging an approver.
            error = refusal
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
        self,
        entry: ApiEntry,
        op: Operation,
        request: WriteRequest,
        caller: Caller,
        *,
        noted: Mapping[str, Any] | None = None,
    ) -> CallResult:
        """Send one approved write. Callers must have checked policy and approval first.

        The request is sent exactly once and redirects are never followed: a 307/308
        would replay the write against another URL. `noted` goes into the audit record
        of this write as it is: the approval it was made under and, for a plan, the step.
        """
        auth = entry.write_auth
        stored: str | None = None
        ref: str | None = None
        if isinstance(auth, OAuth2UserAuth):
            stored = await self._person_token(entry, auth, caller)
            ref = principal_key(entry.id, caller.principal or "")
        elif isinstance(auth, AwsSigV4Auth):
            pass  # signed for this request: there is nothing stored
        else:
            ref = entry.write_secret_ref
            if auth is None or ref is None or is_reserved_secret(ref):
                raise PolicyDenied(f"API {entry.id!r} has no usable write credential")
            stored = await self.secrets.get(ref)
            if stored is None:
                raise CredentialMissing(entry, reason=f"write credential {ref!r} is missing")
        cred = await self._credential(
            entry, op, request.args, auth, stored, caller, write=True, what="the write credential"
        )
        secret = cred.secret
        url, headers, query = _build_request(entry, op, request.args)
        _apply_auth_headers(entry, secret, headers, cred.auth)
        echoes = cred.echoes
        params: dict[str, ArgValue] = {**query, **_auth_query(entry, secret, cred.auth)}
        if request.idempotency_key:
            headers.setdefault("Idempotency-Key", request.idempotency_key)
        excerpt = json.dumps(request.body, default=str)[:2000] if request.body is not None else None
        sent: dict[str, Any] = {**(noted or {}), "args": request.args, "body_excerpt": excerpt}
        if cred.note:
            sent["credential"] = cred.note
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
            if cred.signer is not None:
                cred.signer.sign(http_request)
                echoes = cred.not_to_come_back()
            response: httpx.Response | None = None
            body, cut, incomplete = b"", False, False
            try:
                async with asyncio.timeout(self.deadline_seconds):
                    response = await self.client.send(http_request, stream=True)
                    try:
                        body, cut = await _read_capped(response, self.max_response_bytes)
                    finally:
                        await response.aclose()
            except (httpx.HTTPError, TimeoutError) as exc:
                if response is None:
                    gone = _unavailable(entry, exc, write=True)
                    await self._no_answer(entry, op, caller, gone, echoes, started, sent)
                    raise gone from None
                # The API answered and then the answer broke off: what it decided is in
                # the status line, so the write is reported with that, not as unknown.
                body, cut, incomplete = b"", True, True
            span.set_attribute("http.response.status_code", response.status_code)
        content_type = response.headers.get("content-type", "")
        status_code = response.status_code
        # From here on the write has been sent: whatever its answer looks like, the
        # outcome is recorded and returned. The result is stored with the approval and
        # returned to the agent: neither may hold the write credential, even if the API
        # sent it back.
        try:
            data = _scrub(_decode(body, content_type, cut), echoes, cut=cut)
            refusal, refusal_detail = _refusal(
                entry, status_code, response.headers, data, secret, write=True
            )
        except Exception as exc:
            log.error(
                "%s.%s answered HTTP %s to a write, but the answer could not be read (%s)",
                entry.id,
                op.id,
                status_code,
                type(exc).__name__,
            )
            data, refusal_detail = None, "the answer could not be read"
            refusal = f"{entry.name} answered HTTP {status_code}, but its answer could not be read"
        if incomplete:
            data, refusal_detail = None, "the answer did not arrive completely"
            refusal = (
                f"{entry.name} answered HTTP {status_code}, but the answer did not arrive "
                "completely"
            )
        extra: dict[str, Any] = dict(sent)
        if status_code >= 400:
            extra["upstream"] = _said(data, secret)
            log.info(
                "%s.%s answered HTTP %s to a write by %s: %s",
                entry.id,
                op.id,
                status_code,
                caller.agent,
                refusal_detail or extra["upstream"] or "(no body)",
            )
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
                detail=refusal_detail,
                extra=extra | _via(caller),
            )
        )
        error = refusal
        if status_code == 401 and cred.made:
            if cred.forget is not None:
                cred.forget()  # make a new token for the next write
            error = _made_but_refused(entry, auth, data, secret, write=True)
        elif status_code == 401:
            error = f"{entry.name} refused the write (HTTP 401): check the write credential"
        elif error is None and response.is_redirect:
            error = f"{entry.name} answered with a redirect; writes are never re-sent elsewhere"
        return CallResult(
            status_code=status_code,
            ok=200 <= status_code < 300,
            data=data,
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
        if isinstance(auth, KEYLESS):
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

    async def _credential(
        self,
        entry: ApiEntry,
        op: Operation,
        args: Mapping[str, ArgValue],
        auth: AuthSpec,
        stored: str | None,
        caller: Caller,
        *,
        write: bool = False,
        what: str = "the stored credential",
    ) -> _Credential:
        """What this call goes out with: the stored key, or what is made from it (or
        from nothing stored at all) for this call alone."""
        if (
            isinstance(auth, NEEDS_VERIFIED)
            and not isinstance(auth, OAuth2UserAuth)  # checked where the person's token is read
            and entry.status is not EntryStatus.VERIFIED
        ):
            raise PolicyDenied(
                f"API {entry.id!r} uses {auth.type} authentication, which needs a verified entry"
            )
        secret = _sendable(entry, stored, auth, caller, what=what)
        try:
            if isinstance(auth, AwsSigV4Auth):
                signer = await self.minter.aws_signer(entry, auth)
                return _Credential(None, auth, (), signer=signer, note=signer.note)
            if secret is not None and isinstance(auth, OAuth2ClientAuth | GitHubAppTokenAuth):
                if isinstance(auth, OAuth2ClientAuth):
                    made = await self.minter.client_token(entry, auth, secret)
                else:
                    made = await self.minter.github_token(
                        entry, auth, secret, op, args, write=write
                    )
                token = _sendable(entry, made.token, _AS_A_BEARER_KEY, caller, what="the token")
                # Neither the token nor what it was made from may come back.
                echoes = set(_echoes(token, _AS_A_BEARER_KEY)) | set(credential_forms(secret))
                return _Credential(
                    token,
                    _AS_A_BEARER_KEY,
                    tuple(sorted(echoes, key=lambda f: (-len(f), f))),
                    note=made.note,
                    forget=made.forget,
                )
        except SecretRejected as exc:
            raise CredentialMissing(entry, reason=str(exc)) from None
        except SetupProblem as exc:
            raise CredentialSetupError(f"{entry.name}: {exc}") from None
        return _Credential(secret, auth, _echoes(secret, auth))

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
        signer: AwsSigner | None = None,
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
                if signer is not None:
                    signer.sign(request)  # each request, also after a redirect, has its own
                try:
                    async with asyncio.timeout(self.deadline_seconds):
                        response = await client.send(request, stream=True)
                        try:
                            if response.is_redirect:
                                location = response.headers.get("location", "")
                                next_target = urljoin(current, location)
                                if not _same_origin(entry, next_target):
                                    # Do not follow (and do not leak credentials) off-origin.
                                    return response, b"", False, current
                                current = next_target
                                params = dict(secret_query)  # the target carries its own query
                                if response.status_code in (301, 302, 303):
                                    method, body = "GET", None
                                continue
                            payload, cut = await _read_capped(response, self.max_response_bytes)
                            return response, payload, cut, current
                        finally:
                            await response.aclose()
                except (httpx.HTTPError, TimeoutError) as exc:
                    raise _unavailable(entry, exc, write=False) from None
            finally:
                if client is not self.client:
                    await client.aclose()
        raise PolicyDenied(f"too many redirects calling {entry.id!r}")

    async def _no_answer(
        self,
        entry: ApiEntry,
        op: Operation,
        caller: Caller,
        gone: ApiUnavailable,
        echoes: tuple[str, ...],
        started: float,
        sent: dict[str, Any] | None = None,
    ) -> None:
        """Record a call that got no answer: in the log, with the HTTP client's own words
        minus the credential, and in the audit trail. `sent`: what a write carried."""
        detail = _scrub(gone.detail, echoes)
        log.warning(
            "%s.%s got no answer for %s: %s%s",
            entry.id,
            op.id,
            caller.agent,
            gone.reason,
            f" ({detail})" if detail else "",
        )
        extra: dict[str, Any] = dict(sent) if sent is not None else {}
        if sent is not None:
            extra["sent"] = gone.sent
        await self.audit.record(
            AuditEvent(
                event="call" if sent is None else "write",
                caller=caller.agent,
                principal=caller.principal,
                api=entry.id,
                operation=op.id,
                duration_ms=int((time.monotonic() - started) * 1000),
                detail=f"no answer: {gone.reason}",
                extra=extra | _via(caller),
            )
        )


def _unavailable(entry: ApiEntry, exc: BaseException, *, write: bool) -> ApiUnavailable:
    """Why a call got no answer, in words for the calling agent (see `why_no_answer`)."""
    reason, detail, sent = why_no_answer(exc)
    if not write:
        consequence = "Nothing was read: retry later."
    elif sent:
        consequence = "The write may have reached it: check before sending it again."
    else:
        consequence = "The write was not sent."
    return ApiUnavailable(
        f"{entry.name} gave no answer: {reason}. {consequence}",
        sent=sent,
        reason=reason,
        detail=detail,
    )


SAID_LIMIT = 240
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_TOKEN_SHAPES = re.compile(
    r"(?i)\b(?:bearer\s+\S+|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16})"
)


def _said(data: Any, secret: str | None = None) -> str:
    """What the API said in an error body: one short line, with any credential removed."""
    text = ""
    if isinstance(data, dict):
        found = cast(dict[str, Any], data)
        for key in ("message", "error_description", "detail", "title", "error", "errors"):
            value = found.get(key)
            if value:
                text = value if isinstance(value, str) else json.dumps(value, default=str)
                break
        else:
            text = json.dumps(found, default=str) if found else ""
    elif isinstance(data, str):
        text = data
    elif data is not None:
        text = json.dumps(data, default=str)
    # One line, and nothing a terminal would act on: this text goes into the log.
    text = _CONTROL.sub("", " ".join(text.split()))
    if secret:
        text = text.replace(secret, "[redacted]")
    text = _TOKEN_SHAPES.sub("[redacted]", text)
    return text if len(text) <= SAID_LIMIT else text[: SAID_LIMIT - 1] + "…"


# What the HTTP client refuses to put into a header: these, and anything outside ASCII.
_NOT_FOR_A_HEADER = re.compile(r"[\x00\n\r\x0b\x0c]|[^\x00-\x7f]")


def trimmed(secret: str) -> str:
    """A stored credential without what is never part of it: the whitespace around it (a
    trailing line break, when it was stored from a file) and a byte-order mark."""
    return secret.strip().removeprefix("\ufeff").strip()


def unsendable(secret: str, auth: AuthSpec) -> bool:
    """Whether this (trimmed) credential travels in a header and cannot be put into one."""
    in_header = isinstance(auth, BearerAuth | OAuth2UserAuth) or (
        isinstance(auth, ApiKeyAuth) and auth.location == "header"
    )
    return in_header and bool(_NOT_FOR_A_HEADER.search(secret))


_AS_A_BEARER_KEY = BearerAuth.model_validate({"type": "bearer", "secret_ref": "any/key"})


def credential_forms(secret: str, auth: AuthSpec | None = None) -> tuple[str, ...]:
    """Every form a stored credential can turn up in, for code that looks for one outside
    an API's answer (`librarian doctor`). Without `auth`: as a bearer key would be sent."""
    return _echoes(trimmed(secret), auth if auth is not None else _AS_A_BEARER_KEY)


def _sendable(
    entry: ApiEntry,
    secret: str | None,
    auth: AuthSpec,
    caller: Caller,
    *,
    what: str = "the stored credential",
) -> str | None:
    """The credential as it will be sent.

    Whitespace around it is not part of it: a trailing line break is the usual accident
    when a key is stored from a file (and a byte-order mark when a Windows editor wrote
    the file). A value that cannot go into an HTTP header is refused here, with a reason
    that does not name it. The HTTP client would refuse it as well, but its error quotes
    the header it refused, and that error would travel to the agent and the logs.
    """
    if secret is None:
        return None
    secret = trimmed(secret)
    person = caller.principal if isinstance(auth, OAuth2UserAuth) else None
    if not secret:
        raise CredentialMissing(entry, reason=f"{what} is empty", principal=person)
    if unsendable(secret, auth):
        raise CredentialMissing(
            entry,
            reason=f"{what} contains a line break or another character that cannot be sent "
            "in an HTTP header; store it again",
            principal=person,
        )
    return secret


REDACTED = "[redacted]"
# A shorter value is left alone: replacing a few characters wherever they occur would
# mangle ordinary data, and a value that short protects nothing.
SCRUB_MIN = 6
# An answer cut off inside the credential ends with the start of it. Fewer characters
# than this give nothing away (many keys begin with a public prefix anyway).
CUT_MIN = 4


def _echoes(secret: str | None, auth: AuthSpec) -> tuple[str, ...]:
    """Every form in which an API could send this call's credential back.

    Some APIs answer with the request they received: an echo or debugging endpoint, an
    error page that quotes the URL, a message that names the rejected key. The forms
    are the value as it was sent (for Basic: the encoded pair, the pair, and its secret
    half; after a scheme word such as "Token": the key alone as well), each of them
    also as a URL, a JSON string and an HTML page would write it. Longest first, so a
    form that contains another is replaced whole.
    """
    if secret is None or isinstance(auth, NoAuth):
        return ()
    sent = [secret]
    if isinstance(auth, BasicAuth):
        user, _, password = secret.partition(":")
        # The name says who is calling and turns up in ordinary data (an email address);
        # the password is the secret, unless the key itself is sent as the name.
        sent += [base64.b64encode(secret.encode()).decode(), password or user]
    elif isinstance(auth, ApiKeyAuth) and auth.location == "header":
        words = secret.split()
        if len(words) == 2:  # "Token <key>": a message may name the key without the word
            sent.append(words[1])
    forms: set[str] = set()
    for value in sent:
        forms |= {
            value,
            str(httpx.QueryParams({"k": value}))[2:],  # as httpx writes it into a URL
            quote(value, safe=""),
            quote_plus(value),
            json.dumps(value)[1:-1],
            json.dumps(value, ensure_ascii=False)[1:-1],
            html.escape(value),
        }
    return tuple(sorted((f for f in forms if len(f) >= SCRUB_MIN), key=lambda f: (-len(f), f)))


def _scrub(data: Any, echoes: tuple[str, ...], *, cut: bool = False) -> Any:
    """What an API answered, without the credential the call was sent with.

    `data` is the decoded answer: parsed JSON (cleaned in place: it was decoded for this
    call alone) or text. `cut` says the answer was cut off at the size limit.
    """
    if not echoes:
        return data

    def clean(text: str) -> str:
        for form in echoes:
            if form in text:
                text = text.replace(form, REDACTED)
        return text

    if isinstance(data, str):
        text = clean(data)
        if cut:
            partial = max(
                (
                    size
                    for form in echoes
                    for size in range(CUT_MIN, len(form))
                    if text.endswith(form[:size])
                ),
                default=0,
            )
            if partial:
                text = text[:-partial] + REDACTED
        return text
    return rewrite_strings(data, clean)


def _made_but_refused(
    entry: ApiEntry, auth: AuthSpec | None, data: Any, secret: str | None, *, write: bool = False
) -> str:
    """What to tell the caller when an API answers 401 to a credential made for the call."""
    said = _said(data, secret)
    quoted = f" The API said: {said}" if said else ""
    kind = auth.type if auth is not None else "its"
    what = "the write" if write else "this request"
    return (
        f"{entry.name} refused {what} (HTTP 401) although the credential was made for it a "
        f"moment ago: check what its {kind} authentication asks for (scopes, audience, "
        f"permissions, the role) in the console.{quoted}"
    )


def _reset_time(headers: Mapping[str, str]) -> datetime | None:
    """When the limit lifts: Retry-After (seconds or a date), else X-RateLimit-Reset."""
    try:
        return _stated_reset_time(headers)
    except (OverflowError, ValueError, OSError):  # a number no calendar can hold
        return None


def _stated_reset_time(headers: Mapping[str, str]) -> datetime | None:
    retry_after = headers.get("retry-after")
    if retry_after:
        if retry_after.strip().isdigit():
            return datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=int(retry_after))
        try:
            return parsedate_to_datetime(retry_after).astimezone(UTC)
        except (TypeError, ValueError):
            pass
    reset = headers.get("x-ratelimit-reset") or headers.get("ratelimit-reset")
    if reset and reset.strip().isdigit():
        value = int(reset)
        if value > 10**9:  # an epoch, as GitHub sends it
            return datetime.fromtimestamp(value, UTC)
        return datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=value)
    return None


def _throttled(status_code: int, headers: Mapping[str, str], said: str) -> str | None:
    """How the API throttled the call and when to retry, or None when it did not throttle."""
    lowered = said.lower()
    limited = status_code == 429 or (
        status_code == 403
        and (
            headers.get("x-ratelimit-remaining") == "0"
            or "retry-after" in headers
            or "rate limit" in lowered
            or "too many requests" in lowered
        )
    )
    if not limited:
        return None
    kind = "a secondary rate limit" if "secondary" in lowered else "the rate limit"
    reset = _reset_time(headers)
    if reset is None:
        return f"{kind} is exhausted and the API gave no reset time; retry in a few minutes"
    wait = max(0, int((reset - datetime.now(UTC)).total_seconds()))
    minutes = (wait + 59) // 60
    when = reset.strftime("%Y-%m-%dT%H:%M:%SZ")
    in_words = f"in {minutes} min" if minutes else "now"
    return f"{kind} is exhausted until {when} ({in_words}); retry then"


def _refusal(
    entry: ApiEntry,
    status_code: int,
    headers: Mapping[str, str],
    data: Any,
    secret: str | None,
    *,
    write: bool = False,
) -> tuple[str | None, str | None]:
    """Why the API refused a call, for the caller and for the audit record.

    Rate limiting (429, or GitHub's 403 with the limit exhausted) is told apart from
    a permission problem so nobody goes checking token scopes when the token is fine.
    """
    said = _said(data, secret)
    quoted = f" The API said: {said}" if said else ""
    what = "the write" if write else "this request"
    throttled = _throttled(status_code, headers, said)
    if throttled is not None:
        return (
            f"{entry.name} is rate limiting this credential (HTTP {status_code}): {throttled}. "
            f"The credential and its permissions are fine.{quoted}",
            f"rate limited by the API: {throttled}",
        )
    if status_code != 403:
        return None, None
    if "saml" in said.lower() or "saml" in json.dumps(data, default=str).lower():
        return (
            f"{entry.name} refused {what} (HTTP 403): the organization enforces "
            "SAML single sign-on and this token is not authorized for it. On GitHub, "
            "open the token's settings and use Configure SSO to authorize it for the "
            "organization; the token itself is fine.",
            "refused by the API: token not authorized for SAML single sign-on",
        )
    credential = "the write credential" if write else "the credential"
    return (
        f"{entry.name} refused {what} (HTTP 403): {credential} is valid but lacks permission "
        f"for this resource.{quoted}",
        "permission denied by the API",
    )


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
            safe = "/" if param.allow_slash else ""  # policy checked the segments
            path = path.replace("{" + param.name + "}", quote(str(value), safe=safe))
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
    elif isinstance(auth, ApiKeyAuth) and auth.location == "header":
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


# JSON nested deeper than this is passed on as text: no real answer is, and what
# serialises a result next (for an agent, for the database) gives up well before the
# parser does.
MAX_DEPTH = 100
_HALF_PAIR = re.compile("[\ud800-\udfff]")


def _decode(body: bytes, content_type: str, truncated: bool) -> Any:
    """The answer as data: parsed when it is JSON that can be passed on, else as text.

    "Can be passed on" matters as much as "parses". A document nested hundreds of levels
    deep, a number that is not one (NaN, Infinity, 1e999) and half of a Unicode
    surrogate pair all parse here and then break whatever serialises the result next:
    after a write has already been sent.
    """
    text = body.decode("utf-8", errors="replace")
    if "json" not in content_type or truncated or not text:
        return text
    try:
        data: Any = json.loads(text, parse_constant=_not_a_number, parse_float=_finite)
    except (ValueError, RecursionError):
        return text
    if text.count("[") + text.count("{") > MAX_DEPTH and _deeper_than(data, MAX_DEPTH):
        return text
    if "\\ud" in text or "\\uD" in text:  # an escaped surrogate: is it half a pair?
        data = rewrite_strings(data, lambda value: _HALF_PAIR.sub("\ufffd", value))
    return data


def _not_a_number(_: str) -> None:
    """NaN and Infinity are not JSON; as JavaScript does, pass them on as null."""
    return None


def _finite(text: str) -> float | None:
    value = float(text)
    return value if math.isfinite(value) else None


def _deeper_than(data: Any, limit: int) -> bool:
    todo: list[tuple[Any, int]] = [(data, 1)]
    while todo:
        node, depth = todo.pop()
        if isinstance(node, dict):
            children = list(cast(dict[Any, Any], node).values())
        elif isinstance(node, list):
            children = cast(list[Any], node)
        else:
            continue
        if depth > limit:
            return True
        todo += [(child, depth + 1) for child in children if isinstance(child, dict | list)]
    return False


def rewrite_strings(data: Any, rewrite: Callable[[str], str]) -> Any:
    """Apply `rewrite` to every string in a decoded answer: values and keys, however deep.

    In place (the answer was decoded for this call alone) and without recursion (an
    answer may be nested deeper than a function may call itself).
    """
    if isinstance(data, str):
        return rewrite(data)
    todo: list[Any] = [data]
    while todo:
        node = todo.pop()
        if isinstance(node, dict):
            items = cast(dict[Any, Any], node)
            for key in list(items):
                value = items[key]
                if isinstance(value, str):
                    items[key] = rewrite(value)
                elif isinstance(value, dict | list):
                    todo.append(value)
                if isinstance(key, str) and (renamed := rewrite(key)) != key:
                    items[renamed] = items.pop(key)
        elif isinstance(node, list):
            values = cast(list[Any], node)
            for index, value in enumerate(values):
                if isinstance(value, str):
                    values[index] = rewrite(value)
                elif isinstance(value, dict | list):
                    todo.append(value)
    return data


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


def _via(caller: Caller) -> dict[str, Any]:
    """For the audit record: the Agent Substrate actor (one agent session) behind a
    call, when the agent was identified by its actor token."""
    return {"via": caller.via} if caller.via else {}


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
