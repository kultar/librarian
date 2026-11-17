"""Agents on Agent Substrate: identified by the actor JWT the egress gateway injects,
resolved to their kagent Agent, and given what a rule in the config allows."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

import httpx
import jwt
import pytest
from a2a.helpers.proto_helpers import new_data_part
from a2a.types import TaskState
from cryptography.hazmat.primitives.asymmetric import ec
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from jwt.algorithms import ECAlgorithm
from pydantic import ValidationError

from librarian.actors import (
    ActorAuthenticator,
    ActorError,
    ActorForbidden,
    ActorUnavailable,
    KagentClient,
)
from librarian.approvals import WriteApproval
from librarian.audit import MemoryAuditSink
from librarian.calls import WriteRequest
from librarian.config import ActorMatch, ActorRule, SubstrateSettings
from librarian.secrets import FileSecretStore
from librarian.server import create_app
from librarian.service import Librarian
from tests.conftest import BASE, Router
from tests.test_server import KEYS, send

ISSUER = "https://idp.ate-system.svc"
KAGENT = "http://kagent-controller.kagent.svc:8083"
FIELD = descriptor_pb2.FieldDescriptorProto


def _identity_message() -> Any:
    """kagent's `ResolveAgentIdentityResponse`, built with the protobuf library from
    the published field numbers, so librarian's own small decoder is checked against
    an independent encoder."""
    file = descriptor_pb2.FileDescriptorProto(
        name="kagent/api/v1/agent_identity.proto", package="kagent.api.v1", syntax="proto3"
    )
    agent = file.message_type.add(name="AgentIdentity")
    for number, name in enumerate(("namespace", "name", "uid"), start=1):
        agent.field.add(
            name=name, number=number, type=FIELD.TYPE_STRING, label=FIELD.LABEL_OPTIONAL
        )
    for number, name in ((4, "labels"), (5, "annotations")):
        entry = agent.nested_type.add(name=f"{name.capitalize()}Entry")
        entry.options.map_entry = True
        entry.field.add(name="key", number=1, type=FIELD.TYPE_STRING, label=FIELD.LABEL_OPTIONAL)
        entry.field.add(name="value", number=2, type=FIELD.TYPE_STRING, label=FIELD.LABEL_OPTIONAL)
        agent.field.add(
            name=name,
            number=number,
            type=FIELD.TYPE_MESSAGE,
            label=FIELD.LABEL_REPEATED,
            type_name=f".kagent.api.v1.AgentIdentity.{name.capitalize()}Entry",
        )
    reply = file.message_type.add(name="ResolveAgentIdentityResponse")
    reply.field.add(
        name="agent",
        number=1,
        type=FIELD.TYPE_MESSAGE,
        label=FIELD.LABEL_OPTIONAL,
        type_name=".kagent.api.v1.AgentIdentity",
    )
    reply.field.add(name="session_id", number=2, type=FIELD.TYPE_STRING, label=FIELD.LABEL_OPTIONAL)
    reply.field.add(
        name="runtime_revision", number=3, type=FIELD.TYPE_STRING, label=FIELD.LABEL_OPTIONAL
    )
    # A field a later kagent might add: a decoder must step over what it doesn't know.
    reply.field.add(name="generation", number=9, type=FIELD.TYPE_INT64, label=FIELD.LABEL_OPTIONAL)
    pool = descriptor_pool.DescriptorPool()
    pool.Add(file)
    return message_factory.GetMessageClass(
        pool.FindMessageTypeByName("kagent.api.v1.ResolveAgentIdentityResponse")
    )


IdentityReply: Any = _identity_message()


class FakeSubstrate:
    """The cluster's actor JWT issuer, and kagent's agent lookup."""

    def __init__(self) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.other_key = ec.generate_private_key(ec.SECP256R1())
        # actor uid -> the Agent kagent says is behind it
        self.agents: dict[str, dict[str, Any]] = {
            "uid-1": {
                "namespace": "payments",
                "name": "release-notes",
                "uid": "agent-uid-1",
                "labels": {"team": "payments", "tier": "prod"},
                "session": "session-1",
            }
        }
        self.lookups: list[str] = []
        self.key_fetches = 0
        self.down = False
        self.lookup_error: tuple[int, str] | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused")
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(
                200, json={"issuer": ISSUER, "jwks_uri": f"{ISSUER}/openid/v1/jwks"}
            )
        if path == "/openid/v1/jwks":
            self.key_fetches += 1
            jwk: dict[str, Any] = ECAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
            jwk.update(kid="k1", alg="ES256", use="sig")
            return httpx.Response(200, json={"keys": [jwk]})
        if path == "/kagent.api.v1.AgentIdentityService/ResolveAgentIdentity":
            return self._lookup(request)
        return httpx.Response(404)

    def _lookup(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["content-type"] == "application/grpc-web+proto"
        assert request.content == b"\x00\x00\x00\x00\x00"  # an empty request message
        token = request.headers["authorization"].removeprefix("Bearer ")
        claims: dict[str, Any] = jwt.decode(token, options={"verify_signature": False})
        uid = claims["actor.ate.dev"]["actorUID"]
        self.lookups.append(uid)
        headers = {"content-type": "application/grpc-web+proto"}
        if self.lookup_error is not None:
            status, message = self.lookup_error
            # An error with no message: gRPC sends only the trailers, as headers.
            headers |= {"grpc-status": str(status), "grpc-message": quote(message)}
            return httpx.Response(200, headers=headers)
        found = self.agents.get(uid)
        if found is None:
            headers |= {"grpc-status": "5", "grpc-message": quote("agent identity not found")}
            return httpx.Response(200, headers=headers)
        reply = IdentityReply(session_id=found["session"], runtime_revision="rev", generation=7)
        reply.agent.namespace, reply.agent.name = found["namespace"], found["name"]
        reply.agent.uid = found["uid"]
        reply.agent.labels.update(found["labels"])
        reply.agent.annotations["description"] = "not used by librarian"
        message_bytes: bytes = reply.SerializeToString()
        trailers = b"grpc-status: 0\r\ngrpc-message: \r\n"
        body = (
            b"\x00"
            + len(message_bytes).to_bytes(4, "big")
            + message_bytes
            + b"\x80"
            + len(trailers).to_bytes(4, "big")
            + trailers
        )
        return httpx.Response(200, headers=headers, content=body)

    def token(
        self,
        actor: str = "sess-1",
        uid: str = "uid-1",
        atespace: str = "kagent",
        *,
        signed_by: Any = None,
        **claims: Any,
    ) -> str:
        now = int(time.time())
        payload: dict[str, Any] = {
            "iss": ISSUER,
            "sub": f"actor/{atespace}/{actor}",
            "aud": ["librarian", "kagent-agent-info"],
            "iat": now,
            "nbf": now - 300,
            "exp": now + 900,
            "jti": "MZ4RHTOXLJ6GSVNQ2YAKE7WB3C",
            "actor.ate.dev": {"atespace": atespace, "actorName": actor, "actorUID": uid},
            **claims,
        }
        return jwt.encode(payload, signed_by or self.key, algorithm="ES256", headers={"kid": "k1"})


@pytest.fixture
def substrate() -> FakeSubstrate:
    return FakeSubstrate()


def rules(*items: ActorRule) -> list[ActorRule]:
    return list(items) or [ActorRule()]


def authenticator(
    fake: FakeSubstrate,
    *,
    kagent: bool = True,
    cache_seconds: float = 60,
    rules: list[ActorRule] | None = None,
) -> ActorAuthenticator:
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    config = SubstrateSettings(
        issuer=ISSUER,
        kagent_url=KAGENT if kagent else None,
        kagent_cache_seconds=cache_seconds,
        rules=rules if rules is not None else [ActorRule()],
    )
    return ActorAuthenticator(config, client=client)


# -- who is calling -------------------------------------------------------------------


async def test_an_actor_token_makes_the_agent_a_caller(substrate: FakeSubstrate) -> None:
    auth = authenticator(
        substrate,
        rules=rules(
            ActorRule(
                match=ActorMatch(namespace="payments"),
                apis=["tracker", "github-*"],
                write_apis=["tracker"],
                owner="payments-leads@x.test",
                can_onboard=False,
            )
        ),
    )
    caller = await auth.caller(substrate.token())
    # The caller is the Agent, which outlives its sessions: grants, rate limits and the
    # audit trail are about it.
    assert caller.agent == "payments/release-notes" and caller.authenticated
    assert caller.apis == ("tracker", "github-*") and caller.write_apis == ("tracker",)
    assert not caller.can_onboard and caller.principal is None
    assert caller.may_use("github-enterprise") and not caller.may_use("weather")
    # No person is known through kagent: the rule's owner approves the agent's writes.
    assert caller.requesters() == {"payments-leads@x.test"}
    # The session is on record with every call, without being part of the identity.
    assert caller.via == "actor/kagent/sess-1 uid=uid-1 session=session-1"


async def test_two_sessions_of_one_agent_are_the_same_caller(substrate: FakeSubstrate) -> None:
    substrate.agents["uid-2"] = {**substrate.agents["uid-1"], "session": "session-2"}
    auth = authenticator(substrate)
    first = await auth.caller(substrate.token())
    second = await auth.caller(substrate.token(actor="sess-2", uid="uid-2"))
    assert first.agent == second.agent == "payments/release-notes"
    assert first.via != second.via and "session-2" in (second.via or "")
    # kagent is asked once per actor, not once per call.
    for _ in range(3):
        await auth.caller(substrate.token())
    assert substrate.lookups == ["uid-1", "uid-2"]
    assert substrate.key_fetches == 1


async def test_without_kagent_the_actor_itself_is_the_caller(substrate: FakeSubstrate) -> None:
    auth = authenticator(
        substrate,
        kagent=False,
        rules=rules(
            # A team's sessions as one caller, whatever each session is called.
            ActorRule(match=ActorMatch(atespace="team-a"), agent="team-a", write_apis=["tracker"]),
            ActorRule(match=ActorMatch(atespace="team-b", actor="report-*")),
        ),
    )
    token = substrate.token(actor="nightly-1", atespace="team-a", aud=["librarian"])
    grouped = await auth.caller(token)
    assert grouped.agent == "team-a" and grouped.write_apis == ("tracker",)
    assert grouped.via == "actor/team-a/nightly-1 uid=uid-1"
    plain = await auth.caller(substrate.token(actor="report-7", atespace="team-b"))
    assert plain.agent == "team-b/report-7" and plain.write_apis == ()
    assert substrate.lookups == []  # nobody to ask
    with pytest.raises(ActorForbidden, match=r"no rule under substrate\.rules matches"):
        await auth.caller(substrate.token(actor="other", atespace="team-b"))


# -- what is refused --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("claims", "problem"),
    [
        # A token minted for another service must not work here: whoever receives an
        # actor JWT could otherwise replay it against librarian.
        ({"aud": ["https://api.example.com", "kagent-agent-info"]}, "not issued for librarian"),
        ({"iss": "https://idp.evil.example"}, "untrusted issuer"),
        ({"exp": int(time.time()) - 3600}, "has expired"),
        # The same keys sign tokens for a worker acting for an actor.
        ({"sub": "worker-for-actor/kagent/sess-1"}, "does not identify an actor"),
        ({"actor.ate.dev": None}, "does not identify an actor"),
        ({"sub": "actor/kagent/someone-else"}, "does not identify an actor"),
        (
            {"actor.ate.dev": {"atespace": "kagent", "actorName": "sess-1"}},
            "does not identify an actor",
        ),
    ],
)
async def test_only_a_valid_token_for_librarian_that_names_an_actor_is_accepted(
    substrate: FakeSubstrate, claims: dict[str, Any], problem: str
) -> None:
    auth = authenticator(substrate)
    with pytest.raises(ActorError, match=problem) as refused:
        await auth.caller(substrate.token(**claims))
    assert refused.value.status == 401
    assert substrate.lookups == []  # kagent is never asked about a token that failed


async def test_a_token_signed_by_someone_else_or_not_at_all_is_refused(
    substrate: FakeSubstrate,
) -> None:
    auth = authenticator(substrate)
    forged = substrate.token(signed_by=substrate.other_key)
    with pytest.raises(ActorError, match="invalid"):
        await auth.caller(forged)
    header, payload, _ = substrate.token().split(".")
    with pytest.raises(ActorError):
        await auth.caller(f"{header}.{payload}.")
    claims: dict[str, Any] = jwt.decode(substrate.token(), options={"verify_signature": False})
    unsigned = jwt.encode(claims, "", algorithm="none")
    with pytest.raises(ActorError, match="not accepted"):
        await auth.caller(unsigned)
    assert substrate.lookups == []


async def test_an_agent_no_rule_matches_is_refused(substrate: FakeSubstrate) -> None:
    auth = authenticator(
        substrate,
        rules=rules(
            ActorRule(match=ActorMatch(namespace="payments", labels={"tier": "dev"})),
            ActorRule(match=ActorMatch(namespace="platform")),
        ),
    )
    with pytest.raises(ActorForbidden, match="'payments/release-notes' is not allowed") as no:
        await auth.caller(substrate.token())
    assert no.value.status == 403


async def test_rules_are_tried_in_order_and_labels_must_all_match(
    substrate: FakeSubstrate,
) -> None:
    auth = authenticator(
        substrate,
        rules=rules(
            ActorRule(match=ActorMatch(name="release-*", labels={"team": "payments", "x": "y"})),
            ActorRule(
                match=ActorMatch(namespace="pay*", labels={"tier": "prod"}), write_apis=["github"]
            ),
            ActorRule(apis=["weather"]),  # everyone else: one API, no writes
        ),
    )
    assert (await auth.caller(substrate.token())).write_apis == ("github",)
    substrate.agents["uid-9"] = {
        "namespace": "sandbox",
        "name": "tryout",
        "uid": "a9",
        "labels": {},
        "session": "s9",
    }
    other = await auth.caller(substrate.token(actor="sess-9", uid="uid-9"))
    assert other.agent == "sandbox/tryout" and other.apis == ("weather",)
    assert other.write_apis == ()


async def test_an_actor_kagent_no_longer_vouches_for_is_refused(
    substrate: FakeSubstrate,
) -> None:
    auth = authenticator(substrate, cache_seconds=0)
    assert (await auth.caller(substrate.token())).agent == "payments/release-notes"
    del substrate.agents["uid-1"]  # the agent was deleted, or the session is obsolete
    with pytest.raises(ActorError, match="does not recognise this actor") as gone:
        await auth.caller(substrate.token())
    assert gone.value.status == 401 and "agent identity not found" in str(gone.value)
    for status, said in ((16, "Invalid Agent identity credential"), (7, "not an Agent Session")):
        substrate.lookup_error = (status, said)
        with pytest.raises(ActorError, match=said) as refused:
            await auth.caller(substrate.token())
        assert refused.value.status == 401


async def test_the_token_must_be_usable_for_the_lookup(substrate: FakeSubstrate) -> None:
    auth = authenticator(substrate)
    with pytest.raises(ActorError, match="add 'kagent-agent-info' to the audiences"):
        await auth.caller(substrate.token(aud=["librarian"]))
    assert substrate.lookups == []


async def test_an_outage_is_a_retryable_error_not_a_denial(substrate: FakeSubstrate) -> None:
    auth = authenticator(substrate, cache_seconds=0)
    await auth.caller(substrate.token())  # the signing keys are cached from here on
    for status in (13, 14, 4, 2):  # internal, unavailable, deadline, unknown
        substrate.lookup_error = (status, "kagent's own trouble")
        with pytest.raises(ActorUnavailable) as busy:
            await auth.caller(substrate.token())
        assert busy.value.status == 503 and "kagent's own trouble" not in str(busy.value)
    substrate.lookup_error = None
    substrate.down = True
    with pytest.raises(ActorUnavailable, match="cannot look up the agent"):
        await auth.caller(substrate.token())
    # ...and with the issuer unreachable before any key was fetched
    cold = authenticator(substrate)
    with pytest.raises(ActorUnavailable, match="cannot verify actor tokens right now"):
        await cold.caller(substrate.token())


async def test_an_answer_that_is_not_grpc_is_not_trusted(substrate: FakeSubstrate) -> None:
    def proxy_page(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>welcome</html>")

    # A wrong URL or a proxy's page is librarian's setup, not the caller's fault: 503.
    client = KagentClient(KAGENT, httpx.AsyncClient(transport=httpx.MockTransport(proxy_page)))
    with pytest.raises(ActorUnavailable, match="cannot look up the agent"):
        await client.resolve(substrate.token(), "uid-1")

    def truncated(request: httpx.Request) -> httpx.Response:
        body = b"\x00\x00\x00\x00\x04\x0a\x7f\x01\x02" + b"\x80\x00\x00\x00\x10grpc-status: 0\r\n"
        return httpx.Response(200, content=body)

    client = KagentClient(KAGENT, httpx.AsyncClient(transport=httpx.MockTransport(truncated)))
    with pytest.raises(ActorUnavailable, match="could not read"):
        await client.resolve(substrate.token(), "uid-1")


# -- settings ---------------------------------------------------------------------------


def test_rules_that_cannot_work_are_refused_at_start() -> None:
    with pytest.raises(ValidationError, match=r"substrate\.kagent_url is not set"):
        SubstrateSettings(issuer=ISSUER, rules=[ActorRule(match=ActorMatch(namespace="a"))])
    with pytest.raises(ValidationError, match="must be an https URL"):
        SubstrateSettings(issuer="http://idp.ate-system.svc", rules=[ActorRule()])
    # A misspelt match would otherwise match every agent.
    with pytest.raises(ValidationError):
        ActorRule.model_validate({"match": {"namespaces": "payments"}, "write_apis": ["x"]})
    with pytest.raises(ValidationError):
        ActorRule.model_validate({"match": {}, "write_api": ["x"]})
    off = SubstrateSettings()
    assert not off.enabled and off.audience == "librarian"
    on = SubstrateSettings.model_validate(
        {
            "issuer": ISSUER,
            "kagent_url": KAGENT,
            "rules": [
                {"match": {"namespace": "payments"}, "write_apis": ["github"], "owner": "a@x.test"},
                {"match": {}},
            ],
        }
    )
    assert on.enabled and on.rules[1].apis == ["*"] and on.rules[1].write_apis == []


# -- through the server -------------------------------------------------------------------


WHOAMI = [new_data_part({"skill": "execute", "api": "tracker", "operation": "whoami"})]


async def served(
    librarian: Librarian,
    fake: FakeSubstrate,
    *rule: ActorRule,
    keys: bool = True,
    names_user_in: str = "",
) -> httpx.AsyncClient:
    librarian.settings.substrate = SubstrateSettings(
        issuer=ISSUER, kagent_url=KAGENT, rules=rules(*rule), claimed_user_header=names_user_in
    )
    app = create_app(librarian, caller_keys=KEYS if keys else {})
    if keys:
        app.state.caller_keys.update(KEYS)
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    app.state.actor_authenticator.verifier.client = client
    app.state.actor_authenticator.kagent.client = client
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=librarian.settings.public_url
    )


@pytest.fixture(autouse=True)
async def tracker_ok(secrets_store: FileSecretStore, router: Router) -> None:
    await secrets_store.put("tracker/readonly", "tok")
    router.add("GET", f"{BASE}/v1/me", lambda r: httpx.Response(200, json={"me": 1}))


async def test_an_actor_reads_through_librarian_and_the_audit_names_agent_and_session(
    librarian: Librarian, substrate: FakeSubstrate, audit: MemoryAuditSink
) -> None:
    http = await served(librarian, substrate)
    http.headers["Authorization"] = f"Bearer {substrate.token()}"
    outcome = await send(librarian, http, WHOAMI)
    assert outcome.final == TaskState.TASK_STATE_COMPLETED
    call = next(ev for ev in audit.events if ev.event == "call")
    assert call.caller == "payments/release-notes" and call.principal is None
    assert call.extra["via"] == "actor/kagent/sess-1 uid=uid-1 session=session-1"
    # the token itself is nowhere in what was recorded
    assert substrate.token().split(".")[2] not in str([ev.model_dump() for ev in audit.events])
    # An agent with its own key still works beside it, and is recorded as before.
    http.headers["Authorization"] = "Bearer key-a"
    assert (await send(librarian, http, WHOAMI)).final == TaskState.TASK_STATE_COMPLETED
    keyed = [ev for ev in audit.events if ev.event == "call"][-1]
    assert keyed.caller == "agent-a" and "via" not in keyed.extra


async def test_the_server_answers_401_403_and_503_with_the_reason(
    librarian: Librarian, substrate: FakeSubstrate, caplog: pytest.LogCaptureFixture
) -> None:
    http = await served(librarian, substrate, ActorRule(match=ActorMatch(namespace="platform")))
    mcp = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}

    async def ask(token: str) -> httpx.Response:
        return await http.post("/mcp", json=mcp, headers={"Authorization": f"Bearer {token}"})

    caplog.set_level("WARNING", logger="librarian.actors")
    forbidden = await ask(substrate.token())
    assert forbidden.status_code == 403
    assert "no rule under substrate.rules matches" in forbidden.json()["error"]
    # kagent shows the agent's author only "Forbidden": the reason is in librarian's log,
    # once, however often the agent retries, and without the token.
    await ask(substrate.token())
    said = [r.getMessage() for r in caplog.records if "refused a Substrate caller" in r.message]
    assert len(said) == 1 and "HTTP 403" in said[0] and "'payments/release-notes'" in said[0]
    assert substrate.token().split(".")[2] not in caplog.text
    wrong = await ask(substrate.token(aud=["someone-else"]))
    assert wrong.status_code == 401 and "not issued for librarian" in wrong.json()["error"]
    assert (await ask("not-a-key")).status_code == 401
    assert (await http.post("/mcp", json=mcp)).status_code == 401  # nothing at all
    substrate.agents.clear()
    substrate.lookup_error = (14, "unavailable")
    assert (await ask(substrate.token(actor="new", uid="uid-new"))).status_code == 503


async def test_with_substrate_on_and_no_keys_nobody_is_anonymous(
    librarian: Librarian, substrate: FakeSubstrate, audit: MemoryAuditSink
) -> None:
    http = await served(librarian, substrate, keys=False)
    assert (await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1})).status_code == 401
    http.headers["Authorization"] = f"Bearer {substrate.token()}"
    assert (await send(librarian, http, WHOAMI)).final == TaskState.TASK_STATE_COMPLETED


async def test_a_token_shaped_key_is_just_a_wrong_key_when_substrate_is_off(
    librarian: Librarian, substrate: FakeSubstrate
) -> None:
    app = create_app(librarian, caller_keys=KEYS)
    app.state.caller_keys.update(KEYS)
    assert app.state.actor_authenticator is None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=librarian.settings.public_url
    ) as http:
        refused = await http.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": f"Bearer {substrate.token()}"},
        )
    assert refused.status_code == 401 and refused.json() == {"error": "unauthorized"}


async def test_an_actors_write_waits_for_its_owner(
    librarian: Librarian, substrate: FakeSubstrate
) -> None:
    auth = authenticator(
        substrate,
        rules=rules(
            ActorRule(
                match=ActorMatch(namespace="payments"),
                write_apis=["tracker"],
                owner="payments-leads@x.test",
            )
        ),
    )
    caller = await auth.caller(substrate.token())
    comment = WriteRequest(
        api="tracker", operation="addComment", args={"key": "PAY-1"}, body={"text": "hi"}
    )
    approval = await librarian.request_write(comment, caller)
    assert isinstance(approval, WriteApproval)  # it waits for a person
    assert approval.agent == "payments/release-notes" and approval.principal is None
    assert approval.requesters == ["payments-leads@x.test"]
    # ...and an API the rule does not name cannot even be asked for.
    elsewhere = librarian.policy.check_write(
        WriteRequest(api="weather", operation="forecast", args={"city": "x"}), caller
    )
    assert not elsewhere.allowed and "may not write" in elsewhere.reason


# -- what the agents' platform says about the person, without proof -----------------------

WRITER = ActorRule(
    match=ActorMatch(namespace="payments"), write_apis=["tracker"], owner="payments-leads@x.test"
)
COMMENT = [
    new_data_part(
        {
            "skill": "write",
            "api": "tracker",
            "operation": "addComment",
            "args": {"key": "PAY-1"},
            "body": {"text": "hi"},
            "reason": "test",
        }
    )
]


def waiting(librarian: Librarian) -> WriteApproval:
    """The one write that is waiting for a person."""
    (approval,) = librarian.approvals.pending()
    return approval


async def test_a_name_the_agents_platform_sends_is_shown_as_a_claim_and_decides_nothing(
    librarian: Librarian, substrate: FakeSubstrate, audit: MemoryAuditSink
) -> None:
    """kagent can send the signed-in user's name in a header that nothing signs, set where
    the agent itself runs commands. Asked to, librarian shows it to the approver and records
    it, as what the agent's side says. It is not the person, and not the requester."""
    http = await served(librarian, substrate, WRITER, names_user_in="X-User-Id")
    http.headers["Authorization"] = f"Bearer {substrate.token()}"
    http.headers["X-User-Id"] = "kim@example.com"

    read = await send(librarian, http, WHOAMI)
    assert read.final == TaskState.TASK_STATE_COMPLETED
    call = next(ev for ev in audit.events if ev.event == "call")
    assert call.principal is None and call.extra["claimed_for"] == "kim@example.com"
    assert read.data[-1]["provenance"]["principal"] is None  # no person reached the call

    asked = await send(librarian, http, COMMENT)
    assert asked.final == TaskState.TASK_STATE_AUTH_REQUIRED
    approval = waiting(librarian)
    assert approval.claimed_for == "kim@example.com" and approval.principal is None
    # The rule's owner is the requester. The person named is not, and may not approve.
    assert approval.requesters == ["payments-leads@x.test"]
    librarian.approvals.approver_mode = "requester"
    assert librarian.approvals.may_approve(approval, "payments-leads@x.test", set()) is None
    assert librarian.approvals.may_approve(approval, "kim@example.com", set()) is not None
    requested = next(ev for ev in audit.events if ev.event == "write_requested")
    assert requested.principal is None and requested.extra["claimed_for"] == "kim@example.com"

    page = (await http.get(librarian.approvals.link(approval))).text
    assert (
        "nobody librarian has proof of. The agent's platform names <b>kim@example.com</b>: "
        "nothing signs that, and it does not make them the requester." in page
    )
    await librarian.approvals.deny(approval.id, "payments-leads@x.test")


