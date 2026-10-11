"""Command-line interface: `librarian serve | learn | call | registry ...`."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Annotated, Any

import typer

from librarian.approvals import ApprovalError
from librarian.calls import Caller, CallRequest
from librarian.config import Settings
from librarian.executor import CredentialMissing, PolicyDenied
from librarian.registry.models import Access
from librarian.service import SOURCE_LABELS, Librarian, LibrarianError

# librarian is a server/CLI: keep Pydantic AI's interactive banner out of its output.
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

app = typer.Typer(
    help="An A2A agent that learns APIs and serves them read-only.", no_args_is_help=True
)
registry_app = typer.Typer(help="Inspect and validate the registry.", no_args_is_help=True)
app.add_typer(registry_app, name="registry")
grants_app = typer.Typer(help="List and revoke write grants.", no_args_is_help=True)
app.add_typer(grants_app, name="grants")
approvals_app = typer.Typer(help="Inspect write approvals.", no_args_is_help=True)
app.add_typer(approvals_app, name="approvals")

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

    from librarian.server import create_app, create_pages_app

    logging.basicConfig(level=log_level.upper(), format="%(levelname)s %(name)s: %(message)s")
    from librarian.telemetry import asgi_middleware, setup_telemetry

    settings = _settings(config)
    if settings.otel_enabled:
        setup_telemetry(settings.otel_service_name)
    librarian = Librarian(settings)
    separate = settings.pages_port is not None
    app_: Any = create_app(librarian, surface="a2a" if separate else "all")
    if settings.otel_enabled:
        app_ = asgi_middleware(app_)
    main = uvicorn.Config(
        app_, host=host or settings.host, port=port or settings.port, log_level=log_level
    )
    if not separate:
        uvicorn.Server(main).run()
        return
    pages = uvicorn.Config(
        create_pages_app(librarian),
        host=settings.pages_host,
        port=settings.pages_port or 8081,
        log_level=log_level,
        lifespan="off",
    )

    async def both() -> None:
        await asyncio.gather(uvicorn.Server(main).serve(), uvicorn.Server(pages).serve())

    asyncio.run(both())


@app.command()
def learn(
    api_id: Annotated[str, typer.Argument(help="Registry id for the API, e.g. 'jira'")],
    spec_url: Annotated[str | None, typer.Option(help="URL of the OpenAPI/Swagger spec")] = None,
    base_url: Annotated[
        str | None, typer.Option(help="API base URL (spec auto-discovered if no spec_url)")
    ] = None,
    docs_url: Annotated[
        str | None,
        typer.Option(
            help="Documentation page to learn from when there is no spec (needs base_url)"
        ),
    ] = None,
    name: Annotated[str | None, typer.Option(help="Display name")] = None,
    config: ConfigOption = None,
) -> None:
    """Learn an API (spec, docs, or model knowledge) and save a provisional entry."""

    async def run() -> None:
        librarian = Librarian(_settings(config))
        librarian.load()
        try:
            outcome = await librarian.onboard(
                api_id, spec_url=spec_url, base_url=base_url, docs_url=docs_url, name=name
            )
        finally:
            await librarian.aclose()
        typer.echo(
            f"Saved {outcome.path}: {len(outcome.entry.operations)} operations, "
            f"{outcome.read_operations} read, {outcome.needs_review} need review "
            f"(auth: {outcome.entry.auth.type}, "
            f"learned from {SOURCE_LABELS[outcome.entry.source.kind]})"
        )
        if outcome.entry.source.kind in ("docs", "inferred"):
            typer.echo(
                "Operations are unconfirmed until a real call succeeds; ones that keep "
                "failing are suspended."
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
        await librarian.start()  # durable state: saved recipes, credentials, approvals
        try:
            outcome = await librarian.fetch(intent, Caller(agent="cli"))
        finally:
            await librarian.aclose()
        if outcome.recipe:
            typer.echo(f"(answered from saved recipe {outcome.recipe}, no model call)")
        typer.echo(outcome.answer.summary)
        if outcome.answer.unresolved:
            typer.echo(f"\nUnresolved: {outcome.answer.unresolved}")
        for result in outcome.results:
            p = result.provenance
            typer.echo(f"  - {p.api}.{p.operation} → HTTP {result.status_code} ({p.url})")

    _run(run())


@app.command()
def discover(api: str, config: ConfigOption = None) -> None:
    """Run an API's discovery reads (e.g. Jira custom field names) into its glossary."""

    async def run() -> None:
        librarian = Librarian(_settings(config))
        librarian.load()
        try:
            count = await librarian.discover(api)
        finally:
            await librarian.aclose()
        typer.echo(f"{api}: {count} glossary entries")

    _run(run())


recipes_app = typer.Typer(help="Saved plans replayed without the LLM.", no_args_is_help=True)
app.add_typer(recipes_app, name="recipes")


