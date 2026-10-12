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

from librarian.registry.models import Access, ApiEntry, EntryStatus
from librarian.registry.store import dump_entry

log = logging.getLogger(__name__)


class PublishError(Exception):
    pass


class TokenSource(Protocol):
    async def token(self) -> str: ...


class Publisher(Protocol):
    async def publish(self, entry: ApiEntry, learned_by: str) -> str:
        """Propose the entry for review; returns a URL (e.g. the pull request)."""
        ...

    async def propose_update(self, entry: ApiEntry, title: str, body: str) -> str:
        """Propose a change to an existing (possibly verified) entry; returns a URL."""
        ...

    async def withdraw(self, api_id: str, reason: str) -> list[str]:
        """Close the API's open review proposals (with `reason` as a comment) and delete
        their branches; returns the URLs closed."""
        ...

    async def comment(self, api_id: str, text: str) -> list[str]:
        """Leave `text` on the API's open review proposals; returns their URLs."""
        ...

    async def refresh(self, entry: ApiEntry, note: str) -> str | None:
        """Push the entry as a new commit onto its open proposal (with `note` as a
        comment), so one pull request keeps carrying every decision; None when there
        is no open proposal for it."""
        ...

    async def merge(self, api_id: str, by: str) -> str | None:
        """Merge the API's open proposal as the App (an approver decided so); returns
        its URL, or None when nothing is open. Raises PublishError when the repository's
        rules refuse (then a person merges on GitHub)."""
        ...


class GitHubPublisher:
    def __init__(
        self,
        repo: str,
        token: str | TokenSource,
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
        self._token = token

    async def _headers(self) -> dict[str, str]:
        # A GitHub App mints a fresh short-lived token; a fixed token is used as is.
        token = self._token if isinstance(self._token, str) else await self._token.token()
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = await self.client.request(
            method,
            f"{self.api_url}/repos/{self.repo}{path}",
            headers=await self._headers(),
            **kwargs,
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

    async def _open_proposals(self, api_id: str) -> list[dict[str, Any]]:
        prefixes = (f"librarian/onboard-{api_id}-", f"librarian/update-{api_id}-")
        response = await self._call("GET", "/pulls", params={"state": "open", "per_page": 100})
        pulls: list[dict[str, Any]] = response.json() if response.status_code == 200 else []
        return [p for p in pulls if str(p.get("head", {}).get("ref", "")).startswith(prefixes)]

    async def withdraw(self, api_id: str, reason: str) -> list[str]:
        closed: list[str] = []
        for pr in await self._open_proposals(api_id):
            number = pr["number"]
            await self._call("POST", f"/issues/{number}/comments", json={"body": reason})
            await self._call("PATCH", f"/pulls/{number}", json={"state": "closed"})
            branch = str(pr["head"]["ref"])
            await self._call("DELETE", f"/git/refs/heads/{quote(branch)}")
            closed.append(str(pr["html_url"]))
        return closed

    async def refresh(self, entry: ApiEntry, note: str) -> str | None:
        proposals = await self._open_proposals(entry.id)
        if not proposals:
            return None
        pr = proposals[0]
        branch = str(pr["head"]["ref"])
        file_path = self._file_path(entry)
        current = await self._call("GET", f"/contents/{quote(file_path)}", params={"ref": branch})
        payload: dict[str, Any] = {
            "message": note,
            "content": base64.b64encode(dump_entry(as_approved(entry)).encode()).decode(),
            "branch": branch,
        }
        if current.status_code == 200:
            payload["sha"] = current.json()["sha"]
        await self._call("PUT", f"/contents/{quote(file_path)}", json=payload)
        await self._call("POST", f"/issues/{pr['number']}/comments", json={"body": note})
        return str(pr["html_url"])

    async def merge(self, api_id: str, by: str) -> str | None:
        proposals = await self._open_proposals(api_id)
        if not proposals:
            return None
        pr = proposals[0]
        number = pr["number"]
        await self._call(
            "POST", f"/issues/{number}/comments", json={"body": f"Approved by {by} (librarian)."}
        )
        response = await self.client.request(
            "PUT",
            f"{self.api_url}/repos/{self.repo}/pulls/{number}/merge",
            headers=await self._headers(),
            json={"merge_method": "squash", "commit_title": str(pr.get("title") or "")[:200]},
        )
        if response.status_code not in (200, 201):
            raise PublishError(
                f"GitHub refused to merge {pr['html_url']} (HTTP {response.status_code}): "
                "a repository rule may require something the App cannot give (a review, "
                "a check); merge it on GitHub"
            )
        await self._call("DELETE", f"/git/refs/heads/{quote(str(pr['head']['ref']))}")
        return str(pr["html_url"])

    async def comment(self, api_id: str, text: str) -> list[str]:
        urls: list[str] = []
        for pr in await self._open_proposals(api_id):
            await self._call("POST", f"/issues/{pr['number']}/comments", json={"body": text})
            urls.append(str(pr["html_url"]))
        return urls

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
            "content": base64.b64encode(dump_entry(as_approved(entry)).encode()).decode(),
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


def as_approved(entry: ApiEntry) -> ApiEntry:
    """What a proposal contains: the entry as it will be once approved. Merging the pull
    request IS the approval, so a provisional entry is proposed as verified; operations
    whose access is unknown stay unknown (still refused) until a person decides them."""
    if entry.status is EntryStatus.PROVISIONAL:
        return entry.model_copy(update={"status": EntryStatus.VERIFIED})
    return entry


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
