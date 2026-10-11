"""What the librarian has seen each operation do, and the change signals that follow.

Observations are runtime facts (first success, response shape, deprecation headers,
repeated failures). They live in the state database, never in registry files, so
they cannot shadow a reviewer's edit to an entry. Each observation is tied to the
operation's definition by a fingerprint: when a reviewer changes the operation,
the old observation (including any suspension) no longer applies.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from pydantic import BaseModel, Field

from librarian.registry.models import MAX_SHAPE_PATHS, ApiEntry, Operation, Origin

log = logging.getLogger(__name__)

MAX_SHAPE_DEPTH = 4
# A docs/model operation that keeps answering "not here" was probably made up.
UNCONFIRMED_NOT_FOUND_LIMIT = 3
# A confirmed operation that keeps answering "gone" may have been removed.
GONE_LIMIT = 3
NOT_FOUND_LIMIT = 5
# Responses missing most of the recorded fields this many times in a row.
SHAPE_MISMATCH_LIMIT = 3
SHAPE_MISSING_FRACTION = 0.5


def fingerprint(op: Operation) -> str:
    """Identity of an operation's definition (what is sent, not its description)."""
    defining = op.model_dump(
        mode="json",
        include={"id", "method", "path", "parameters", "access", "accepts_body", "writable"},
    )
    return hashlib.sha256(repr(sorted(defining.items())).encode()).hexdigest()[:24]


class Observation(BaseModel):
    api: str
    operation: str
    fingerprint: str
    confirmed_at: datetime | None = None
    shape: list[str] | None = None
    suspended: str | None = None
    warning: str | None = None
    deprecation: str | None = None
    sunset: str | None = None
    not_found_streak: int = 0
    gone_streak: int = 0
    shape_mismatches: int = 0
    reported: list[str] = Field(default_factory=list[str])  # finding kinds already notified
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Finding(BaseModel):
    """A change worth telling a human about."""

    api: str
    operation: str | None = None
    kind: str  # deprecated | gone | not_found | shape | unconfirmed_missing | spec_changed
    severity: str  # info | breaking | safety
    detail: str


class DocStore(Protocol):
    async def save_doc(self, kind: str, doc_id: str, state: str, data: str) -> None: ...
    async def list_docs(self, kind: str, state: str | None = None) -> list[str]: ...


FindingHook = Callable[[Finding], Awaitable[None]]


