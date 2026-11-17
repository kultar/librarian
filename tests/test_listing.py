"""What tells the world where librarian is has to agree with what is released.

`server.json` is librarian's entry in the MCP Registry. The registry lists a package only if
the package itself names the server, and a client that finds librarian there starts it the
way the file says: each of those is checked here, because none of them fails until a release
is already out."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

import typer
import yaml

import librarian
from librarian import cli
from librarian.config import Settings

ROOT = Path(__file__).parent.parent
SERVER: dict[str, Any] = json.loads((ROOT / "server.json").read_text("utf-8"))
NAME: str = SERVER["name"]
PACKAGES: dict[str, dict[str, Any]] = {p["registryType"]: p for p in SERVER["packages"]}


def test_the_entry_names_the_version_that_is_released() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]
    assert project["version"] == librarian.__version__
    assert SERVER["version"] == librarian.__version__
    assert PACKAGES["pypi"]["identifier"] == project["name"]
    assert PACKAGES["pypi"]["version"] == librarian.__version__
    assert PACKAGES["oci"]["identifier"] == f"ghcr.io/kultar/librarian:{librarian.__version__}"
    assert 0 < len(SERVER["description"]) <= 100  # the registry refuses a longer one


def test_each_package_says_it_is_this_server() -> None:
    """How the registry checks that whoever publishes the entry owns the packages."""
    assert NAME == "io.github.kultar/librarian"
    readme = (ROOT / "README.md").read_text("utf-8")  # the description on PyPI
    assert re.search(rf"mcp-name: {re.escape(NAME)}(\s|-->)", readme)
    dockerfile = (ROOT / "Dockerfile").read_text("utf-8")
    assert f'LABEL io.modelcontextprotocol.server.name="{NAME}"' in dockerfile


def test_a_client_can_start_it_the_way_the_entry_says() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]
    package = PACKAGES["pypi"]
    # `uvx <package> ...` runs the command that has the package's name.
    assert package["runtimeHint"] == "uvx"
    assert project["scripts"][package["identifier"]] == project["scripts"]["librarian"]
    command, port = package["packageArguments"]
    commands: Any = typer.main.get_command(cli.app)
    serve = commands.commands[command["value"]]  # the entry names a command that exists
    assert port["name"] in {name for option in serve.params for name in option.opts}
    # ...and its MCP endpoint is where the entry says: the default address, that port.
    assert package["transport"] == {
        "type": "streamable-http",
        "url": "http://127.0.0.1:{" + port["name"] + "}/mcp",
    }
    assert port["default"] == str(Settings.model_fields["port"].default)
    assert Settings.model_fields["host"].default == "127.0.0.1"
    image = PACKAGES["oci"]
    assert image["transport"]["url"] == f"http://127.0.0.1:{port['default']}/mcp"
    published = next(a["value"] for a in image["runtimeArguments"] if a["name"] == "-p")
    assert published == f"127.0.0.1:{port['default']}:{port['default']}"


def test_the_skill_is_one_an_agent_platform_accepts_and_is_in_the_image() -> None:
    """`skills/librarian` is an Agent Skill. What the format asks of its first lines is
    checked here, because a platform refuses a skill that does not keep to it only when an
    agent is started with it."""
    folder = ROOT / "skills" / "librarian"
    text = (folder / "SKILL.md").read_text("utf-8")
    assert text.startswith("---\n")
    front, body = text[4:].split("\n---\n", 1)
    meta = yaml.safe_load(front)
    assert meta["name"] == folder.name and re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", meta["name"])
    assert 0 < len(meta["description"]) <= 1024
    assert body.strip()
    # Nothing but files an agent reads: a platform that mounts the folder checks every entry.
    assert all(path.is_file() and not path.is_symlink() for path in folder.rglob("*"))
    # Every tool the skill tells an agent to use is one librarian has.
    named = set(re.findall(r"`([a-z_]+)`", body)) & {
        "capabilities", "execute", "fetch", "write", "write_plan", "write_status",
        "enable_status", "onboard",
    }  # fmt: skip
    assert named == {
        "capabilities", "execute", "fetch", "write", "write_plan", "write_status",
        "enable_status", "onboard",
    }  # fmt: skip
    # The image carries it where the integrations guide says a platform finds it.
    assert "\nCOPY skills /skills\n" in (ROOT / "Dockerfile").read_text("utf-8")
    guide = (ROOT / "docs" / "integrations.md").read_text("utf-8")
    assert "path: skills/librarian" in guide
