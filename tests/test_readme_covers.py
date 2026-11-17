"""Nothing ships that the README does not name.

The README is what people read to learn what librarian does. A command, a setting, a tool
or a kind of authentication that is only in the code, or only in `docs/`, does not exist
for them. These tests fail when one is added without its place in the README, and when the
README still lists one that is gone."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, get_args, get_origin

import pytest
import typer
from pydantic import BaseModel

from librarian import cli
from librarian.config import Settings
from librarian.mcp_server import build_mcp
from librarian.registry.models import ApiEntry, AuthSpec
from librarian.server import build_agent_card
from librarian.service import Librarian

README = (Path(__file__).parent.parent / "README.md").read_text("utf-8")


def section(heading: str) -> str:
    """The README from this heading to the next one of the same or a higher level."""
    level = heading.split(" ", 1)[0]
    start = README.index(f"\n{heading}\n")
    following = re.search(rf"\n#{{1,{len(level)}}} ", README[start + 1 :])
    return README[start : start + 1 + following.start()] if following else README[start:]


def commands(app: typer.Typer, prefix: str = "") -> list[str]:
    names: list[str] = []
    for command in app.registered_commands:
        callback = command.callback
        assert callback is not None
        names.append(prefix + (command.name or callback.__name__.replace("_", "-")))
    for group in app.registered_groups:
        assert group.typer_instance is not None and group.name is not None
        names += commands(group.typer_instance, f"{prefix}{group.name} ")
    return names


def models_in(annotation: Any) -> list[tuple[type[BaseModel], bool]]:
    """The settings models inside an annotation, and whether they are items of a list."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [(annotation, False)]
    listed = get_origin(annotation) is list
    return [
        (model, inner or listed) for arg in get_args(annotation) for model, inner in models_in(arg)
    ]


def settings(model: type[BaseModel] = Settings, prefix: str = "") -> list[str]:
    """Every setting as it is written in the README: `writes.grants[].agent`."""
    paths: list[str] = []
    for name, info in model.model_fields.items():
        inner = [] if get_origin(info.annotation) is dict else models_in(info.annotation)
        if inner:
            sub, listed = inner[0]
            paths += settings(sub, f"{prefix}{name}{'[]' if listed else ''}.")
        else:
            paths.append(prefix + name)
    return paths


def test_every_command_is_in_the_readme_and_none_that_is_gone() -> None:
    table = section("### Commands")
    listed = {
        re.split(r' [<\["]', row, maxsplit=1)[0]
        for row in re.findall(r"^\| `librarian ([^`]+)`", table, flags=re.MULTILINE)
    }
    assert listed == set(commands(cli.app))


def test_every_setting_is_in_the_readme_and_none_that_is_gone() -> None:
    tables = section("### Settings")
    listed = set(re.findall(r"^\| `([^`]+)` \|", tables, flags=re.MULTILINE))
    known = set(settings())
    assert known - listed == set(), "settings the README does not list"
    assert listed - known == set(), "settings the README lists that do not exist"
    assert len(known) > 100  # the walk found the nested ones too


def test_the_defaults_in_the_readme_are_the_ones_in_the_code() -> None:
    """For the settings with a plain default: what the README says is what happens."""
    rows = dict(
        re.findall(r"^\| `([^`]+)` \| `([^`]+)` \|", section("### Settings"), flags=re.MULTILINE)
    )
    defaults = Settings(_env_file=None).model_dump(mode="json")  # pyright: ignore[reportCallIssue]
    checked = 0
    for path, shown in rows.items():
        if "[]" in path or path.startswith("publish.github_app."):
            continue  # items of a list and optional blocks have no instance to read
        value: Any = defaults
        for part in path.split("."):
            value = value[part]
        if isinstance(value, int | float) and not isinstance(value, bool):
            assert float(shown) == value, path  # 24 and 24.0 are the same default
        else:
            expected = str(value).lower() if isinstance(value, bool) else str(value)
            assert shown.replace('"', "'") == expected.replace('"', "'"), path
        checked += 1
    assert checked > 50


@pytest.mark.parametrize("kind", ["tool", "skill"])
async def test_every_tool_and_skill_is_in_the_readme(librarian: Librarian, kind: str) -> None:
    librarian.settings.model = "test"  # `fetch` is only offered with a model
    if kind == "tool":
        names = {tool.name for tool in await build_mcp(librarian).list_tools()}
    else:
        names = {skill.id for skill in build_agent_card(librarian).skills}
    assert names, kind
    where = section("## Skills (A2A)")
    missing = {name for name in names if f"`{name}`" not in where}
    assert missing == set(), f"{kind}s the README's skills section does not name"


def entry_fields(model: type[BaseModel], seen: set[type[BaseModel]]) -> set[str]:
    """Every field of a registry entry, by the name it has in a file."""
    if model in seen:
        return set()
    seen.add(model)
    names: set[str] = set()
    for name, info in model.model_fields.items():
        names.add(info.alias or name)
        for sub, _ in models_in(info.annotation):
            names |= entry_fields(sub, seen)
    return names


def test_every_field_of_a_registry_entry_is_in_the_format_reference() -> None:
    """The README says the entry format is described in full in docs/registry.md."""
    assert "Every field of an entry is described\nin [docs/registry.md]" in README
    reference = (Path(__file__).parent.parent / "docs" / "registry.md").read_text("utf-8")
    fields = entry_fields(ApiEntry, set())
    assert len(fields) > 80
    missing = {name for name in fields if not re.search(rf"\b{re.escape(name)}\b", reference)}
    assert missing == set(), "fields of an entry that docs/registry.md does not name"


def test_every_kind_of_authentication_is_in_the_readme() -> None:
    union = get_args(AuthSpec)[0]
    kinds = {get_args(model.model_fields["type"].annotation)[0] for model in get_args(union)}
    assert len(kinds) == 8
    table = section("## The registry")
    listed = {
        kind
        for row in re.findall(r"^\| (`[^|]+`) \|", table, flags=re.MULTILINE)
        for kind in re.findall(r"`([a-z0-9_]+)`", row)
    }
    assert listed - {"type"} == kinds  # "type" is the column heading
