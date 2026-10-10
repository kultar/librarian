from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from librarian.audit import MemoryAuditSink
from librarian.config import Settings
from librarian.credentials import CredentialRequest, Notifier
from librarian.secrets import FileSecretStore
from librarian.service import Librarian

BASE = "https://api.example.test"

TRACKER: dict[str, Any] = {
    "id": "tracker",
    "name": "Issue Tracker",
    "description": "Issues, projects and users for the example issue tracker.",
    "base_url": BASE + "/v1",
    "status": "verified",
    "auth": {"type": "bearer", "secret_ref": "tracker/readonly"},
    "write_auth": {"type": "bearer", "secret_ref": "tracker/write"},
    "credential": {
        "scopes": ["read:issues"],
        "create_url": "https://tracker.example.test/settings/tokens",
        "validation_operation": "whoami",
    },
    "owners": {"channel": "#tracker-admins", "approver_group": "S0TRACKER"},
    "operations": [
        {"id": "whoami", "method": "GET", "path": "/me", "access": "read"},
        {
            "id": "getIssue",
            "method": "GET",
            "path": "/issues/{key}",
            "summary": "Get one issue",
            "access": "read",
            "parameters": [{"name": "key", "in": "path"}],
        },
        {
            "id": "listIssues",
            "method": "GET",
            "path": "/issues",
            "summary": "List issues for a team",
            "access": "read",
            "parameters": [
                {"name": "team", "in": "query"},
                {"name": "state", "in": "query"},
                {"name": "X-Trace", "in": "header"},
            ],
        },
        {
            "id": "searchIssues",
            "method": "POST",
            "path": "/search",
            "access": "read",
            "read_basis": "reviewed",
            "accepts_body": True,
        },
        {
            "id": "deleteIssue",
            "method": "DELETE",
            "path": "/issues/{key}",
            "access": "write",
            "writable": True,
            "parameters": [{"name": "key", "in": "path"}],
        },
        {
            "id": "addComment",
            "method": "POST",
            "path": "/issues/{key}/comments",
            "summary": "Add a comment to an issue",
            "access": "write",
            "writable": True,
            "accepts_body": True,
            "parameters": [{"name": "key", "in": "path"}],
        },
        {
            "id": "renameProject",
            "method": "PUT",
            "path": "/projects/{p}",
            "access": "write",
            "parameters": [{"name": "p", "in": "path"}],
        },
    ],
}

PUBLIC: dict[str, Any] = {
    "id": "weather",
    "name": "Weather",
    "description": "Public weather forecasts.",
    "base_url": "https://weather.example.test",
    "status": "verified",
    "auth": {"type": "api_key", "secret_ref": "weather/key", "in": "query", "name": "appid"},
    "operations": [
        {
            "id": "forecast",
            "method": "GET",
            "path": "/forecast",
            "access": "read",
            "parameters": [{"name": "city", "in": "query", "required": True}],
        }
    ],
}

OPEN: dict[str, Any] = {
    "id": "status",
    "name": "Status page",
    "base_url": "https://status.example.test",
    "status": "provisional",
    "operations": [{"id": "summary", "method": "GET", "path": "/summary", "access": "read"}],
}


class RecordingNotifier(Notifier):
    def __init__(self) -> None:
        self.opened_links: list[str] = []
        self.events: list[tuple[str, CredentialRequest]] = []
        self.write_links: list[str] = []
        self.write_events: list[tuple[str, Any]] = []

    async def opened(self, request: CredentialRequest, link: str) -> None:
        self.opened_links.append(link)
        self.events.append(("opened", request))

    async def updated(self, request: CredentialRequest) -> None:
        self.events.append(("updated", request))

    async def closed(self, request: CredentialRequest) -> None:
        self.events.append(("closed", request))

    async def write_opened(self, approval: Any, link: str) -> None:
        self.write_links.append(link)
        self.write_events.append(("opened", approval))

    async def write_closed(self, approval: Any) -> None:
        self.write_events.append(("closed", approval))

    async def grant_used(self, grant: Any, approval_like: Any) -> None:
        self.write_events.append(("grant_used", grant))


Handler = Callable[[httpx.Request], httpx.Response]


class Router:
    """A tiny programmable fake for outbound HTTP."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Handler] = {}
        self.requests: list[httpx.Request] = []

    def add(self, method: str, url: str, handler: Handler) -> None:
        self.routes[(method, url)] = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, str(request.url.copy_with(query=None)))
        handler = self.routes.get(key)
        if handler is None:
            return httpx.Response(404, json={"error": "no route", "url": str(request.url)})
        return handler(request)


def write_entries(directory: Path, *entries: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        (directory / f"{entry['id']}.yaml").write_text(yaml.safe_dump(entry), "utf-8")


@pytest.fixture
def router() -> Router:
    return Router()


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
def audit() -> MemoryAuditSink:
    return MemoryAuditSink()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        registry_dir=tmp_path / "registry",
        secrets_dir=tmp_path / "secrets",
        public_url="https://librarian.example.test",
        rate_limit_per_minute=1000,
        credential_wait_seconds=5,
        allow_private_networks=True,  # tests never touch real DNS
        database_url=None,  # in-memory; persistence has its own tests
    )


@pytest.fixture
def secrets_store(settings: Settings) -> FileSecretStore:
    assert settings.secrets_dir is not None
    return FileSecretStore(settings.secrets_dir)


@pytest.fixture
def librarian(
    settings: Settings,
    router: Router,
    notifier: RecordingNotifier,
    audit: MemoryAuditSink,
    secrets_store: FileSecretStore,
) -> Librarian:
    write_entries(settings.registry_dir, TRACKER, PUBLIC, OPEN)
    lib = Librarian(
        settings,
        secrets_store=secrets_store,
        audit=audit,
        notifier=notifier,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(router)),
    )
    lib.load()
    return lib
