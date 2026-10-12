from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from typer.testing import CliRunner

from librarian.cli import app
from tests.conftest import Router
from tests.test_state import replica
from tests.test_writes import KIM, approve_when_open, comment


@pytest.fixture
def state(tmp_path: Path, router: Router, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("LIBRARIAN_REGISTRY_DIR", str(tmp_path / "registry"))
    monkeypatch.setenv("LIBRARIAN_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setenv("LIBRARIAN_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/state.db")
    monkeypatch.setenv("LIBRARIAN_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    return tmp_path


def test_grants_list_and_revoke(state: Path, router: Router) -> None:
    async def make_grant() -> None:
        lib = replica(state, router)
        await lib.start()
        await lib.secrets.put("tracker/write", "w")
        import httpx

        router.add(
            "POST",
            "https://api.example.test/v1/issues/PAY-1/comments",
            lambda r: httpx.Response(201, json={}),
        )
        task = asyncio.create_task(lib.write(comment(), KIM))
        await approve_when_open(lib, remember=True, pin=["key"])
        await asyncio.wait_for(task, 5)
        await lib.aclose()

    asyncio.run(make_grant())
    runner = CliRunner()
    listed = runner.invoke(app, ["grants", "list"])
    assert listed.exit_code == 0, listed.output
    assert "notes-bot → tracker.addComment" in listed.output
    grant_id = listed.output.split()[0]
    revoked = runner.invoke(app, ["grants", "revoke", grant_id, "--by", "ops@x.test"])
    assert revoked.exit_code == 0 and "revoked" in revoked.output
    assert runner.invoke(app, ["grants", "list"]).output.strip() == ""
    assert "revoked" in runner.invoke(app, ["grants", "list", "--all"]).output
    assert runner.invoke(app, ["grants", "revoke", "nope"]).exit_code == 1


def test_example_config_loads() -> None:
    from librarian.config import Settings

    settings = Settings.load(Path(__file__).parent.parent / "examples" / "librarian.yaml")
    assert settings.principals.mode == "off"
    assert settings.drift.recheck_hours == 24


def test_bare_yaml_off_means_off(tmp_path: Path) -> None:
    from librarian.config import Settings

    config = tmp_path / "c.yaml"
    config.write_text("principals:\n  mode: off\n")
    assert Settings.load(config).principals.mode == "off"


def test_learn_then_list_in_a_fresh_folder_with_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a new user does first: no config, the default SQLite database, an empty folder."""
    from typing import Any

    from librarian import service as service_module
    from librarian.cli import app
    from tests.test_openapi import SPEC

    async def fake_fetch(url: str, client: Any, *, net: Any) -> Any:
        return SPEC

    monkeypatch.setattr(service_module, "fetch_spec", fake_fetch)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    learned = runner.invoke(app, ["learn", "jira", "--spec-url", "https://x.example/spec.json"])
    assert learned.exit_code == 0, learned.output
    assert "Saved jira (in the database)" in learned.output
    listed = runner.invoke(app, ["registry", "list"])
    assert listed.exit_code == 0, listed.output
    assert "jira" in listed.output and "provisional" in listed.output
