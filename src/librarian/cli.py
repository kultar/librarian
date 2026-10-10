"""Command-line interface: `librarian serve | learn | call | registry ...`."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Annotated

import typer

from librarian.calls import Caller, CallRequest
from librarian.config import Settings
from librarian.executor import CredentialMissing, PolicyDenied
from librarian.registry.models import Access
from librarian.service import Librarian, LibrarianError

# librarian is a server/CLI: keep Pydantic AI's interactive banner out of its output.
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

app = typer.Typer(
    help="An A2A agent that learns APIs and serves them read-only.", no_args_is_help=True
)
registry_app = typer.Typer(help="Inspect and validate the registry.", no_args_is_help=True)
app.add_typer(registry_app, name="registry")

ConfigOption = Annotated[
    Path | None, typer.Option("--config", "-c", help="YAML config file", exists=True)
]


def _settings(config: Path | None) -> Settings:
    return Settings.load(config)


@app.command()
def serve(
    config: ConfigOption = None,
    host: Annotated[str | None, typer.Option(help="Bind address")] = None,
    port: Annotated[int | None, typer.Option(help="Port")] = None,
    log_level: str = "info",
) -> None:
    """Run the A2A server."""
    import uvicorn

    from librarian.server import create_app

    logging.basicConfig(level=log_level.upper(), format="%(levelname)s %(name)s: %(message)s")
    settings = _settings(config)
    librarian = Librarian(settings)
    uvicorn.run(
        create_app(librarian),
        host=host or settings.host,
        port=port or settings.port,
        log_level=log_level,
    )


@app.command()
def learn(
    api_id: Annotated[str, typer.Argument(help="Registry id for the API, e.g. 'jira'")],
    spec_url: Annotated[str | None, typer.Option(help="URL of the OpenAPI/Swagger spec")] = None,
    base_url: Annotated[
        str | None, typer.Option(help="API base URL (spec auto-discovered if no spec_url)")
    ] = None,
    name: Annotated[str | None, typer.Option(help="Display name")] = None,
    config: ConfigOption = None,
) -> None:
    """Learn an API from its OpenAPI spec and save a provisional registry entry."""

    async def run() -> None:
        librarian = Librarian(_settings(config))
        librarian.load()
        try:
            outcome = await librarian.onboard(
                api_id, spec_url=spec_url, base_url=base_url, name=name
            )
        finally:
            await librarian.aclose()
        typer.echo(
            f"Saved {outcome.path}: {len(outcome.entry.operations)} operations, "
            f"{outcome.read_operations} read, {outcome.needs_review} need review "
            f"(auth: {outcome.entry.auth.type})"
        )
        if outcome.review_url:
            typer.echo(f"Review: {outcome.review_url}")
        if outcome.review_error:
            typer.echo(f"Warning: {outcome.review_error}", err=True)

    _run(run())


@app.command()
def call(
    api: str,
    operation: str,
    arg: Annotated[
        list[str] | None, typer.Option("--arg", "-a", help="Argument as name=value (repeatable)")
    ] = None,
    pages: int = 1,
    select: Annotated[
        list[str] | None,
        typer.Option("--select", "-s", help="Keep only this dotted path (repeatable)"),
    ] = None,
    config: ConfigOption = None,
) -> None:
    """Run one read operation locally (same policy as the server)."""
    args: dict[str, str] = {}
    for item in arg or []:
        key, sep, value = item.partition("=")
        if not sep:
            raise typer.BadParameter(f"expected name=value, got {item!r}")
        args[key] = value

    async def run() -> None:
        librarian = Librarian(_settings(config))
        librarian.load()
        try:
            request = CallRequest(
                api=api, operation=operation, args=dict(args), max_pages=pages, select=select or []
            )
            result = await librarian.executor.execute(request, Caller(agent="cli"))
        finally:
            await librarian.aclose()
        typer.echo(json.dumps(result.model_dump(mode="json"), indent=2, default=str))

    _run(run())


@app.command()
def ask(
    intent: Annotated[str, typer.Argument(help="Plain-language request")],
    model: Annotated[str | None, typer.Option(help="Override the configured model")] = None,
    config: ConfigOption = None,
) -> None:
    """Run the `fetch` skill locally: plan and run policy-checked reads with the LLM."""
    settings = _settings(config)
    if model is not None:
        settings = settings.model_copy(update={"model": model})

    async def run() -> None:
        librarian = Librarian(settings)
        librarian.load()
        try:
            outcome = await librarian.fetch(intent, Caller(agent="cli"))
        finally:
            await librarian.aclose()
        typer.echo(outcome.answer.summary)
        if outcome.answer.unresolved:
            typer.echo(f"\nUnresolved: {outcome.answer.unresolved}")
        for result in outcome.results:
            p = result.provenance
            typer.echo(f"  - {p.api}.{p.operation} → HTTP {result.status_code} ({p.url})")

    _run(run())


@registry_app.command("list")
def registry_list(config: ConfigOption = None) -> None:
    """List registered APIs."""
    librarian = Librarian(_settings(config))
    librarian.load()
    for entry in librarian.registry.all():
        reads = sum(1 for op in entry.operations if op.access is Access.READ)
        typer.echo(f"{entry.id:<24} {entry.status.value:<12} {reads:>4} reads  {entry.base_url}")
    for name, error in librarian.registry.errors.items():
        typer.echo(f"INVALID {name}: {error}", err=True)


@registry_app.command("validate")
def registry_validate(config: ConfigOption = None) -> None:
    """Validate every registry file; exits non-zero on errors (use in CI)."""
    librarian = Librarian(_settings(config))
    librarian.load()
    errors = librarian.registry.errors
    for name, error in errors.items():
        typer.echo(f"{name}: {error}", err=True)
    typer.echo(f"{len(librarian.registry.all())} valid, {len(errors)} invalid")
    raise typer.Exit(code=1 if errors else 0)


def _run(coro: object) -> None:
    try:
        asyncio.run(coro)  # type: ignore[arg-type]
    except (LibrarianError, PolicyDenied, CredentialMissing) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
