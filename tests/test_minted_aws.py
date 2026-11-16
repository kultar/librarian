"""AWS Signature Version 4: nothing is stored and nothing is sent but a signature that
fits one request. The signatures are checked here by an implementation of the published
algorithm that shares no code with the one that made them."""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, quote

import httpx
import pytest
from pydantic import ValidationError

from librarian import minted
from librarian.audit import MemoryAuditSink
from librarian.calls import Caller, CallRequest, WriteRequest
from librarian.errors import CredentialSetupError
from librarian.executor import PolicyDenied
from librarian.minted import Minter
from librarian.registry.models import ApiEntry, AwsSigV4Auth, EntryStatus
from librarian.service import Librarian
from tests.conftest import Router

CALLER = Caller(agent="agent-a", write_apis=("orders",))
API = "https://abc123.execute-api.eu-west-1.example.test"
ACCESS_KEY = "AKIDEXAMPLE0000000001"
SECRET_KEY = "s3cret-signing-key-0123456789"
SESSION_TOKEN = "sess1on-token-0123456789"
SIGNED = {"type": "aws_sigv4", "service": "execute-api", "region": "eu-west-1"}
ORDERS: dict[str, Any] = {
    "id": "orders",
    "name": "Orders",
    "base_url": f"{API}/prod",
    "status": "verified",
    "auth": SIGNED,
    "write_auth": SIGNED,
    "operations": [
        {
            "id": "getOrder",
            "method": "GET",
            "path": "/orders/{id}",
            "access": "read",
            "parameters": [{"name": "id", "in": "path"}, {"name": "expand", "in": "query"}],
        },
        {
            "id": "addNote",
            "method": "POST",
            "path": "/orders/{id}/notes",
            "access": "write",
            "writable": True,
            "accepts_body": True,
            "parameters": [{"name": "id", "in": "path"}],
        },
    ],
}


def expected_signature(request: httpx.Request, secret_key: str, *, s3: bool = False) -> str:
    """The signature AWS would compute for this request, as it arrived."""
    authorization = request.headers["authorization"]
    credential = re.search(r"Credential=([^,]+)", authorization)
    signed = re.search(r"SignedHeaders=([^,]+)", authorization)
    assert credential and signed
    _, date, region, service, _ = credential.group(1).split("/")
    names = signed.group(1).split(";")
    path, _, query = request.url.raw_path.decode().partition("?")
    canonical_path = path if s3 else quote(path, safe="/~")
    pairs = sorted(parse_qsl(query, keep_blank_values=True))
    canonical_query = "&".join(f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in pairs)
    canonical_headers = "".join(
        f"{name}:{' '.join(request.headers[name].split())}\n" for name in names
    )
    payload = hashlib.sha256(request.content).hexdigest()
    canonical = "\n".join(
        [
            request.method,
            canonical_path,
            canonical_query,
            canonical_headers,
            ";".join(names),
            payload,
        ]
    )
    stamp = request.headers["x-amz-date"]
    scope = f"{date}/{region}/{service}/aws4_request"
    to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()]
    )

    def step(key: bytes, text: str) -> bytes:
        return hmac.new(key, text.encode(), hashlib.sha256).digest()

    key = step(
        step(step(step(f"AWS4{secret_key}".encode(), date), region), service), "aws4_request"
    )
    return hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()


