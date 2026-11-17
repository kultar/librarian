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
