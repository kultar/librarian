"""The database is the registry: APIs learned, approved, blocked or changed on one
replica are seen by all, files are only a way in, and nothing on disk can overrule a
decision made in librarian."""

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


def edit_file(lib: Any, api_id: str, **changes: Any) -> None:
    """Someone edits (or a sync writes) a registry file behind librarian's back."""
    path = Path(lib.settings.registry_dir) / f"{api_id}.yaml"
    current = lib.registry.get(api_id)
    data: dict[str, Any] = (
        yaml.safe_load(path.read_text("utf-8"))
        if path.exists()
        else yaml.safe_load(dump_entry(current))
    )
    path.write_text(yaml.safe_dump({**data, **changes}), "utf-8")


async def reload(lib: Any) -> None:
    """What the server's sweeper does every few seconds."""
    lib.registry.reload_if_changed()
    await lib.registry.refresh_stored()
    await lib.registry.import_files()


async def test_an_approval_is_a_database_write_every_replica_sees_at_once(pair: Any) -> None:
    a, b = pair
    await a.start()
    await b.start()
    await a.onboard("jira", spec_url="https://x.example/spec.json", caller=AGENT)
    await a.approve_api("jira", "sam@x.test")
    await b.registry.refresh_stored()
    seen = b.registry.get("jira")
    assert seen is not None and seen.status is EntryStatus.VERIFIED
    assert not (Path(a.settings.registry_dir) / "jira.yaml").exists()  # no file involved
    await a.aclose()
    await b.aclose()


async def test_a_review_opened_on_one_replica_waits_on_the_others_too(pair: Any) -> None:
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from librarian.console import console_routes

    a, b = pair
    await a.start()
    await b.start()
    b.settings.approvers.console = "open"
    await a.onboard("jira", spec_url="https://x.example/spec.json", caller=AGENT)
    await b.reviews.refresh()  # what the console and the sweeper do
    waiting = b.reviews.pending_for("jira")
    assert waiting is not None and waiting.api == "jira"
    # Approved through the other replica's console: settled for both.
    await b.registry.refresh_stored()
    with TestClient(Starlette(routes=console_routes(b))) as client:
        assert "Review <b>Jira-ish</b>" in client.get("/waiting").text
    await b.approve_api("jira", "sam@x.test")
    await a.reviews.refresh()
    assert a.reviews.pending() == [] and b.reviews.pending() == []
    await a.registry.refresh_stored()
    approved = a.registry.get("jira")
    assert approved is not None and approved.status is EntryStatus.VERIFIED
    await a.aclose()
    await b.aclose()


async def test_a_file_cannot_overrule_a_decision_made_in_librarian(pair: Any) -> None:
    a, b = pair
    await a.start()
    await a.onboard("jira", spec_url="https://x.example/spec.json", caller=AGENT)
    await a.approve_api("jira", "sam@x.test")
    decided = a.registry.get("jira")
    assert decided is not None
    write_op = next(op.id for op in decided.operations if op.access.value == "write")

    # A file for the API turns up saying more than anyone approved here: say the record
    # repository is synced back to disk and somebody merged an edit there.
    ops = yaml.safe_load(dump_entry(decided))["operations"]
    for op in ops:
        if op["id"] == write_op:
            op["writable"] = True
    edit_file(a, "jira", operations=ops, description="Edited on GitHub")
    await reload(a)
    live = a.registry.get("jira")
    assert live is not None and live.description != "Edited on GitHub"
    assert not live.operation(write_op).writable  # type: ignore[union-attr]
    assert "decisions were made about this API in librarian" in a.registry.notices["jira"]
    # ...on a replica that starts with that file already there, too
    await b.start()
    fresh = b.registry.get("jira")
    assert fresh is not None and not fresh.operation(write_op).writable  # type: ignore[union-attr]
    assert "jira" in b.registry.notices

    # The same file as the database (what librarian itself wrote to the record) is quiet.
    (Path(a.settings.registry_dir) / "jira.yaml").write_text(dump_entry(live), "utf-8")
    await reload(a)
    assert a.registry.notices == {}
    await a.aclose()
    await b.aclose()


async def test_hand_written_files_are_imported_until_someone_decides_here(pair: Any) -> None:
    a, b = pair
    await a.start()
    # The file is in the database from the first start: it no longer needs the file.
    assert "tracker" in a.registry._stored  # pyright: ignore[reportPrivateUsage]
    edit_file(a, "tracker", description="Edited by hand")
    await reload(a)
    tracker = a.registry.get("tracker")
    assert tracker is not None and tracker.description == "Edited by hand"  # still follows
    # What librarian looks up at runtime is not a decision: the file is still followed.
    await a.registry.save(tracker.model_copy(update={"glossary": {"customfield_1": "Sprint"}}))
    edit_file(a, "tracker", description="Edited again")
    await reload(a)
    tracker = a.registry.get("tracker")
    assert tracker is not None and tracker.description == "Edited again"

    # A person decides something in librarian: from then on the database's entry wins.
    await a.set_operation_access("tracker", "renameProject", "exclude", "sam@x.test")
    # The file is as it was: reading it again (a restart, another replica) is no news.
    a.registry.load()
    await a.registry.refresh_stored(force=True)
    assert a.registry.notices == {} and await a.registry.import_files() == []
    decided = a.registry.get("tracker")
    assert decided is not None and "marked `renameProject` as exclude" in decided.notes
    edit_file(a, "tracker", description="Edited a third time")
    await reload(a)
    tracker = a.registry.get("tracker")
    assert tracker is not None and tracker.description == "Edited again"
    assert "sam@x.test marked `renameProject` as exclude" in tracker.notes
    assert "tracker" in a.registry.notices

    # Take the files away altogether (git-sync switched off): nothing is lost.
    (Path(a.settings.registry_dir) / "tracker.yaml").unlink()
    await b.start()
    kept = b.registry.get("tracker")
    assert kept is not None and kept.description == "Edited again"
    assert b.registry.notices == {}
    await a.aclose()
    await b.aclose()


