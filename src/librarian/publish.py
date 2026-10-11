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
from typing import Any, Protocol, cast
from urllib.parse import quote

import httpx
import yaml

from librarian.registry.models import Access, ApiEntry
from librarian.registry.store import dump_entry

log = logging.getLogger(__name__)


class PublishError(Exception):
    pass


class Publisher(Protocol):
    async def publish(self, entry: ApiEntry, learned_by: str) -> str:
        """Propose the entry for review; returns a URL (e.g. the pull request)."""
        ...

    async def propose_update(self, entry: ApiEntry, title: str, body: str) -> str:
        """Propose a change to an existing (possibly verified) entry; returns a URL."""
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
        file_path = self._file_path(entry)
        existing = await self._call(
            "GET", f"/contents/{quote(file_path)}", params={"ref": self.base_branch}
        )
        if existing.status_code == 200 and _is_verified(existing.json()):
            # Never propose replacing a reviewed entry with an unreviewed one.
            raise PublishError(
                f"{file_path} is already verified in {self.repo}; not proposing a replacement"
            )
        return await self._open_pr(
            entry,
            existing,
            branch_kind="onboard",
            message=f"registry: learn {entry.id} ({entry.name})",
            title=f"registry: review learned API `{entry.id}`",
            body=review_body(entry, learned_by),
        )

    async def propose_update(self, entry: ApiEntry, title: str, body: str) -> str:
        file_path = self._file_path(entry)
        existing = await self._call(
            "GET", f"/contents/{quote(file_path)}", params={"ref": self.base_branch}
        )
        return await self._open_pr(
            entry,
            existing,
            branch_kind="update",
            message=f"registry: update {entry.id} ({entry.name})",
            title=title,
            body=body,
        )

    def _file_path(self, entry: ApiEntry) -> str:
        return f"{self.directory}/{entry.id}.yaml" if self.directory else f"{entry.id}.yaml"

    async def _open_pr(
        self,
        entry: ApiEntry,
        existing: httpx.Response,
        *,
        branch_kind: str,
        message: str,
        title: str,
        body: str,
    ) -> str:
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        branch = f"librarian/{branch_kind}-{entry.id}-{stamp}"
        base = await self._call("GET", f"/git/ref/heads/{quote(self.base_branch)}")
        if base.status_code == 404:
            raise PublishError(f"base branch {self.base_branch!r} not found in {self.repo}")
        sha = base.json()["object"]["sha"]
        await self._call("POST", "/git/refs", json={"ref": f"refs/heads/{branch}", "sha": sha})

        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(dump_entry(entry).encode()).decode(),
            "branch": branch,
        }
        if existing.status_code == 200:
            payload["sha"] = existing.json()["sha"]
        await self._call("PUT", f"/contents/{quote(self._file_path(entry))}", json=payload)

        pr = await self._call(
            "POST",
            "/pulls",
            json={"title": title, "head": branch, "base": self.base_branch, "body": body},
        )
        return str(pr.json()["html_url"])


def _is_verified(content: dict[str, Any]) -> bool:
    try:
        text = base64.b64decode(str(content.get("content", ""))).decode("utf-8")
        data: Any = yaml.safe_load(text)
    except (ValueError, yaml.YAMLError):
        return False
    return isinstance(data, dict) and cast(dict[str, Any], data).get("status") == "verified"


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
        f"| Learned from | {_source_label(entry)} |",
        f"| Reads | {len(reads)} |",
        f"| Need review | {len(unknown)} |",
        f"| Writes (refused unless a reviewer enables them) | {writes} |",
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
    if entry.source.kind in ("docs", "inferred"):
        lines.append(
            "- [ ] Spot-check operations against the API's documentation (they were read by "
            "a model; each docs-derived one quotes its source below)"
        )
    lines += [
        "- [ ] Set `status: verified` when satisfied",
        "",
        "_Opened automatically by librarian._",
    ]
    quoted = [op for op in entry.operations if op.evidence][:40]
    if quoted:
        lines += ["", "<details><summary>Where each operation came from</summary>", ""]
        lines += [
            f'- `{op.method} {op.path}`: "{(op.evidence or "")[:200]}" ({op.evidence_url})'
            for op in quoted
        ]
        lines += ["", "</details>"]
    return "\n".join(lines)


def _source_label(entry: ApiEntry) -> str:
    source = entry.source
    if source.kind == "openapi":
        return f"OpenAPI spec {source.url or ''}".strip()
    if source.kind == "docs":
        return f"documentation, {len(source.pages)} page(s) from {source.url}"
    if source.kind == "inferred":
        return "the model's own knowledge (no spec or docs): every operation is unconfirmed"
    return "written by hand"
