"""GitHub App sign-in and one-step creation, against a fake GitHub."""

from __future__ import annotations

import asyncio
import json
import re
import socket
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from librarian.config import GitHubAppSettings
from librarian.github_app import (
    GitHubAppAuth,
    GitHubAppError,
    app_jwt,
    create_app,
    exchange_code,
    start_page,
)
from librarian.publish import GitHubPublisher
from tests.test_openapi import SPEC

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
REPO = "acme/api-registry"


class FakeGitHub:
    def __init__(self, installed_after: int = 0) -> None:
        self.calls: list[tuple[str, str, dict[str, str], Any]] = []
        self.minted = 0
        self.installed_after = installed_after  # installation lookups that 404 first
        self.expires = datetime.now(UTC) + timedelta(hours=1)
        self.branches: list[dict[str, str]] = [{"name": "main"}]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path
        self.calls.append((request.method, path, dict(request.headers), body))
        if path == f"/repos/{REPO}/installation":
            self._check_app_jwt(request)
            if self.installed_after > 0:
                self.installed_after -= 1
                return httpx.Response(404, json={})
            return httpx.Response(200, json={"id": 4242})
        if path == "/app/installations/4242/access_tokens":
            self._check_app_jwt(request)
            self.minted += 1
            return httpx.Response(
                201,
                json={"token": f"ghs_token{self.minted}", "expires_at": self.expires.isoformat()},
            )
        if path == "/users/acme":
            return httpx.Response(200, json={"type": "Organization"})
        if path == "/app-manifests/one-time-code/conversions":
            return httpx.Response(
                201,
                json={"id": 777, "slug": "librarian-acme", "client_id": "Iv1.x", "pem": PEM},
            )
        if path == f"/repos/{REPO}/branches":
            return httpx.Response(200, json=self.branches)
        if path == f"/repos/{REPO}/contents/README.md" and request.method == "PUT":
            assert request.headers["authorization"].startswith("Bearer ghs_")  # as the App
            assert self.branches == [], "must not overwrite an existing repo"
            self.branches = [{"name": "main"}]
            return httpx.Response(201, json={"commit": {"sha": "c0ffee"}})
        if path.startswith("/repos/acme/api-registry/"):  # the publisher's calls
            if path.endswith("/git/ref/heads/main"):
                return httpx.Response(200, json={"object": {"sha": "s"}})
            if request.method == "GET":
                return httpx.Response(404, json={})
            if path.endswith("/pulls"):
                return httpx.Response(201, json={"html_url": "https://github.test/pr/1"})
            return httpx.Response(201, json={})
        return httpx.Response(404, json={})

    @staticmethod
    def _check_app_jwt(request: httpx.Request) -> None:
        token = request.headers["authorization"].removeprefix("Bearer ")
        claims = jwt.decode(token, KEY.public_key(), algorithms=["RS256"])
        assert claims["iss"] == "777"


def client(fake: FakeGitHub) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(fake))


def test_app_jwt_is_short_lived_and_signed() -> None:
    token = app_jwt("777", PEM, now=1_000_000)
    claims = jwt.decode(
        token, KEY.public_key(), algorithms=["RS256"], options={"verify_exp": False}
    )
    assert claims == {"iss": "777", "iat": 999_940, "exp": 1_000_540}
    with pytest.raises(GitHubAppError, match="not usable"):
        app_jwt("777", "not a key")


async def test_tokens_are_limited_to_the_repo_and_cached() -> None:
    fake = FakeGitHub()
    now = [datetime.now(UTC)]
    auth = GitHubAppAuth("777", PEM, REPO, client=client(fake), clock=lambda: now[0])
    assert await auth.token() == "ghs_token1"
    assert auth.installation_id == 4242  # found automatically
    mint = next(c for c in fake.calls if c[1].endswith("/access_tokens"))
    assert mint[3] == {
        "repositories": ["api-registry"],
        "permissions": {"contents": "write", "pull_requests": "write"},
    }
    assert await auth.token() == "ghs_token1"  # cached
    now[0] = fake.expires - timedelta(minutes=4)  # inside the refresh margin
    assert await auth.token() == "ghs_token2"


async def test_not_installed_is_a_clear_error() -> None:
    auth = GitHubAppAuth("777", PEM, REPO, client=client(FakeGitHub(installed_after=1)))
    with pytest.raises(GitHubAppError, match="not installed on acme/api-registry"):
        await auth.token()


