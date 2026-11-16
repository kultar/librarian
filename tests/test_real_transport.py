"""What only a real connection can do to librarian.

The fake transport of the other tests hands over whole answers. A real API can also
send a byte now and then for ever, hang up in the middle of an answer, send a small
compressed body that unpacks to gigabytes, refuse the connection, or present a
certificate nobody trusts. These tests use a real HTTP client against a raw TCP server
that does exactly what each test tells it to.
"""

from __future__ import annotations

import asyncio
import gzip
import socket
import ssl
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from librarian.audit import MemoryAuditSink
from librarian.calls import Caller, CallRequest, WriteRequest
from librarian.config import Settings
from librarian.errors import ApiUnavailable
from librarian.executor import _unavailable  # pyright: ignore[reportPrivateUsage]
from librarian.registry.models import ApiEntry
from librarian.secrets import FileSecretStore
from librarian.service import Librarian
from tests.conftest import PUBLIC, TRACKER, RecordingNotifier, write_entries

CALLER = Caller(agent="agent-a")
WRITER = Caller(agent="notes-bot", principal="kim@x.test", write_apis=("tracker",))
WHOAMI = CallRequest(api="tracker", operation="whoami")
COMMENT = WriteRequest(
    api="tracker", operation="addComment", args={"key": "PAY-1"}, body={"body": "hello"}
)
KEY = "r3ad-key-0123456789"

Answer = Callable[[asyncio.StreamWriter], Awaitable[None]]


class RawServer:
    """Answers every connection with whatever `self.answer` writes to it."""

    def __init__(self) -> None:
        self.answer: Answer | None = None
        self.requests = 0
        self.port = 0
        self._server: asyncio.Server | None = None

    async def start(self, tls: ssl.SSLContext | None = None) -> str:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0, ssl=tls)
        self.port = self._server.sockets[0].getsockname()[1]
        return f"{'https' if tls else 'http'}://127.0.0.1:{self.port}"

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            length = next(
                (
                    int(line.split(b":")[1])
                    for line in head.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                ),
                0,
            )
            if length:
                await reader.readexactly(length)
            self.requests += 1
            if self.answer is not None:
                await self.answer(writer)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()


@pytest.fixture
async def server() -> AsyncIterator[RawServer]:
    raw = RawServer()
    yield raw
    await raw.stop()


async def librarian_for(
    base: str,
    settings: Settings,
    secrets_store: FileSecretStore,
    audit: MemoryAuditSink,
    notifier: RecordingNotifier,
    *,
    client_timeout: float = 5,
    deadline: float = 10,
) -> Librarian:
    """A librarian with a real HTTP client, whose tracker API lives at `base`."""
    write_entries(settings.registry_dir, {**TRACKER, "base_url": f"{base}/v1"}, PUBLIC)
    librarian = Librarian(
        settings,
        secrets_store=secrets_store,
        audit=audit,
        notifier=notifier,
        http_client=httpx.AsyncClient(timeout=client_timeout),
    )
    librarian.load()
    librarian.executor.deadline_seconds = deadline
    await secrets_store.put("tracker/readonly", KEY)
    await secrets_store.put("tracker/write", KEY)
    return librarian


async def write(librarian: Librarian) -> Any:
    entry = librarian.registry.get("tracker")
    assert entry is not None
    op = entry.operation("addComment")
    assert op is not None
    return await librarian.executor.execute_write(entry, op, COMMENT, WRITER)


def head(status: str = "200 OK", **headers: str) -> bytes:
    lines = [f"HTTP/1.1 {status}", *(f"{k.replace('_', '-')}: {v}" for k, v in headers.items())]
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


async def test_an_api_that_sends_a_byte_now_and_then_is_cut_off(
    server: RawServer,
    settings: Settings,
    secrets_store: FileSecretStore,
    audit: MemoryAuditSink,
    notifier: RecordingNotifier,
) -> None:
    """Each byte arrives within the client's timeout, so only a limit on the whole
    request ends it."""

    async def trickle(writer: asyncio.StreamWriter) -> None:
        writer.write(head(content_type="application/json", transfer_encoding="chunked"))
        while True:
            writer.write(b"1\r\n \r\n")
            await writer.drain()
            await asyncio.sleep(0.05)

    server.answer = trickle
    base = await server.start()
    librarian = await librarian_for(
        base, settings, secrets_store, audit, notifier, client_timeout=2, deadline=0.6
    )
    try:
        started = time.monotonic()
        with pytest.raises(ApiUnavailable) as read:
            await librarian.executor.execute(WHOAMI, CALLER)
        assert time.monotonic() - started < 3
        assert "did not answer in time" in str(read.value) and "retry later" in str(read.value)
        # A write: the status line arrived before the trickle, so what the API decided
        # is known, and the write is reported with it instead of as "outcome unknown".
        started = time.monotonic()
        result = await write(librarian)
        assert time.monotonic() - started < 3
        assert result.status_code == 200 and result.data is None
        assert result.error and "did not arrive completely" in result.error
        attempts = [(e.event, e.status_code, e.detail) for e in audit.events]
        assert attempts == [
            ("call", None, "no answer: it did not answer in time"),
            ("write", 200, "the answer did not arrive completely"),
        ]
    finally:
        await librarian.aclose()


