"""The external record of the registry.

librarian's database is where APIs are approved and decided. So that those decisions
also live somewhere outside it, every approved entry and every later decision is
written to a git repository: one YAML file per API, one commit per decision, saying
who decided what. Nothing is read back from it while librarian runs: a change made in
the repository does not change what agents may do. (`librarian registry restore` reads
it on purpose, to rebuild a lost database.)

The GitHub publisher uses the REST API only: no git binary or clone needed. It commits
to the base branch directly; where a rule requires pull requests, it opens one and
merges it as the App in the same step.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from urllib.parse import quote

import httpx
import yaml
from pydantic import ValidationError

from librarian.registry.models import ApiEntry
from librarian.registry.store import dump_entry, load_yaml

log = logging.getLogger(__name__)

# Review pull requests opened before 0.20, when merging one was the approval.
LEGACY_BRANCHES = ("librarian/onboard-", "librarian/update-")


class PublishError(Exception):
    pass


class TokenSource(Protocol):
    async def token(self) -> str: ...


class Publisher(Protocol):
    async def record(self, entry: ApiEntry, message: str) -> str:
        """Write the entry to the record with `message` as the commit message; returns
        a URL for it (the commit, or the file when it already said this)."""
        ...

    async def remove(self, api_id: str, message: str) -> str | None:
        """Delete the API's file from the record; None when it was never there."""
        ...

    async def read_all(self) -> tuple[list[ApiEntry], dict[str, str]]:
        """Every entry in the record, and the files that could not be read (name ->
        why). Only for restoring a database."""
        ...

    async def close_proposals(self, reason: str) -> list[str]:
        """Close review pull requests left open by a librarian older than 0.20 (with
        `reason` as a comment); returns their URLs."""
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

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = await self._headers() | kwargs.pop("headers", {})
        return await self.client.request(
            method, f"{self.api_url}/repos/{self.repo}{path}", headers=headers, **kwargs
        )

    async def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = await self._send(method, path, **kwargs)
        if response.status_code >= 400 and response.status_code != 404:
            raise PublishError(f"GitHub {method} {path}: {_said(response)}")
        return response

    def _web_url(self, path: str) -> str:
        """Where a person finds the file (github.com, or a GitHub Enterprise host)."""
        web = self.api_url.replace("://api.github.com", "://github.com").removesuffix("/api/v3")
        return f"{web}/{self.repo}/blob/{self.base_branch}/{path}"

    def _file_path(self, api_id: str) -> str:
        return f"{self.directory}/{api_id}.yaml" if self.directory else f"{api_id}.yaml"

    # -- writing ---------------------------------------------------------------------

    async def record(self, entry: ApiEntry, message: str) -> str:
        url = await self._write(entry.id, dump_entry(entry), message)
        if url is None:  # only a removal returns nothing
            raise PublishError(f"could not record {entry.id}")
        return url

    async def remove(self, api_id: str, message: str) -> str | None:
        return await self._write(api_id, None, message)

    async def _write(self, api_id: str, text: str | None, message: str) -> str | None:
        """Make the file on the base branch say `text` (None: delete it)."""
        path = self._file_path(api_id)
        said = ""
        for _ in range(3):
            current = await self._call(
                "GET", f"/contents/{quote(path)}", params={"ref": self.base_branch}
            )
            sha, url = _file(current)
            if text is None and sha is None:
                return None
            if text is not None and sha == blob_sha(text):
                return url  # the record already says this
            payload: dict[str, Any] = {"message": message, "branch": self.base_branch}
            if sha is not None:
                payload["sha"] = sha
            if text is not None:
                payload["content"] = base64.b64encode(text.encode()).decode()
            response = await self._send(
                "PUT" if text is not None else "DELETE", f"/contents/{quote(path)}", json=payload
            )
            if response.status_code in (200, 201):
                return _commit_url(response) or url or self._web_url(path)
            said = _said(response)
            if _moved(response):
                continue  # another replica committed in between: look again
            if response.status_code in (403, 409, 422):
                # Most likely a rule that changes go through a pull request.
                return await self._write_by_pull_request(api_id, text, message, said)
            raise PublishError(f"GitHub refused to write {path}: {said}")
        raise PublishError(f"could not write {path}: {self.base_branch} kept changing ({said})")

    async def _record_pulls(self, api_id: str) -> list[dict[str, Any]]:
        return await self._open_pulls((f"librarian/record-{api_id}-",))

    async def _open_pulls(self, prefixes: tuple[str, ...]) -> list[dict[str, Any]]:
        response = await self._call("GET", "/pulls", params={"state": "open", "per_page": 100})
        pulls: list[dict[str, Any]] = response.json() if response.status_code == 200 else []
        return [p for p in pulls if str(p.get("head", {}).get("ref", "")).startswith(prefixes)]

    async def _write_by_pull_request(
        self, api_id: str, text: str | None, message: str, why: str
    ) -> str | None:
        """The base branch takes no direct commits: put the change on a branch, open a
        pull request and merge it as the App right away. A pull request a rule kept
        from merging earlier is reused, so there is never more than one per API."""
        path = self._file_path(api_id)
        title = message.splitlines()[0][:200] if message else f"registry: record {api_id}"
        pulls = await self._record_pulls(api_id)
        for attempt in range(2):
            pr = pulls[0] if pulls else None
            branch = (
                str(pr["head"]["ref"])
                if pr is not None
                else f"librarian/record-{api_id}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
            )
            if pr is None:
                base = await self._call("GET", f"/git/ref/heads/{quote(self.base_branch)}")
                if base.status_code == 404:
                    raise PublishError(f"base branch {self.base_branch!r} not found in {self.repo}")
                await self._call(
                    "POST",
                    "/git/refs",
                    json={"ref": f"refs/heads/{branch}", "sha": base.json()["object"]["sha"]},
                )
            current = await self._call("GET", f"/contents/{quote(path)}", params={"ref": branch})
            sha, _ = _file(current)
            payload: dict[str, Any] = {"message": message, "branch": branch}
            if sha is not None:
                payload["sha"] = sha
            if text is None:
                if sha is not None:
                    await self._call("DELETE", f"/contents/{quote(path)}", json=payload)
            elif sha != blob_sha(text):
                payload["content"] = base64.b64encode(text.encode()).decode()
                await self._call("PUT", f"/contents/{quote(path)}", json=payload)
            if pr is None:
                body = (
                    f"{message}\n\n_Recorded by librarian. The decision was made in librarian "
                    "and is already in effect; this pull request only writes it down, because "
                    f"`{self.base_branch}` takes no direct commits._"
                )
                opened = await self._call(
                    "POST",
                    "/pulls",
                    json={"title": title, "head": branch, "base": self.base_branch, "body": body},
                )
                if opened.status_code == 404:
                    raise PublishError(f"GitHub POST /pulls: {_said(opened)}")
                pr = cast(dict[str, Any], opened.json())
            number = pr["number"]
            merged = await self._send(
                "PUT",
                f"/pulls/{number}/merge",
                json={"merge_method": "squash", "commit_title": title},
            )
            if merged.status_code in (200, 201):
                await self._call("DELETE", f"/git/refs/heads/{quote(branch)}")
                return str(pr["html_url"])
            state = await self._call("GET", f"/pulls/{number}")
            dirty = state.status_code == 200 and state.json().get("mergeable_state") == "dirty"
            if dirty and attempt == 0:
                # The branch conflicts with what was recorded since: start over from
                # the base branch as it is now.
                await self._call("PATCH", f"/pulls/{number}", json={"state": "closed"})
                await self._call("DELETE", f"/git/refs/heads/{quote(branch)}")
                pulls = []
                continue
            raise PublishError(
                f"{self.repo} takes no direct commits to {self.base_branch} ({why}) and "
                f"refused to merge {pr['html_url']} ({_said(merged)}): let the librarian App "
                "bypass the rule, or merge that pull request by hand"
            )
        raise PublishError(f"could not record {path}")  # pragma: no cover - loop always returns

    # -- reading (restore only) ------------------------------------------------------

    async def read_all(self) -> tuple[list[ApiEntry], dict[str, str]]:
        listing = await self._call(
            "GET", f"/contents/{quote(self.directory)}", params={"ref": self.base_branch}
        )
        entries: list[ApiEntry] = []
        errors: dict[str, str] = {}
        items: Any = listing.json() if listing.status_code == 200 else []
        for item in cast(list[dict[str, Any]], items if isinstance(items, list) else []):
            name = str(item.get("name", ""))
            if item.get("type") != "file" or not name.endswith((".yaml", ".yml")):
                continue
            # The raw form: entries for large APIs are over the 1 MB the JSON form carries.
            raw = await self._call(
                "GET",
                f"/contents/{quote(str(item['path']))}",
                params={"ref": self.base_branch},
                headers={"Accept": "application/vnd.github.raw+json"},
            )
            try:
                if raw.status_code != 200:
                    raise ValueError(f"HTTP {raw.status_code}")
                entry = ApiEntry.model_validate(load_yaml(raw.text))
                if entry.id != name.rsplit(".", 1)[0]:
                    raise ValueError(f"file name must match id {entry.id!r}")
            except (ValidationError, yaml.YAMLError, TypeError, ValueError) as exc:
                errors[name] = str(exc)[:500]
                continue
            entries.append(entry)
        return entries, errors

    # -- pull requests from before 0.20 ------------------------------------------------

    async def close_proposals(self, reason: str) -> list[str]:
        closed: list[str] = []
        for pr in await self._open_pulls(LEGACY_BRANCHES):
            number = pr["number"]
            await self._call("POST", f"/issues/{number}/comments", json={"body": reason})
            await self._call("PATCH", f"/pulls/{number}", json={"state": "closed"})
            await self._call("DELETE", f"/git/refs/heads/{quote(str(pr['head']['ref']))}")
            closed.append(str(pr["html_url"]))
        return closed


