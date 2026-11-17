"""MCP endpoint: librarian's skills as MCP tools, for MCP clients and gateways.

Served over Streamable HTTP at `/mcp` on the A2A listener, stateless with JSON
responses (no session affinity, so it works behind gateways such as LiteLLM and
across replicas). Callers authenticate exactly like A2A callers (per-agent bearer
keys, optional X-Principal-Token), and the same policy engine decides every call.

MCP tool calls should return promptly, so nothing here waits on a human unless the
agent asks for it:
- a missing credential returns `credential_pending` (approvers are notified);
- a write that needs approval returns `pending_approval`; the librarian runs it
  once approved, and `write_status` reports the outcome (and waits for it, a bounded
  time, when asked to with `wait_seconds`);
- a write on an operation nobody has enabled yet returns `write_not_enabled`
  (approvers are asked to enable it; `enable_status` says where it stands);
- `fetch` stops at its time budget and marks the answer `partial`, with the
  results it had.
"""

from __future__ import annotations

from typing import Any

import pydantic_core
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from librarian import __version__, plans
from librarian.approvals import WriteApproval
from librarian.calls import ArgValue, Caller, CallRequest, WriteRequest
from librarian.executor import PolicyDenied
from librarian.server_state import CALLER_STATE_KEY
from librarian.service import (
    ConsentPending,
    CredentialPending,
    Librarian,
    LibrarianError,
    WriteNotEnabled,
    WriteOutcome,
)
from librarian.shaping import outline


def sent_bytes(answer: dict[str, Any]) -> int:
    """How large a tool result is on its way to the agent. An MCP server sends a
    structured answer twice: as structured content, and (for clients that only read
    text) as the same JSON, indented, in a text block."""
    text = pydantic_core.to_json(answer, fallback=str, indent=2).decode()
    return len(pydantic_core.to_json(answer, fallback=str)) + len(pydantic_core.to_json(text))


INSTRUCTIONS = """\
librarian gives you read access to registered APIs (Jira, GitHub, internal services, ...)
and, where allowed, writes that a human approves.

Typical flow: `capabilities` (optionally with a query, or with `api` for one API's
operations) to see which APIs and operations exist -> `execute` to run a read operation
(use `select` to keep only the fields you need; an answer too large to hand over comes
back as `too_large` with an outline of its fields) -> `write` to request a change, then
`write_status` until it is executed (with `wait_seconds` it waits for the decision, so
you need not ask again and again).
A change that takes several writes (create a blob, a tree, a commit, a branch, a pull
request) goes in one `write_plan`: a human approves all of it once.
`fetch` answers a plain-language request when a planner model is configured.
"""


def _caller(ctx: Context) -> Caller:
    request: Any = ctx.request_context.request
    caller = request.scope.get(CALLER_STATE_KEY) if request is not None else None
    return caller if isinstance(caller, Caller) else Caller()


def _write_outcome(outcome: WriteOutcome | WriteApproval, librarian: Librarian) -> dict[str, Any]:
    if isinstance(outcome, WriteApproval):
        what = (
            f"this plan of {len(outcome.steps)} writes on {outcome.api_name}"
            if outcome.steps
            else f"this {outcome.operation} on {outcome.api_name}"
        )
        wait = librarian.settings.mcp_max_wait_seconds
        return {
            "status": "pending_approval",
            "approval": outcome.id,
            "message": (
                f"A human must approve {what}. Call write_status with this approval id to "
                "get the result"
                + (
                    f": with wait_seconds (up to {wait}) it waits for the decision."
                    if wait
                    else "."
                )
            ),
            **{k: v for k, v in librarian.write_status(outcome).items() if k != "approval"},
        }
    return {
        "status": "executed",
        "how": outcome.how,  # approved | grant | auto | idempotent
        "message": outcome.message,
        "approval": outcome.approval_id,
        "grant": outcome.grant_id,
        "result": outcome.result.model_dump(mode="json"),
        **({"steps": outcome.steps} if outcome.steps is not None else {}),
    }


