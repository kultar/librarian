"""Models for API calls flowing through the librarian."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from librarian.registry.models import EntryStatus

ArgValue = str | int | float | bool | list[str] | list[int]


class Caller(BaseModel):
    """Who is asking.

    `agent` is authenticated by the server (or `anonymous` when caller auth is
    off). `principal` is who the agent acts for; in v0.1 it is *asserted* by the
    calling agent and recorded for audit, not verified.
    """

    model_config = ConfigDict(frozen=True)

    agent: str = "anonymous"
    authenticated: bool = False
    principal: str | None = None


class CallRequest(BaseModel):
    """A fully specified call: which API, which operation, which arguments."""

    model_config = ConfigDict(extra="forbid")

    api: str = Field(min_length=1, max_length=64)
    operation: str = Field(min_length=1, max_length=128)
    args: dict[str, ArgValue] = Field(default_factory=dict[str, ArgValue])
    body: dict[str, Any] | None = None
    max_pages: int = Field(default=1, ge=1, le=20)


class Provenance(BaseModel):
    """Where a result came from, so callers can cite and auditors can trace it."""

    api: str
    api_status: EntryStatus
    operation: str
    method: str
    url: str  # never contains secrets: query-string API keys are redacted
    requested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    caller: str
    principal: str | None = None
    credential_ref: str | None = None
    pages: int = 1


class CallResult(BaseModel):
    status_code: int
    ok: bool
    data: Any = None
    truncated: bool = False
    content_type: str = ""
    provenance: Provenance
