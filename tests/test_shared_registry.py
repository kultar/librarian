"""Several replicas: APIs learned, blocked or changed on one are seen by all, and a
reviewer's merged change always wins over the librarian's own copy."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from librarian.calls import Caller, CallRequest
from librarian.config import Settings
from librarian.registry.models import EntryStatus
from librarian.registry.store import dump_entry
from tests.conftest import Router
from tests.test_openapi import SPEC
from tests.test_state import replica

AGENT = Caller(agent="agent-a")
JIRA = "https://acme.atlassian.example/rest"


@pytest.fixture
def pair(tmp_path: Path, router: Router) -> Any:
    router.add("GET", "https://x.example/spec.json", lambda r: httpx.Response(200, json=SPEC))
    router.add(
        "GET", f"{JIRA}/api/3/issue/ABC-1", lambda r: httpx.Response(200, json={"key": "ABC-1"})
    )
    libs = [replica(tmp_path, router), replica(tmp_path, router)]
    for lib in libs:
        lib.source_client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(router))
    return libs


async def test_an_api_learned_on_one_replica_is_usable_on_another_at_once(pair: Any) -> None:
    a, b = pair
    await a.start()
    await b.start()
    await a.onboard("jira", spec_url="https://x.example/spec.json", caller=AGENT)
    assert not (Path(a.settings.registry_dir) / "jira.yaml").exists()  # in the database
    await a.secrets.put("jira/readonly", "user:token")

    # No sweep has run on B: asking for an API it doesn't know refreshes from the database.
    result = await b.execute(
        CallRequest(api="jira", operation="getIssue", args={"issueIdOrKey": "ABC-1"}), AGENT
    )
    assert result.ok and result.data == {"key": "ABC-1"}
    await a.aclose()
    await b.aclose()


async def test_a_block_on_one_replica_reaches_the_others(pair: Any) -> None:
    a, b = pair
    await a.start()
    await b.start()
    await a.onboard("jira", spec_url="https://x.example/spec.json", caller=AGENT)
    await b.registry.refresh_stored()
    await a.registry.set_status("jira", EntryStatus.BLOCKED)
    assert await b.registry.refresh_stored() is True
    blocked = b.registry.get("jira")
    assert blocked is not None and blocked.status is EntryStatus.BLOCKED
    assert await b.registry.refresh_stored() is False  # nothing new: cheap
    await a.aclose()
    await b.aclose()


async def test_a_merged_review_wins_over_the_librarians_copy(pair: Any) -> None:
    a, b = pair
    await a.start()
    await a.onboard("jira", spec_url="https://x.example/spec.json", caller=AGENT)
    learned = a.registry.get("jira")
    assert learned is not None and learned.status is EntryStatus.PROVISIONAL

    # Afterwards the librarian changes its copy again (e.g. a glossary from discovery).
    await a.registry.save(learned.model_copy(update={"glossary": {"customfield_1": "Sprint"}}))

    # The review pull request is merged (git-sync writes the reviewed file).
    reviewed = learned.model_copy(update={"status": EntryStatus.VERIFIED})
    (Path(a.settings.registry_dir) / "jira.yaml").write_text(dump_entry(reviewed), "utf-8")
    await b.start()
    merged = b.registry.get("jira")
    assert merged is not None and merged.status is EntryStatus.VERIFIED  # the review wins

    # Changes the librarian makes on top of the reviewed version do apply...
    await b.registry.save(merged.model_copy(update={"glossary": {"customfield_1": "Sprint"}}))
    a.registry.load()
    await a.registry.refresh_stored()
    current = a.registry.get("jira")
    assert current is not None and current.glossary == {"customfield_1": "Sprint"}
    assert current.status is EntryStatus.VERIFIED

    # ...until a reviewer changes the entry again: then the new review wins.
    data: dict[str, Any] = yaml.safe_load(dump_entry(reviewed))
    data["description"] = "Edited by a reviewer"
    (Path(a.settings.registry_dir) / "jira.yaml").write_text(yaml.safe_dump(data), "utf-8")
    a.registry.load()
    current = a.registry.get("jira")
    assert current is not None and current.description == "Edited by a reviewer"
    await a.aclose()
    await b.aclose()


async def test_recipes_saved_on_one_replica_reach_the_others(pair: Any) -> None:
    from librarian.recipes import Recipe, RecipeStep

    a, b = pair
    await a.start()
    await b.start()
    recipe = Recipe(
        id="r1",
        template="Is {p0} open",
        pattern="^Is (.+) open$",
        params=["p0"],
        steps=[RecipeStep(api="jira", operation="getIssue")],
        example="Is ABC-1 open",
    )
    a.recipes[recipe.id] = recipe
    await a._save_recipe(recipe)  # pyright: ignore[reportPrivateUsage]
    await b.reload_recipes()
    assert "r1" in b.recipes
    await a.delete_recipe("r1")
    await b.reload_recipes()
    assert "r1" not in b.recipes
    await a.aclose()
    await b.aclose()


def test_replicas_refuse_per_replica_state() -> None:
    with pytest.raises(ValueError, match="needs a shared database"):
        Settings(replicas=2, database_url="sqlite+aiosqlite:///x.db")
    with pytest.raises(ValueError, match="needs a shared secret store"):
        Settings(replicas=2, database_url="postgresql+asyncpg://u:p@db/lib")
    ok = Settings(replicas=3, database_url="postgresql+asyncpg://u:p@db/lib", secrets_backend="aws")
    assert ok.replicas == 3


def test_kubernetes_service_links_do_not_break_the_port(monkeypatch: pytest.MonkeyPatch) -> None:
    # A Service named "librarian" makes Kubernetes inject this into every pod.
    monkeypatch.setenv("LIBRARIAN_PORT", "tcp://10.96.108.186:8080")
    assert Settings().port == 8080
    monkeypatch.setenv("LIBRARIAN_PORT", "9000")
    assert Settings().port == 9000
