"""Audit sinks. Every call decision is recorded; secret values never are."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TextIO

from pydantic import BaseModel, Field


class AuditEvent(BaseModel):
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    event: str  # call | denied | credential_request | credential_resolved | onboard
    caller: str | None = None
    principal: str | None = None
    api: str | None = None
    operation: str | None = None
    status_code: int | None = None
    bytes: int | None = None
    duration_ms: int | None = None
    detail: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict[str, Any])


class AuditSink(Protocol):
    async def record(self, event: AuditEvent) -> None: ...


class JsonlAuditSink:
    """Writes one JSON object per line to a stream (stdout by default) or a file."""

    def __init__(self, path: Path | None = None, stream: TextIO | None = None) -> None:
        self.path = path
        self.stream = stream if stream is not None else (None if path else sys.stdout)
        self._lock = asyncio.Lock()

    async def record(self, event: AuditEvent) -> None:
        line = json.dumps({"audit": event.model_dump(mode="json", exclude_none=True)})
        async with self._lock:
            if self.path is not None:
                await asyncio.to_thread(_append, self.path, line)
            elif self.stream is not None:
                self.stream.write(line + "\n")
                self.stream.flush()


def _append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


class MemoryAuditSink:
    """Keeps events in memory; used by tests."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def record(self, event: AuditEvent) -> None:
        self.events.append(event)
