# Releasing

1. Bump `version` in `pyproject.toml` and `__version__` in `src/librarian/__init__.py`, run
   `uv lock`, and merge to `main` with CI green.
2. Tag and push: `git tag v0.4.0 && git push origin v0.4.0`.
   (Or run the **release** workflow manually with the tag; pushes made with an automation token
   do not trigger workflows.)

The release workflow then:

- checks the tag matches the package version and runs the tests;
- builds the wheel and sdist and creates a GitHub release with them;
- builds and pushes `ghcr.io/kultar/librarian:{version, major.minor, latest}` for amd64 and arm64;
- publishes `librarian-a2a` to PyPI **if** enabled (below).

## One-time setup

**Container image visibility.** `ghcr.io/kultar/librarian` is public (it was created public
from this public repository). If a fork's package ends up private, change it under GitHub →
Packages → librarian → Package settings → Change visibility.

**PyPI (trusted publishing, no API token).**

1. On pypi.org → Account → Publishing → *Add a new pending publisher*: project `librarian-a2a`,
   owner `kultar`, repository `librarian`, workflow `release.yml`, environment `pypi`.
2. In the GitHub repo: Settings → Environments → create `pypi`; Settings → Variables → add
   repository variable `PYPI_PUBLISH` = `true`.
