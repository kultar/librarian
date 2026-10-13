"""Acting as the person: OAuth 2.0 authorization-code sign-in to an API, per principal.

Some APIs have no credential that is right for everyone: what Google Drive or Jira
returns depends on who is asking. For those, librarian is the OAuth client. The
first time a verified person's agent needs the API, the person is sent a one-time
link (a *consent request*), signs in to the provider in their browser, and the
callback stores their tokens in the secret store under the API and a hash of the
principal. From then on every call for that person carries that person's token, so
the API enforces the person's own access; librarian's policy applies on top.

Agents never see the tokens. Tokens are refreshed by librarian; a refresh that fails
simply asks the person to sign in again.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import secrets as pysecrets
import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, Field

from librarian.audit import AuditEvent, AuditSink
from librarian.calls import Caller
from librarian.registry.models import ApiEntry, OAuth2UserAuth
from librarian.secrets import SecretStore

log = logging.getLogger(__name__)

CONSENT_LIFETIME = timedelta(minutes=30)
REFRESH_MARGIN = timedelta(seconds=60)
POLL_SECONDS = 2.0


class OAuthError(Exception):
    pass


def principal_key(api_id: str, principal: str) -> str:
    """Where a person's tokens for an API live in the secret store (no email in names)."""
    digest = hashlib.sha256(principal.strip().lower().encode()).hexdigest()[:24]
    return f"{api_id}/users/{digest}"


class UserTokens(BaseModel):
    access_token: str
    refresh_token: str | None = None
    token_type: str = "Bearer"  # noqa: S105 (a scheme name, not a secret)
    expires_at: datetime | None = None
    scopes: list[str] = Field(default_factory=list[str])

    def expires_soon(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or datetime.now(UTC)) >= self.expires_at - REFRESH_MARGIN


class ConsentState(StrEnum):
    PENDING = "pending"
    CONNECTED = "connected"
    DENIED = "denied"
    EXPIRED = "expired"