async def test_what_the_platform_sends_is_made_fit_to_show(
    librarian: Librarian, substrate: FakeSubstrate
) -> None:
    """The header is whatever the agent's side chose to send: it is shown as text, on one
    line, and no longer than a name needs."""
    http = await served(librarian, substrate, WRITER, names_user_in="X-User-Id")
    http.headers["Authorization"] = f"Bearer {substrate.token()}"
    http.headers["X-User-Id"] = "<script>alert(1)</script>\x1b[31m  kim\t&  co " + "x" * 400
    assert (await send(librarian, http, COMMENT)).final == TaskState.TASK_STATE_AUTH_REQUIRED
    approval = waiting(librarian)
    assert approval.claimed_for is not None
    assert approval.claimed_for.startswith("<script>alert(1)</script> [31m kim & co xxx")
    assert len(approval.claimed_for) == 200 and approval.claimed_for.isprintable()
    page = (await http.get(librarian.approvals.link(approval))).text
    assert "<script>alert(1)" not in page
    assert "<b>&lt;script&gt;alert(1)&lt;/script&gt; [31m kim &amp; co xxx" in page
    await librarian.approvals.deny(approval.id, "payments-leads@x.test")


async def test_the_platforms_word_is_not_taken_unless_asked_for_and_never_from_a_key(
    librarian: Librarian, substrate: FakeSubstrate, audit: MemoryAuditSink
) -> None:
    # Not asked for (the default): the header is not read at all.
    http = await served(librarian, substrate, WRITER)
    http.headers["Authorization"] = f"Bearer {substrate.token()}"
    http.headers["X-User-Id"] = "kim@example.com"
    assert (await send(librarian, http, COMMENT)).final == TaskState.TASK_STATE_AUTH_REQUIRED
    approval = waiting(librarian)
    assert approval.claimed_for is None
    page = (await http.get(librarian.approvals.link(approval))).text
    assert "nobody (autonomous agent)" in page and "kim@example.com" not in page
    await librarian.approvals.deny(approval.id, "payments-leads@x.test")
    assert "claimed_for" not in str([ev.extra for ev in audit.events])

    # Asked for: only from an agent the platform vouches for. An agent with a key of its
    # own says who it works for the way it always did (`principal`), not in this header.
    http = await served(librarian, substrate, WRITER, names_user_in="X-User-Id")
    http.headers["Authorization"] = "Bearer key-a"
    http.headers["X-User-Id"] = "kim@example.com"
    assert (await send(librarian, http, WHOAMI)).final == TaskState.TASK_STATE_COMPLETED
    keyed = [ev for ev in audit.events if ev.event == "call"][-1]
    assert keyed.caller == "agent-a" and "claimed_for" not in keyed.extra
