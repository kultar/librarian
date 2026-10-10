"""Publishing learned entries for human review.

When the librarian learns an API, the entry is usable at once (provisional,
GET-only) and is also proposed as a pull request against the registry repo.
A human reviews it there; once merged and synced back (e.g. by git-sync), the
reviewer's edits, such as `status: verified`, take effect via hot reload.

The GitHub publisher uses the REST API only: no git binary or clone needed.
"""

from __future__ import annotations

import base64
import logging
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from librarian.registry.models import Access, ApiEntry
from librarian.registry.store import dump_entry

log = logging.getLogger(__name__)


class PublishError(Exception):
    pass


class Publisher(Protocol):
    async def publish(self, entry: ApiEntry, learned_by: str) -> str:
        """Propose the entry for review; returns a URL (e.g. the pull request)."""
        ...


class GitHubPublisher:
    def __init__(
        self,
        repo: str,
        token: str,
        *,
        base_branch: str = "main",
        directory: str = "registry",
        api_url: str = "https://api.github.com",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if repo.count("/") != 1:
            raise ValueError("repo must look like 'owner/name'")
        self.repo = repo
        self.base_branch = base_branch
        self.directory = directory.strip("/")
        self.api_url = api_url.rstrip("/")
        self.client = client or httpx.AsyncClient(timeout=30)
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = await self.client.request(
            method, f"{self.api_url}/repos/{self.repo}{path}", headers=self._headers, **kwargs
        )
        if response.status_code >= 400 and response.status_code != 404:
            raise PublishError(f"GitHub {method} {path}: HTTP {response.status_code}")
        return response

    async def publish(self, entry: ApiEntry, learned_by: str) -> str:
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        branch = f"librarian/onboard-{entry.id}-{stamp}"
        file_path = f"{self.directory}/{entry.id}.yaml" if self.directory else f"{entry.id}.yaml"

        base = await self._call("GET", f"/git/ref/heads/{quote(self.base_branch)}")
        if base.status_code == 404:
            raise PublishError(f"base branch {self.base_branch!r} not found in {self.repo}")
        sha = base.json()["object"]["sha"]
        await self._call("POST", "/git/refs", json={"ref": f"refs/heads/{branch}", "sha": sha})

        existing = await self._call(
            "GET", f"/contents/{quote(file_path)}", params={"ref": self.base_branch}
        )
        payload: dict[str, Any] = {
            "message": f"registry: learn {entry.id} ({entry.name})",
            "content": base64.b64encode(dump_entry(entry).encode()).decode(),
            "branch": branch,
        }
        if existing.status_code == 200:
            payload["sha"] = existing.json()["sha"]
        await self._call("PUT", f"/contents/{quote(file_path)}", json=payload)

        pr = await self._call(
            "POST",
            "/pulls",
            json={
                "title": f"registry: review learned API `{entry.id}`",
                "head": branch,
                "base": self.base_branch,
                "body": review_body(entry, learned_by),
            },
        )
        return str(pr.json()["html_url"])


def review_body(entry: ApiEntry, learned_by: str) -> str:
    reads = [op.id for op in entry.operations if op.access is Access.READ]
    unknown = [op.id for op in entry.operations if op.access is Access.UNKNOWN]
    writes = sum(1 for op in entry.operations if op.access is Access.WRITE)
    lines = [
        f"The librarian learned **{entry.name}** (`{entry.id}`) "
        f"because `{learned_by}` asked for it.",
        "It is already usable as a **provisional** entry: GET/HEAD reads only.",
        "",
        "| | |",
        "|---|---|",
        f"| Base URL | `{entry.base_url}` |",
        f"| Auth | `{entry.auth.type}`"
        + (f", secret `{entry.secret_ref}`" if entry.secret_ref else "")
        + " |",
        f"| Spec | {entry.source.url or 'n/a'} |",
        f"| Reads | {len(reads)} |",
        f"| Need review | {len(unknown)} |",
        f"| Writes (always refused) | {writes} |",
        f"| Key test | `{entry.credential.validation_operation or 'none'}` |",
        "",
        "### Review checklist",
        "- [ ] The base URL is the real API host (not a look-alike)",
        "- [ ] The auth type and secret reference make sense",
        "- [ ] Only operations that cannot change data are `access: read`",
    ]
    if unknown:
        lines.append(
            "- [ ] Decide on the operations below. To allow one, set `access: read` and "
            "`read_basis: reviewed` and explain why it cannot change data:"
        )
        lines.extend(f"  - `{op}`" for op in unknown[:30])
    lines += [
        "- [ ] Set `status: verified` when satisfied",
        "",
        "_Opened automatically by librarian._",
    ]
    return "\n".join(lines)
