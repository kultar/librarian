"""Secret stores.

The librarian only ever handles *references* to secrets (e.g. `jira/readonly`).
Values are resolved at the moment an outbound request is made and are never
placed in messages, logs, LLM context or registry files.
"""

from __future__ import annotations

import asyncio
import os
import re
import stat
from pathlib import Path
from typing import Protocol, runtime_checkable

from librarian.registry.models import SECRET_REF


class SecretStoreError(Exception):
    pass


@runtime_checkable
class SecretStore(Protocol):
    async def get(self, ref: str) -> str | None:
        """Return the secret value, or None if it does not exist."""
        ...

    async def put(self, ref: str, value: str) -> None:
        """Create or replace a secret."""
        ...

    async def list_names(self) -> list[str]:
        """List secret references (names only, never values)."""
        ...

    @property
    def writable(self) -> bool: ...


def _check_ref(ref: str) -> None:
    if not SECRET_REF.match(ref) or ".." in ref:
        raise SecretStoreError(f"invalid secret reference {ref!r}")


class EnvSecretStore:
    """Reads secrets from environment variables (read-only).

    `jira/readonly` maps to `LIBRARIAN_SECRET_JIRA_READONLY`.
    """

    def __init__(self, prefix: str = "LIBRARIAN_SECRET_") -> None:
        self.prefix = prefix

    def env_name(self, ref: str) -> str:
        _check_ref(ref)
        return self.prefix + re.sub(r"[^A-Z0-9]", "_", ref.upper())

    async def get(self, ref: str) -> str | None:
        value = os.environ.get(self.env_name(ref))
        return value or None

    async def put(self, ref: str, value: str) -> None:
        raise SecretStoreError("the environment secret store is read-only")

    async def list_names(self) -> list[str]:
        names = [
            key[len(self.prefix) :].lower().replace("_", "/", 1)
            for key in os.environ
            if key.startswith(self.prefix)
        ]
        return sorted(names)

    @property
    def writable(self) -> bool:
        return False


class FileSecretStore:
    """One file per secret under a directory, created with 0600 permissions.

    Suitable for local mode and for a Kubernetes volume. `jira/readonly` is
    stored at `<dir>/jira/readonly`.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, ref: str) -> Path:
        _check_ref(ref)
        path = (self.directory / ref).resolve()
        if not path.is_relative_to(self.directory.resolve()):
            raise SecretStoreError(f"secret reference escapes the store: {ref!r}")
        return path

    async def get(self, ref: str) -> str | None:
        path = self._path(ref)

        def read() -> str | None:
            if not path.is_file():
                return None
            return path.read_text("utf-8").strip() or None

        return await asyncio.to_thread(read)

    async def put(self, ref: str, value: str) -> None:
        path = self._path(ref)

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(value)

        await asyncio.to_thread(write)

    async def list_names(self) -> list[str]:
        def scan() -> list[str]:
            if not self.directory.exists():
                return []
            root = self.directory.resolve()
            return sorted(
                p.resolve().relative_to(root).as_posix()
                for p in root.rglob("*")
                if p.is_file() and not p.name.startswith(".")
            )

        return await asyncio.to_thread(scan)

    @property
    def writable(self) -> bool:
        return True


class ChainSecretStore:
    """Tries several stores in order; writes go to the first writable one."""

    def __init__(self, stores: list[SecretStore]) -> None:
        self.stores = stores

    async def get(self, ref: str) -> str | None:
        for store in self.stores:
            value = await store.get(ref)
            if value is not None:
                return value
        return None

    async def put(self, ref: str, value: str) -> None:
        for store in self.stores:
            if store.writable:
                await store.put(ref, value)
                return
        raise SecretStoreError("no writable secret store is configured")

    async def list_names(self) -> list[str]:
        names: set[str] = set()
        for store in self.stores:
            names.update(await store.list_names())
        return sorted(names)

    @property
    def writable(self) -> bool:
        return any(store.writable for store in self.stores)