async def test_an_api_that_accepts_and_says_nothing_times_out(
    server: RawServer,
    settings: Settings,
    secrets_store: FileSecretStore,
    audit: MemoryAuditSink,
    notifier: RecordingNotifier,
) -> None:
    async def silence(writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(30)

    server.answer = silence
    base = await server.start()
    librarian = await librarian_for(
        base, settings, secrets_store, audit, notifier, client_timeout=0.4
    )
    try:
        with pytest.raises(ApiUnavailable, match="did not answer in time"):
            await librarian.executor.execute(WHOAMI, CALLER)
        with pytest.raises(ApiUnavailable) as written:
            await write(librarian)
        assert written.value.sent  # the request went out; only the answer is missing
    finally:
        await librarian.aclose()


async def test_an_answer_that_breaks_off_is_reported_with_what_is_known(
    server: RawServer,
    settings: Settings,
    secrets_store: FileSecretStore,
    audit: MemoryAuditSink,
    notifier: RecordingNotifier,
) -> None:
    async def hang_up(writer: asyncio.StreamWriter) -> None:
        writer.write(head("201 Created", content_type="application/json", content_length="500"))
        writer.write(b'{"id": 7, "rest": "')
        await writer.drain()

    server.answer = hang_up
    base = await server.start()
    librarian = await librarian_for(base, settings, secrets_store, audit, notifier)
    try:
        with pytest.raises(ApiUnavailable, match="the connection broke before it had answered"):
            await librarian.executor.execute(WHOAMI, CALLER)
        # A write: the status line arrived, so what the API decided is known.
        result = await write(librarian)
        assert result.status_code == 201 and result.ok and result.data is None
        assert result.error and "did not arrive completely" in result.error
        written = [e for e in audit.events if e.event == "write"]
        assert [(e.status_code, e.detail) for e in written] == [
            (201, "the answer did not arrive completely")
        ]
    finally:
        await librarian.aclose()


async def test_a_small_body_that_unpacks_to_a_huge_one_is_cut_at_the_limit(
    server: RawServer,
    settings: Settings,
    secrets_store: FileSecretStore,
    audit: MemoryAuditSink,
    notifier: RecordingNotifier,
) -> None:
    bomb = gzip.compress(b"0" * 200_000_000)  # 200 MB of zeros in about 200 kB

    async def explode(writer: asyncio.StreamWriter) -> None:
        writer.write(
            head(content_type="text/plain", content_encoding="gzip", content_length=str(len(bomb)))
        )
        writer.write(bomb)
        await writer.drain()

    server.answer = explode
    base = await server.start()
    librarian = await librarian_for(base, settings, secrets_store, audit, notifier)
    librarian.executor.max_response_bytes = 50_000
    try:
        started = time.monotonic()
        result = await librarian.executor.execute(WHOAMI, CALLER)
        assert result.truncated and len(result.data) <= 50_000
        assert time.monotonic() - started < 5  # it stopped unpacking, it did not finish
    finally:
        await librarian.aclose()


async def test_a_refused_connection_says_so_and_a_write_was_not_sent(
    server: RawServer,
    settings: Settings,
    secrets_store: FileSecretStore,
    audit: MemoryAuditSink,
    notifier: RecordingNotifier,
) -> None:
    base = await server.start()
    await server.stop()  # nothing listens there any more
    await asyncio.sleep(0.05)
    librarian = await librarian_for(base, settings, secrets_store, audit, notifier)
    try:
        with pytest.raises(ApiUnavailable) as read:
            await librarian.executor.execute(WHOAMI, CALLER)
        assert "it could not be reached (the connection was refused)" in str(read.value)
        with pytest.raises(ApiUnavailable) as written:
            await write(librarian)
        assert not written.value.sent and "The write was not sent" in str(written.value)
        assert server.requests == 0
    finally:
        await librarian.aclose()


def self_signed(folder: Path) -> ssl.SSLContext:
    """A TLS certificate for 127.0.0.1 that no client has reason to trust."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = folder / "cert.pem", folder / "key.pem"
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    return context


async def test_a_certificate_nobody_trusts_is_named_as_the_reason(
    server: RawServer,
    settings: Settings,
    secrets_store: FileSecretStore,
    audit: MemoryAuditSink,
    notifier: RecordingNotifier,
    tmp_path: Path,
) -> None:
    base = await server.start(await asyncio.to_thread(self_signed, tmp_path))
    librarian = await librarian_for(base, settings, secrets_store, audit, notifier)
    try:
        with pytest.raises(ApiUnavailable) as read:
            await librarian.executor.execute(WHOAMI, CALLER)
        assert "its TLS certificate was not accepted" in str(read.value)
        assert server.requests == 0  # and the key was never sent to it
    finally:
        await librarian.aclose()


def test_why_a_connection_failed_is_read_from_the_chain_of_causes() -> None:
    """The HTTP client wraps the real reason several layers deep; a name that does not
    resolve cannot be tested for real (resolvers differ), so the chain is built by hand."""
    entry = ApiEntry.model_validate(TRACKER)

    def told(cause: BaseException) -> str:
        middle = OSError("All connection attempts failed")
        middle.__cause__ = cause
        outer = httpx.ConnectError("All connection attempts failed")
        outer.__cause__ = middle
        return str(_unavailable(entry, outer, write=False))

    assert "(its host name did not resolve)" in told(socket.gaierror(-2, "Name not known"))
    assert "(the connection was refused)" in told(ConnectionRefusedError(111, "refused"))
    assert "(its TLS certificate was not accepted)" in told(
        ssl.SSLCertVerificationError(1, "certificate verify failed")
    )
    assert "(the TLS handshake failed)" in told(ssl.SSLError(1, "wrong version number"))
    several = ExceptionGroup("multiple", [OSError("unreachable"), ConnectionRefusedError()])
    assert "(the connection was refused)" in told(several)
    plain = told(OSError("network is unreachable"))
    assert "it could not be reached. Nothing was read" in plain  # no guess when unsure
    # None of it quotes what the client said.
    quoting = httpx.LocalProtocolError("Illegal header value b'Bearer r3ad-key-0123456789'")
    refused = _unavailable(entry, quoting, write=True)
    assert KEY not in str(refused) and refused.detail == "" and not refused.sent