class Observations:
    def __init__(self, store: DocStore | None = None) -> None:
        self.store = store
        self._items: dict[tuple[str, str], Observation] = {}
        self.on_finding: FindingHook | None = None

    async def load(self) -> None:
        if self.store is None:
            return
        items: dict[tuple[str, str], Observation] = {}
        for data in await self.store.list_docs("observation"):
            obs = Observation.model_validate_json(data)
            items[(obs.api, obs.operation)] = obs
        self._items = items

    def get(self, entry: ApiEntry, op: Operation) -> Observation | None:
        """The observation for this exact definition of the operation, if any."""
        obs = self._items.get((entry.id, op.id))
        return obs if obs is not None and obs.fingerprint == fingerprint(op) else None

    def suspended(self, entry: ApiEntry, op: Operation) -> str | None:
        if op.suspended:
            return op.suspended
        obs = self.get(entry, op)
        return obs.suspended if obs is not None else None

    def confirmed(self, entry: ApiEntry, op: Operation) -> bool:
        if op.confirmed_at is not None or op.origin in (Origin.SPEC, Origin.MANUAL):
            return True
        obs = self.get(entry, op)
        return obs is not None and obs.confirmed_at is not None

    def warnings(self, entry: ApiEntry, op: Operation) -> list[str]:
        notes: list[str] = []
        if op.warning:
            notes.append(op.warning)
        if not self.confirmed(entry, op):
            source = "its documentation" if op.origin is Origin.DOCS else "the model's knowledge"
            notes.append(
                f"{op.id} was learned from {source} and has not been confirmed by a "
                "successful call yet"
            )
        obs = self.get(entry, op)
        if obs is not None:
            if obs.warning:
                notes.append(obs.warning)
            if obs.deprecation or obs.sunset:
                notes.append(
                    f"{entry.name} marks {op.id} as deprecated"
                    + (f" (sunset {obs.sunset})" if obs.sunset else "")
                )
        return notes

    def _current(self, entry: ApiEntry, op: Operation) -> Observation:
        obs = self.get(entry, op)
        if obs is None:
            obs = Observation(api=entry.id, operation=op.id, fingerprint=fingerprint(op))
            self._items[(entry.id, op.id)] = obs
        return obs

    async def _save(self, obs: Observation) -> None:
        obs.updated_at = datetime.now(UTC)
        if self.store is not None:
            await self.store.save_doc(
                "observation", f"{obs.api}/{obs.operation}"[:64], "active", obs.model_dump_json()
            )

    async def suspend(self, entry: ApiEntry, op: Operation, reason: str) -> None:
        obs = self._current(entry, op)
        obs.suspended = reason[:500]
        await self._save(obs)

    async def warn(self, entry: ApiEntry, op: Operation, warning: str | None) -> None:
        obs = self._current(entry, op)
        obs.warning = warning[:500] if warning else None
        await self._save(obs)

    async def clear(self, entry: ApiEntry, op: Operation) -> bool:
        obs = self.get(entry, op)
        if obs is None or (obs.suspended is None and obs.warning is None):
            return False
        obs.suspended = obs.warning = None
        obs.reported = []
        await self._save(obs)
        return True

    async def observe(
        self,
        entry: ApiEntry,
        op: Operation,
        status_code: int,
        data: Any,
        headers: dict[str, str],
    ) -> None:
        """Record one read call's outcome and raise findings for change signals."""
        obs = self._current(entry, op)
        before = obs.model_dump()
        findings: list[Finding] = []
        has_path_params = bool(op.path_params)

        deprecation = headers.get("deprecation")
        sunset = headers.get("sunset")
        if deprecation or sunset:
            obs.deprecation, obs.sunset = (deprecation or "true")[:100], (sunset or None)
            findings.append(
                Finding(
                    api=entry.id,
                    operation=op.id,
                    kind="deprecated",
                    severity="info",
                    detail=f"{entry.name} marks {op.id} ({op.method} {op.path}) deprecated"
                    + (f"; sunset {sunset}" if sunset else ""),
                )
            )

        if 200 <= status_code < 300:
            obs.not_found_streak = obs.gone_streak = 0
            obs.reported = [kind for kind in obs.reported if kind != "gone"]  # recovered
            if obs.confirmed_at is None:
                obs.confirmed_at = datetime.now(UTC)
            shape = response_shape(data)
            if shape:
                if not obs.shape:
                    obs.shape = shape
                    obs.shape_mismatches = 0
                elif _mostly_missing(obs.shape, shape):
                    obs.shape_mismatches += 1
                    if obs.shape_mismatches >= SHAPE_MISMATCH_LIMIT:
                        missing = sorted(set(obs.shape) - set(shape))[:10]
                        findings.append(
                            Finding(
                                api=entry.id,
                                operation=op.id,
                                kind="shape",
                                severity="breaking",
                                detail=f"{op.id} responses no longer contain fields seen "
                                f"before: {', '.join(missing)}",
                            )
                        )
                        obs.shape, obs.shape_mismatches = shape, 0  # new baseline
                else:
                    obs.shape_mismatches = 0
        elif status_code in (405, 410) or (status_code == 404 and not has_path_params):
            obs.gone_streak += 1
        elif status_code == 404:
            obs.not_found_streak += 1

        unconfirmed = obs.confirmed_at is None and op.origin in (Origin.DOCS, Origin.MODEL)
        if unconfirmed and op.confirmed_at is None:
            streak = obs.gone_streak + obs.not_found_streak
            limit = 1 if status_code in (405, 410) else UNCONFIRMED_NOT_FOUND_LIMIT
            if status_code in (404, 405, 410) and streak >= limit and obs.suspended is None:
                obs.suspended = (
                    f"{op.method} {op.path} answered HTTP {status_code} {streak} time(s) and "
                    "never succeeded; it was learned from "
                    + ("documentation" if op.origin is Origin.DOCS else "the model's knowledge")
                    + " and may not exist"
                )
                findings.append(
                    Finding(
                        api=entry.id,
                        operation=op.id,
                        kind="unconfirmed_missing",
                        severity="breaking",
                        detail=obs.suspended,
                    )
                )
        elif obs.gone_streak >= (GONE_LIMIT if status_code != 404 else NOT_FOUND_LIMIT):
            findings.append(
                Finding(
                    api=entry.id,
                    operation=op.id,
                    kind="gone",
                    severity="breaking",
                    detail=f"{op.method} {op.path} answered HTTP {status_code} "
                    f"{obs.gone_streak} times in a row; it may have been removed or moved",
                )
            )

        fresh = [f for f in findings if f.kind not in obs.reported]
        obs.reported = sorted({*obs.reported, *(f.kind for f in fresh)})
        if obs.model_dump(exclude={"updated_at"}) != {
            k: v for k, v in before.items() if k != "updated_at"
        }:
            await self._save(obs)
        for finding in fresh:
            if self.on_finding is not None:
                try:
                    await self.on_finding(finding)
                except Exception:
                    log.exception("reporting a finding for %s failed", finding.api)


def response_shape(data: Any) -> list[str]:
    """Field paths in a JSON value, e.g. ["issues[]", "issues[].key", "total"]."""
    paths: set[str] = set()

    def walk(value: Any, prefix: str, depth: int) -> None:
        if depth > MAX_SHAPE_DEPTH or len(paths) >= MAX_SHAPE_PATHS:
            return
        if isinstance(value, dict):
            for key, child in cast(dict[str, Any], value).items():
                path = f"{prefix}.{key}" if prefix else str(key)
                paths.add(path[:200])
                walk(child, path, depth + 1)
        elif isinstance(value, list):
            items = cast(list[Any], value)
            path = f"{prefix}[]"
            paths.add(path[:200])
            for child in items[:5]:
                walk(child, path, depth + 1)

    walk(data, "", 0)
    return sorted(paths)[:MAX_SHAPE_PATHS]


def _mostly_missing(baseline: list[str], current: list[str]) -> bool:
    # Only compare the top two levels: deeper fields are often optional.
    top = {p for p in baseline if p.count(".") <= 1}
    if not top:
        return False
    missing = top - set(current)
    return len(missing) / len(top) >= SHAPE_MISSING_FRACTION
