"""`librarian doctor`: look for credentials where they must never be.

librarian keeps credentials in the secret store and nowhere else. Versions before 0.20
could let one into the database: an API that sent its key back in the answer to a
write, or an HTTP client error that quoted a key. Nothing rewrites those rows. This
looks through every table of the database, and the audit file, for every credential in
the secret store, in each form it can take, and can replace what it finds with
`[redacted]`. It also says which stored credentials cannot be used at all.

It never prints or returns a credential: a finding names the secret by its reference.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from sqlalchemy import MetaData, Table, and_, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from librarian.executor import (
    REDACTED,
    credential_forms,
    rewrite_strings,
    trimmed,
    unsendable,
)
from librarian.oauth import principal_key
from librarian.registry.models import AuthSpec, NoAuth

if TYPE_CHECKING:
    from librarian.service import Librarian

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Finding:
    # stored: a credential is somewhere it must not be. unusable: a stored credential
    # that cannot be sent. unreadable: a secret the store could not hand over.
    kind: Literal["stored", "unusable", "unreadable"]
    secret: str  # its reference, e.g. tracker/write: never its value
    where: str
    redacted: bool = False


@dataclass
class Report:
    secrets: int = 0
    rows: int = 0
    # False when the store could not list its secrets: then only those the registry
    # and the configuration name were looked for.
    listed: bool = True
    findings: list[Finding] = field(default_factory=list[Finding])

    @property
    def open(self) -> list[Finding]:
        """What still needs someone's attention."""
        return [f for f in self.findings if not f.redacted]


async def examine(librarian: Librarian, *, redact: bool = False) -> Report:
    report = Report()
    needles = await _needles(librarian, report)
    if librarian.engine is not None:
        await _examine_database(librarian.engine, needles, report, redact)
    audit_file = librarian.settings.audit_file
    if audit_file is not None:
        report.findings += await asyncio.to_thread(_examine_file, audit_file, needles)
    return report


async def _needles(librarian: Librarian, report: Report) -> dict[str, str]:
    """Every form of every credential, mapped to the reference of the secret it is from."""
    how_sent: dict[str, list[AuthSpec]] = {}
    for entry in librarian.registry.all():
        for auth in (entry.auth, entry.write_auth):
            if auth is None or isinstance(auth, NoAuth):
                continue
            for name in ("secret_ref", "client_secret_ref"):
                ref = getattr(auth, name, None)
                if isinstance(ref, str) and ref:
                    how_sent.setdefault(ref, []).append(auth)
    references = set(how_sent)
    references.add(librarian.settings.signing_key_ref)
    references.update(caller.key_ref for caller in librarian.settings.callers)
    for connection in await librarian.consents.connections():
        references.add(principal_key(connection.api, connection.principal))
    try:
        references.update(await librarian.secrets.list_names())
    except Exception as exc:  # e.g. no permission to list: look for the ones we know of
        report.listed = False
        log.warning("the secret store could not list its secrets (%s)", type(exc).__name__)

    needles: dict[str, str] = {}
    for ref in sorted(references):
        try:
            stored = await librarian.secrets.get(ref)
        except Exception as exc:
            report.findings.append(
                Finding("unreadable", ref, f"the secret store failed ({type(exc).__name__})")
            )
            continue
        if stored is None:
            continue
        report.secrets += 1
        value = trimmed(stored)
        for auth in how_sent.get(ref, []):
            if not value:
                report.findings.append(Finding("unusable", ref, "it is empty"))
            elif unsendable(value, auth):
                report.findings.append(
                    Finding(
                        "unusable",
                        ref,
                        "it contains a line break or another character that cannot be sent "
                        "in an HTTP header: store it again",
                    )
                )
        for part in _parts(value):
            for auth in how_sent.get(ref) or [None]:
                for form in credential_forms(part, auth):
                    needles.setdefault(form, ref)
    return dict(sorted(needles.items(), key=lambda item: (-len(item[0]), item[0])))


