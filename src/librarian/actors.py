"""Callers on Agent Substrate: agents that prove who they are with a signed token.

On Agent Substrate (and kagent, its distribution) an agent session runs as an *actor*
in a sandbox that holds no credentials. Everything it sends out passes an egress
gateway, which can put an **actor JWT** into the request: a short-lived token, signed
by the cluster's Substrate issuer, saying which actor is calling. librarian accepts
that token in place of a per-agent key:

1. the token is verified against the issuer's published keys, for librarian's
   audience, and must name an actor (not a worker acting for one);
2. an actor is one *session*, so with kagent the token is exchanged for the Agent
   behind it (namespace, name, labels): the thing grants, rate limits and the audit
   trail should be about;
3. the first matching rule in `substrate.rules` says what that agent may do.

The token is a bearer token with no revocation (it lives up to an hour): it is never
logged, stored or echoed back, and the lookup result is cached only briefly so a
deleted agent stops working within a minute.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any
from urllib.parse import unquote

import httpx

from librarian.calls import Caller
from librarian.config import ActorRule, SubstrateSettings
from librarian.principals import Issuer, KeysUnavailable, PrincipalError, PrincipalVerifier

log = logging.getLogger(__name__)

ACTOR_CLAIM = "actor.ate.dev"
KAGENT_AUDIENCE = "kagent-agent-info"
LOOKUP_PATH = "/kagent.api.v1.AgentIdentityService/ResolveAgentIdentity"
MAX_CACHED = 10_000
# gRPC statuses with which kagent says "not a current agent session": not found (the
# Agent was deleted or recreated), permission denied (an obsolete session, or an actor
# that is not an agent at all) and unauthenticated (the token itself is not accepted).
# Anything else that is not OK is kagent's trouble, not the caller's.
KAGENT_SAYS_NO = frozenset({5, 7, 16})


class ActorError(Exception):
    """The caller could not be identified. The message is safe to return to it."""

    status = 401


class ActorForbidden(ActorError):
    """Identified, but no rule lets this agent use librarian."""

    status = 403


class ActorUnavailable(ActorError):
    """The identity could not be checked right now; the caller may retry."""

    status = 503


@dataclass(frozen=True)
class AgentRef:
    """The kagent Agent an actor runs."""

    namespace: str
    name: str
    uid: str = ""
    labels: dict[str, str] = field(default_factory=dict[str, str])


@dataclass(frozen=True)
class Actor:
    """A verified Substrate actor: one agent session."""

    atespace: str
    name: str
    uid: str
    agent: AgentRef | None = None  # the kagent Agent behind it, when looked up
    session: str | None = None

    @property
    def subject(self) -> str:
        return f"actor/{self.atespace}/{self.name}"

    @property
    def caller_name(self) -> str:
        """What librarian calls this agent: the Agent when known (it outlives its
        sessions), else the actor."""
        if self.agent is not None:
            return f"{self.agent.namespace}/{self.agent.name}"
        return f"{self.atespace}/{self.name}"

    @property
    def via(self) -> str:
        text = f"{self.subject} uid={self.uid}"
        return (text + (f" session={self.session}" if self.session else ""))[:300]


class KagentClient:
    """Asks kagent which Agent is behind an actor JWT.

    One unary call over gRPC-Web, which is plain HTTP/1.1: the request message is
    empty and the token in `Authorization` is the only input, so kagent answers for
    the token's own actor and nothing else. The answer is small and its field
    numbers are part of kagent's public `kagent.api.v1` contract, so it is decoded
    here rather than through generated code.
    """

    def __init__(
        self, url: str, client: httpx.AsyncClient | None = None, cache_seconds: float = 60
    ) -> None:
        self.url = url.rstrip("/") + LOOKUP_PATH
        self.client = client or httpx.AsyncClient(timeout=10)
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, AgentRef, str | None]] = {}

    async def resolve(self, token: str, actor_uid: str) -> tuple[AgentRef, str | None]:
        """(the Agent, its session id). Cached per actor for a short while."""
        now = time.monotonic()
        cached = self._cache.get(actor_uid)
        if cached is not None and cached[0] > now:
            return cached[1], cached[2]
        try:
            response = await self.client.post(
                self.url,
                content=b"\x00\x00\x00\x00\x00",  # one frame holding an empty message
                headers={
                    "content-type": "application/grpc-web+proto",
                    "accept": "application/grpc-web+proto",
                    "x-grpc-web": "1",
                    "authorization": f"Bearer {token}",
                },
            )
        except httpx.HTTPError as exc:
            log.error("kagent agent lookup failed: %s", type(exc).__name__)
            raise ActorUnavailable("cannot look up the agent behind this actor right now") from exc
        message, status, detail = _grpc_web_reply(response)
        if status in KAGENT_SAYS_NO:
            raise ActorError(
                "kagent does not recognise this actor as a current agent session"
                + (f" ({detail[:200]})" if detail else "")
            )
        if status != 0:
            log.error("kagent agent lookup: grpc-status %s (%s)", status, detail[:200])
            raise ActorUnavailable("cannot look up the agent behind this actor right now")
        agent, session = _agent_identity(message)
        if not agent.namespace or not agent.name:
            raise ActorUnavailable("kagent returned no agent for this actor")
        if len(self._cache) >= MAX_CACHED:
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
        self._cache[actor_uid] = (now + self.cache_seconds, agent, session)
        return agent, session


class ActorAuthenticator:
    """Turns an actor JWT into a librarian caller, or says why not."""

    def __init__(
        self,
        settings: SubstrateSettings,
        *,
        client: httpx.AsyncClient | None = None,
        kagent: KagentClient | None = None,
    ) -> None:
        if settings.issuer is None:
            raise ValueError("substrate.issuer is not set")
        self.settings = settings
        self.verifier = PrincipalVerifier(
            [
                Issuer(
                    issuer=settings.issuer,
                    audience=settings.audience,
                    jwks_url=settings.jwks_url,
                    claim="sub",
                    algorithms=tuple(settings.algorithms),
                    leeway_seconds=settings.leeway_seconds,
                    ca_file=settings.ca_file,
                )
            ],
            client=client,
            noun="actor token",
        )
        self.kagent = kagent
        if kagent is None and settings.kagent_url:
            self.kagent = KagentClient(
                settings.kagent_url, client, cache_seconds=settings.kagent_cache_seconds
            )
        self._seen: set[str] = set()
        self._refusals: dict[str, float] = {}

    def refused(self, error: ActorError) -> None:
        """Say in the log why a caller was turned away: the platform's own log only
        shows the status (kagent reports "Forbidden" and drops the reason), so this
        is where an operator finds it. Each distinct reason at most once a minute."""
        reason = str(error)
        now = time.monotonic()
        if now - self._refusals.get(reason, -60.0) < 60:
            return
        if len(self._refusals) >= 1000:
            self._refusals.clear()
        self._refusals[reason] = now
        log.warning("refused a Substrate caller (HTTP %d): %s", error.status, reason)

    async def actor(self, token: str) -> Actor:
        """The verified actor behind a token (and, with kagent, its Agent)."""
        try:
            _, claims = await self.verifier.claims(token)
        except KeysUnavailable as exc:
            raise ActorUnavailable(str(exc)) from exc
        except PrincipalError as exc:
            raise ActorError(str(exc)) from exc
        subject = str(claims.get("sub", ""))
        identity: Any = claims.get(ACTOR_CLAIM)
        if not subject.startswith("actor/") or not isinstance(identity, dict):
            # The same keys sign `worker-for-actor/...` tokens, which a worker uses
            # when acting for an actor: only the actor itself is a caller.
            raise ActorError("actor token does not identify an actor")
        atespace, name, uid = (
            str(identity.get(key) or "") for key in ("atespace", "actorName", "actorUID")
        )
        if not (atespace and name and uid) or subject != f"actor/{atespace}/{name}":
            raise ActorError("actor token does not identify an actor")
        actor = Actor(atespace=atespace, name=name, uid=uid)
        if self.kagent is None:
            return actor
        audiences: Any = claims.get("aud")
        listed = audiences if isinstance(audiences, list) else [audiences]
        if KAGENT_AUDIENCE not in listed:
            raise ActorError(
                f"actor token cannot be used to look up its agent: add {KAGENT_AUDIENCE!r} to "
                "the audiences (RemoteMCPServer spec.actorJwt.audiences)"
            )
        agent, session = await self.kagent.resolve(token, uid)
        return Actor(atespace=atespace, name=name, uid=uid, agent=agent, session=session)

    def rule_for(self, actor: Actor) -> ActorRule | None:
        return next((rule for rule in self.settings.rules if _matches(rule, actor)), None)

    async def caller(self, token: str) -> Caller:
        actor = await self.actor(token)
        rule = self.rule_for(actor)
        if rule is None:
            raise ActorForbidden(
                f"agent {actor.caller_name!r} is not allowed to use librarian: no rule under "
                "substrate.rules matches it"
            )
        name = rule.agent or actor.caller_name
        if actor.uid not in self._seen:
            if len(self._seen) >= MAX_CACHED:
                self._seen.clear()
            self._seen.add(actor.uid)
            log.info("substrate %s is calling as agent %r", actor.via, name)
        return Caller(
            agent=name,
            authenticated=True,
            apis=tuple(rule.apis),
            can_onboard=rule.can_onboard,
            write_apis=tuple(rule.write_apis),
            owner=rule.owner,
            approval_wait_seconds=rule.approval_wait_seconds,
            via=actor.via,
            limits=dict(rule.limits),
        )


def _matches(rule: ActorRule, actor: Actor) -> bool:
    """Every field the rule gives must match; an empty match is every actor."""
    match = rule.match
    if match.atespace is not None and not fnmatchcase(actor.atespace, match.atespace):
        return False
    if match.actor is not None and not fnmatchcase(actor.name, match.actor):
        return False
    if match.namespace is None and match.name is None and not match.labels:
        return True
    agent = actor.agent
    if agent is None:
        return False  # the rule is about the kagent Agent, and there is none
    if match.namespace is not None and not fnmatchcase(agent.namespace, match.namespace):
        return False
    if match.name is not None and not fnmatchcase(agent.name, match.name):
        return False
    return all(agent.labels.get(key) == value for key, value in match.labels.items())


# -- gRPC-Web and protobuf, just enough for one small answer ---------------------------


def _grpc_web_reply(response: httpx.Response) -> tuple[bytes, int, str]:
    """(message bytes, grpc status, grpc message) of a unary gRPC-Web response."""
    status = response.headers.get("grpc-status")
    detail = unquote(response.headers.get("grpc-message", ""))
    message = b""
    body = response.content
    position = 0
    while position + 5 <= len(body):
        flags = body[position]
        size = int.from_bytes(body[position + 1 : position + 5], "big")
        payload = body[position + 5 : position + 5 + size]
        position += 5 + size
        if flags & 0x80:  # the trailers, moved into the body
            for line in payload.decode("utf-8", "replace").split("\r\n"):
                key, _, value = line.partition(":")
                if key.strip().lower() == "grpc-status":
                    status = value.strip()
                elif key.strip().lower() == "grpc-message":
                    detail = unquote(value.strip())
        else:
            message = payload
    if status is None:
        # Not a gRPC answer at all (a proxy's error page, a wrong URL): unknown.
        return b"", 2, f"HTTP {response.status_code}, not a gRPC answer"
    try:
        return message, int(status), detail
    except ValueError:
        return b"", 2, detail


def _fields(data: bytes) -> list[tuple[int, bytes]]:
    """The length-delimited fields of a protobuf message, as (field number, bytes).
    Other wire types are skipped: this answer has only strings, maps and messages."""
    found: list[tuple[int, bytes]] = []
    position = 0

    def varint() -> int:
        nonlocal position
        value = shift = 0
        while True:
            if position >= len(data) or shift > 63:
                raise ValueError("truncated protobuf")
            byte = data[position]
            position += 1
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return value
            shift += 7

    while position < len(data):
        key = varint()
        number, wire = key >> 3, key & 7
        if wire == 0:
            varint()
        elif wire == 1:
            position += 8
        elif wire == 5:
            position += 4
        elif wire == 2:
            size = varint()
            if position + size > len(data):
                raise ValueError("truncated protobuf")
            found.append((number, data[position : position + size]))
            position += size
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
    return found


def _agent_identity(message: bytes) -> tuple[AgentRef, str | None]:
    """Decode `kagent.api.v1.ResolveAgentIdentityResponse`:
    agent = 1 {namespace = 1, name = 2, uid = 3, labels = 4 (map)}, session_id = 2."""
    try:
        namespace = name = uid = ""
        labels: dict[str, str] = {}
        session: str | None = None
        for number, value in _fields(message):
            if number == 2:
                session = value.decode("utf-8")[:200] or None
            elif number == 1:
                for inner, item in _fields(value):
                    if inner == 1:
                        namespace = item.decode("utf-8")
                    elif inner == 2:
                        name = item.decode("utf-8")
                    elif inner == 3:
                        uid = item.decode("utf-8")
                    elif inner == 4:
                        entry = dict(_fields(item))
                        labels[entry.get(1, b"").decode("utf-8")] = entry.get(2, b"").decode(
                            "utf-8"
                        )
        return AgentRef(namespace=namespace, name=name, uid=uid, labels=labels), session
    except (ValueError, UnicodeDecodeError) as exc:
        raise ActorUnavailable("kagent returned an answer librarian could not read") from exc
