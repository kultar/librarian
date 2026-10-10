"""Opening a real review pull request (GitHub publisher), then cleaning up.

Needs a throwaway repo with a `main` branch and a token that can push and open PRs:
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
from tests.test_openapi import SPEC

REPO = os.environ.get("LIBRARIAN_GITHUB_REPO", "")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
pytestmark = pytest.mark.skipif(
    not (os.environ.get("LIBRARIAN_INTEGRATION") and REPO and TOKEN),
    reason="set LIBRARIAN_INTEGRATION=1, LIBRARIAN_GITHUB_REPO and GITHUB_TOKEN",
)
API = "https://api.github.com"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"}


async def test_learned_api_becomes_a_reviewable_pull_request() -> None:
    api_id = f"it-{uuid.uuid4().hex[:8]}"
    entry = entry_from_spec(SPEC, api_id=api_id, spec_url="https://example.test/spec.json")
    url = await GitHubPublisher(REPO, TOKEN).publish(entry, "integration-test")
    number = int(url.rstrip("/").rsplit("/", 1)[1])
    async with httpx.AsyncClient(headers=HEADERS, timeout=30) as gh:
        try:
            pr: dict[str, Any] = (await gh.get(f"{API}/repos/{REPO}/pulls/{number}")).json()
            assert pr["state"] == "open" and pr["base"]["ref"] == "main"
            assert "Review checklist" in pr["body"]
            assert "searchForIssuesUsingJqlPost" in pr["body"]  # flagged for a human decision
            files = (await gh.get(f"{API}/repos/{REPO}/pulls/{number}/files")).json()
            assert [f["filename"] for f in files] == [f"registry/{api_id}.yaml"]
            content = (
                await gh.get(
                    f"{API}/repos/{REPO}/contents/registry/{api_id}.yaml",
                    params={"ref": pr["head"]["ref"]},
                )
            ).json()
            data = yaml.safe_load(base64.b64decode(content["content"]))
            assert data["id"] == api_id and data["status"] == "provisional"
        finally:
            await gh.patch(f"{API}/repos/{REPO}/pulls/{number}", json={"state": "closed"})
            prefix = f"heads/librarian/onboard-{api_id}"
            refs = (await gh.get(f"{API}/repos/{REPO}/git/matching-refs/{prefix}")).json()
            for ref in refs:
                await gh.delete(f"{API}/repos/{REPO}/git/{ref['ref']}")
