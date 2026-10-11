"""File-backed registry: one YAML file per API in a directory (usually a git repo)."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel, ValidationError

from librarian.registry.models import Access, ApiEntry, EntryStatus

log = logging.getLogger(__name__)

_WORD = re.compile(r"[a-z0-9]+")


class RegistryError(Exception):
    pass


@dataclass(frozen=True)
class SearchHit:
    entry: ApiEntry
    score: float


class EntryStore(Protocol):
    """Shared storage for the librarian's own changes (see `librarian.state.StateStore`)."""

    async def save_doc(self, kind: str, doc_id: str, state: str, data: str) -> None: ...
    async def list_docs(self, kind: str, state: str | None = None) -> list[str]: ...
    async def docs_version(self, kind: str) -> str: ...


STORED_KIND = "registry-entry"


class StoredEntry(BaseModel):
    entry: ApiEntry
    # Fingerprint of the reviewed (git) version this copy was made from; None when the
    # API was learned and has no reviewed version yet.
    base: str | None = None


def _content_hash(entry: ApiEntry) -> str:
    return hashlib.sha256(entry.model_dump_json(by_alias=True).encode()).hexdigest()


class Registry:
    """Loads, searches and saves API entries.

    `directory` is the reviewed registry (often a git checkout kept current by
    git-sync). The librarian's own changes (learned entries, blocks, write
    enablement, glossaries) go to the shared database when `store` is set, so every
    replica sees an API the moment another one learned it; otherwise to `overlay`
    (files), which suits a single instance. For each API the entry with the newest
    `updated_at` wins; on a tie the reviewed one wins, so a reviewer's merge takes
    effect.

    Invalid files are logged and skipped rather than crashing the server, so one
    bad community entry cannot take the librarian down.
    """

    def __init__(
        self, directory: Path, overlay: Path | None = None, store: EntryStore | None = None
    ) -> None:
        self.directory = directory
        self.overlay = overlay
        self.store = store
        self._entries: dict[str, ApiEntry] = {}
        self._from_files: dict[str, ApiEntry] = {}
        self._stored: dict[str, tuple[ApiEntry, str | None]] = {}
        self._stored_version: str | None = None
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
        self._from_files = entries
        self._errors = {**errors, **{k: v for k, v in self._errors.items() if k.startswith("db/")}}
        self._fingerprint = self._current_fingerprint()
        self._merge()
        log.info("registry loaded: %d APIs, %d invalid files", len(self._entries), len(errors))

    def _merge(self) -> None:
        entries = dict(self._from_files)
        for api_id, (stored, base) in self._stored.items():
            reviewed = entries.get(api_id)
            # The librarian's copy applies on top of the reviewed version it was made from.
            # Any change a reviewer merges since (status: verified, fixes, ...) wins.
            if reviewed is None or base == _content_hash(reviewed):
                entries[api_id] = stored
        self._entries = entries

    async def refresh_stored(self, force: bool = False) -> bool:
        """Pick up entries other replicas saved. Cheap when nothing changed."""
        if self.store is None:
            return False
        version = await self.store.docs_version(STORED_KIND)
        if version == self._stored_version and not force:
            return False
        stored: dict[str, tuple[ApiEntry, str | None]] = {}
        errors = {k: v for k, v in self._errors.items() if not k.startswith("db/")}
        for data in await self.store.list_docs(STORED_KIND, "active"):
            try:
                doc = StoredEntry.model_validate_json(data)
            except ValidationError as exc:
                errors[f"db/{data[:40]}"] = str(exc)
                continue
            stored[doc.entry.id] = (doc.entry, doc.base)
        self._stored, self._stored_version, self._errors = stored, version, errors
        self._merge()
        return True

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
        """Save the librarian's change to an entry, stamping `updated_at`: to the shared
        database when there is one, else to the overlay (or registry) directory. Returns
        where it went (a `db:` pseudo-path for the database)."""
        if self.store is not None:
            entry = entry.model_copy(update={"updated_at": datetime.now(UTC)})
            reviewed = self._from_files.get(entry.id)
            base = _content_hash(reviewed) if reviewed is not None else None
            doc = StoredEntry(entry=entry, base=base)
            await self.store.save_doc(
                STORED_KIND, entry.id, "active", doc.model_dump_json(by_alias=True)
            )
            self._stored[entry.id] = (entry, base)
            self._entries[entry.id] = entry
            return Path(f"db:{STORED_KIND}/{entry.id}")
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
