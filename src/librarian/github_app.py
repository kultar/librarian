"""Signing in to GitHub as a GitHub App, and creating that App in one step.

An App is owned by the organization, not a person, and its access tokens are minted
per use and expire within the hour. librarian asks for tokens limited to the one
registry repository and to the two permissions it needs (contents, pull requests).

`create_app` runs GitHub's manifest flow: GitHub creates the App from a manifest after
one click, then hands its credentials to a one-time local callback. The private key is
written straight to a file only the current user can read; it is never printed.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any, cast

import httpx
import jwt

API = "https://api.github.com"
# Refresh an installation token this long before GitHub says it expires.
REFRESH_MARGIN = timedelta(minutes=5)
PERMISSIONS = {"contents": "write", "pull_requests": "write"}


class GitHubAppError(Exception):
    pass


def app_jwt(app_id: str, private_key: str, now: float | None = None) -> str:
    """The short-lived token that identifies the App itself (not an installation)."""
    issued = int(now if now is not None else time.time()) - 60  # allow for clock drift
    payload = {"iat": issued, "exp": issued + 600, "iss": str(app_id)}
    try:
        return jwt.encode(payload, private_key, algorithm="RS256")
    except (ValueError, TypeError, jwt.PyJWTError) as exc:
        raise GitHubAppError(f"the GitHub App private key is not usable: {exc}") from exc


class GitHubAppAuth:
    """Mints installation tokens for one repository, caching each until near expiry."""

    def __init__(
        self,
        app_id: str,
        private_key: str,
        repo: str,
        *,
        installation_id: int | None = None,
        api_url: str = API,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if repo.count("/") != 1:
            raise ValueError("repo must look like 'owner/name'")
        self.app_id = str(app_id)
        self.private_key = private_key
        self.repo = repo
        self.installation_id = installation_id
        self.api_url = api_url.rstrip("/")
        self.client = client or httpx.AsyncClient(timeout=30)
        self._clock = clock
        self._token: str | None = None
        self._expires: datetime | None = None
        self._lock = asyncio.Lock()

    def _app_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {app_jwt(self.app_id, self.private_key)}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def find_installation(self) -> int:
        """The App's installation on the repository (404 = not installed there yet)."""
        response = await self.client.get(
            f"{self.api_url}/repos/{self.repo}/installation", headers=self._app_headers()
        )
        if response.status_code == 404:
            raise GitHubAppError(f"the GitHub App is not installed on {self.repo}")
        if response.status_code != 200:
            raise GitHubAppError(f"looking up the installation: HTTP {response.status_code}")
        return int(response.json()["id"])

    async def token(self) -> str:
        async with self._lock:
            if (
                self._token is not None
                and self._expires is not None
                and self._clock() < self._expires - REFRESH_MARGIN
            ):
                return self._token
            if self.installation_id is None:
                self.installation_id = await self.find_installation()
            response = await self.client.post(
                f"{self.api_url}/app/installations/{self.installation_id}/access_tokens",
                headers=self._app_headers(),
                # Narrower than the installation: this repository, these permissions.
                json={"repositories": [self.repo.split("/", 1)[1]], "permissions": PERMISSIONS},
            )
            if response.status_code != 201:
                raise GitHubAppError(f"minting an installation token: HTTP {response.status_code}")
            data = cast(dict[str, Any], response.json())
            self._token = str(data["token"])
            self._expires = datetime.fromisoformat(str(data["expires_at"]).replace("Z", "+00:00"))
            return self._token


FIRST_README = """# API registry

One YAML file per API, reviewed by pull request. librarian opens a pull request here for every
API it learns; merging it marks the entry `verified`.
"""


