"""The docs show a recording of the kagent example and say, for each stack, what to run.
A page that names a clip or an example that is not there shows a broken image or a dead
link to the very people it is meant to convince; and a recording that grows without notice
makes every clone of the repository heavier."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
DEMO = ROOT / "site" / "assets" / "demo"
RAW = "https://raw.githubusercontent.com/kultar/librarian/main/site/assets/demo/"
PAGES = {
    name: (ROOT / name).read_text("utf-8")
    for name in (
        "README.md",
        "docs/integrations.md",
        "examples/kagent/README.md",
        "site/index.html",
    )
}


def test_every_clip_the_docs_show_is_in_the_repository_and_none_lies_unused() -> None:
    shown = {name for text in PAGES.values() for name in re.findall(r"assets/demo/([\w.-]+)", text)}
    assert shown == {path.name for path in DEMO.iterdir()}
    for path in DEMO.iterdir():
        assert path.stat().st_size < 2_000_000, f"{path.name} is too large to keep in git"


def test_each_clip_is_shown_light_and_dark() -> None:
    names = {path.name for path in DEMO.iterdir()}
    for name in names:
        stem, _, kind = name.rpartition(".")
        other = (
            f"{stem.removesuffix('-dark')}.{kind}"
            if stem.endswith("-dark")
            else f"{stem}-dark.{kind}"
        )
        assert other in names, f"{name} has no counterpart for the other colour scheme"
    # In Markdown, by GitHub's address for `main` (a relative one would not show on PyPI's
    # page): the dark clip for a dark page, the light one otherwise.
    for name in ("README.md", "docs/integrations.md", "examples/kagent/README.md"):
        pictures = re.findall(r"<picture>(.*?)</picture>", PAGES[name], flags=re.S)
        clips = [picture for picture in pictures if "assets/demo/" in picture]
        assert clips, f"{name} shows no clip"
        for picture in clips:
            dark = re.search(
                r'<source media="\(prefers-color-scheme: dark\)" srcset="([^"]+)">', picture
            )
            light = re.search(r'<img src="([^"]+)" alt="[^"]{20,}"', picture)
            assert dark is not None and light is not None, name
            assert dark.group(1).startswith(RAW) and dark.group(1).endswith("-dark.gif")
            assert light.group(1) == dark.group(1).replace("-dark.gif", ".gif")
    # On the site, one video per colour scheme, each with its poster.
    videos = re.findall(
        r'<video class="(light|dark)"[^>]*poster="([^"]+)"[^>]*src="([^"]+)"',
        PAGES["site/index.html"],
    )
    assert {scheme for scheme, _, _ in videos} == {"light", "dark"}
    for scheme, poster, source in videos:
        assert ("-dark" in poster) == ("-dark" in source) == (scheme == "dark")


def test_every_stack_in_the_readme_has_something_to_run_that_is_there() -> None:
    readme = PAGES["README.md"]
    section = readme[readme.index("## In the stack you already run") :]
    rows = [
        line
        for line in section[: section.index("\nThree things")].splitlines()
        if line.startswith("| **")
    ]
    assert len(rows) == 5
    for row in rows:
        stack, _added, to_run, tested = (cell.strip() for cell in row.strip("|").split("|"))
        links = re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", to_run)
        assert links, f"nothing to run for {stack}"
        for link in links:
            assert (ROOT / link).exists(), f"{stack}: {link} is not in the repository"
        assert tested, f"{stack} does not say how it is tested"
    # The same five, in the same order, where the integrations guide begins.
    guide = PAGES["docs/integrations.md"]
    opening = guide[: guide.index("## Any A2A client")]
    for row in [line for line in opening.splitlines() if line.startswith("| ") and "](" in line]:
        for link in re.findall(r"\]\((\.\./[^)#]+|[a-z-]+\.md)(?:#[^)]*)?\)", row):
            assert (ROOT / "docs" / link).resolve().exists(), f"{link} is not in the repository"