@recipes_app.command("list")
def recipes_list(config: ConfigOption = None) -> None:
    """Saved recipes, most used first."""

    async def run() -> None:
        librarian = Librarian(_settings(config))
        await librarian.start()
        try:
            for r in sorted(librarian.recipes.values(), key=lambda r: -r.uses):
                steps = ", ".join(f"{s.api}.{s.operation}" for s in r.steps)
                typer.echo(f"{r.id}  uses={r.uses:<4} '{r.template}' -> {steps}")
        finally:
            await librarian.aclose()

    _run(run())


@recipes_app.command("delete")
def recipes_delete(recipe_id: str, config: ConfigOption = None) -> None:
    """Forget a recipe (the planner will be used again for such requests)."""

    async def run() -> None:
        librarian = Librarian(_settings(config))
        await librarian.start()
        try:
            await librarian.delete_recipe(recipe_id)
        finally:
            await librarian.aclose()
        typer.echo(f"deleted {recipe_id}")

    _run(run())


@registry_app.command("recheck")
def registry_recheck(
    api_id: Annotated[str | None, typer.Argument(help="One API (default: all)")] = None,
    config: ConfigOption = None,
) -> None:
    """Re-read APIs' specs or docs now and report (and handle) any changes."""

    async def run() -> None:
        librarian = Librarian(_settings(config))
        await librarian.start()
        try:
            ids = [api_id] if api_id else [e.id for e in librarian.registry.all()]
            for item in ids:
                report = await librarian.recheck(item)
                typer.echo(
                    f"{report.api:<24} {report.status}"
                    + (f"  {report.detail}" if report.detail else "")
                )
                for change in report.changes:
                    typer.echo(f"    [{change.severity}] {change.detail}")
                for action in report.actions:
                    typer.echo(f"    -> {action}")
                if report.review_url:
                    typer.echo(f"    review: {report.review_url}")
        finally:
            await librarian.aclose()

    _run(run())


@registry_app.command("clear")
def registry_clear(
    api_id: str,
    operation: str,
    config: ConfigOption = None,
) -> None:
    """Lift a suspension or warning on one operation after checking it by hand."""

    async def run() -> None:
        librarian = Librarian(_settings(config))
        await librarian.start()
        try:
            entry = librarian.registry.get(api_id)
            op = entry.operation(operation) if entry is not None else None
            if entry is None or op is None:
                raise typer.BadParameter(f"unknown operation {api_id}.{operation}")
            if op.suspended:
                typer.echo(
                    "This suspension is in the registry entry itself; remove `suspended` "
                    "there (via review).",
                    err=True,
                )
            cleared = await librarian.observations.clear(entry, op)
        finally:
            await librarian.aclose()
        typer.echo("cleared" if cleared else "nothing to clear")

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
    except (LibrarianError, PolicyDenied, CredentialMissing, ApprovalError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@grants_app.command("list")
def grants_list(
    all_: Annotated[bool, typer.Option("--all", help="Include expired/revoked grants")] = False,
    config: ConfigOption = None,
) -> None:
    """Standing write permissions (from approvals and from config)."""

    async def run() -> None:
        librarian = Librarian(_settings(config))
        await librarian.start()
        try:
            for g in librarian.approvals.grants(include_inactive=all_):
                limits = []
                if g.max_uses is not None:
                    limits.append(f"{g.uses}/{g.max_uses} uses")
                if g.max_uses_per_day is not None:
                    limits.append(f"{g.max_uses_per_day}/day")
                if g.expires_at is not None:
                    limits.append(f"until {g.expires_at:%Y-%m-%d %H:%M}")
                state = "revoked" if g.revoked else "active"
                typer.echo(
                    f"{g.id}  {state:<8} {g.agent} → {g.api}.{g.operation} "
                    f"pinned={g.pinned_args or '{}'} ({', '.join(limits) or 'no limits'}; "
                    f"by {g.created_by}, {g.source})"
                )
        finally:
            await librarian.aclose()

    _run(run())


@grants_app.command("revoke")
def grants_revoke(
    grant_id: str,
    by: Annotated[str, typer.Option(help="Who is revoking (for the audit log)")] = "cli",
    config: ConfigOption = None,
) -> None:
    """Revoke a grant at once (the kill switch for an agent's unattended writes)."""

    async def run() -> None:
        librarian = Librarian(_settings(config))
        await librarian.start()
        try:
            grant = await librarian.approvals.revoke_grant(grant_id, by)
        finally:
            await librarian.aclose()
        note = (
            " (config grants come back on restart: remove them from the config)"
            if (grant.source == "config")
            else ""
        )
        typer.echo(f"revoked {grant.id}: {grant.agent} → {grant.api}.{grant.operation}{note}")

    _run(run())


@approvals_app.command("list")
def approvals_list(config: ConfigOption = None) -> None:
    """Pending write approvals."""

    async def run() -> None:
        librarian = Librarian(_settings(config))
        await librarian.start()
        try:
            for a in librarian.approvals.pending():
                typer.echo(
                    f"{a.id}  {a.agent} → {a.api}.{a.operation} args={a.request.args} "
                    f"for={a.principal or '-'} expires {a.expires_at:%Y-%m-%d %H:%M}"
                )
                typer.echo(f"    {librarian.approvals.link(a)}")
        finally:
            await librarian.aclose()

    _run(run())