def blob_sha(text: str) -> str:
    """The git blob id of a file with this content: what GitHub reports as its `sha`,
    so "is the record up to date" needs no download."""
    data = text.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def _file(response: httpx.Response) -> tuple[str | None, str | None]:
    """(blob sha, web URL) of a contents response; (None, None) when there is no file."""
    if response.status_code != 200:
        return None, None
    data: Any = response.json()
    if not isinstance(data, dict):
        return None, None
    found = cast(dict[str, Any], data)
    sha, url = found.get("sha"), found.get("html_url")
    return (str(sha) if sha else None), (str(url) if url else None)


def _commit_url(response: httpx.Response) -> str | None:
    try:
        data: Any = response.json()
        return str(data["commit"]["html_url"])
    except (ValueError, KeyError, TypeError):
        return None


def _said(response: httpx.Response) -> str:
    """The status and GitHub's own words, for an error a person can act on."""
    message = ""
    try:
        data: Any = response.json()
        if isinstance(data, dict):
            message = str(cast(dict[str, Any], data).get("message") or "")
    except ValueError:
        pass
    return f"HTTP {response.status_code}" + (f": {message[:300]}" if message else "")


def _moved(response: httpx.Response) -> bool:
    """The write was refused because the file changed since it was read."""
    text = _said(response).lower()
    if response.status_code == 409:
        return "does not match" in text or "is at" in text
    return response.status_code == 422 and "sha" in text
