"""Secret stores.

The librarian only ever handles *references* to secrets (e.g. `jira/readonly`).
Values are resolved at the moment an outbound request is made and are never
placed in messages, logs, LLM context or registry files.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

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


class AwsSecretsManagerStore:
    """AWS Secrets Manager (install the `aws` extra).

    `jira/readonly` maps to the secret named `<prefix>jira/readonly`. A `#key`
    suffix selects one key of a JSON secret (`service/jira#api_token`), which
    fits organizations that keep one JSON secret per service. Values are cached
    briefly to limit API calls; writes invalidate the cache.
    """

    def __init__(
        self,
        prefix: str = "",
        region: str | None = None,
        cache_seconds: float = 60,
        client: Any = None,
    ) -> None:
        if client is None:
            import boto3  # pyright: ignore[reportMissingTypeStubs]

            client = boto3.client("secretsmanager", region_name=region)  # pyright: ignore[reportUnknownMemberType]
        self.client: Any = client
        self.prefix = prefix
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, str | None]] = {}

    def _split(self, ref: str) -> tuple[str, str | None]:
        _check_ref(ref)
        name, _, key = ref.partition("#")
        return self.prefix + name, key or None

    def _fetch(self, secret_id: str) -> str | None:
        cached = self._cache.get(secret_id)
        if cached is not None and time.monotonic() - cached[0] < self.cache_seconds:
            return cached[1]
        try:
            response = self.client.get_secret_value(SecretId=secret_id)
            value: str | None = response.get("SecretString")
        except Exception as exc:
            if _aws_code(exc) == "ResourceNotFoundException":
                value = None
            else:
                raise SecretStoreError(
                    f"cannot read secret {secret_id!r}: {_aws_code(exc)}"
                ) from exc
        self._cache[secret_id] = (time.monotonic(), value)
        return value

    async def get(self, ref: str) -> str | None:
        secret_id, key = self._split(ref)
        raw = await asyncio.to_thread(self._fetch, secret_id)
        if raw is None or key is None:
            return raw or None
        try:
            document: Any = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(document, dict):
            return None
        value = cast(dict[str, Any], document).get(key)
        return str(value) if value not in (None, "") else None

    async def put(self, ref: str, value: str) -> None:
        secret_id, key = self._split(ref)

        def write() -> None:
            payload = value
            if key is not None:
                current = self._fetch(secret_id)
                document: dict[str, Any] = {}
                if current:
                    try:
                        loaded: Any = json.loads(current)
                        if isinstance(loaded, dict):
                            document = cast(dict[str, Any], loaded)
                    except ValueError:
                        pass
                document[key] = value
                payload = json.dumps(document)
            try:
                self.client.put_secret_value(SecretId=secret_id, SecretString=payload)
            except Exception as exc:
                if _aws_code(exc) != "ResourceNotFoundException":
                    raise SecretStoreError(
                        f"cannot write secret {secret_id!r}: {_aws_code(exc)}"
                    ) from exc
                self.client.create_secret(
                    Name=secret_id,
                    SecretString=payload,
                    Description="Created by librarian",
                    Tags=[{"Key": "managed-by", "Value": "librarian"}],
                )
            self._cache.pop(secret_id, None)

        await asyncio.to_thread(write)

    async def list_names(self) -> list[str]:
        def scan() -> list[str]:
            kwargs: dict[str, Any] = {}
            if self.prefix:
                kwargs["Filters"] = [{"Key": "name", "Values": [self.prefix]}]
            names: list[str] = []
            for page in self.client.get_paginator("list_secrets").paginate(**kwargs):
                for item in page.get("SecretList", []):
                    name = str(item.get("Name", ""))
                    if name.startswith(self.prefix):
                        ref = name[len(self.prefix) :]
                        if SECRET_REF.match(ref):
                            names.append(ref)
            return sorted(names)

        return await asyncio.to_thread(scan)

    @property
    def writable(self) -> bool:
        return True


def _aws_code(exc: Exception) -> str:
    response: Any = getattr(exc, "response", None)
    if isinstance(response, dict):
        error: Any = cast(dict[str, Any], response).get("Error", {})
        if isinstance(error, dict):
            return str(cast(dict[str, Any], error).get("Code", type(exc).__name__))
    return type(exc).__name__