class ConsentRequest(BaseModel):
    """One person's pending sign-in to one API."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    api: str
    api_name: str
    host: str
    principal: str
    agent: str
    scopes: list[str] = Field(default_factory=list[str])
    state: ConsentState = ConsentState.PENDING
    code_verifier: str = Field(default_factory=lambda: pysecrets.token_urlsafe(48))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime
    waiting: int = 0
    note: str | None = None


class Connection(BaseModel):
    """What the console shows: who is connected to what (never the tokens)."""

    api: str
    principal: str
    scopes: list[str] = Field(default_factory=list[str])
    connected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ConsentNotifier(Protocol):
    async def consent_opened(self, request: ConsentRequest, link: str) -> None: ...
    async def consent_closed(self, request: ConsentRequest) -> None: ...


class DocStore(Protocol):
    async def save_doc(self, kind: str, doc_id: str, state: str, data: str) -> None: ...
    async def get_doc(self, kind: str, doc_id: str) -> str | None: ...
    async def list_docs(self, kind: str, state: str | None = None) -> list[str]: ...


# -- the provider ----------------------------------------------------------------


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorize_url(auth: OAuth2UserAuth, redirect_uri: str, state: str, verifier: str) -> str:
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": auth.client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(auth.scopes),
        "state": state,
        **auth.extra_authorize_params,
    }
    if auth.pkce:
        params["code_challenge"] = _code_challenge(verifier)
        params["code_challenge_method"] = "S256"
    joiner = "&" if "?" in auth.authorization_url else "?"
    return f"{auth.authorization_url}{joiner}{urlencode(params)}"


async def _token_request(
    auth: OAuth2UserAuth,
    secrets: SecretStore,
    client: httpx.AsyncClient,
    data: dict[str, str],
) -> UserTokens:
    form = {"client_id": auth.client_id, **data}
    if auth.client_secret_ref:
        secret = await secrets.get(auth.client_secret_ref)
        if secret is None:
            raise OAuthError(f"the OAuth client secret {auth.client_secret_ref!r} is not set")
        form["client_secret"] = secret
    try:
        response = await client.post(
            auth.token_url, data=form, headers={"Accept": "application/json"}, timeout=20
        )
    except httpx.HTTPError as exc:
        raise OAuthError(f"the token endpoint could not be reached: {exc}") from exc
    if response.status_code != 200:
        raise OAuthError(f"the token endpoint answered HTTP {response.status_code}")
    try:
        payload: Any = response.json()
    except ValueError as exc:
        raise OAuthError("the token endpoint did not return JSON") from exc
    if not isinstance(payload, dict) or not payload.get("access_token"):  # pyright: ignore[reportUnknownMemberType]
        raise OAuthError("the token endpoint returned no access token")
    body: dict[str, Any] = payload  # pyright: ignore[reportUnknownVariableType]
    expires_in = body.get("expires_in")
    expires_at = (
        datetime.now(UTC) + timedelta(seconds=int(expires_in))
        if isinstance(expires_in, int | float) and expires_in > 0
        else None
    )
    scope = body.get("scope")
    return UserTokens(
        access_token=str(body["access_token"]),
        refresh_token=str(body["refresh_token"]) if body.get("refresh_token") else None,
        token_type=str(body.get("token_type") or "Bearer"),
        expires_at=expires_at,
        scopes=str(scope).split() if isinstance(scope, str) and scope else list(auth.scopes),
    )


async def exchange_code(
    auth: OAuth2UserAuth,
    secrets: SecretStore,
    client: httpx.AsyncClient,
    *,
    code: str,
    redirect_uri: str,
    verifier: str,
) -> UserTokens:
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
    if auth.pkce:
        data["code_verifier"] = verifier
    return await _token_request(auth, secrets, client, data)


async def refresh_tokens(
    auth: OAuth2UserAuth, secrets: SecretStore, client: httpx.AsyncClient, tokens: UserTokens
) -> UserTokens:
    if not tokens.refresh_token:
        raise OAuthError("no refresh token: the person has to sign in again")
    fresh = await _token_request(
        auth,
        secrets,
        client,
        {"grant_type": "refresh_token", "refresh_token": tokens.refresh_token},
    )
    if fresh.refresh_token is None:  # most providers keep the old one
        fresh.refresh_token = tokens.refresh_token
    return fresh


# -- the broker ------------------------------------------------------------------


class ConsentBroker:
    """Opens consent requests, waits for them, and holds each person's tokens."""

    def __init__(
        self,
        secrets: SecretStore,
        notifier: Any,
        audit: AuditSink,
        *,
        public_url: str,
        signing_key: bytes,
        client_factory: Any = None,
        store: DocStore | None = None,
    ) -> None:
        self.secrets = secrets
        self.notifier: ConsentNotifier = notifier
        self.audit = audit
        self.public_url = public_url.rstrip("/")
        self._signing_key = signing_key
        self.store = store
        self._client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=20))
        self._requests: dict[str, ConsentRequest] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._refreshing: dict[str, asyncio.Lock] = {}
        self._lock = asyncio.Lock()

    def set_signing_key(self, key: bytes) -> None:
        self._signing_key = key

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_url}/oauth/callback"

    # -- persistence -------------------------------------------------------------

    async def _save(self, request: ConsentRequest) -> None:
        if self.store is not None:
            await self.store.save_doc(
                "consent", request.id, request.state.value, request.model_dump_json()
            )

    async def load(self, request_id: str) -> ConsentRequest | None:
        if self.store is not None:
            data = await self.store.get_doc("consent", request_id)
            if data is not None:
                stored = ConsentRequest.model_validate_json(data)
                current = self._requests.get(request_id)
                if current is None:
                    self._requests[request_id] = stored
                    self._events.setdefault(request_id, asyncio.Event())
                    return stored
                for name in ConsentRequest.model_fields:
                    setattr(current, name, getattr(stored, name))
                return current
        return self._requests.get(request_id)

    def pending_for(self, api: str, principal: str) -> ConsentRequest | None:
        for request in self._requests.values():
            if (
                request.state is ConsentState.PENDING
                and request.api == api
                and request.principal.lower() == principal.lower()
            ):
                return request
        return None

    def pending(self) -> list[ConsentRequest]:
        return sorted(
            (r for r in self._requests.values() if r.state is ConsentState.PENDING),
            key=lambda r: r.created_at,
        )

    # -- links -------------------------------------------------------------------

    def token_for(self, request: ConsentRequest) -> str:
        message = f"consent:{request.id}:{request.created_at.isoformat()}".encode()
        return hmac.new(self._signing_key, message, hashlib.sha256).hexdigest()[:40]

    def check_token(self, request: ConsentRequest, token: str) -> bool:
        if request.state is not ConsentState.PENDING or datetime.now(UTC) > request.expires_at:
            return False
        return hmac.compare_digest(self.token_for(request), token)

    def link(self, request: ConsentRequest) -> str:
        return f"{self.public_url}/connect/{request.id}?t={self.token_for(request)}"

    def state_for(self, request: ConsentRequest) -> str:
        """The OAuth `state` parameter: the request id plus its link token."""
        return f"{request.id}.{self.token_for(request)}"

    # -- lifecycle ---------------------------------------------------------------

    async def open(self, entry: ApiEntry, caller: Caller, reason: str) -> ConsentRequest:
        """Open (or join) the pending sign-in of this person to this API."""
        if caller.principal is None:
            raise ValueError("a consent request needs a principal")
        auth = entry.auth if isinstance(entry.auth, OAuth2UserAuth) else entry.write_auth
        scopes = list(auth.scopes) if isinstance(auth, OAuth2UserAuth) else []
        async with self._lock:
            existing = self.pending_for(entry.id, caller.principal)
            if existing is not None:
                existing.waiting += 1
                request, is_new = existing, False
            else:
                request = ConsentRequest(
                    api=entry.id,
                    api_name=entry.name,
                    host=entry.host,
                    principal=caller.principal,
                    agent=caller.agent,
                    scopes=scopes,
                    waiting=1,
                    expires_at=datetime.now(UTC) + CONSENT_LIFETIME,
                )
                self._requests[request.id] = request
                self._events.setdefault(request.id, asyncio.Event())
                is_new = True
            await self._save(request)
        if is_new:
            await self.audit.record(
                AuditEvent(
                    event="consent_request",
                    caller=caller.agent,
                    principal=caller.principal,
                    api=entry.id,
                    detail=reason,
                )
            )
            await self.notifier.consent_opened(request, self.link(request))
        return request

    async def wait(self, request_id: str, max_wait: float | None = None) -> ConsentRequest:
        request = self._requests[request_id]
        event = self._events[request_id]
        remaining = (request.expires_at - datetime.now(UTC)).total_seconds()
        limit = remaining if max_wait is None else min(max_wait, remaining)
        deadline = asyncio.get_running_loop().time() + max(limit, 0)
        try:
            while request.state is ConsentState.PENDING:
                left = deadline - asyncio.get_running_loop().time()
                if left <= 0:
                    if datetime.now(UTC) >= request.expires_at:
                        await self._close(request, ConsentState.EXPIRED, "nobody signed in")
                    break
                try:
                    await asyncio.wait_for(event.wait(), timeout=min(left, POLL_SECONDS))
                except TimeoutError:
                    await self.load(request_id)  # another replica may have finished it
        finally:
            if request.waiting > 0:
                request.waiting -= 1
        return request

    async def _close(self, request: ConsentRequest, state: ConsentState, note: str) -> None:
        request.state = state
        request.note = note
        await self._save(request)
        self._events.setdefault(request.id, asyncio.Event()).set()
        await self.audit.record(
            AuditEvent(
                event="consent_resolved",
                principal=request.principal,
                api=request.api,
                detail=f"{state}: {note}",
            )
        )
        await self.notifier.consent_closed(request)

    async def complete(self, request: ConsentRequest, entry: ApiEntry, code: str) -> None:
        """The provider sent the person back with a code: trade it for tokens, store them."""
        auth = entry.auth if isinstance(entry.auth, OAuth2UserAuth) else entry.write_auth
        if not isinstance(auth, OAuth2UserAuth):
            raise OAuthError(f"API {entry.id!r} does not use per-person sign-in")
        async with self._client_factory() as client:
            tokens = await exchange_code(
                auth,
                self.secrets,
                client,
                code=code,
                redirect_uri=self.redirect_uri,
                verifier=request.code_verifier,
            )
        await self.secrets.put(principal_key(entry.id, request.principal), tokens.model_dump_json())
        await self._record_connection(entry.id, request.principal, tokens.scopes)
        await self._close(request, ConsentState.CONNECTED, "signed in")

    async def deny(self, request: ConsentRequest, note: str = "the person declined") -> None:
        await self._close(request, ConsentState.DENIED, note)

    # -- tokens for calls --------------------------------------------------------

    async def tokens_for(
        self, entry: ApiEntry, auth: OAuth2UserAuth, principal: str
    ) -> UserTokens | None:
        """This person's current access token for the API, refreshed if about to expire;
        None when they have not signed in (or their sign-in no longer works)."""
        key = principal_key(entry.id, principal)
        raw = await self.secrets.get(key)
        if raw is None:
            return None
        try:
            tokens = UserTokens.model_validate_json(raw)
        except ValueError:
            return None
        if not tokens.expires_soon():
            return tokens
        lock = self._refreshing.setdefault(key, asyncio.Lock())
        async with lock:
            raw = await self.secrets.get(key)  # another task or replica may have refreshed
            current = UserTokens.model_validate_json(raw) if raw else tokens
            if not current.expires_soon():
                return current
            try:
                async with self._client_factory() as client:
                    fresh = await refresh_tokens(auth, self.secrets, client, current)
            except OAuthError as exc:
                log.warning("refreshing %s's sign-in to %s failed: %s", principal, entry.id, exc)
                await self.forget_tokens(entry.id, principal)
                return None
            await self.secrets.put(key, fresh.model_dump_json())
            return fresh

    async def refresh_now(
        self, entry: ApiEntry, auth: OAuth2UserAuth, principal: str
    ) -> str | None:
        """Renew the person's token regardless of expiry; None when it worked, else why
        not (the sign-in is dropped then, so the person is asked again)."""
        key = principal_key(entry.id, principal)
        raw = await self.secrets.get(key)
        if not raw:
            return "not signed in"
        try:
            tokens = UserTokens.model_validate_json(raw)
            async with self._client_factory() as client:
                fresh = await refresh_tokens(auth, self.secrets, client, tokens)
        except (OAuthError, ValueError) as exc:
            await self.forget_tokens(entry.id, principal, auth)
            return str(exc)
        await self.secrets.put(key, fresh.model_dump_json())
        return None

    async def forget_tokens(
        self, api_id: str, principal: str, auth: OAuth2UserAuth | None = None
    ) -> None:
        """Overwrite the person's tokens with nothing usable (stores have no delete), and
        revoke them at the provider when the entry says where."""
        key = principal_key(api_id, principal)
        if auth is not None and auth.revocation_url:
            raw = await self.secrets.get(key)
            if raw:
                try:
                    tokens = UserTokens.model_validate_json(raw)
                    async with self._client_factory() as client:
                        for token in (tokens.refresh_token, tokens.access_token):
                            if token:
                                await client.post(
                                    auth.revocation_url,
                                    data={"token": token, "client_id": auth.client_id},
                                    timeout=10,
                                )
                except (httpx.HTTPError, ValueError) as exc:  # best effort
                    log.warning("revoking %s's token at %s failed: %s", principal, api_id, exc)
        await self.secrets.put(key, "")
        if self.store is not None:
            doc_id = principal_key(api_id, principal).replace("/", ":")[:64]
            data = await self.store.get_doc("connection", doc_id)
            if data is not None:
                await self.store.save_doc("connection", doc_id, "disconnected", data)

    async def _record_connection(self, api_id: str, principal: str, scopes: list[str]) -> None:
        if self.store is None:
            return
        connection = Connection(api=api_id, principal=principal, scopes=scopes)
        doc_id = principal_key(api_id, principal).replace("/", ":")[:64]
        await self.store.save_doc("connection", doc_id, "active", connection.model_dump_json())

    async def connections(self, api_id: str | None = None) -> list[Connection]:
        """Who is signed in to what (needs a database; None = every API)."""
        if self.store is None:
            return []
        found: list[Connection] = []
        for data in await self.store.list_docs("connection", "active"):
            connection = Connection.model_validate_json(data)
            if api_id is None or connection.api == api_id:
                found.append(connection)
        return sorted(found, key=lambda c: (c.api, c.principal))

    async def connections_of(self, principal: str) -> list[Connection]:
        """A person's own sign-ins (needs a database)."""
        who = principal.strip().lower()
        return [c for c in await self.connections() if c.principal.lower() == who]
