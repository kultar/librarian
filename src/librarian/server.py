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
from typing import Any, Literal

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
from pydantic_ai.exceptions import AgentRunError
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from librarian import __version__, telemetry
from librarian.approvals import WriteApproval
from librarian.calls import Caller, CallRequest, WriteRequest
from librarian.config import PrincipalSettings
from librarian.credentials import CredentialRequest
from librarian.executor import PolicyDenied
from librarian.mcp_server import build_mcp
from librarian.principals import Issuer, PrincipalError, PrincipalVerifier
from librarian.server_state import CALLER_STATE_KEY, TRACE_STATE_KEY
from librarian.service import CredentialUnavailable, Librarian, LibrarianError, WriteRefused
from librarian.state import retry_already_exists
from librarian.web import approval_routes, credential_routes

log = logging.getLogger(__name__)

RPC_PATH = "/"
MCP_PATH = "/mcp"


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
    skills.append(
        AgentSkill(
            id="write",
            name="Request a write",
            description=(
                "Change data through an API, subject to human approval (or a standing grant). "
                'Send {"skill":"write","api":"jira","operation":"addComment","args":{...},'
                '"body":{...},"reason":"why","idempotency_key":"optional"}. The task waits in '
                "AUTH_REQUIRED until an approver decides; poll or subscribe for the result."
            ),
            tags=["api", "write", "approval"],
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
                url=(settings.a2a_url or settings.public_url).rstrip("/") + RPC_PATH,
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
        claimed = command.get("principal")
        if isinstance(claimed, str) and claimed and not caller.principal_verified:
            if self.librarian.settings.principals.mode == "required":
                await updater.reject(
                    updater.new_agent_message(
                        [
                            new_text_part(
                                "Refused: this librarian only accepts principals proven with a "
                                f"token in the {self.librarian.settings.principals.header} header."
                            )
                        ]
                    )
                )
                return
            caller = caller.model_copy(update={"principal": claimed[:200]})

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

        async def on_approval_required(approval: WriteApproval) -> None:
            await updater.requires_auth(
                updater.new_agent_message(
                    [
                        new_text_part(
                            f"Write to {approval.api_name} ({approval.operation}) is waiting for "
                            f"human approval (approval {approval.id}). This task continues "
                            "automatically once someone approves or denies it."
                        ),
                        new_data_part(
                            {
                                "write_approval": approval.id,
                                "api": approval.api,
                                "operation": approval.operation,
                                "state": approval.state.value,
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
                result = await self._run(
                    skill, command, caller, on_auth_required, on_approval_required
                )
        except PolicyDenied as exc:
            await updater.reject(
                updater.new_agent_message([new_text_part(f"Refused: {exc.reason}")])
            )
            return
        except WriteRefused as exc:
            await updater.reject(updater.new_agent_message([new_text_part(str(exc))]))
            return
        except CredentialUnavailable as exc:
            await updater.failed(updater.new_agent_message([new_text_part(str(exc))]))
            return
        except (LibrarianError, ValidationError, ValueError) as exc:
            await updater.failed(updater.new_agent_message([new_text_part(f"Error: {exc}")]))
            return
        except AgentRunError as exc:
            log.warning("planner failed: %s", exc)
            await updater.failed(
                updater.new_agent_message([new_text_part(f"Planner/model error: {str(exc)[:500]}")])
            )
            return
        except Exception:
            # Never surface an unexpected crash as a protocol error: fail the task cleanly.
            log.exception("skill %s failed", skill)
            await updater.failed(
                updater.new_agent_message(
                    [new_text_part("Internal error in the librarian (details in its logs).")]
                )
            )
            return

        await updater.add_artifact([new_data_part(json_safe(result))], name=f"{skill}-result")
        await updater.complete()

    async def _run(
        self,
        skill: str,
        command: dict[str, Any],
        caller: Caller,
        on_auth_required: Any,
        on_approval_required: Any = None,
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
        if skill == "write":
            write_request = WriteRequest.model_validate(
                {
                    k: command[k]
                    for k in ("api", "operation", "args", "body", "idempotency_key", "reason")
                    if k in command
                }
            )
            outcome = await librarian.write(write_request, caller, on_approval_required)
            return {
                "how": outcome.how,
                "approval": outcome.approval_id,
                "grant": outcome.grant_id,
                "result": outcome.result.model_dump(mode="json"),
            }
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
                "proposed_writes": [w.model_dump(mode="json") for w in outcome.proposed_writes],
                "recipe": outcome.recipe,
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
    """Authenticates calling agents (per-agent bearer keys) and verifies principal tokens."""

    def __init__(
        self,
        app: Any,
        keys: dict[str, str],
        profiles: dict[str, Caller],
        principals: PrincipalSettings,
        verifier: PrincipalVerifier | None,
    ) -> None:
        super().__init__(app)
        self.keys = keys  # agent -> key
        self.profiles = profiles  # agent -> permissions
        self.principals = principals
        self.verifier = verifier

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        is_a2a = path == RPC_PATH and request.method == "POST"
        is_mcp = path == MCP_PATH or path.startswith(MCP_PATH + "/")
        if not (is_a2a or is_mcp):
            return await call_next(request)
        if not self.keys:
            caller = Caller(agent="anonymous", authenticated=False)
        else:
            header = request.headers.get("authorization", "")
            token = header[7:].strip() if header.lower().startswith("bearer ") else ""
            agent = next(
                (n for n, key in self.keys.items() if token and hmac.compare_digest(token, key)),
                None,
            )
            if agent is None:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            caller = self.profiles.get(agent, Caller(agent=agent)).model_copy(
                update={"authenticated": True}
            )
        caller = caller.model_copy(
            update={"trust_claimed_principal": self.principals.mode == "off"}
        )
        principal_token = request.headers.get(self.principals.header, "").strip()
        if principal_token and self.principals.mode != "off":
            if self.verifier is None:
                return JSONResponse({"error": "no principal token issuers configured"}, 401)
            try:
                subject = await self.verifier.verify(principal_token)
            except PrincipalError as exc:
                return JSONResponse({"error": str(exc)}, status_code=401)
            caller = caller.model_copy(update={"principal": subject, "principal_verified": True})
        request.scope[CALLER_STATE_KEY] = caller
        return await call_next(request)


async def _caller_keys(librarian: Librarian) -> dict[str, str]:
    keys: dict[str, str] = {}
    for caller in librarian.settings.callers:
        value = await librarian.secrets.get(caller.key_ref)
        if value is None:
            raise RuntimeError(f"caller {caller.agent!r}: secret {caller.key_ref!r} not found")
        keys[caller.agent] = value
    return keys


Surface = Literal["all", "a2a", "pages"]


def create_pages_app(librarian: Librarian) -> Starlette:
    """Only the human pages, for a separate listener behind the auth proxy.

    It has no lifespan of its own: the A2A app's lifespan starts the librarian.
    """

    async def healthz(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    return Starlette(
        routes=[
            *credential_routes(librarian.broker, librarian.settings.approvers),
            *approval_routes(librarian.approvals, librarian.settings.approvers),
            Route("/healthz", healthz, methods=["GET"]),
        ]
    )


def create_app(
    librarian: Librarian,
    caller_keys: dict[str, str] | None = None,
    surface: Surface = "all",
) -> Starlette:
    """Build the ASGI app. `caller_keys` overrides keys loaded from settings (tests).

    `surface="a2a"` leaves out the human pages (serve them with `create_pages_app`).
    """
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
        Route("/healthz", healthz, methods=["GET"]),
    ]
    mcp = build_mcp(librarian) if librarian.settings.mcp_enabled else None
    if mcp is not None:
        # Stateless + JSON responses: no session affinity (gateways, several replicas).
        mcp_app = mcp.streamable_http_app(
            streamable_http_path=MCP_PATH, stateless_http=True, json_response=True
        )
        routes += list(mcp_app.routes)
    if surface == "all":
        if librarian.settings.approvers.mode == "trusted_header":
            log.warning(
                "approvers.mode=trusted_header but the approval pages share the A2A listener: "
                "anyone who can reach it could forge identity headers. Set pages_port and put "
                "the pages listener behind the auth proxy only."
            )
        routes += [
            *credential_routes(librarian.broker, librarian.settings.approvers),
            *approval_routes(librarian.approvals, librarian.settings.approvers),
        ]

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None]:
        await librarian.start()
        if isinstance(task_store, DatabaseTaskStore):
            # Create the task table now (with the same race tolerance as our own
            # tables) rather than lazily on each replica's first request.
            await retry_already_exists(task_store.initialize)
        keys = caller_keys if caller_keys is not None else await _caller_keys(librarian)
        app.state.caller_keys.update(keys)
        sweeper = asyncio.create_task(_sweep_forever(librarian))
        async with contextlib.AsyncExitStack() as stack:
            if mcp is not None:
                await stack.enter_async_context(mcp.session_manager.run())
            try:
                yield
            finally:
                sweeper.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sweeper
                await librarian.aclose()

    profiles = {
        c.agent: Caller(
            agent=c.agent,
            apis=tuple(c.apis),
            can_onboard=c.can_onboard,
            write_apis=tuple(c.write_apis),
            owner=c.owner,
            approval_wait_seconds=c.approval_wait_seconds,
        )
        for c in librarian.settings.callers
    }
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.caller_keys = {}
    principals = librarian.settings.principals
    verifier = (
        PrincipalVerifier(
            [
                Issuer(
                    issuer=i.issuer,
                    audience=i.audience,
                    jwks_url=i.jwks_url,
                    claim=i.claim,
                    algorithms=tuple(i.algorithms),
                    leeway_seconds=i.leeway_seconds,
                )
                for i in principals.issuers
            ]
        )
        if principals.issuers
        else None
    )
    app.state.principal_verifier = verifier
    app.add_middleware(
        CallerAuthMiddleware,
        keys=app.state.caller_keys,
        profiles=profiles,
        principals=principals,
        verifier=verifier,
    )
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
