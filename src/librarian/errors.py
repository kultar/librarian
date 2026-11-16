"""Errors that are safe to show to a calling agent."""

from __future__ import annotations


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