def build_mcp(librarian: Librarian) -> MCPServer:
    server: MCPServer = MCPServer(name="librarian", instructions=INSTRUCTIONS, version=__version__)
    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=True)

    @server.tool(annotations=read_only)
    async def capabilities(
        ctx: Context, query: str | None = None, api: str | None = None
    ) -> dict[str, Any]:
        """List the APIs you may use and their read operations, best matches first when a
        query is given (e.g. "jira issue comments"). With `api` (an API's id), that API
        alone: add a query to get its operations that fit. Each operation names its
        arguments (`args`; a trailing ? marks one that may be left out) and says when it
        takes a `body`. Where you may ask for changes, the operations that write are
        listed too (`write_operations`): use them with `write` or `write_plan`.
        `enabled: false` means nobody has allowed writes through that operation yet;
        asking for one asks the approvers to. An answer that would be too large is
        shortened and says so in `note`, with how to see what it left out."""
        limit = librarian.settings.mcp_max_result_bytes
        try:
            return librarian.capabilities(
                query,
                _caller(ctx),
                api=api,
                # What `capabilities_max_bytes` allows may still be more than a tool result
                # can hold (it is sent twice): the lists are cut to that as well.
                fits=(lambda answer: sent_bytes(answer) <= limit) if limit else None,
            )
        except PolicyDenied as exc:
            raise ToolError(f"refused: {exc.reason}") from exc

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
        provenance, or `credential_pending` if a human must first provide access. An
        answer too large for one tool result comes back as `too_large` with an `outline`
        of its fields instead of the data: call again with `select` for the fields you
        need."""
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
        except ConsentPending as consent:
            return {
                "status": "consent_pending",
                "consent_request": consent.request.id,
                "principal": consent.request.principal,
                "message": str(consent),
            }
        except ValidationError as exc:
            raise ToolError(f"invalid request: {exc.errors()[0]['msg']}") from exc
        except PolicyDenied as exc:
            raise ToolError(f"refused: {exc.reason}") from exc
        except LibrarianError as exc:  # e.g. the API gave no answer: say so, in its words
            raise ToolError(str(exc)) from exc
        answer = {"status": "ok", **result.model_dump(mode="json")}
        limit = librarian.settings.mcp_max_result_bytes
        if limit and (size := sent_bytes(answer)) > limit:
            # Megabytes are no use to a model, and on the way to it a gateway or the MCP
            # client would refuse them with an error that says nothing. Say what is there.
            return {
                **answer,
                "status": "too_large",
                "data": None,
                "outline": outline(result.data),
                "message": (
                    f"The answer is too large for one tool result ({size:,} bytes as sent; "
                    f"the limit is {limit:,}). Ask for less: name the fields you need in "
                    "`select` (the outline shows what there is), narrow the request with "
                    "the operation's arguments, or read fewer pages."
                ),
            }
        return answer

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
                "partial": outcome.partial,
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
        also_needs: list[str] | None = None,
    ) -> dict[str, Any]:
        """Request a change (e.g. add a comment). Runs at once under a standing grant;
        otherwise returns `pending_approval` and a human decides. Give a clear `reason`:
        the approver sees it. Use `idempotency_key` to make retries safe. If the
        operation is not enabled for writes yet, returns `write_not_enabled`: the
        approvers were asked to enable it; check `enable_status` and retry later. Name
        the other operations of the same API your flow will need in `also_needs`, so one
        approver action enables them all."""
        try:
            request = WriteRequest(
                api=api,
                operation=operation,
                args=args or {},
                body=body,
                reason=reason,
                idempotency_key=idempotency_key,
                also_needs=also_needs or [],
            )
            outcome = await librarian.request_write(request, _caller(ctx))
        except WriteNotEnabled as asked:
            return {
                "status": "write_not_enabled",
                "enable_request": asked.request.id,
                "operations": asked.request.operations,
                "message": str(asked),
            }
        except ValidationError as exc:
            raise ToolError(f"invalid request: {exc.errors()[0]['msg']}") from exc
        except PolicyDenied as exc:
            raise ToolError(f"refused: {exc.reason}") from exc
        except LibrarianError as exc:  # refused for another reason, or no answer
            raise ToolError(str(exc)) from exc
        return _write_outcome(outcome, librarian)

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, open_world_hint=True
        )
    )
    async def write_plan(
        ctx: Context,
        api: str,
        steps: list[dict[str, Any]],
        reason: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Request several writes on one API as one plan: a human sees all of them
        together and approves once, then librarian makes them in order. Each step is
        {"id": "blob", "operation": "...", "args": {...}, "body": {...}}; `id` is a short
        name of your choosing. Where a later step needs what an earlier one returned,
        write {"$from": "<step id>.<field>"} as the value, in `args` or anywhere in
        `body` (e.g. {"sha": {"$from": "blob.sha"}}). Only writes go in a plan: read what
        you need first with `execute` and write the values in. A DELETE step must have
        all its values written out. The first step that fails ends the plan and what was
        made before it is not undone, so order the steps so that a partial result is
        harmless. Returns `pending_approval`; `write_status` with the approval id then
        shows every step and its result. Give a clear `reason`."""
        try:
            plan = plans.parse(
                {"api": api, "steps": steps, "reason": reason, "idempotency_key": idempotency_key}
            )
            outcome = await librarian.request_plan(plan, _caller(ctx))
        except WriteNotEnabled as asked:
            return {
                "status": "write_not_enabled",
                "enable_request": asked.request.id,
                "operations": asked.request.operations,
                "message": str(asked),
            }
        except PolicyDenied as exc:
            raise ToolError(f"refused: {exc.reason}") from exc
        except LibrarianError as exc:
            raise ToolError(str(exc)) from exc
        return _write_outcome(outcome, librarian)

    @server.tool(annotations=read_only)
    async def write_status(ctx: Context, approval: str, wait_seconds: int = 0) -> dict[str, Any]:
        """State of a write or a plan you requested: pending, approved, executing, executed
        (with the result), failed, denied or expired. For a plan, `steps` lists each step
        with what it answered and its state: waiting, being sent, made, failed, not sent,
        or unknown (it may or may not have reached the API: check before sending it
        again). `wait_seconds` waits that long (librarian caps it) for the write to end
        before answering: use it once, instead of asking again and again, when a person
        is likely to decide soon. Given an `enable_request` id instead, answers like
        `enable_status`."""
        found = await librarian.approvals.load(approval)
        if found is None or found.agent != _caller(ctx).agent:
            try:
                return await librarian.enable_status(approval, _caller(ctx))
            except LibrarianError:
                raise ToolError(f"no write approval {approval!r} for this agent") from None
        wait = min(wait_seconds, librarian.settings.mcp_max_wait_seconds)
        if wait > 0:
            found = await librarian.settled(found, wait)
        return librarian.write_status(found)

    @server.tool(annotations=read_only)
    async def enable_status(ctx: Context, enable_request: str) -> dict[str, Any]:
        """Where a `write_not_enabled` request stands: pending (approvers asked), enabled
        (decided; waiting for a write credential), live (in
        effect: retry the write), declined until a date, or expired."""
        try:
            return await librarian.enable_status(enable_request, _caller(ctx))
        except LibrarianError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, open_world_hint=True))
    async def onboard(
        ctx: Context,
        api: str,
        spec_url: str | None = None,
        base_url: str | None = None,
        docs_url: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Teach the librarian a new API: from its OpenAPI spec, by auto-discovery under
        base_url, or from documentation pages (docs_url + base_url) when it has no spec.
        The API becomes usable read-only at once; approvers are asked to approve it."""
        try:
            outcome = await librarian.onboard(
                api,
                spec_url=spec_url,
                base_url=base_url,
                docs_url=docs_url,
                name=name,
                caller=_caller(ctx),
            )
        except LibrarianError as exc:
            raise ToolError(str(exc)) from exc
        return {
            "api": outcome.entry.id,
            "status": outcome.entry.status.value,
            "read_operations": outcome.read_operations,
            "needs_review": outcome.needs_review,
            "learned_from": outcome.entry.source.kind,
            "review": "approvers were asked to approve it" if outcome.review else None,
        }

    return server