@pytest.fixture(autouse=True)
def identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """librarian's own AWS identity, as its pod's role would provide it."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", ACCESS_KEY)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", SECRET_KEY)
    monkeypatch.setenv("AWS_SESSION_TOKEN", SESSION_TOKEN)
    for name in ("AWS_PROFILE", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
async def orders(librarian: Librarian, router: Router) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def api(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"headers": dict(request.headers), "id": 7})

    router.add("GET", f"{API}/prod/orders/A%201", api)
    router.add("POST", f"{API}/prod/orders/A%201/notes", api)
    await librarian.registry.save(ApiEntry.model_validate(ORDERS))
    return seen


GET_ORDER = CallRequest(
    api="orders", operation="getOrder", args={"id": "A 1", "expand": "lines,customer name"}
)


async def write(librarian: Librarian) -> Any:
    entry = librarian.registry.get("orders")
    assert entry is not None
    op = entry.operation("addNote")
    assert op is not None
    request = WriteRequest(
        api="orders", operation="addNote", args={"id": "A 1"}, body={"text": "päid ✓"}
    )
    return await librarian.executor.execute_write(entry, op, request, CALLER)


async def test_each_request_carries_a_signature_that_fits_it_and_nothing_else(
    librarian: Librarian, orders: list[httpx.Request], audit: MemoryAuditSink
) -> None:
    result = await librarian.executor.execute(GET_ORDER, CALLER)
    [request] = orders
    authorization = request.headers["authorization"]
    assert authorization.startswith(f"AWS4-HMAC-SHA256 Credential={ACCESS_KEY}/")
    assert "/eu-west-1/execute-api/aws4_request" in authorization
    assert request.headers["x-amz-security-token"] == SESSION_TOKEN
    # The signature is the one AWS would compute for exactly what arrived: this path
    # (with its escaped space), this query, this host.
    assert authorization.endswith(f"Signature={expected_signature(request, SECRET_KEY)}")
    # ...and the query travels in the one encoding AWS computes its own signature over.
    assert request.url.query == b"expand=lines%2Ccustomer%20name"
    assert request.url.params["expand"] == "lines,customer name"  # the same value
    # The signing key itself is never sent.
    sent = str(request.url) + str(dict(request.headers))
    assert SECRET_KEY not in sent
    # The API repeated the headers: the signature and the session token do not come back.
    assert result.ok and result.data["id"] == 7
    assert result.data["headers"]["authorization"] == "[redacted]"
    assert result.data["headers"]["x-amz-security-token"] == "[redacted]"
    assert result.provenance.credential_ref is None  # there is no stored key to name
    left = result.model_dump_json() + "".join(e.model_dump_json() for e in audit.events)
    signature = authorization.rpartition("Signature=")[2]
    for value in (SESSION_TOKEN, signature, SECRET_KEY):
        assert value not in left
    assert audit.events[-1].extra["credential"] == "signed with librarian's own AWS identity"


async def test_a_write_is_signed_over_the_body_that_is_sent(
    librarian: Librarian, orders: list[httpx.Request]
) -> None:
    result = await write(librarian)
    [request] = orders
    assert request.method == "POST" and "päid ✓".encode() in request.content
    signed = re.search(r"SignedHeaders=([^,]+)", request.headers["authorization"])
    assert signed and "content-type" in signed.group(1).split(";")
    assert request.headers["authorization"].endswith(
        f"Signature={expected_signature(request, SECRET_KEY)}"
    )
    assert result.ok and result.data["headers"]["authorization"] == "[redacted]"


async def test_s3_is_signed_its_own_way(
    librarian: Librarian, orders: list[httpx.Request], router: Router
) -> None:
    entry = librarian.registry.get("orders")
    assert entry is not None
    s3 = AwsSigV4Auth.model_validate({**SIGNED, "service": "s3"})
    await librarian.registry.save(entry.model_copy(update={"auth": s3}))
    await librarian.executor.execute(GET_ORDER, CALLER)
    [request] = orders
    # S3 signs the path as it is sent and names the hash of the body in a header.
    assert request.headers["x-amz-content-sha256"] == hashlib.sha256(b"").hexdigest()
    assert request.headers["authorization"].endswith(
        f"Signature={expected_signature(request, SECRET_KEY, s3=True)}"
    )


class FakeSts:
    """What boto3 is asked for, and what it answers."""

    def __init__(self) -> None:
        self.asked: list[dict[str, Any]] = []
        self.refuse: Exception | None = None
        self.identity: Any = object()

    def Session(self) -> FakeSts:
        return self

    def client(self, service: str, region_name: str) -> FakeSts:
        assert (service, region_name) == ("sts", "eu-west-1")
        return self

    def assume_role(self, **request: Any) -> dict[str, Any]:
        self.asked.append(request)
        if self.refuse is not None:
            raise self.refuse
        return {
            "Credentials": {
                "AccessKeyId": "ROLEKEYEXAMPLE0000001",
                "SecretAccessKey": "r0le-signing-key-0123456789",
                "SessionToken": f"r0le-session-{len(self.asked):04d}",
                "Expiration": datetime.now(UTC) + timedelta(minutes=15),
            }
        }

    def get_credentials(self) -> Any:
        return None  # no identity of its own


async def test_a_role_assumed_for_the_api_signs_instead_of_librarians_own_identity(
    librarian: Librarian,
    orders: list[httpx.Request],
    audit: MemoryAuditSink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sts = FakeSts()
    monkeypatch.setattr(minted, "_boto3", lambda: sts)
    entry = librarian.registry.get("orders")
    assert entry is not None
    role = "arn:aws:iam::123456789012:role/orders-read"
    assumed = AwsSigV4Auth.model_validate({**SIGNED, "role_arn": role, "external_id": "lib"})
    await librarian.registry.save(entry.model_copy(update={"auth": assumed}))
    now = datetime.now(UTC)
    librarian.executor.minter = Minter(librarian.executor.client, clock=lambda: now)
    for _ in range(3):
        await librarian.executor.execute(GET_ORDER, CALLER)
    assert sts.asked == [
        {
            "RoleArn": role,
            "RoleSessionName": "librarian-orders",
            "DurationSeconds": 900,  # the shortest AWS issues
            "ExternalId": "lib",
        }
    ]
    request = orders[-1]
    assert "Credential=ROLEKEYEXAMPLE0000001/" in request.headers["authorization"]
    assert request.headers["x-amz-security-token"] == "r0le-session-0001"
    assert request.headers["authorization"].endswith(
        f"Signature={expected_signature(request, 'r0le-signing-key-0123456789')}"
    )
    assert audit.events[-1].extra["credential"] == "signed as orders-read"
    now += timedelta(minutes=14, seconds=30)  # the role's credentials are about to run out
    await librarian.executor.execute(GET_ORDER, CALLER)
    assert len(sts.asked) == 2 and orders[-1].headers["x-amz-security-token"].endswith("0002")


async def test_a_role_librarian_may_not_assume_is_a_setup_problem(
    librarian: Librarian, orders: list[httpx.Request], monkeypatch: pytest.MonkeyPatch
) -> None:
    from botocore.exceptions import ClientError  # pyright: ignore[reportMissingTypeStubs]

    sts = FakeSts()
    sts.refuse = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "not authorized, key AKIDEXAMPLE"}},
        "AssumeRole",
    )
    monkeypatch.setattr(minted, "_boto3", lambda: sts)
    entry = librarian.registry.get("orders")
    assert entry is not None
    role = "arn:aws:iam::123456789012:role/orders-read"
    assumed = AwsSigV4Auth.model_validate({**SIGNED, "role_arn": role})
    await librarian.registry.save(entry.model_copy(update={"auth": assumed}))
    with pytest.raises(CredentialSetupError) as problem:
        await librarian.executor.execute(GET_ORDER, CALLER)
    assert str(problem.value) == f"Orders: librarian may not assume {role} (AccessDenied)"
    assert orders == []


async def test_without_an_aws_identity_nothing_is_sent(
    librarian: Librarian, orders: list[httpx.Request], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(minted, "_boto3", FakeSts)
    with pytest.raises(CredentialSetupError, match="no AWS identity to sign with"):
        await librarian.executor.execute(GET_ORDER, CALLER)
    assert orders == []


async def test_only_an_approved_entry_may_be_signed_for(
    librarian: Librarian, orders: list[httpx.Request]
) -> None:
    entry = librarian.registry.get("orders")
    assert entry is not None
    await librarian.registry.save(entry.model_copy(update={"status": EntryStatus.PROVISIONAL}))
    with pytest.raises(PolicyDenied, match="aws_sigv4 authentication, which needs a verified"):
        await librarian.executor.execute(GET_ORDER, CALLER)
    assert orders == []


def test_what_an_entry_may_say_about_signing() -> None:
    assert AwsSigV4Auth.model_validate(SIGNED).role_arn is None
    for bad in (
        {**SIGNED, "role_arn": "arn:aws:iam::123:role/x"},  # not an account id
        {**SIGNED, "role_arn": "arn:aws:s3:::bucket"},
        {**SIGNED, "service": "Execute API"},
        {**SIGNED, "region": ""},
        {**SIGNED, "secret_ref": "orders/key"},  # there is no key to refer to
    ):
        with pytest.raises(ValidationError):
            AwsSigV4Auth.model_validate(bad)
    entry = ApiEntry.model_validate(ORDERS)
    assert entry.secret_ref is None and entry.write_secret_ref is None