def _parts(value: str) -> list[str]:
    """The credentials in a stored value: the value itself, or, for a person's sign-in,
    the tokens in it.

    Nothing else is taken apart. A secret holding several keys is reached through the
    references the registry uses (`service/jira#api_token`), which name the key; guessing
    which other strings in a document are credentials would have `--redact` replace
    ordinary words (a sign-in also holds the word "Bearer").
    """
    try:
        document: Any = json.loads(value)
    except ValueError:
        return [value]
    if not isinstance(document, dict) or "access_token" not in document:
        return [value]
    tokens = cast(dict[str, Any], document)
    return [
        token
        for token in (tokens.get("access_token"), tokens.get("refresh_token"))
        if isinstance(token, str) and token
    ]


def _clean_text(text: str, needles: dict[str, str]) -> tuple[str, set[str]]:
    found: set[str] = set()
    for form, ref in needles.items():
        if form in text:
            found.add(ref)
            text = text.replace(form, REDACTED)
    return text, found


def _clean(value: Any, needles: dict[str, str]) -> tuple[Any, set[str], bool]:
    """A cell without credentials, the secrets found in it, and whether the cell can be
    rewritten (binary data is only reported)."""
    if isinstance(value, str):
        text, found = _clean_text(value, needles)
        return text, found, True
    if isinstance(value, bytes | bytearray | memoryview):
        _, found = _clean_text(bytes(value).decode("utf-8", errors="ignore"), needles)
        return value, found, False
    if isinstance(value, dict | list):
        found_in: set[str] = set()

        def clean(text: str) -> str:
            cleaned, found = _clean_text(text, needles)
            found_in.update(found)
            return cleaned

        return rewrite_strings(copy.deepcopy(cast(Any, value)), clean), found_in, True
    return value, set(), True


async def _examine_database(
    engine: AsyncEngine, needles: dict[str, str], report: Report, redact: bool
) -> None:
    def reflect(connection: Connection) -> list[Table]:
        metadata = MetaData()
        metadata.reflect(bind=connection)
        return list(metadata.tables.values())

    async with engine.connect() as connection:
        tables = await connection.run_sync(reflect)
    for table in sorted(tables, key=lambda t: t.name):
        keys = list(table.primary_key.columns)
        changes: list[tuple[dict[str, Any], dict[str, Any]]] = []
        async with engine.connect() as connection:
            result = await connection.stream(select(table))
            async for row in result:
                report.rows += 1
                values = row._mapping  # pyright: ignore[reportPrivateUsage]
                identity = {key.name: values[key.name] for key in keys}
                label = "/".join(str(v) for v in identity.values()) or "(a row without a key)"
                change: dict[str, Any] = {}
                for column in table.columns:
                    cleaned, found, rewritable = _clean(values[column.name], needles)
                    can_fix = redact and rewritable and bool(keys)
                    if found and can_fix:
                        change[column.name] = cleaned
                    for ref in sorted(found):
                        where = f"{table.name} {label} ({column.name})"
                        report.findings.append(Finding("stored", ref, where, can_fix))
                if change:
                    changes.append((identity, change))
        # Rewritten after the table has been read: reading and writing one table at the
        # same moment locks SQLite.
        if changes:
            async with engine.begin() as connection:
                for identity, change in changes:
                    match = and_(*(table.c[name] == value for name, value in identity.items()))
                    await connection.execute(update(table).where(match).values(**change))


def _examine_file(path: Path, needles: dict[str, str]) -> list[Finding]:
    """The audit file is read, never rewritten: librarian appends to it while it runs."""
    findings: list[Finding] = []
    try:
        with path.open(encoding="utf-8", errors="ignore") as lines:
            for number, line in enumerate(lines, start=1):
                _, found = _clean_text(line, needles)
                findings += [
                    Finding("stored", ref, f"{path}, line {number}") for ref in sorted(found)
                ]
    except FileNotFoundError:
        pass
    return findings
