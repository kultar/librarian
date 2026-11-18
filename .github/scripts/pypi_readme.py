"""PyPI shows the README as the package's page. There, a link to a file of this repository
leads nowhere: `docs/deploy.md` is looked for on pypi.org. Before the package is built, the
release points every such link at that file on GitHub, as it is at the release's tag.

    python .github/scripts/pypi_readme.py v1.2.3      rewrites README.md in place
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPOSITORY = "https://github.com/kultar/librarian"
# A Markdown link or an HTML one, to something that is not a web address. (A link to a
# place on the page itself begins with #, and a path here cannot.)
LINK = re.compile(r'(\]\(|href=")(?!https?://|mailto:)([^)"\s#]+)(#[^)"\s]*)?')


def absolute(text: str, tag: str, root: Path) -> str:
    """The README with its links into the repository made links to GitHub at `tag`."""

    def to_github(found: re.Match[str]) -> str:
        opening, path, anchor = found.group(1), found.group(2), found.group(3) or ""
        kind = "tree" if (root / path).is_dir() else "blob"
        return f"{opening}{REPOSITORY}/{kind}/{tag}/{path}{anchor}"

    return LINK.sub(to_github, text)


if __name__ == "__main__":
    readme = Path("README.md")
    readme.write_text(absolute(readme.read_text("utf-8"), sys.argv[1], Path.cwd()), "utf-8")
