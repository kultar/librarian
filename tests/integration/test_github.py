"""Writing the external record to a real repository (GitHub publisher), then cleaning up.

Needs a throwaway repo with a `main` branch and a token that can write contents (and
open and merge pull requests, when `main` only takes those):
    LIBRARIAN_INTEGRATION=1 LIBRARIAN_GITHUB_REPO=owner/repo GITHUB_TOKEN=... \\
        uv run pytest tests/integration/test_github.py -v
"""

from __future__ import annotations

import base64
import os
import uuid
from typing import Any

import httpx
import pytest
import yaml

from librarian.openapi import entry_from_spec
from librarian.publish import GitHubPublisher
from librarian.registry.models import EntryStatus
from tests.test_openapi import SPEC

REPO = os.environ.get("LIBRARIAN_GITHUB_REPO", "")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
pytestmark = pytest.mark.skipif(
    not (os.environ.get("LIBRARIAN_INTEGRATION") and REPO and TOKEN),
    reason="set LIBRARIAN_INTEGRATION=1, LIBRARIAN_GITHUB_REPO and GITHUB_TOKEN",
)
API = "https://api.github.com"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"}


async def test_a_decision_is_written_to_the_repository_and_can_be_read_back() -> None:
    api_id = f"it-{uuid.uuid4().hex[:8]}"
    path = f"registry/{api_id}.yaml"
    entry = entry_from_spec(SPEC, api_id=api_id, spec_url="https://example.test/spec.json")
    entry = entry.model_copy(update={"status": EntryStatus.VERIFIED})
    publisher = GitHubPublisher(REPO, TOKEN)
    message = f"{api_id}: approved (integration test)\n\nDecided by integration-test in librarian."
    async with httpx.AsyncClient(headers=HEADERS, timeout=30) as gh:

        async def history() -> list[dict[str, Any]]:
            found = await gh.get(f"{API}/repos/{REPO}/commits", params={"path": path})
            return list(found.json())

        try:
            url = await publisher.record(entry, message)
            assert url.startswith("https://github.com/")
            content = (await gh.get(f"{API}/repos/{REPO}/contents/{path}")).json()
            data = yaml.safe_load(base64.b64decode(content["content"]))
            assert data["id"] == api_id and data["status"] == "verified"
            commits = await history()
            assert len(commits) == 1
            assert commits[0]["commit"]["message"].startswith(f"{api_id}: approved")

            # What the record already says is not committed again.
            again = await publisher.record(entry, "nothing new")
            assert again.endswith(path) and len(await history()) == 1

            # A later decision replaces the file, in one more commit.
            changed = entry.model_copy(update={"description": "Changed by a later decision"})
            await publisher.record(changed, f"{api_id}: a later decision")
            assert len(await history()) == 2

            entries, unreadable = await publisher.read_all()
            mine = next(e for e in entries if e.id == api_id)
            assert mine.description == "Changed by a later decision"
            assert f"{api_id}.yaml" not in unreadable
        finally:
            await publisher.remove(api_id, f"{api_id}: forgotten (integration test)")
        gone = await gh.get(f"{API}/repos/{REPO}/contents/{path}")
        assert gone.status_code == 404
        assert await publisher.remove(api_id, "again") is None
        left = await gh.get(f"{API}/repos/{REPO}/pulls", params={"state": "open"})
        assert not [p for p in left.json() if api_id in p["head"]["ref"]]
