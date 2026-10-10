"""File-backed registry: one YAML file per API in a directory (usually a git repo)."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from librarian.registry.models import Access, ApiEntry, EntryStatus

log = logging.getLogger(__name__)

_WORD = re.compile(r"[a-z0-9]+")


class RegistryError(Exception):
    pass


@dataclass(frozen=True)
class SearchHit:
    entry: ApiEntry
    score: float


class Registry:
    """Loads, searches and saves API entries.

    Invalid files are logged and skipped rather than crashing the server, so one
    bad community entry cannot take the librarian down.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._entries: dict[str, ApiEntry] = {}
        self._errors: dict[str, str] = {}
        self._lock = asyncio.Lock()

    @property
    def errors(self) -> dict[str, str]:
        return dict(self._errors)

    def load(self) -> None:
        entries: dict[str, ApiEntry] = {}
        errors: dict[str, str] = {}
        if self.directory.exists():
            for path in sorted(self.directory.glob("*.y*ml")):
                try:
                    entry = load_entry(path)
                except (RegistryError, ValidationError, yaml.YAMLError) as exc:
                    errors[path.name] = str(exc)
                    log.warning("skipping invalid registry file %s: %s", path.name, exc)
                    continue
                if entry.id in entries:
                    errors[path.name] = f"duplicate API id {entry.id!r}"
                    continue
                entries[entry.id] = entry
        self._entries = entries
        self._errors = errors
        log.info("registry loaded: %d APIs, %d invalid files", len(entries), len(errors))

    def all(self) -> list[ApiEntry]:
        return sorted(self._entries.values(), key=lambda e: e.id)

    def get(self, api_id: str) -> ApiEntry | None:
        return self._entries.get(api_id)

    def find_by_host(self, host: str) -> ApiEntry | None:
        host = host.lower()
        return next((e for e in self._entries.values() if e.host.lower() == host), None)

    def search(self, query: str, limit: int = 5) -> list[SearchHit]:
        """Keyword search over names, descriptions and operation summaries."""
        terms = set(_WORD.findall(query.lower()))
        if not terms:
            return []
        hits: list[SearchHit] = []
        for entry in self._entries.values():
            if entry.status is EntryStatus.BLOCKED:
                continue
            score = _score(terms, entry)
            if score > 0:
                hits.append(SearchHit(entry, score))
        hits.sort(key=lambda h: (-h.score, h.entry.id))
        return hits[:limit]

    async def save(self, entry: ApiEntry) -> Path:
        async with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / f"{entry.id}.yaml"
            text = dump_entry(entry)
            await asyncio.to_thread(path.write_text, text, "utf-8")
            self._entries[entry.id] = entry
            self._errors.pop(path.name, None)
            return path

    async def set_status(self, api_id: str, status: EntryStatus) -> ApiEntry:
        entry = self.get(api_id)
        if entry is None:
            raise RegistryError(f"unknown API {api_id!r}")
        updated = entry.model_copy(update={"status": status})
        await self.save(updated)
        return updated


def _score(terms: set[str], entry: ApiEntry) -> float:
    def words(text: str) -> set[str]:
        return set(_WORD.findall(text.lower()))

    score = 0.0
    score += 5 * len(terms & (words(entry.id) | words(entry.name)))
    score += 2 * len(terms & words(entry.description))
    score += 1 * len(terms & words(entry.host))
    op_words: set[str] = set()
    for op in entry.operations:
        if op.access is Access.READ:
            op_words |= words(op.id) | words(op.summary)
    score += 0.5 * len(terms & op_words)
    return score


def load_entry(path: Path) -> ApiEntry:
    data: Any = yaml.safe_load(path.read_text("utf-8"))
    if not isinstance(data, dict):
        raise RegistryError(f"{path.name}: expected a mapping at the top level")
    entry = ApiEntry.model_validate(data)
    if path.stem != entry.id:
        raise RegistryError(f"{path.name}: file name must match id {entry.id!r}")
    return entry


def dump_entry(entry: ApiEntry) -> str:
    data = entry.model_dump(mode="json", by_alias=True, exclude_defaults=False)
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
