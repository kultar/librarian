"""File-backed registry: one YAML file per API in a directory (usually a git repo)."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
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

    `directory` is the reviewed registry (often a git checkout kept current by
    git-sync). `overlay`, if set, is where the librarian writes its own changes
    (learned entries, blocks, write enablement) so they survive git-sync replacing
    the checkout. For each API the entry with the newest `updated_at` wins; on a tie
    the reviewed one wins, so a reviewer's merge takes effect.

    Invalid files are logged and skipped rather than crashing the server, so one
    bad community entry cannot take the librarian down.
    """

    def __init__(self, directory: Path, overlay: Path | None = None) -> None:
        self.directory = directory
        self.overlay = overlay
        self._entries: dict[str, ApiEntry] = {}
        self._errors: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._fingerprint: tuple[tuple[str, int, int], ...] = ()

    @property
    def errors(self) -> dict[str, str]:
        return dict(self._errors)

    @property
    def _directories(self) -> list[Path]:
        return [self.directory] if self.overlay is None else [self.directory, self.overlay]

    def _load_dir(self, directory: Path, errors: dict[str, str], label: str) -> dict[str, ApiEntry]:
        entries: dict[str, ApiEntry] = {}
        if not directory.exists():
            return entries
        for path in sorted(directory.glob("*.y*ml")):
            name = f"{label}{path.name}"
            try:
                entry = load_entry(path)
            except (RegistryError, ValidationError, yaml.YAMLError) as exc:
                errors[name] = str(exc)
                log.warning("skipping invalid registry file %s: %s", name, exc)
                continue
            if entry.id in entries:
                errors[name] = f"duplicate API id {entry.id!r}"
                continue
            entries[entry.id] = entry
        return entries

    def load(self) -> None:
        errors: dict[str, str] = {}
        entries = self._load_dir(self.directory, errors, "")
        if self.overlay is not None:
            for api_id, local in self._load_dir(self.overlay, errors, "overlay/").items():
                reviewed = entries.get(api_id)
                if reviewed is None or local.updated_at > reviewed.updated_at:
                    entries[api_id] = local
        self._entries = entries
        self._errors = errors
        self._fingerprint = self._current_fingerprint()
        log.info("registry loaded: %d APIs, %d invalid files", len(entries), len(errors))

    def _current_fingerprint(self) -> tuple[tuple[str, int, int], ...]:
        stats: list[tuple[str, int, int]] = []
        for directory in self._directories:
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.y*ml")):
                try:
                    st = path.stat()
                except FileNotFoundError:
                    continue
                stats.append((str(path), st.st_mtime_ns, st.st_size))
        return tuple(stats)

    def reload_if_changed(self) -> bool:
        """Reload when files were added, removed or edited (e.g. by git-sync)."""
        if self._current_fingerprint() == self._fingerprint:
            return False
        self.load()
        return True

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
        """Write an entry (to the overlay when configured), stamping `updated_at`."""
        async with self._lock:
            target = self.overlay or self.directory
            target.mkdir(parents=True, exist_ok=True)
            entry = entry.model_copy(update={"updated_at": datetime.now(UTC)})
            path = target / f"{entry.id}.yaml"
            text = dump_entry(entry)
            await asyncio.to_thread(path.write_text, text, "utf-8")
            self._entries[entry.id] = entry
            self._errors.pop(path.name, None)
            self._fingerprint = self._current_fingerprint()  # our own write is not a change
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