async def init_repo(auth: GitHubAppAuth, *, readme: str = FIRST_README) -> bool:
    """Give an empty registry repo its first commit (a README) as the App, so git-sync and
    review pull requests have a branch to work with. Returns False if it already had one.

    Creates the default branch, so it works unless a ruleset restricts creating it; once the
    branch exists, branch rules apply as usual (librarian only opens pull requests)."""
    headers = {
        "Authorization": f"Bearer {await auth.token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    branches = await auth.client.get(f"{auth.api_url}/repos/{auth.repo}/branches", headers=headers)
    if branches.status_code != 200:
        raise GitHubAppError(f"listing branches of {auth.repo}: HTTP {branches.status_code}")
    if branches.json():
        return False
    response = await auth.client.put(
        f"{auth.api_url}/repos/{auth.repo}/contents/README.md",
        headers=headers,
        json={
            "message": "Start the API registry",
            "content": base64.b64encode(readme.encode("utf-8")).decode("ascii"),
        },
    )
    if response.status_code != 201:
        raise GitHubAppError(
            f"creating the first commit in {auth.repo}: HTTP {response.status_code}"
        )
    return True


# -- one-step creation (manifest flow) --------------------------------------------------


def manifest(name: str, redirect_url: str) -> dict[str, Any]:
    return {
        "name": name,
        "url": "https://github.com/kultar/librarian",
        "description": "librarian: opens pull requests for APIs it learned, and reads the "
        "reviewed registry.",
        "redirect_url": redirect_url,
        "public": False,
        "default_permissions": {**PERMISSIONS, "metadata": "read"},
        "default_events": [],
    }


def new_app_url(owner: str, owner_is_org: bool, state: str) -> str:
    base = (
        f"https://github.com/organizations/{owner}/settings/apps/new"
        if owner_is_org
        else "https://github.com/settings/apps/new"
    )
    return f"{base}?state={state}"


def start_page(action: str, app_manifest: dict[str, Any]) -> str:
    """A page that posts the manifest to GitHub (GitHub requires a form POST)."""
    return (
        "<!doctype html><meta charset=utf-8><title>Create the librarian GitHub App</title>"
        "<body style='font-family:system-ui;max-width:40em;margin:3em auto'>"
        "<h1>Create the librarian GitHub App</h1>"
        f"<form method=post action='{escape(action)}'>"
        f"<input type=hidden name=manifest value='{escape(json.dumps(app_manifest))}'>"
        "<p>GitHub will show the App's name and permissions (contents and pull requests: "
        "write) and ask you to confirm.</p><button type=submit>Continue to GitHub</button>"
        "</form><script>document.forms[0].submit()</script>"
    )


@dataclass
class CreatedApp:
    app_id: int
    slug: str
    client_id: str
    html_url: str
    key_file: Path


async def exchange_code(
    code: str, key_file: Path, client: httpx.AsyncClient, api_url: str = API
) -> CreatedApp:
    """Trade the one-time code for the App's credentials; save the private key."""
    response = await client.post(f"{api_url}/app-manifests/{code}/conversions")
    if response.status_code != 201:
        raise GitHubAppError(f"GitHub did not accept the code: HTTP {response.status_code}")
    data = cast(dict[str, Any], response.json())
    write_private_key(key_file, str(data["pem"]))
    return CreatedApp(
        app_id=int(data["id"]),
        slug=str(data["slug"]),
        client_id=str(data.get("client_id") or ""),
        html_url=str(data.get("html_url") or ""),
        key_file=key_file,
    )


def write_private_key(path: Path, pem: str) -> None:
    """Owner-only file, never overwriting an existing one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(pem)


def new_state() -> str:
    return secrets.token_urlsafe(24)


async def create_app(
    repo: str,
    key_file: Path,
    *,
    name: str | None = None,
    port: int = 8977,
    client: httpx.AsyncClient,
    api_url: str = API,
    echo: Callable[[str], None] = print,
    on_ready: Callable[[str], object] | None = None,
    wait_seconds: float = 900,
    poll_seconds: float = 3,
) -> tuple[CreatedApp, int]:
    """Create the App from a manifest, wait for it to be installed on `repo`, and
    return it with its installation id. A local page on 127.0.0.1 does the browser part."""
    import uvicorn
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import HTMLResponse
    from starlette.routing import Route

    if repo.count("/") != 1:
        raise GitHubAppError("repo must look like 'owner/name'")
    if await asyncio.to_thread(key_file.exists):
        raise GitHubAppError(f"{key_file} already exists; choose another --key-file")
    owner = repo.split("/", 1)[0]
    who = await client.get(f"{api_url}/users/{owner}")
    if who.status_code != 200:
        raise GitHubAppError(f"GitHub account {owner!r} not found (HTTP {who.status_code})")
    is_org = who.json().get("type") == "Organization"
    state = new_state()
    local = f"http://127.0.0.1:{port}"
    app_manifest = manifest(name or f"librarian-{owner}", f"{local}/callback")
    created: asyncio.Future[CreatedApp] = asyncio.get_running_loop().create_future()

    async def start(request: Request) -> HTMLResponse:
        return HTMLResponse(start_page(new_app_url(owner, is_org, state), app_manifest))

    async def callback(request: Request) -> HTMLResponse:
        code = request.query_params.get("code", "")
        if request.query_params.get("state") != state or not code or created.done():
            return HTMLResponse("<h1>This link is not valid</h1>", status_code=400)
        try:
            result = await exchange_code(code, key_file, client, api_url)
        except GitHubAppError as exc:
            created.set_exception(exc)
            return HTMLResponse(f"<h1>Could not create the App</h1><p>{escape(str(exc))}</p>")
        created.set_result(result)
        install = f"https://github.com/apps/{result.slug}/installations/new"
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><body style='font-family:system-ui;"
            "max-width:40em;margin:3em auto'><h1>App created</h1>"
            f"<p>Last step: <a href='{escape(install)}'>install it on <b>{escape(repo)}</b></a> "
            "(choose <i>Only select repositories</i>). The terminal continues by itself.</p>"
        )

    server = uvicorn.Server(
        uvicorn.Config(
            Starlette(routes=[Route("/", start), Route("/callback", callback)]),
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
    )
    serving = asyncio.create_task(server.serve())
    try:
        while not server.started:
            if serving.done():
                raise GitHubAppError(f"could not listen on {local} (port in use?)")
            await asyncio.sleep(0.05)
        echo(f"Open {local}/ in your browser and confirm on GitHub.")
        if on_ready is not None:
            on_ready(f"{local}/")
        app = await asyncio.wait_for(created, wait_seconds)
    finally:
        server.should_exit = True
        await serving
    echo(f"Created GitHub App {app.slug} (id {app.app_id}); private key saved to {app.key_file}")
    echo(f"Now install it on {repo}: https://github.com/apps/{app.slug}/installations/new")
    key = await asyncio.to_thread(app.key_file.read_text, "utf-8")
    auth = GitHubAppAuth(str(app.app_id), key, repo, api_url=api_url, client=client)
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            installation = await auth.find_installation()
            break
        except GitHubAppError:
            if time.monotonic() > deadline:
                raise
            await asyncio.sleep(poll_seconds)
    echo(f"Installed on {repo} (installation {installation}).")
    return app, installation
