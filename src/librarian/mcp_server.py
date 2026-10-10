"""MCP endpoint: librarian's skills as MCP tools, for MCP clients and gateways.

Served over Streamable HTTP at `/mcp` on the A2A listener, stateless with JSON
responses (no session affinity, so it works behind gateways such as LiteLLM and
across replicas). Callers authenticate exactly like A2A callers (per-agent bearer
keys, optional X-Principal-Token), and the same policy engine decides every call.

MCP tool calls should return promptly, so nothing here waits on a human:
- a missing credential returns `credential_pending` (approvers are notified);
- a write that needs approval returns `pending_approval`; the librarian runs it
  once approved, and `write_status` reports the outcome.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from librarian import __version__
from librarian.approvals import WriteApproval
from librarian.calls import ArgValue, Caller, CallRequest, WriteRequest
from librarian.executor import PolicyDenied
from librarian.server_state import CALLER_STATE_KEY
from librarian.service import CredentialPending, Librarian, LibrarianError, WriteOutcome

INSTRUCTIONS = """\
librarian gives you read access to registered APIs (Jira, GitHub, internal services, ...)
and, where allowed, writes that a human approves.

Typical flow: `capabilities` (optionally with a query) to see which APIs and operations
exist -> `execute` to run a read operation (use `select` to keep only the fields you
need) -> `write` to request a change, then `write_status` until it is executed.
`fetch` answers a plain-language request when a planner model is configured.
"""


def _caller(ctx: Context) -> Caller:
    request: Any = ctx.request_context.request
    caller = request.scope.get(CALLER_STATE_KEY) if request is not None else None
    return caller if isinstance(caller, Caller) else Caller()


def _write_outcome(outcome: WriteOutcome | WriteApproval, librarian: Librarian) -> dict[str, Any]:
    if isinstance(outcome, WriteApproval):
        return {
            "status": "pending_approval",
            "approval": outcome.id,
            "message": (
                f"A human must approve this {outcome.operation} on {outcome.api_name}. "
                "Call write_status with this approval id to get the result."
            ),
            **{k: v for k, v in librarian.write_status(outcome).items() if k != "approval"},
        }
    return {
        "status": "executed",
        "how": outcome.how,  # grant | idempotent | approved
        "approval": outcome.approval_id,
        "grant": outcome.grant_id,
        "result": outcome.result.model_dump(mode="json"),
    }


def build_mcp(librarian: Librarian) -> MCPServer:
    server: MCPServer = MCPServer(name="librarian", instructions=INSTRUCTIONS, version=__version__)
    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=True)

    @server.tool(annotations=read_only)
    async def capabilities(ctx: Context, query: str | None = None) -> dict[str, Any]:
        """List the APIs you may use and their read operations, best matches first when a
        query is given (e.g. "jira issue comments")."""
        return {"apis": librarian.capabilities(query, _caller(ctx))}

    @server.tool(annotations=read_only)
    async def execute(
        ctx: Context,
        api: str,
        operation: str,
        args: dict[str, ArgValue] | None = None,
        select: list[str] | None = None,
        max_pages: int = 1,
    ) -> dict[str, Any]:
        """Run one read operation. `select` keeps only these dotted paths (lists are
        traversed: ["issues.key", "issues.fields.summary"]). Returns the data plus
        provenance, or `credential_pending` if a human must first provide access."""
        try:
            request = CallRequest(
                api=api,
                operation=operation,
                args=args or {},
                select=select or [],
                max_pages=max(1, min(max_pages, 20)),
            )
            result = await librarian.execute(request, _caller(ctx), wait_for_credential=False)
        except CredentialPending as pending:
            return {
                "status": "credential_pending",
                "credential_request": pending.request.id,
                "message": str(pending),
            }
        except ValidationError as exc:
            raise ToolError(f"invalid request: {exc.errors()[0]['msg']}") from exc
        except PolicyDenied as exc:
            raise ToolError(f"refused: {exc.reason}") from exc
        return {"status": "ok", **result.model_dump(mode="json")}

    if librarian.planner is not None:

        @server.tool(annotations=read_only)
        async def fetch(ctx: Context, intent: str) -> dict[str, Any]:
            """Answer a plain-language request ("open bugs assigned to the payments team")
            by planning policy-checked read calls. Slower than `execute`."""
            try:
                outcome = await librarian.fetch(intent, _caller(ctx))
            except LibrarianError as exc:
                raise ToolError(str(exc)) from exc
            return {
                "answer": outcome.answer.model_dump(mode="json"),
                "results": [r.model_dump(mode="json") for r in outcome.results],
                "proposed_writes": [w.model_dump(mode="json") for w in outcome.proposed_writes],
                "recipe": outcome.recipe,
            }

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, open_world_hint=True
        )
    )
    async def write(
        ctx: Context,
        api: str,
        operation: str,
        args: dict[str, ArgValue] | None = None,
        body: dict[str, Any] | list[Any] | None = None,
        reason: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Request a change (e.g. add a comment). Runs at once under a standing grant;
        otherwise returns `pending_approval` and a human decides. Give a clear `reason`:
        the approver sees it. Use `idempotency_key` to make retries safe."""
        try:
            request = WriteRequest(
                api=api,
                operation=operation,
                args=args or {},
                body=body,
                reason=reason,
                idempotency_key=idempotency_key,
            )
            outcome = await librarian.request_write(request, _caller(ctx))
        except ValidationError as exc:
            raise ToolError(f"invalid request: {exc.errors()[0]['msg']}") from exc
        except PolicyDenied as exc:
            raise ToolError(f"refused: {exc.reason}") from exc
        return _write_outcome(outcome, librarian)

    @server.tool(annotations=read_only)
    async def write_status(ctx: Context, approval: str) -> dict[str, Any]:
        """State of a write you requested: pending, approved, executing, executed (with the
        result), failed, denied or expired."""
        found = await librarian.approvals.load(approval)
        if found is None or found.agent != _caller(ctx).agent:
            raise ToolError(f"no write approval {approval!r} for this agent")
        return librarian.write_status(found)

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, open_world_hint=True))
    async def onboard(
        ctx: Context, api: str, spec_url: str | None = None, base_url: str | None = None
    ) -> dict[str, Any]:
        """Teach the librarian a new API from its OpenAPI spec (or auto-discover it from the
        base URL). The API becomes usable read-only at once and is proposed for review."""
        try:
            outcome = await librarian.onboard(
                api, spec_url=spec_url, base_url=base_url, caller=_caller(ctx)
            )
        except LibrarianError as exc:
            raise ToolError(str(exc)) from exc
        return {
            "api": outcome.entry.id,
            "status": outcome.entry.status.value,
            "read_operations": outcome.read_operations,
            "needs_review": outcome.needs_review,
            "review_url": outcome.review_url,
        }

    return server
