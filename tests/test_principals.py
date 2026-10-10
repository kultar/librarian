"""Verified principals against a fake OIDC identity provider."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

import httpx
import jwt
import pytest
from a2a.helpers.proto_helpers import new_data_part
from a2a.types import TaskState
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from librarian.calls import Caller
from librarian.config import IssuerSettings, PrincipalSettings
from librarian.principals import Issuer, PrincipalError, PrincipalVerifier
from librarian.secrets import FileSecretStore
from librarian.server import create_app
from librarian.service import Librarian
from tests.conftest import BASE, Router
from tests.test_server import KEYS, send

ISSUER = "https://idp.example.test"
AUDIENCE = "librarian"


class FakeIdP:
    def __init__(self) -> None:
        self.rsa = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.ec = ec.generate_private_key(ec.SECP256R1())
        self.published: dict[str, rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey] = {"k1": self.rsa}
        self.fetches = 0

    def jwks(self) -> dict[str, Any]:
        keys: list[dict[str, Any]] = []
        for kid, key in self.published.items():
            if isinstance(key, rsa.RSAPrivateKey):
                jwk: dict[str, Any] = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
                jwk.update(kid=kid, alg="RS256", use="sig")
            else:
                jwk = ECAlgorithm.to_jwk(key.public_key(), as_dict=True)
                jwk.update(kid=kid, alg="ES256", use="sig")
            keys.append(jwk)
        return {"keys": keys}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": f"{ISSUER}/keys"})
        if request.url.path == "/keys":
            self.fetches += 1
            return httpx.Response(200, json=self.jwks())
        return httpx.Response(404)

    def token(self, kid: str = "k1", **claims: Any) -> str:
        now = int(time.time())
        payload = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "email": "kim@x.test",
            "iat": now,
            "exp": now + 300,
            **claims,
        }
        key = self.published.get(kid) or self.rsa
        alg = "ES256" if isinstance(key, ec.EllipticCurvePrivateKey) else "RS256"
        return jwt.encode(payload, key, algorithm=alg, headers={"kid": kid})


@pytest.fixture
def idp() -> FakeIdP:
    return FakeIdP()


def verifier(idp: FakeIdP) -> PrincipalVerifier:
    return PrincipalVerifier(
        [Issuer(issuer=ISSUER, audience=AUDIENCE)],
        client=httpx.AsyncClient(transport=httpx.MockTransport(idp.handler)),
    )


async def test_valid_token_yields_the_principal(idp: FakeIdP) -> None:
    v = verifier(idp)
    assert await v.verify(idp.token()) == "kim@x.test"
    assert await v.verify(idp.token()) == "kim@x.test"
    assert idp.fetches == 1  # keys are cached


@pytest.mark.parametrize(
    ("claims", "message"),
    [
        ({"exp": int(time.time()) - 3600}, "expired"),
        ({"aud": "some-other-app"}, "not issued for librarian"),
        ({"iss": "https://evil.example"}, "untrusted issuer"),
        ({"email": None}, "no 'email' claim"),
    ],
)
async def test_bad_tokens_are_refused(idp: FakeIdP, claims: dict[str, Any], message: str) -> None:
    with pytest.raises(PrincipalError, match=message):
        await verifier(idp).verify(idp.token(**claims))


async def test_unsigned_and_symmetric_tokens_are_refused(idp: FakeIdP) -> None:
    payload = {"iss": ISSUER, "aud": AUDIENCE, "email": "eve@x.test", "exp": int(time.time()) + 60}
    header_none = {"alg": "none", "typ": "JWT", "kid": "k1"}
    unsigned = f"{_b64(header_none)}.{_b64(payload)}."
    with pytest.raises(PrincipalError, match="not accepted"):
        await verifier(idp).verify(unsigned)

    # Algorithm confusion: HS256 "signed" with the IdP's *public* key as the secret.
    public_pem = idp.rsa.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    header_hs = {"alg": "HS256", "typ": "JWT", "kid": "k1"}
    signing_input = f"{_b64(header_hs)}.{_b64(payload)}".encode()
    mac = hmac.new(public_pem, signing_input, hashlib.sha256).digest()
    forged = signing_input.decode() + "." + base64.urlsafe_b64encode(mac).rstrip(b"=").decode()
    with pytest.raises(PrincipalError, match="not accepted"):
        await verifier(idp).verify(forged)


def _b64(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


async def test_tampered_signature_is_refused(idp: FakeIdP) -> None:
    header, body, signature = idp.token().split(".")
    other = idp.token(email="admin@x.test").split(".")[1]
    assert body != other
    with pytest.raises(PrincipalError, match="invalid"):
        await verifier(idp).verify(f"{header}.{other}.{signature}")


async def test_key_rotation_triggers_one_refresh(idp: FakeIdP) -> None:
    v = verifier(idp)
    await v.verify(idp.token())
    idp.published["k2"] = idp.ec  # IdP rotates in an EC key...
    v._last_refresh[ISSUER] -= 60  # ...a minute later  # pyright: ignore[reportPrivateUsage]
    v.issuers[ISSUER] = Issuer(issuer=ISSUER, audience=AUDIENCE, algorithms=("RS256", "ES256"))
    assert await v.verify(idp.token(kid="k2")) == "kim@x.test"
    assert idp.fetches == 2
    with pytest.raises(PrincipalError, match="unknown key"):
        await v.verify(idp.token(kid="k9"))  # refresh is rate-limited; still unknown
    assert idp.fetches == 2


def test_none_algorithm_cannot_be_configured() -> None:
    with pytest.raises(ValueError, match="not allowed"):
        PrincipalVerifier([Issuer(issuer=ISSUER, audience=AUDIENCE, algorithms=("none",))])


def test_claimed_principal_is_only_trusted_when_verification_is_off() -> None:
    claimed = Caller(agent="a", principal="kim@x.test", owner="ops@x.test")
    assert claimed.requesters() == {"kim@x.test", "ops@x.test"}
    untrusted = claimed.model_copy(update={"trust_claimed_principal": False})
    assert untrusted.requesters() == {"ops@x.test"}
    verified = untrusted.model_copy(update={"principal_verified": True})
    assert verified.requesters() == {"kim@x.test", "ops@x.test"}


# -- end to end over A2A -------------------------------------------------------------------


async def app_client(
    librarian: Librarian, idp: FakeIdP, mode: str, token: str | None
) -> httpx.AsyncClient:
    librarian.settings.principals = PrincipalSettings(
        mode=mode,  # type: ignore[arg-type]
        issuers=[IssuerSettings(issuer=ISSUER, audience=AUDIENCE)],
    )
    app = create_app(librarian, caller_keys=KEYS)
    app.state.caller_keys.update(KEYS)
    app.state.principal_verifier.client = httpx.AsyncClient(
        transport=httpx.MockTransport(idp.handler)
    )
    headers = {"Authorization": "Bearer key-a"}
    if token is not None:
        headers["X-Principal-Token"] = token
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=librarian.settings.public_url,
        headers=headers,
    )


@pytest.fixture(autouse=True)
async def tracker_ok(secrets_store: FileSecretStore, router: Router) -> None:
    await secrets_store.put("tracker/readonly", "tok")
    router.add("GET", f"{BASE}/v1/me", lambda r: httpx.Response(200, json={}))


WHOAMI = [new_data_part({"skill": "execute", "api": "tracker", "operation": "whoami"})]


async def test_verified_principal_reaches_provenance(librarian: Librarian, idp: FakeIdP) -> None:
    async with await app_client(librarian, idp, "required", idp.token()) as http:
        outcome = await send(librarian, http, WHOAMI)
    prov = outcome.data[-1]["provenance"]
    assert prov["principal"] == "kim@x.test" and prov["principal_verified"] is True


async def test_required_mode_refuses_claimed_principals(librarian: Librarian, idp: FakeIdP) -> None:
    async with await app_client(librarian, idp, "required", None) as http:
        claimed = await send(librarian, http, WHOAMI, metadata={"principal": "ceo@x.test"})
        assert claimed.final == TaskState.TASK_STATE_REJECTED
        anonymous = await send(librarian, http, WHOAMI)  # no person at all: fine
        assert anonymous.final == TaskState.TASK_STATE_COMPLETED


async def test_verified_token_beats_a_conflicting_claim(librarian: Librarian, idp: FakeIdP) -> None:
    async with await app_client(librarian, idp, "optional", idp.token()) as http:
        outcome = await send(librarian, http, WHOAMI, metadata={"principal": "ceo@x.test"})
    assert outcome.data[-1]["provenance"]["principal"] == "kim@x.test"


async def test_invalid_token_is_rejected_at_the_door(librarian: Librarian, idp: FakeIdP) -> None:
    async with await app_client(librarian, idp, "optional", idp.token(aud="other")) as http:
        response = await http.post("/", json={})
    assert response.status_code == 401
    assert "not issued for librarian" in response.json()["error"]


async def test_optional_mode_records_but_does_not_trust_claims(
    librarian: Librarian, idp: FakeIdP
) -> None:
    async with await app_client(librarian, idp, "optional", None) as http:
        outcome = await send(librarian, http, WHOAMI, metadata={"principal": "ceo@x.test"})
    prov = outcome.data[-1]["provenance"]
    assert prov["principal"] == "ceo@x.test" and prov["principal_verified"] is False
