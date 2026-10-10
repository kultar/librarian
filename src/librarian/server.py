"""The A2A server: agent card, skill dispatch, task lifecycle and caller auth.

Message format (A2A `SendMessage`):
- a data part `{"skill": "execute", "api": ..., "operation": ..., "args": {...}}`
- a data part `{"skill": "capabilities", "query": "..."}`         (optional query)
- a data part `{"skill": "onboard", "api": ..., "spec_url" | "base_url": ...}`
- a data part `{"skill": "fetch", "intent": "..."}`, or plain text (= fetch)
An optional `"principal"` (in the data part or message metadata) names who the
calling agent acts for; it is recorded for audit.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
from collections.abc import AsyncGenerator
from typing import Any

from a2a.helpers.proto_helpers import (
    get_data_parts,
    new_data_part,
    new_task_from_user_message,
    new_text_part,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandlerV2
from a2a.server.routes import (
    DefaultServerCallContextBuilder,
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.server.tasks.database_task_store import DatabaseTaskStore
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from a2a.server.tasks.task_store import TaskStore
from a2a.server.tasks.task_updater import TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)
from opentelemetry import propagate, trace
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from librarian import __version__, telemetry
from librarian.calls import Caller, CallRequest
from librarian.credentials import CredentialRequest
from librarian.executor import PolicyDenied
from librarian.service import CredentialUnavailable, Librarian, LibrarianError
from librarian.web import credential_routes

log = logging.getLogger(__name__)

RPC_PATH = "/"
CALLER_STATE_KEY = "librarian_caller"
TRACE_STATE_KEY = "librarian_trace"


def build_agent_card(librarian: Librarian, *, require_auth: bool | None = None) -> AgentCard:
    settings = librarian.settings
    skills = [
        AgentSkill(
            id="execute",
            name="Execute an API read",
            description=(
                "Run one read-only operation on a registered API. Deterministic: no LLM. "
                'Send a data part: {"skill":"execute","api":"jira","operation":"getIssue",'
                '"args":{"issueIdOrKey":"ABC-1"}}.'
            ),
            tags=["api", "read-only", "deterministic"],
            examples=['{"skill":"execute","api":"github","operation":"repos_get","args":{}}'],
        ),
        AgentSkill(
            id="capabilities",
            name="List capabilities",
            description=(
                'Which APIs and read operations are available. {"skill":"capabilities",'
                '"query":"optional keywords"}.'
            ),
            tags=["discovery"],
        ),
    ]
    if settings.onboarding_enabled:
        skills.append(
            AgentSkill(
                id="onboard",
                name="Learn a new API",
                description=(
                    "Learn an API from its OpenAPI spec and register it (provisional, read-only "
                    'until reviewed). {"skill":"onboard","api":"jira","spec_url":"https://..."} '
                    'or {"skill":"onboard","api":"x","base_url":"https://..."} to auto-discover.'
                ),
                tags=["onboarding"],
            )
        )
    if librarian.planner is not None:
        skills.append(
            AgentSkill(
                id="fetch",
                name="Fetch data (plain language)",
                description=(
                    "Describe the data you need in plain language; the librarian finds the API, "
                    "plans read calls (policy-checked) and returns results with provenance."
                ),
                tags=["api", "read-only", "llm"],
                examples=["List the open bugs assigned to the payments team in Jira."],
            )
        )

    card = AgentCard(
        name="librarian",
        description=(
            "Central API librarian: learns APIs on demand and gives other agents safe, "
            "read-only, audited access to them. Credentials never leave the librarian."
        ),
        version=__version__,
        provider=AgentProvider(organization="librarian", url="https://github.com/kultar/librarian"),
        supported_interfaces=[
            AgentInterface(
                url=settings.public_url.rstrip("/") + RPC_PATH,
                protocol_binding="JSONRPC",
                protocol_version="1.0",
            )
        ],
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        default_input_modes=["application/json", "text/plain"],
        default_output_modes=["application/json", "text/plain"],
        skills=skills,
    )
    if require_auth if require_auth is not None else bool(settings.callers):
        card.security_schemes["bearer"].CopyFrom(
            SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(
                    scheme="Bearer", description="Per-agent API key issued by the operator."
                )
            )
        )
        card.security_requirements.append(
            SecurityRequirement(schemes={"bearer": StringList(list=[])})
        )
    return card


class LibrarianExecutor(AgentExecutor):
    def __init__(self, librarian: Librarian) -> None:
        self.librarian = librarian

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        message = context.message
        if message is None:
            raise ValueError("no message in request")
        task = context.current_task or new_task_from_user_message(message)
        if context.current_task is None:
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)

        caller = _caller_from(context)
        command = _parse_command(context)
        if command.get("principal") and isinstance(command["principal"], str):
            caller = caller.model_copy(update={"principal": command["principal"][:200]})

        async def on_auth_required(request: CredentialRequest) -> None:
            await updater.requires_auth(
                updater.new_agent_message(
                    [
                        new_text_part(
                            f"{request.api_name} needs a credential. An approver has been "
                            f"notified (request {request.id}); this task resumes automatically "
                            "once access is granted."
                        ),
                        new_data_part(
                            {
                                "credential_request": request.id,
                                "api": request.api,
                                "state": request.state.value,
                            }
                        ),
                    ]
                )
            )

        await updater.start_work()
        skill = str(command.get("skill") or "fetch")
        carrier = context.call_context.state.get(TRACE_STATE_KEY)
        span = telemetry.tracer.start_span(
            "librarian.skill",
            context=propagate.extract(carrier) if isinstance(carrier, dict) else None,
            attributes={"librarian.skill": skill, "librarian.caller": caller.agent},
        )
        try:
            with trace.use_span(span, end_on_exit=True):
                result = await self._run(skill, command, caller, on_auth_required)
        except PolicyDenied as exc:
            await updater.reject(
                updater.new_agent_message([new_text_part(f"Refused: {exc.reason}")])
            )
            return
        except CredentialUnavailable as exc:
            await updater.failed(updater.new_agent_message([new_text_part(str(exc))]))
            return
        except (LibrarianError, ValidationError, ValueError) as exc:
            await updater.failed(updater.new_agent_message([new_text_part(f"Error: {exc}")]))
            return

        await updater.add_artifact([new_data_part(json_safe(result))], name=f"{skill}-result")
        await updater.complete()

    async def _run(
        self,
        skill: str,
        command: dict[str, Any],
        caller: Caller,
        on_auth_required: Any,
    ) -> dict[str, Any]:
        librarian = self.librarian
        if skill == "execute":
            request = CallRequest.model_validate(
                {
                    k: command[k]
                    for k in ("api", "operation", "args", "body", "max_pages", "select")
                    if k in command
                }
            )
            result = await librarian.execute(request, caller, on_auth_required)
            return result.model_dump(mode="json")
        if skill == "capabilities":
            query = command.get("query")
            return {"apis": librarian.capabilities(str(query) if query else None, caller)}
        if skill == "onboard":
            api = command.get("api")
            if not isinstance(api, str):
                raise LibrarianError("onboard needs an 'api' id")
            outcome = await librarian.onboard(
                api,
                spec_url=_opt_str(command.get("spec_url")),
                base_url=_opt_str(command.get("base_url")),
                name=_opt_str(command.get("name")),
                caller=caller,
            )
            return {
                "api": outcome.entry.id,
                "status": outcome.entry.status.value,
                "base_url": outcome.entry.base_url,
                "operations": len(outcome.entry.operations),
                "read_operations": outcome.read_operations,
                "needs_review": outcome.needs_review,
                "auth": outcome.entry.auth.type,
                "review_url": outcome.review_url,
                "review_error": outcome.review_error,
            }
        if skill == "fetch":
            intent = command.get("intent")
            if not isinstance(intent, str) or not intent.strip():
                raise LibrarianError("fetch needs a plain-language 'intent'")
            outcome = await librarian.fetch(intent, caller, on_auth_required)
            return {
                "answer": outcome.answer.model_dump(mode="json"),
                "results": [r.model_dump(mode="json") for r in outcome.results],
            }
        raise LibrarianError(f"unknown skill {skill!r}")

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if task is not None:
            await TaskUpdater(event_queue, task.id, task.context_id).cancel()


MAX_SAFE_INT = 2**53


def json_safe(value: Any) -> Any:
    """Prepare a result for an A2A data part.

    A2A data parts are protobuf `Value`s, which store every number as a double.
    Integers beyond 2^53 (e.g. 64-bit IDs) would silently lose precision, so they
    are sent as strings instead.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and abs(value) > MAX_SAFE_INT:
        return str(value)
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, list):
        return [json_safe(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
    return value


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _parse_command(context: RequestContext) -> dict[str, Any]:
    message = context.message
    if message is None:
        raise LibrarianError("no message in request")
    command: dict[str, Any] = {}
    for data in get_data_parts(message.parts):
        if isinstance(data, dict):
            command.update(data)
    if "skill" not in command:
        text = context.get_user_input().strip()
        if text:
            command.setdefault("intent", text)
            command["skill"] = "fetch"
    meta = context.metadata or {}
    if "principal" in meta and "principal" not in command:
        command["principal"] = meta["principal"]
    return command


def _caller_from(context: RequestContext) -> Caller:
    caller = context.call_context.state.get(CALLER_STATE_KEY)
    return caller if isinstance(caller, Caller) else Caller()


class CallerContextBuilder(DefaultServerCallContextBuilder):
    """Copies the caller identity set by `CallerAuthMiddleware` into the A2A context."""

    def build(self, request: Request) -> Any:
        context = super().build(request)
        caller = request.scope.get(CALLER_STATE_KEY)
        context.state[CALLER_STATE_KEY] = caller if isinstance(caller, Caller) else Caller()
        # The SDK runs the executor in a background task that does not inherit the
        # request's OTel context, so carry the trace across explicitly.
        carrier: dict[str, str] = {}
        propagate.inject(carrier)
        context.state[TRACE_STATE_KEY] = carrier
        return context


class CallerAuthMiddleware(BaseHTTPMiddleware):
    """Authenticates calling agents on the A2A endpoint with per-agent bearer keys."""

    def __init__(self, app: Any, keys: dict[str, str], profiles: dict[str, Caller]) -> None:
        super().__init__(app)
        self.keys = keys  # agent -> key
        self.profiles = profiles  # agent -> permissions

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path != RPC_PATH or request.method != "POST":
            return await call_next(request)
        if not self.keys:
            request.scope[CALLER_STATE_KEY] = Caller(agent="anonymous", authenticated=False)
            return await call_next(request)
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        agent = next(
            (name for name, key in self.keys.items() if token and hmac.compare_digest(token, key)),
            None,
        )
        if agent is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        profile = self.profiles.get(agent, Caller(agent=agent))
        request.scope[CALLER_STATE_KEY] = profile.model_copy(update={"authenticated": True})
        return await call_next(request)


async def _caller_keys(librarian: Librarian) -> dict[str, str]:
    keys: dict[str, str] = {}
    for caller in librarian.settings.callers:
        value = await librarian.secrets.get(caller.key_ref)
        if value is None:
            raise RuntimeError(f"caller {caller.agent!r}: secret {caller.key_ref!r} not found")
        keys[caller.agent] = value
    return keys


def create_app(librarian: Librarian, caller_keys: dict[str, str] | None = None) -> Starlette:
    """Build the ASGI app. `caller_keys` overrides keys loaded from settings (tests)."""
    card = build_agent_card(
        librarian, require_auth=bool(librarian.settings.callers) or bool(caller_keys)
    )
    task_store: TaskStore = (
        DatabaseTaskStore(librarian.engine, table_name="librarian_tasks")
        if librarian.engine is not None
        else InMemoryTaskStore()
    )
    handler = DefaultRequestHandlerV2(
        agent_executor=LibrarianExecutor(librarian),
        task_store=task_store,
        agent_card=card,
    )

    async def healthz(_: Request) -> Response:
        return JSONResponse(
            {
                "status": "ok",
                "apis": len(librarian.registry.all()),
                "invalid_registry_files": len(librarian.registry.errors),
            }
        )

    routes = [
        *create_agent_card_routes(card),
        *create_jsonrpc_routes(handler, RPC_PATH, context_builder=CallerContextBuilder()),
        *credential_routes(librarian.broker, librarian.settings.approvers),
        Route("/healthz", healthz, methods=["GET"]),
    ]

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None]:
        await librarian.start()
        keys = caller_keys if caller_keys is not None else await _caller_keys(librarian)
        app.state.caller_keys.update(keys)
        sweeper = asyncio.create_task(_sweep_forever(librarian))
        try:
            yield
        finally:
            sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweeper
            await librarian.aclose()

    profiles = {
        c.agent: Caller(agent=c.agent, apis=tuple(c.apis), can_onboard=c.can_onboard)
        for c in librarian.settings.callers
    }
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.caller_keys = {}
    app.add_middleware(CallerAuthMiddleware, keys=app.state.caller_keys, profiles=profiles)
    return app


async def _sweep_forever(librarian: Librarian) -> None:
    """Housekeeping: pick up registry changes, send reminders, expire requests."""
    ticks = 0
    while True:
        await asyncio.sleep(15)
        ticks += 1
        try:
            if librarian.registry.reload_if_changed():
                log.info("registry reloaded: %d APIs", len(librarian.registry.all()))
        except Exception:
            log.exception("registry reload failed")
        if ticks % 4 == 0:
            try:
                await librarian.broker.sweep()
            except Exception:
                log.exception("credential sweep failed")