async def test_publisher_uses_minted_tokens() -> None:
    from librarian.openapi import entry_from_spec

    fake = FakeGitHub()
    http = client(fake)
    auth = GitHubAppAuth("777", PEM, REPO, client=http)
    publisher = GitHubPublisher(REPO, auth, client=http)
    entry = entry_from_spec(SPEC, api_id="jira", spec_url="https://x.example/spec.json")
    assert await publisher.publish(entry, "agent") == "https://github.test/pr/1"
    publisher_calls = [
        c
        for c in fake.calls
        if c[1].startswith("/repos/acme/api-registry/") and not c[1].endswith("/installation")
    ]
    assert publisher_calls and all(
        c[2]["authorization"] == "Bearer ghs_token1" for c in publisher_calls
    )


async def test_exchange_writes_an_owner_only_key_and_never_overwrites(tmp_path: Path) -> None:
    key_file = tmp_path / "app.pem"
    created = await exchange_code("one-time-code", key_file, client(FakeGitHub()))
    assert (created.app_id, created.slug) == (777, "librarian-acme")
    assert key_file.read_text() == PEM
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        await exchange_code("one-time-code", key_file, client(FakeGitHub()))


def test_start_page_escapes_the_manifest() -> None:
    page = start_page("https://github.com/settings/apps/new?state=s", {"name": "x'><script>"})
    assert '<script>"' not in page and "x&#x27;&gt;&lt;script&gt;" in page


def test_settings_need_exactly_one_key(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        GitHubAppSettings(app_id="1")
    key_file = tmp_path / "k.pem"
    key_file.write_text(PEM)
    assert GitHubAppSettings(app_id="1", private_key_file=key_file).key() == PEM


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_one_step_creation_end_to_end(tmp_path: Path) -> None:
    fake = FakeGitHub(installed_after=2)  # the person takes a moment to install it
    key_file = tmp_path / "github-app.pem"
    port = free_port()
    ready: asyncio.Queue[str] = asyncio.Queue()
    lines: list[str] = []
    task = asyncio.create_task(
        create_app(
            REPO,
            key_file,
            port=port,
            client=client(fake),
            echo=lines.append,
            on_ready=ready.put_nowait,
            wait_seconds=10,
            poll_seconds=0.01,
        )
    )
    url = await asyncio.wait_for(ready.get(), 10)
    async with httpx.AsyncClient() as browser:
        page = (await browser.get(url)).text
        action = re.search(r"action='([^']+)'", page)
        assert action is not None
        assert action.group(1).startswith(
            "https://github.com/organizations/acme/settings/apps/new?state="
        )
        state = action.group(1).split("state=", 1)[1]
        manifest = json.loads(
            re.search(r"name=manifest value='([^']+)'", page).group(1).replace("&quot;", '"')  # type: ignore[union-attr]
        )
        assert manifest["default_permissions"] == {
            "contents": "write",
            "pull_requests": "write",
            "metadata": "read",
        }
        assert manifest["redirect_url"] == f"http://127.0.0.1:{port}/callback"
        forged = await browser.get(f"{url}callback", params={"code": "x", "state": "wrong"})
        assert forged.status_code == 400
        done = await browser.get(f"{url}callback", params={"code": "one-time-code", "state": state})
        assert "install it on <b>acme/api-registry</b>" in done.text
    created, installation = await asyncio.wait_for(task, 10)
    assert (created.app_id, installation) == (777, 4242)
    assert key_file.read_text() == PEM
    assert not any(PEM.splitlines()[1] in line for line in lines)  # the key is never printed


async def test_init_repo_makes_the_first_commit_only_when_the_repo_is_empty() -> None:
    import base64

    from librarian.github_app import FIRST_README, GitHubAppAuth, init_repo

    fake = FakeGitHub()
    fake.branches = []  # a freshly created repo: no branch at all
    auth = GitHubAppAuth("777", PEM, REPO, client=client(fake))
    assert await init_repo(auth) is True
    put = next(c for c in fake.calls if c[0] == "PUT" and c[1].endswith("/contents/README.md"))
    assert base64.b64decode(put[3]["content"]).decode() == FIRST_README
    assert put[3]["message"]
    assert fake.branches == [{"name": "main"}]
    assert await init_repo(auth) is False  # second run: nothing to do, nothing written
    assert sum(1 for c in fake.calls if c[0] == "PUT") == 1
