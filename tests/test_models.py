from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from librarian.registry import Access, ApiEntry, Operation, ReadBasis, Registry
from tests.conftest import TRACKER, write_entries


def entry(**overrides: Any) -> dict[str, Any]:
    return {**TRACKER, **overrides}


def test_valid_entry_loads() -> None:
    api = ApiEntry.model_validate(TRACKER)
    assert api.host == "api.example.test"
    assert api.secret_ref == "tracker/readonly"
    op = api.operation("getIssue")
    assert op is not None and op.read_basis is ReadBasis.METHOD
    search = api.operation("searchIssues")
    assert search is not None and search.read_basis is ReadBasis.REVIEWED


@pytest.mark.parametrize(
    "base_url",
    [
        "ftp://example.test",
        "https://user:pw@example.test",
        "https://example.test/?q=1",
        "example.test",
    ],
)
def test_bad_base_urls_rejected(base_url: str) -> None:
    with pytest.raises(ValidationError):
        ApiEntry.model_validate(entry(base_url=base_url))


@pytest.mark.parametrize("api_id", ["Jira", "-x", "a" * 70, "has space", "a/b"])
def test_bad_ids_rejected(api_id: str) -> None:
    with pytest.raises(ValidationError):
        ApiEntry.model_validate(entry(id=api_id))


def test_post_cannot_be_read_by_method() -> None:
    with pytest.raises(ValidationError, match="reviewed"):
        Operation(id="x", method="POST", path="/x", access=Access.READ, read_basis=ReadBasis.METHOD)


def test_absolute_paths_rejected() -> None:
    with pytest.raises(ValidationError):
        Operation(id="x", method="GET", path="https://evil.test/x", access=Access.READ)
    with pytest.raises(ValidationError):
        Operation(id="x", method="GET", path="//evil.test/x", access=Access.READ)


def test_unknown_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        ApiEntry.model_validate(entry(surprise=True))


def test_validation_operation_must_be_a_read() -> None:
    bad = entry(credential={"validation_operation": "deleteIssue"})
    with pytest.raises(ValidationError, match="read"):
        ApiEntry.model_validate(bad)


def test_duplicate_operation_ids_rejected() -> None:
    ops = [TRACKER["operations"][0], TRACKER["operations"][0]]
    with pytest.raises(ValidationError, match="duplicate"):
        ApiEntry.model_validate(entry(operations=ops))


def test_registry_skips_invalid_files(tmp_path: Path) -> None:
    write_entries(tmp_path, TRACKER)
    (tmp_path / "broken.yaml").write_text("id: broken\nname: [unclosed\n")
    (tmp_path / "mismatch.yaml").write_text("id: other\nname: X\nbase_url: https://x.test\n")
    registry = Registry(tmp_path)
    registry.load()
    assert [e.id for e in registry.all()] == ["tracker"]
    assert set(registry.errors) == {"broken.yaml", "mismatch.yaml"}


def test_search_ranks_by_relevance(tmp_path: Path) -> None:
    write_entries(
        tmp_path, TRACKER, {**TRACKER, "id": "wiki", "name": "Wiki", "description": "Pages"}
    )
    registry = Registry(tmp_path)
    registry.load()
    hits = registry.search("open issues for a team")
    assert hits[0].entry.id == "tracker"


async def test_save_roundtrip(tmp_path: Path) -> None:
    registry = Registry(tmp_path)
    api = ApiEntry.model_validate(TRACKER)
    await registry.save(api)
    reloaded = Registry(tmp_path)
    reloaded.load()
    loaded = reloaded.get("tracker")
    assert loaded is not None
    assert loaded.model_dump(exclude={"updated_at"}) == api.model_dump(exclude={"updated_at"})
    assert loaded.updated_at >= api.updated_at  # save stamps the time


async def test_registry_reloads_only_on_change(tmp_path: Path) -> None:
    import os

    write_entries(tmp_path, TRACKER)
    registry = Registry(tmp_path)
    registry.load()
    assert not registry.reload_if_changed()
    await registry.save(registry.get("tracker").model_copy(update={"name": "Renamed"}))  # type: ignore[union-attr]
    assert not registry.reload_if_changed()  # our own write
    write_entries(tmp_path, {**TRACKER, "id": "wiki", "name": "Wiki"})
    assert registry.reload_if_changed()  # e.g. git-sync pulled a merged PR
    assert registry.get("wiki") is not None
    (tmp_path / "wiki.yaml").unlink()
    os.utime(tmp_path)
    assert registry.reload_if_changed()
    assert registry.get("wiki") is None


async def test_overlay_keeps_local_changes_and_lets_reviews_win(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    reviewed_dir, overlay_dir = tmp_path / "git", tmp_path / "overlay"
    old = datetime.now(UTC) - timedelta(days=1)
    write_entries(reviewed_dir, {**TRACKER, "updated_at": old.isoformat()})
    registry = Registry(reviewed_dir, overlay_dir)
    registry.load()

    # The librarian's own change (a block after a denial) goes to the overlay and wins.
    from librarian.registry import EntryStatus

    await registry.set_status("tracker", EntryStatus.BLOCKED)
    assert not (reviewed_dir / "tracker.yaml").read_text().count("blocked")
    registry.load()
    assert registry.get("tracker").status is EntryStatus.BLOCKED  # type: ignore[union-attr]

    # A learned API lives only in the overlay.
    await registry.save(
        ApiEntry.model_validate({**TRACKER, "id": "learned", "status": "provisional"})
    )
    assert (overlay_dir / "learned.yaml").exists()

    # A reviewer merges a newer version in git: the reviewed entry now wins.
    newer = datetime.now(UTC) + timedelta(minutes=1)
    write_entries(reviewed_dir, {**TRACKER, "status": "verified", "updated_at": newer.isoformat()})
    assert registry.reload_if_changed()
    assert registry.get("tracker").status is EntryStatus.VERIFIED  # type: ignore[union-attr]
    assert registry.get("learned") is not None
