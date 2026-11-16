"""The registry: every API librarian knows.

With a database, the database is the registry: entries and every decision people make
about them live there, and take effect on every replica at once. YAML files in the
registry directory are a way in (hand-written entries, a seed), imported into the
database when they are new. Without a database the files are the registry.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import IntegrityError

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


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class StoredEntry(BaseModel):
    """An entry in the database, which is where it lives and is decided."""

    entry: ApiEntry
    # Fingerprint of the registry file this entry was imported from (or, before 0.20,
    # made on top of); None for an API that never had a file.
    base: str | None = None
    # What the entry said when it was imported from that file. While it still says the
    # same, nobody has decided anything here, so an edited file may replace it.
    origin: str | None = None
    # The status to go back to when a blocked entry is unblocked.
    blocked_from: EntryStatus | None = None
    # 1: written before 0.20, when a changed registry file always won over this copy.
    v: int = 1


class Tombstone(BaseModel):
    """What is left of a forgotten API: enough to keep an unchanged file from bringing
    it back."""

    tombstone: bool = True
    id: str
    base: str | None = None  # what its registry file said then (if it had one)
    substance: str | None = None  # what the forgotten entry said


def _content_hash(entry: ApiEntry) -> str:
    return hashlib.sha256(entry.model_dump_json(by_alias=True).encode()).hexdigest()


def recordable(entry: ApiEntry) -> ApiEntry:
    """The entry without what librarian looked up at runtime (the glossary holds names
    read from the API with its credential): the part people decide, and the part that
    is written to the external record."""
    return entry.model_copy(update={"glossary": {}}) if entry.glossary else entry


def substance(entry: ApiEntry) -> str:
    """Fingerprint of what an entry says, whenever it was saved."""
    return _content_hash(recordable(entry).model_copy(update={"updated_at": _EPOCH}))


class Registry:
    """Loads, searches and saves API entries.

    With `store` (the shared database) the database is the source of truth: learned
    APIs, approvals and every later decision are saved there and every replica sees
    them at once. Files in `directory` are imported into it: a new file always; a
    changed file only while nobody has decided anything about that API in librarian.
    Once the database copy holds decisions it wins, and a file that says something
    else is reported (`notices`) instead of applied.

    Without a store the files are the registry: `directory`, plus `overlay` for
    librarian's own changes when `directory` is managed elsewhere; the newer
    `updated_at` wins.

    Invalid files are logged and skipped rather than crashing the server, so one
    bad entry cannot take the librarian down.
    """

    def __init__(
        self, directory: Path, overlay: Path | None = None, store: EntryStore | None = None
    ) -> None:
        self.directory = directory
        self.overlay = overlay
        self.store = store
        self._entries: dict[str, ApiEntry] = {}
        self._from_files: dict[str, ApiEntry] = {}
        self._file_marks: dict[str, tuple[str, str]] = {}  # id -> (fingerprint, substance)
        self._stored: dict[str, StoredEntry] = {}
        self._stored_substance: dict[str, str] = {}
        self._retired: dict[str, Tombstone] = {}
        self._stored_version: str | None = None
        self._errors: dict[str, str] = {}
        self._notices: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._fingerprint: tuple[tuple[str, int, int], ...] = ()

    @property
    def errors(self) -> dict[str, str]:
        return dict(self._errors)

    @property
    def notices(self) -> dict[str, str]:
        """Registry files that say something else than the database, which wins."""
        return dict(self._notices)

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
        log.info("loading the registry from %s", self.directory)
        entries = self._load_dir(self.directory, errors, "")
        if self.overlay is not None:
            for api_id, local in self._load_dir(self.overlay, errors, "overlay/").items():
                reviewed = entries.get(api_id)
                if reviewed is None or local.updated_at > reviewed.updated_at:
                    entries[api_id] = local
        self._from_files = entries
        self._file_marks = (
            {api_id: (_content_hash(e), substance(e)) for api_id, e in entries.items()}
            if self.store is not None
            else {}
        )
        self._errors = {**errors, **{k: v for k, v in self._errors.items() if k.startswith("db/")}}
        self._fingerprint = self._current_fingerprint()
        self._merge()
        log.info("registry loaded: %d APIs, %d invalid files", len(self._entries), len(errors))

    def _substance_of(self, api_id: str) -> str:
        found = self._stored_substance.get(api_id)
        if found is None:
            found = self._stored_substance[api_id] = substance(self._stored[api_id].entry)
        return found

    def _file_action(self, api_id: str) -> str:
        """What a registry file means for the database: `import` it, `keep` the
        database's entry, keep it although the file `differs`, or leave the API
        `forgotten`."""
        if api_id not in self._file_marks:
            self._file_marks[api_id] = (
                _content_hash(self._from_files[api_id]),
                substance(self._from_files[api_id]),
            )
        fingerprint, says = self._file_marks[api_id]
        doc = self._stored.get(api_id)
        if doc is None:
            gone = self._retired.get(api_id)
            if gone is not None and (gone.base in (fingerprint, says) or gone.substance == says):
                return "forgotten"  # the same file that was there when it was forgotten
            return "import"
        if doc.base == fingerprint:
            return "keep"  # this version of the file is already accounted for
        if doc.v < 2:
            return "import"  # before 0.20 a changed file was the live entry
        if doc.origin == says:
            # The file says what it said when it was imported. (Its fingerprint alone
            # cannot tell: a file without `updated_at` gets a new one on every load.)
            return "keep"
        mine = self._substance_of(api_id)
        if mine == says:
            return "keep"
        if doc.origin is not None and mine == doc.origin:
            return "import"  # only the file changed: nothing was decided here since
        return "differs"

    def _merge(self) -> None:
        if self.store is None:
            self._entries = dict(self._from_files)
            return
        entries: dict[str, ApiEntry] = {}
        notices: dict[str, str] = {}
        for api_id, from_file in self._from_files.items():
            action = self._file_action(api_id)
            if action == "import":
                entries[api_id] = from_file  # saved to the database by `import_files`
            elif action == "differs":
                notices[api_id] = (
                    f"{api_id}.yaml changed on disk, but decisions were made about this API "
                    "in librarian since it was imported: the database's entry is used and "
                    "the file is ignored (forget the API to import the file again)"
                )
        for api_id, doc in self._stored.items():
            if api_id not in entries:
                entries[api_id] = doc.entry
        self._entries, self._notices = entries, notices

    async def _put(self, doc: StoredEntry) -> None:
        if self.store is None:
            raise RegistryError("no database is configured")
        await self.store.save_doc(
            STORED_KIND, doc.entry.id, "active", doc.model_dump_json(by_alias=True)
        )
        self._stored[doc.entry.id] = doc
        self._stored_substance.pop(doc.entry.id, None)
        self._retired.pop(doc.entry.id, None)
        self._entries[doc.entry.id] = doc.entry
        self._notices.pop(doc.entry.id, None)

    async def import_files(self) -> list[str]:
        """Bring registry files into the database (see the class notes for which).
        Returns the ids imported. Does nothing without a database."""
        if self.store is None:
            return []
        imported: list[str] = []
        for api_id, from_file in list(self._from_files.items()):
            if self._file_action(api_id) != "import":
                continue
            fingerprint, says = self._file_marks[api_id]
            doc = StoredEntry(entry=from_file, base=fingerprint, origin=says, v=2)
            try:
                await self._put(doc)
            except IntegrityError:  # another replica is importing the same file
                continue
            imported.append(api_id)
        if imported:
            log.info("imported %d registry file(s) into the database: %s", len(imported), imported)
        return imported

    async def refresh_stored(self, force: bool = False) -> bool:
        """Pick up entries other replicas saved. Cheap when nothing changed."""
        if self.store is None:
            return False
        version = await self.store.docs_version(STORED_KIND)
        if version == self._stored_version and not force:
            return False
        stored: dict[str, StoredEntry] = {}
        errors = {k: v for k, v in self._errors.items() if not k.startswith("db/")}
        for data in await self.store.list_docs(STORED_KIND, "active"):
            try:
                doc = StoredEntry.model_validate_json(data)
            except ValidationError as exc:
                errors[f"db/{data[:40]}"] = str(exc)
                continue
            stored[doc.entry.id] = doc
        retired: dict[str, Tombstone] = {}
        for data in await self.store.list_docs(STORED_KIND, "retired"):
            gone = _tombstone(data)
            if gone is not None and gone.id not in stored:
                retired[gone.id] = gone
        self._stored, self._retired = stored, retired
        self._stored_substance = {}
        self._stored_version, self._errors = version, errors
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
            await self._put(self._doc_for(entry))
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

    def _doc_for(self, entry: ApiEntry) -> StoredEntry:
        """The database document for a changed entry, remembering which file (if any)
        it came from."""
        doc = self._stored.get(entry.id)
        blocked_from = doc.blocked_from if doc is not None else None
        if doc is not None and doc.v >= 2:
            return StoredEntry(
                entry=entry, base=doc.base, origin=doc.origin, blocked_from=blocked_from, v=2
            )
        if entry.id in self._from_files:
            # Not imported yet, or a pre-0.20 copy: the file is what it was made from.
            self._file_action(entry.id)
            fingerprint, says = self._file_marks[entry.id]
            return StoredEntry(
                entry=entry, base=fingerprint, origin=says, blocked_from=blocked_from, v=2
            )
        base = doc.base if doc is not None else None
        return StoredEntry(entry=entry, base=base, blocked_from=blocked_from, v=2)

    async def set_status(self, api_id: str, status: EntryStatus) -> ApiEntry:
        entry = self.get(api_id)
        if entry is None:
            raise RegistryError(f"unknown API {api_id!r}")
        updated = entry.model_copy(update={"status": status})
        await self.save(updated)
        return self.get(api_id) or updated

    async def block(self, api_id: str) -> ApiEntry:
        """Refuse the API to agents, remembering the status to return to."""
        entry = self.get(api_id)
        if entry is None:
            raise RegistryError(f"unknown API {api_id!r}")
        if entry.status is EntryStatus.BLOCKED:
            return entry
        blocked = entry.model_copy(
            update={"status": EntryStatus.BLOCKED, "updated_at": datetime.now(UTC)}
        )
        if self.store is None:
            await self.save(blocked)
            return self.get(api_id) or blocked
        doc = self._doc_for(blocked)
        await self._put(doc.model_copy(update={"blocked_from": entry.status}))
        return blocked

    async def unblock(self, api_id: str) -> ApiEntry:
        """Back to the status the API had before it was blocked (provisional when that
        is not known: an approver can approve it again)."""
        entry = self.get(api_id)
        if entry is None:
            raise RegistryError(f"unknown API {api_id!r}")
        status = EntryStatus.PROVISIONAL
        doc = self._stored.get(api_id)
        from_file = self._from_files.get(api_id)
        if doc is not None and doc.blocked_from is not None:
            status = doc.blocked_from
        elif from_file is not None and from_file.status is not EntryStatus.BLOCKED:
            status = from_file.status  # blocked before 0.20, or without a database
        restored = entry.model_copy(update={"status": status, "updated_at": datetime.now(UTC)})
        if self.store is None:
            await self.save(restored)
            return self.get(api_id) or restored
        await self._put(self._doc_for(restored).model_copy(update={"blocked_from": None}))
        return restored

    def approved(self, api_id: str) -> bool:
        """A person approved this API at some point: it is verified, or was when it
        was blocked. Only approved entries go to the external record."""
        entry = self.get(api_id)
        if entry is None:
            return False
        if entry.status is not EntryStatus.BLOCKED:
            return entry.status is EntryStatus.VERIFIED
        doc = self._stored.get(api_id)
        if doc is not None and doc.blocked_from is not None:
            return doc.blocked_from is EntryStatus.VERIFIED
        from_file = self._from_files.get(api_id)
        return from_file is not None and from_file.status is EntryStatus.VERIFIED

    def reviewed(self, api_id: str) -> ApiEntry | None:
        """The entry as a file in the registry directory has it (None when there is no
        such file)."""
        path = self.directory / f"{api_id}.yaml"
        if not path.exists():
            return None
        return self._from_files.get(api_id)

    def can_forget(self, api_id: str) -> bool:
        """With a database any API can be forgotten. Without one, an entry that is a
        file in the registry directory is removed by deleting the file."""
        return self.store is not None or self.reviewed(api_id) is None

    async def forget(self, api_id: str) -> None:
        """Drop an entry. With a database: retire it there (a registry file for it is
        ignored until that file changes). Without: delete its overlay file."""
        entry = self._entries.get(api_id)
        if entry is None:
            raise RegistryError(f"unknown API {api_id!r}")
        if self.store is not None:
            doc = self._stored.get(api_id)
            base = doc.base if doc is not None else None
            if api_id in self._from_files:
                self._file_action(api_id)
                # What the file says, not its fingerprint: a hand-written file without
                # `updated_at` gets a new one on every load.
                base = self._file_marks[api_id][1]
            gone = Tombstone(id=api_id, base=base, substance=substance(entry))
            await self.store.save_doc(STORED_KIND, api_id, "retired", gone.model_dump_json())
            self._stored.pop(api_id, None)
            self._stored_substance.pop(api_id, None)
            self._retired[api_id] = gone
            self._entries.pop(api_id, None)
            self._notices.pop(api_id, None)
            return
        if self.reviewed(api_id) is not None:
            raise RegistryError(
                f"{api_id!r} is a file in the registry directory; delete the file to remove it"
            )
        if self.overlay is None:
            raise RegistryError(f"{api_id!r} is a file in the registry directory; delete it there")
        async with self._lock:
            path = self.overlay / f"{api_id}.yaml"
            await asyncio.to_thread(path.unlink, True)
            self._from_files.pop(api_id, None)
            self._entries.pop(api_id, None)
            self._fingerprint = self._current_fingerprint()


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


def _tombstone(data: str) -> Tombstone | None:
    """A retired document: a tombstone, or (before 0.20) the whole forgotten entry."""
    try:
        raw: Any = json.loads(data)
        if isinstance(raw, dict) and "entry" in raw:
            doc = StoredEntry.model_validate(raw)
            return Tombstone(id=doc.entry.id, base=doc.base, substance=substance(doc.entry))
        return Tombstone.model_validate(raw)
    except (ValueError, ValidationError):
        return None


def load_yaml(text: str) -> Any:
    """Parse YAML safely with libyaml when the wheel has it: PyYAML's pure-Python
    parser takes seconds per megabyte, and a large API's entry is a megabyte."""
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    return yaml.load(text, Loader=loader)  # noqa: S506 - a safe loader either way


def load_entry(path: Path) -> ApiEntry:
    data: Any = load_yaml(path.read_text("utf-8"))
    if not isinstance(data, dict):
        raise RegistryError(f"{path.name}: expected a mapping at the top level")
    entry = ApiEntry.model_validate(data)
    if path.stem != entry.id:
        raise RegistryError(f"{path.name}: file name must match id {entry.id!r}")
    return entry


def dump_entry(entry: ApiEntry) -> str:
    data = entry.model_dump(mode="json", by_alias=True, exclude_defaults=False)
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