async def test_entries_saved_before_0_20_carry_over(pair: Any) -> None:
    """0.19 kept reviewed entries in files and its own changes in the database, where a
    copy applied only on top of the file version it was made from. The first start of
    0.20 must end up with exactly the entries 0.19 was serving."""
    import json

    from librarian.registry.store import _content_hash  # pyright: ignore[reportPrivateUsage]
    from tests.conftest import PUBLIC, TRACKER, write_entries

    a, b = pair
    registry_dir = Path(a.settings.registry_dir)
    write_entries(registry_dir, PUBLIC)
    a.load()
    # Reviewed files were written by librarian (through a pull request), so they carry
    # their `updated_at` and read the same on every load.
    for entry in a.registry.all():
        (registry_dir / f"{entry.id}.yaml").write_text(dump_entry(entry), "utf-8")
    a.load()
    await a.state.initialize()
    tracker, weather = a.registry.get("tracker"), a.registry.get("weather")
    assert tracker is not None and weather is not None

    async def old_copy(entry: Any, base: str | None) -> None:
        doc = {"entry": json.loads(entry.model_dump_json(by_alias=True)), "base": base}
        await a.state.save_doc("registry-entry", entry.id, "active", json.dumps(doc))

    # A copy made on top of the current file (a block): 0.19 served the copy.
    await old_copy(
        tracker.model_copy(update={"status": EntryStatus.BLOCKED}), _content_hash(tracker)
    )
    # A copy made on top of an older file version: 0.19 served the file.
    await old_copy(weather.model_copy(update={"description": "A stale copy"}), "an-older-hash")
    # A learned API whose review was never merged: no file, 0.19 served the copy.
    learned = tracker.model_copy(update={"id": "learned", "status": EntryStatus.PROVISIONAL})
    await old_copy(learned, None)

    await a.start()
    served = {e.id: e for e in a.registry.all()}
    assert served["tracker"].status is EntryStatus.BLOCKED
    assert served["weather"].description == "Public weather forecasts."
    assert served["learned"].status is EntryStatus.PROVISIONAL
    assert a.registry.notices == {}
    # Unblocking goes back to what the API was (0.19 took that from the file).
    assert (await a.unblock("tracker", "sam@x.test")).status is EntryStatus.VERIFIED

    # From here on the files are not needed: remove them and start another replica.
    for name in ("tracker.yaml", "weather.yaml"):
        (registry_dir / name).unlink()
    await b.start()
    assert {e.id: e.status for e in b.registry.all()} == {
        "tracker": EntryStatus.VERIFIED,
        "weather": EntryStatus.VERIFIED,
        "learned": EntryStatus.PROVISIONAL,
    }
    assert TRACKER["id"] == "tracker"
    await a.aclose()
    await b.aclose()


async def test_any_api_can_be_forgotten_and_its_file_does_not_bring_it_back(pair: Any) -> None:
    a, b = pair
    await a.start()
    assert a.registry.can_forget("tracker")
    # (with a decision made about it first, so the entry no longer equals its file)
    await a.set_operation_access("tracker", "renameProject", "exclude", "sam@x.test")
    await a.forget("tracker", "sam@x.test")
    assert a.registry.get("tracker") is None
    await reload(a)
    assert a.registry.get("tracker") is None  # the file is still on disk, unchanged
    await b.start()  # a restart, or another replica
    assert b.registry.get("tracker") is None

    # The file changing is someone asking for it again.
    edit_file(a, "tracker", description="Back, by hand")
    await reload(a)
    back = a.registry.get("tracker")
    assert back is not None and back.description == "Back, by hand"
    await a.aclose()
    await b.aclose()


async def test_blocking_remembers_what_to_go_back_to(pair: Any) -> None:
    a, b = pair
    await a.start()
    await b.start()
    await a.onboard("jira", spec_url="https://x.example/spec.json", caller=AGENT)
    await a.approve_api("jira", "sam@x.test")
    await a.block("jira", "sam@x.test")
    await b.registry.refresh_stored()
    assert b.registry.approved("jira")  # still an approved API, just refused for now
    assert (await b.unblock("jira", "kim@x.test")).status is EntryStatus.VERIFIED
    # an API nobody approved goes back to provisional
    await a.onboard("jira2", spec_url="https://x.example/spec.json", caller=AGENT)
    await a.block("jira2", "sam@x.test")
    assert not a.registry.approved("jira2")
    assert (await a.unblock("jira2", "sam@x.test")).status is EntryStatus.PROVISIONAL
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
