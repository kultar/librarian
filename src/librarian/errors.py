"""Errors that are safe to show to a calling agent, and how a failed request is worded."""

from __future__ import annotations

import socket
import ssl
from typing import cast

import httpx


class LibrarianError(Exception):
    """An error safe to show to the calling agent."""


class ApiUnavailable(LibrarianError):
    """The API could not be reached, or did not finish answering.

    `sent` says whether the request can have reached the API. For a read it does not
    matter; for a write it is the difference between "nothing happened" and "check
    before trying again". `reason` is the short form for the audit trail. `detail` is
    what the HTTP client said, for librarian's own log only: it is never shown to an
    agent.
    """

    def __init__(self, message: str, *, sent: bool, reason: str = "", detail: str = "") -> None:
        super().__init__(message)
        self.sent = sent
        self.reason = reason or message
        self.detail = detail


class CredentialSetupError(LibrarianError):
    """A credential could not be made for a call, and a new key would not help: what the
    entry says about its authentication (a token URL, scopes, a role, where an App is
    installed) does not work. For a person to fix in the console."""


# The failures that happen before any of the request has left: a write was not made.
_NOT_SENT = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.LocalProtocolError,
    httpx.UnsupportedProtocol,
)


def why_no_answer(exc: BaseException) -> tuple[str, str, bool]:
    """Why a request got no answer: in words for an agent, in the HTTP client's own
    words (for the log only), and whether the request can have been sent.

    The words for the agent are built from the kind of failure, never from the client's
    message: that one can quote the request it refused, credential included.
    """
    sent = not isinstance(exc, _NOT_SENT)
    detail = str(exc)
    if isinstance(exc, httpx.LocalProtocolError):
        reason, detail = "the request has a header that is not valid HTTP", ""
    elif isinstance(exc, httpx.PoolTimeout):
        reason = "librarian has too many calls to it in progress"
    elif isinstance(exc, httpx.ConnectTimeout):
        reason = "it could not be reached: connecting took too long"
    elif isinstance(exc, httpx.ConnectError):
        reason = "it could not be reached" + _why_not_reached(exc)
    elif isinstance(exc, TimeoutError | httpx.ReadTimeout | httpx.WriteTimeout):
        reason = "it did not answer in time"
    elif isinstance(exc, httpx.DecodingError):
        reason = "its answer could not be decompressed"
    elif isinstance(exc, httpx.RemoteProtocolError | httpx.ReadError | httpx.WriteError):
        reason = "the connection broke before it had answered"
    else:
        reason = f"the call failed ({type(exc).__name__})"
    return reason, detail, sent


def _why_not_reached(exc: BaseException) -> str:
    """What stopped a connection, if the chain of causes says: the name, the TLS
    certificate, or a refusal."""
    seen: set[int] = set()
    todo: list[BaseException] = [exc]
    while todo:
        cause = todo.pop()
        if id(cause) in seen:
            continue
        seen.add(id(cause))
        if isinstance(cause, ssl.SSLCertVerificationError):
            return " (its TLS certificate was not accepted)"
        if isinstance(cause, ssl.SSLError):
            return " (the TLS handshake failed)"
        if isinstance(cause, socket.gaierror):
            return " (its host name did not resolve)"
        if isinstance(cause, ConnectionRefusedError):
            return " (the connection was refused)"
        todo += [c for c in (cause.__cause__, cause.__context__) if c is not None]
        if isinstance(cause, BaseExceptionGroup):
            todo += list(cast(BaseExceptionGroup[BaseException], cause).exceptions)
    return ""
