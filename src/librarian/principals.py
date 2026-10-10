"""Verified principals: who an agent acts for, proven by a signed token.

A calling agent passes a JWT from the organisation's identity provider (OIDC) in
the `X-Principal-Token` header. librarian verifies the signature against the
issuer's published keys (JWKS), the issuer, the audience (must be librarian, so
tokens minted for other apps are refused) and the validity window. Only
asymmetric algorithms are accepted.

The token travels in a header rather than in A2A message metadata because
metadata is stored with the task, which would leave tokens in the database.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, cast

import httpx
import jwt
from jwt import PyJWK

log = logging.getLogger(__name__)

ALLOWED_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}
)
JWKS_TTL_SECONDS = 3600
MIN_REFRESH_SECONDS = 30  # rate limit for refreshes triggered by unknown key ids


class PrincipalError(Exception):
    """The token could not be verified. The message is safe to return to the caller."""


@dataclass(frozen=True)
class Issuer:
    issuer: str
    audience: str
    jwks_url: str | None = None  # default: discovered from the issuer
    claim: str = "email"
    algorithms: tuple[str, ...] = ("RS256", "ES256")
    leeway_seconds: int = 30


@dataclass
class _KeySet:
    keys: dict[str, PyJWK]
    fetched_at: float


class PrincipalVerifier:
    def __init__(self, issuers: list[Issuer], client: httpx.AsyncClient | None = None) -> None:
        for issuer in issuers:
            bad = set(issuer.algorithms) - ALLOWED_ALGORITHMS
            if bad:
                raise ValueError(f"{issuer.issuer}: algorithms not allowed: {sorted(bad)}")
        self.issuers = {i.issuer.rstrip("/"): i for i in issuers}
        self.client = client or httpx.AsyncClient(timeout=10)
        self._keys: dict[str, _KeySet] = {}
        self._last_refresh: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def verify(self, token: str) -> str:
        """Return the verified principal (the configured claim, e.g. email)."""
        try:
            header = jwt.get_unverified_header(token)
            unverified: dict[str, Any] = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError as exc:
            raise PrincipalError("malformed principal token") from exc
        issuer = self.issuers.get(str(unverified.get("iss", "")).rstrip("/"))
        if issuer is None:
            raise PrincipalError("principal token is from an untrusted issuer")
        algorithm = str(header.get("alg", ""))
        if algorithm not in issuer.algorithms:
            raise PrincipalError(f"principal token algorithm {algorithm!r} is not accepted")
        key = await self._key(issuer, str(header.get("kid", "")))
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key.key,
                algorithms=[algorithm],
                audience=issuer.audience,
                issuer=unverified["iss"],
                leeway=issuer.leeway_seconds,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise PrincipalError("principal token has expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise PrincipalError("principal token was not issued for librarian") from exc
        except jwt.PyJWTError as exc:
            raise PrincipalError(f"principal token is invalid: {exc}") from exc
        subject = claims.get(issuer.claim)
        if not isinstance(subject, str) or not subject:
            raise PrincipalError(f"principal token has no {issuer.claim!r} claim")
        return subject[:200]

    async def _key(self, issuer: Issuer, kid: str) -> PyJWK:
        keyset = self._keys.get(issuer.issuer)
        fresh = keyset is not None and time.monotonic() - keyset.fetched_at < JWKS_TTL_SECONDS
        if keyset is not None and fresh and kid in keyset.keys:
            return keyset.keys[kid]
        # Unknown key id (rotation) or stale cache: refresh, at most every MIN_REFRESH_SECONDS.
        async with self._lock:
            last = self._last_refresh.get(issuer.issuer, 0.0)
            if keyset is None or not fresh or time.monotonic() - last >= MIN_REFRESH_SECONDS:
                keyset = await self._fetch(issuer)
                self._keys[issuer.issuer] = keyset
                self._last_refresh[issuer.issuer] = time.monotonic()
        if kid not in keyset.keys:
            raise PrincipalError("principal token is signed with an unknown key")
        return keyset.keys[kid]

    async def _fetch(self, issuer: Issuer) -> _KeySet:
        try:
            url = issuer.jwks_url
            if url is None:
                discovery = await self.client.get(
                    issuer.issuer.rstrip("/") + "/.well-known/openid-configuration"
                )
                discovery.raise_for_status()
                url = str(discovery.json()["jwks_uri"])
            response = await self.client.get(url)
            response.raise_for_status()
            document: Any = response.json()
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            log.error("cannot fetch signing keys for %s: %s", issuer.issuer, exc)
            raise PrincipalError("cannot verify principal tokens right now") from exc
        keys: dict[str, PyJWK] = {}
        for item in cast(list[Any], document.get("keys", [])) if isinstance(document, dict) else []:
            if not isinstance(item, dict) or "kid" not in item:
                continue
            try:
                keys[str(item["kid"])] = PyJWK(cast(dict[str, Any], item))
            except (jwt.PyJWTError, ValueError, TypeError):
                log.warning(
                    "skipping unusable key %s from %s", json.dumps(item)[:80], issuer.issuer
                )
        return _KeySet(keys, time.monotonic())
