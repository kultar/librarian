"""The Librarian service: wires registry, policy, executor, credentials and planner."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets as pysecrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
from pydantic_ai import Agent
from pydantic_ai.exceptions import (
    AgentRunError,
    ContentFilterError,
    ModelAPIError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel

from librarian import telemetry
from librarian.approvals import ApprovalBroker, ApprovalState, Grant, WriteApproval
from librarian.audit import AuditEvent, AuditSink, JsonlAuditSink
from librarian.calls import Caller, CallRequest, CallResult, WriteRequest
from librarian.config import Settings
from librarian.credentials import CredentialBroker, CredentialRequest, Notifier, RequestState
from librarian.docs import (
    ExtractedApi,
    auth_from_extracted,
    build_extractor,
    crawl_docs,
    entry_from_docs,
    entry_from_knowledge,
    extract_from_docs,
    extract_from_knowledge,
)
from librarian.drift import Change, diff, proposed_update, review_body, worst
from librarian.executor import CredentialMissing, Executor, PolicyDenied
from librarian.finddocs import (
    SearchDidNotRun,
    Searcher,
    brave_searcher,
    find_docs,
    model_searcher,
    searxng_searcher,
    trust_of,
)
from librarian.github_app import GitHubAppAuth
from librarian.notifiers import SlackNotifier, WebhookNotifier
from librarian.oauth import ConsentBroker, ConsentRequest, ConsentState
from librarian.observations import Finding, Observations
from librarian.openapi import (
    SpecError,
    discover_spec,
    entry_from_spec,
    fetch_spec,
    guess_validation_operation,
    spec_hash,
)
from librarian.opsearch import rank
from librarian.planner import FetchAnswer, FetchOutcome, Planner
from librarian.policy import PolicyEngine
from librarian.publish import GitHubPublisher, Publisher, PublishError
from librarian.recipes import Recipe, recipe_summary
from librarian.recipes import learn as learn_recipe
from librarian.registry.models import (
    Access,
    ApiEntry,
    CredentialGuidance,
    EntryStatus,
    NoAuth,
    Operation,
    ReadBasis,
)
from librarian.registry.store import Registry
from librarian.reviews import ReviewBroker, ReviewRequest, ReviewState
from librarian.secrets import (
    AwsSecretsManagerStore,
    ChainSecretStore,
    EnvSecretStore,
    FileSecretStore,
    SecretStore,
)
from librarian.shaping import get_path
from librarian.state import StateStore, make_engine

log = logging.getLogger(__name__)

SYSTEM_CALLER = Caller(agent="librarian", authenticated=True)

AuthRequiredHook = Callable[[CredentialRequest | ConsentRequest], Awaitable[None]]


class LibrarianError(Exception):
    """An error safe to show to the calling agent."""


class CredentialUnavailable(LibrarianError):
    def __init__(self, request: CredentialRequest) -> None:
        super().__init__(
            f"no credential for {request.api!r}: request {request.state}"
            + (f" ({request.resolution_note})" if request.resolution_note else "")
        )
        self.request = request


class ConsentPending(LibrarianError):
    """The person behind the agent must sign in to the API themselves; they were sent a
    link. Retry later (non-waiting callers)."""

    def __init__(self, request: ConsentRequest) -> None:
        super().__init__(
            f"{request.api_name} acts as the person: {request.principal} must sign in to it "
            f"first; they were sent a link (sign-in {request.id}). Retry once they have."
        )
        self.request = request


class CredentialPending(LibrarianError):
    """A credential is needed; approvers were notified. Retry later (non-waiting callers)."""

    def __init__(self, request: CredentialRequest) -> None:
        super().__init__(
            f"{request.api_name} needs a credential; approvers were notified "
            f"(request {request.id}). Retry once access is granted."
        )
        self.request = request


class WriteRefused(LibrarianError):
    """The write was denied, expired, or could not be approved."""


@dataclass
class WriteOutcome:
    result: CallResult
    how: str  # "approved" | "grant" | "idempotent"
    approval_id: str | None = None
    grant_id: str | None = None


ApprovalHook = Callable[[WriteApproval], Awaitable[None]]


@dataclass
class OnboardResult:
    entry: ApiEntry
    path: Path
    read_operations: int
    needs_review: int
    review_url: str | None = None
    review_error: str | None = None


_FINDING_TITLES = {
    "deprecated": "the API marks an operation deprecated",
    "gone": "an operation may have been removed",
    "shape": "responses changed shape",
    "unconfirmed_missing": "a learned operation does not seem to exist (suspended)",
}

SOURCE_LABELS = {
    "openapi": "an OpenAPI spec",
    "docs": "documentation",
    "inferred": "the model's own knowledge",
    "manual": "a hand-written entry",
}


@dataclass
class ForgetReport:
    """What forgetting an API did besides dropping the entry."""

    closed: list[str] = field(default_factory=list[str])  # review proposals closed
    review_error: str | None = None  # why they could not be closed
    cancelled: int = 0  # credential requests and write approvals denied
    observations: int = 0  # operations whose observations were dropped


@dataclass
class RecheckReport:
    api: str
    status: str  # unchanged | changed | skipped | error
    changes: list[Change] = field(default_factory=list[Change])
    actions: list[str] = field(default_factory=list[str])
    review_url: str | None = None
    detail: str | None = None


def _with_source(entry: ApiEntry, provenance: dict[str, Any]) -> ApiEntry:
    note = (
        f"\nThe source was found by {provenance['found_by']} ({provenance['trust']}): "
        f"{entry.source.url}. Check it is the API's real documentation."
    )
    return entry.model_copy(
        update={
            "source": entry.source.model_copy(update=provenance),
            "notes": (entry.notes + note)[:8000],
        }
    )


def _keep_human_fields(existing: ApiEntry, fresh: ApiEntry) -> ApiEntry:
    """Re-learning a provisional entry keeps what a person set: owners and credentials."""
    update: dict[str, Any] = {"owners": existing.owners}
    if existing.secret_ref:
        update["auth"] = existing.auth
    return fresh.model_copy(update=update)


class Librarian:
    def __init__(
        self,
        settings: Settings,
        *,
        registry: Registry | None = None,
        secrets_store: SecretStore | None = None,
        audit: AuditSink | None = None,
        notifier: Notifier | None = None,
        http_client: httpx.AsyncClient | None = None,
        planner_model: Model | str | None = None,
        publisher: Publisher | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry or Registry(settings.registry_dir, settings.registry_overlay_dir)
        self.secrets = secrets_store or _default_secrets(settings)
        self.audit = audit or JsonlAuditSink(settings.audit_file)
        self.http = http_client or httpx.AsyncClient(
            timeout=settings.request_timeout_seconds, follow_redirects=False
        )
        self.engine = make_engine(settings.database_url) if settings.database_url else None
        self.state = StateStore(self.engine) if self.engine is not None else None
        if self.state is not None and self.registry.store is None:
            # Learned APIs live in the shared database, so every replica sees them.
            self.registry.store = self.state
        self.observations = Observations(self.state)
        self.observations.on_finding = self._report_finding
        self.policy = PolicyEngine(
            self.registry,
            rate_limit_per_minute=settings.rate_limit_per_minute,
            write_rate_limit_per_minute=settings.writes.rate_limit_per_minute,
            allow_provisional=settings.allow_provisional,
            observations=self.observations,
        )
        self.executor = Executor(
            self.policy,
            self.secrets,
            self.audit,
            client=self.http,
            max_response_bytes=settings.max_response_bytes,
            net=settings.network,
        )
        self.notifier = notifier or _default_notifier(settings)
        self.broker = CredentialBroker(
            self.registry,
            self.secrets,
            self.notifier,
            self.audit,
            public_url=settings.public_url,
            signing_key=pysecrets.token_bytes(32),
            expire_after=timedelta(hours=settings.notifier.expire_after_hours),
            remind_after=timedelta(hours=settings.notifier.remind_after_hours),
            default_channel=settings.notifier.slack.channel,
            default_approver_group=settings.notifier.slack.approver_group,
            store=self.state,
        )
        self.broker.validator = self._validate_credential
        self.broker.after_fulfilled = self._discover_quietly
        self.approvals = ApprovalBroker(
            self.notifier,
            self.audit,
            public_url=settings.public_url,
            signing_key=pysecrets.token_bytes(32),
            approver_mode=settings.writes.approvers,
            expire_after=timedelta(hours=settings.writes.approval_expire_hours),
            default_channel=settings.notifier.slack.channel,
            default_approver_group=settings.notifier.slack.approver_group,
            store=self.state,
            config_grants=[
                Grant(
                    id=f"config-{i}",
                    agent=g.agent,
                    principal=g.principal,
                    api=g.api,
                    operation=g.operation,
                    pinned_args=g.pinned_args,
                    max_uses_per_day=g.max_uses_per_day,
                    notify_owner=g.notify_owner,
                    created_by="config",
                    source="config",
                )
                for i, g in enumerate(settings.writes.grants)
            ],
        )
        self.publisher = publisher if publisher is not None else _default_publisher(settings)
        self.recipes: dict[str, Recipe] = {}
        self._background: set[asyncio.Task[None]] = set()
        model = planner_model if planner_model is not None else _model_from_settings(settings)
        mode = settings.planner_output
        if mode == "auto":
            mode = "prompted" if settings.model_base_url else "tool"
        self.planner = (
            Planner(model, settings.max_calls_per_fetch, mode) if model is not None else None
        )
        # Learning (docs extraction, web search) may use its own model; by default the same.
        learning_model = model
        if planner_model is None and settings.learning.model and model is not None:
            learning_model = _model_from_settings(settings, settings.learning.model)
        if planner_model is None and settings.fallback_model and model is not None:
            model = _with_fallback(model, settings)
            if learning_model is not None:
                learning_model = _with_fallback(learning_model, settings)
        self.extractor: Agent[None, Any] | None = (
            build_extractor(learning_model, mode) if learning_model is not None else None
        )
        self.search_model: Model | str | None = learning_model
        self.executor.on_unexpected_auth = self._settle_auth_after_401
        self._auth_notified: set[str] = set()
        slack = settings.notifier.slack if settings.notifier.type == "slack" else None
        self.consents = ConsentBroker(
            self.secrets,
            self.notifier,
            self.audit,
            public_url=settings.public_url,
            signing_key=pysecrets.token_bytes(32),
            store=self.state,
        )
        self.executor.consents = self.consents
        self.executor.require_verified_principal = settings.principals.mode != "off"
        self.reviews = ReviewBroker(
            self.notifier,
            self.audit,
            public_url=settings.public_url,
            signing_key=pysecrets.token_bytes(32),
            default_channel=slack.channel if slack else None,
            default_approver_group=slack.approver_group if slack else None,
            store=self.state,
        )
        self._last_hashes: dict[str, str] = {}  # without a database
        # Fetches specs and docs pages (a fresh client each time: they are rare).
        self.source_client_factory: Callable[[], httpx.AsyncClient] = lambda: httpx.AsyncClient(
            timeout=30, follow_redirects=False
        )
        self._last_recheck_bucket = -1

    def load(self) -> None:
        self.registry.load()

    async def start(self) -> None:
        """Load the registry and durable state; call once before serving."""
        self.load()
        if self.state is not None:
            await self.state.initialize()
            await self.registry.refresh_stored(force=True)
            await self.observations.load()
            restored = await self.broker.restore()
            if restored:
                log.info("restored %d pending credential request(s)", restored)
            await self.approvals.restore()
            for data in await self.state.list_docs("recipe", "active"):
                recipe = Recipe.model_validate_json(data)
                self.recipes[recipe.id] = recipe
            # Detached writes (MCP callers) are finished by the librarian itself; pick
            # them back up after a restart. The claim step keeps replicas from both
            # running one.
            for approval in self.approvals.detached_unfinished():
                self._finish_in_background(
                    approval, Caller(agent=approval.agent, principal=approval.principal)
                )
        key = await self._signing_key()
        self.broker.set_signing_key(key)
        self.approvals.set_signing_key(key)
        self.reviews.set_signing_key(key)
        self.consents.set_signing_key(key)
        await self.reviews.restore()

    async def _signing_key(self) -> bytes:
        ref = self.settings.signing_key_ref
        stored = await self.secrets.get(ref)
        if stored is not None:
            return stored.encode()
        key = pysecrets.token_hex(32)
        if self.secrets.writable:
            # Replicas starting together must agree on one key: first writer wins.
            key = await self.secrets.put_if_absent(ref, key)
        else:
            log.warning(
                "no signing key at %r and the secret store is read-only: approval links "
                "will stop working after a restart",
                ref,
            )
        return key.encode()

    async def aclose(self) -> None:
        await self.http.aclose()
        if self.engine is not None:
            await self.engine.dispose()

    # -- skills ---------------------------------------------------------------

    async def execute(
        self,
        request: CallRequest,
        caller: Caller,
        on_auth_required: AuthRequiredHook | None = None,
        wait_for_credential: bool = True,
    ) -> CallResult:
        """Run one read operation; if a credential is missing, ask a human (and wait,
        unless `wait_for_credential` is False, in which case raise CredentialPending)."""
        try:
            return await self.executor.execute(request, caller)
        except CredentialMissing as missing:
            if not wait_for_credential:
                if missing.principal:
                    consent = await self.consents.open(missing.entry, caller, missing.reason)
                    raise ConsentPending(consent) from missing
                pending = await self.broker.open(missing.entry, caller, missing.reason)
                raise CredentialPending(pending) from missing
            await self._await_credential(missing, caller, on_auth_required)
        try:
            return await self.executor.execute(request, caller)
        except CredentialMissing as exc:
            raise LibrarianError(f"credential for {exc.entry.id!r} still not usable") from exc

    async def fetch(
        self,
        intent: str,
        caller: Caller,
        on_auth_required: AuthRequiredHook | None = None,
    ) -> FetchOutcome:
        replayed = await self._replay_recipe(intent, caller, on_auth_required)
        if replayed is not None:
            return replayed
        if self.planner is None:
            raise LibrarianError("the fetch skill is disabled: no model is configured")
        for _attempt in range(2):
            try:
                outcome = await self.planner.fetch(intent, self.registry, self.executor, caller)
            except CredentialMissing as missing:
                await self._await_credential(missing, caller, on_auth_required)
                continue
            except UsageLimitExceeded as exc:
                raise LibrarianError(
                    "the planner used up its step budget without an answer; try a narrower "
                    "request, or raise max_calls_per_fetch"
                ) from exc
            except (ContentFilterError, ModelAPIError, UnexpectedModelBehavior) as exc:
                raise await self._model_failed(exc, caller, "planning") from exc
            await self._learn_recipe(intent, outcome)
            return outcome
        raise LibrarianError("credential still not usable after it was provided")

    # -- recipes ---------------------------------------------------------------------

    async def _replay_recipe(
        self, intent: str, caller: Caller, on_auth_required: AuthRequiredHook | None
    ) -> FetchOutcome | None:
        if not self.settings.recipes_enabled:
            return None
        for recipe in sorted(self.recipes.values(), key=lambda r: -r.uses):
            values = recipe.match(intent)
            if values is None:
                continue
            requests = recipe.calls(values)
            if not all(caller.may_use(r.api) for r in requests):
                continue
            results: list[CallResult] = []
            try:
                for request in requests:
                    results.append(await self.execute(request, caller, on_auth_required))
            except PolicyDenied:
                continue  # e.g. the registry changed; let the planner handle it
            recipe.uses += 1
            await self._save_recipe(recipe)
            return FetchOutcome(
                answer=FetchAnswer(summary=recipe_summary(recipe, results)),
                results=results,
                requests=requests,
                recipe=recipe.id,
            )
        return None

    async def _learn_recipe(self, intent: str, outcome: FetchOutcome) -> None:
        if not self.settings.recipes_enabled or len(self.recipes) >= 500:
            return
        recipe = learn_recipe(intent, outcome.results, outcome.requests)
        if recipe is None:
            return
        if any(
            r.template == recipe.template and r.steps == recipe.steps for r in self.recipes.values()
        ):
            return
        self.recipes[recipe.id] = recipe
        await self._save_recipe(recipe)
        await self.audit.record(
            AuditEvent(event="recipe_learned", detail=f"{recipe.id}: {recipe.template}")
        )

    async def reload_recipes(self) -> None:
        """Pick up recipes saved by other replicas (and deletions)."""
        if self.state is None:
            return
        active = [
            Recipe.model_validate_json(d) for d in await self.state.list_docs("recipe", "active")
        ]
        self.recipes = {r.id: self.recipes.get(r.id, r) for r in active}

    async def _save_recipe(self, recipe: Recipe) -> None:
        if self.state is not None:
            await self.state.save_doc("recipe", recipe.id, "active", recipe.model_dump_json())

    async def delete_recipe(self, recipe_id: str) -> None:
        if self.recipes.pop(recipe_id, None) is None:
            raise LibrarianError(f"unknown recipe {recipe_id!r}")
        if self.state is not None:
            await self.state.save_doc("recipe", recipe_id, "deleted", "{}")

    async def write(
        self,
        request: WriteRequest,
        caller: Caller,
        on_approval_required: ApprovalHook | None = None,
    ) -> WriteOutcome:
        """Make a write: run it under a matching grant, or ask a human and wait."""
        started = await self._start_write(request, caller)
        if isinstance(started, WriteOutcome):
            return started
        approval = started
        if on_approval_required is not None:
            await on_approval_required(approval)
        approval = await self.approvals.wait(approval.id, max_wait=caller.approval_wait_seconds)
        if approval.state is ApprovalState.PENDING:
            await self.approvals.expire(approval.id, f"{caller.agent} stopped waiting")
        return await self._run_approved(approval, caller)

    async def request_write(
        self, request: WriteRequest, caller: Caller
    ) -> WriteOutcome | WriteApproval:
        """Like `write`, but never waits for a human: returns the pending approval, and
        the librarian runs the write itself once it is approved (callers poll)."""
        started = await self._start_write(request, caller)
        if isinstance(started, WriteOutcome):
            return started
        await self.approvals.mark_detached(started)
        self._finish_in_background(started, caller)
        return started

    def write_status(self, approval: WriteApproval) -> dict[str, Any]:
        status: dict[str, Any] = {
            "approval": approval.id,
            "state": approval.state.value,
            "api": approval.api,
            "operation": approval.operation,
            "decided_by": approval.decided_by,
            "note": approval.note,
            "expires_at": approval.expires_at.isoformat(),
        }
        if approval.result is not None:
            status["result"] = approval.result
        return status

    def _finish_in_background(self, approval: WriteApproval, caller: Caller) -> None:
        async def finish() -> None:
            decided = await self.approvals.wait(approval.id)
            try:
                await self._run_approved(decided, caller)
            except WriteRefused as exc:
                log.info("detached write %s not run: %s", approval.id, exc)

        task = asyncio.create_task(finish())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _start_write(
        self, request: WriteRequest, caller: Caller
    ) -> WriteOutcome | WriteApproval:
        """Policy, idempotency and grants; opens an approval when a human must decide."""
        if request.idempotency_key:
            prior = self.approvals.find_executed(caller, request.idempotency_key)
            if prior is not None and prior.result is not None:
                return WriteOutcome(
                    CallResult.model_validate(prior.result), "idempotent", prior.id, prior.grant_id
                )
        if self.registry.get(request.api) is None:
            await self.registry.refresh_stored()  # learned on another replica?
        decision = self.policy.check_write(request, caller)
        if not decision.allowed or decision.entry is None or decision.operation is None:
            await self.audit.record(
                AuditEvent(
                    event="write_denied",
                    caller=caller.agent,
                    principal=caller.principal,
                    api=request.api,
                    operation=request.operation,
                    detail=decision.reason,
                )
            )
            telemetry.write_requests.add(1, {"librarian.api": request.api, "outcome": "denied"})
            raise PolicyDenied(decision.reason)
        entry, op = decision.entry, decision.operation

        grant = self.approvals.find_grant(request, caller)
        if grant is not None:
            await self.approvals.use_grant(grant)
            result = await self.executor.execute_write(entry, op, request, caller)
            telemetry.write_requests.add(1, {"librarian.api": entry.id, "outcome": "granted"})
            notice = WriteApproval(
                fingerprint=request.fingerprint(caller),
                api=entry.id,
                api_name=entry.name,
                host=entry.host,
                operation=op.id,
                method=op.method,
                path=op.path,
                request=request,
                agent=caller.agent,
                principal=caller.principal,
                expires_at=datetime.now(UTC),
                channel=entry.owners.channel or self.settings.notifier.slack.channel,
            )
            await self.notifier.grant_used(grant, notice)
            return WriteOutcome(result, "grant", grant_id=grant.id)

        group = entry.owners.approver_group or self.settings.notifier.slack.approver_group
        problem = self.approvals.approval_route(caller.requesters(), group)
        if problem is not None:
            raise PolicyDenied(problem)
        return await self.approvals.open(entry, op, request, caller)

    async def _run_approved(self, approval: WriteApproval, caller: Caller) -> WriteOutcome:
        """Run exactly what was approved, once, against the entry as it is now."""
        if approval.state is not ApprovalState.APPROVED:
            telemetry.write_requests.add(
                1, {"librarian.api": approval.api, "outcome": approval.state}
            )
            raise WriteRefused(
                f"write {approval.state}" + (f" ({approval.note})" if approval.note else "")
            )
        if not await self.approvals.claim(approval):
            raise WriteRefused("this approved write is already being run")
        current = self.registry.get(approval.api)
        current_op = current.operation(approval.operation) if current is not None else None
        if current is None or current_op is None or not current_op.writable:
            await self.approvals.finish(approval, False, None, "operation no longer writable")
            raise WriteRefused("the operation is no longer enabled for writes")
        suspended = self.observations.suspended(current, current_op)
        if suspended:
            await self.approvals.finish(approval, False, None, f"suspended: {suspended}")
            raise WriteRefused(f"the operation was suspended after approval: {suspended}")
        try:
            result = await self.executor.execute_write(
                current, current_op, approval.request, caller
            )
        except (CredentialMissing, PolicyDenied, httpx.HTTPError) as exc:
            await self.approvals.finish(approval, False, None, str(exc))
            raise WriteRefused(f"approved write failed: {exc}") from exc
        stored = result.model_dump(mode="json")
        if len(json.dumps(stored, default=str)) > 65_536:
            stored = {**stored, "data": None, "truncated": True}
        note = "done" if result.ok else (result.error or f"HTTP {result.status_code}")
        await self.approvals.finish(approval, result.ok, stored, note)
        telemetry.write_requests.add(1, {"librarian.api": approval.api, "outcome": "executed"})
        return WriteOutcome(result, "approved", approval.id, approval.grant_id)

    def capabilities(
        self, query: str | None = None, caller: Caller = SYSTEM_CALLER
    ) -> list[dict[str, Any]]:
        entries = (
            [h.entry for h in self.registry.search(query, limit=10)]
            if query
            else [e for e in self.registry.all() if e.status is not EntryStatus.BLOCKED]
        )
        entries = [e for e in entries if caller.may_use(e.id)]

        def read_ops(entry: ApiEntry) -> list[Operation]:
            reads = [
                op
                for op in entry.operations
                if op.access is Access.READ and not self.observations.suspended(entry, op)
            ]
            return rank(entry, query, reads, limit=20) if query else reads[:50]

        return [
            {
                "api": e.id,
                "name": e.name,
                "description": e.description[:300],
                "status": e.status.value,
                "base_url": e.base_url,
                "read_operations": [
                    {"operation": op.id, "summary": op.summary[:160]} for op in read_ops(e)
                ],
                "total_read_operations": sum(1 for op in e.operations if op.access is Access.READ),
                "credential_ready": None if e.secret_ref is None else "unknown",
                "learned_from": e.source.kind,
            }
            for e in entries
        ]

    async def onboard(
        self,
        api_id: str,
        *,
        spec_url: str | None = None,
        base_url: str | None = None,
        docs_url: str | None = None,
        name: str | None = None,
        caller: Caller = SYSTEM_CALLER,
        replace_provisional: bool = True,
    ) -> OnboardResult:
        """Learn an API and save it as a provisional entry.

        From an OpenAPI spec (`spec_url`, or discovered under `base_url`), from
        documentation pages (`docs_url` + `base_url`), or, when neither exists, from the
        model's own knowledge (`base_url` + a name; reads only, unconfirmed).
        """
        if not self.settings.onboarding_enabled:
            raise LibrarianError("onboarding is disabled on this librarian")
        if not caller.can_onboard:
            raise LibrarianError(f"caller {caller.agent!r} is not allowed to onboard APIs")
        await self.registry.refresh_stored()  # another replica may have learned it
        existing = self.registry.get(api_id)
        if existing is not None and (
            existing.status is not EntryStatus.PROVISIONAL or not replace_provisional
        ):
            raise LibrarianError(
                f"API {api_id!r} already exists ({existing.status}); edit it via review"
            )
        if spec_url is None and base_url is None and docs_url is None:
            raise LibrarianError("onboarding needs spec_url, docs_url or base_url")
        try:
            entry = await self._learn(
                api_id, spec_url=spec_url, base_url=base_url, docs_url=docs_url, name=name
            )
            entry, warnings = await self._settle_auth(entry)
        except (ContentFilterError, ModelAPIError, UnexpectedModelBehavior) as exc:
            raise await self._model_failed(exc, caller, "learning") from exc
        if existing is not None:
            entry = _keep_human_fields(existing, entry)
        internal = self._internal_note(entry)
        if internal:
            entry = entry.model_copy(update={"notes": (entry.notes + "\n" + internal)[:8000]})
        path = await self.registry.save(entry)
        reads = sum(1 for op in entry.operations if op.access is Access.READ)
        review = sum(1 for op in entry.operations if op.access is Access.UNKNOWN)
        review_url = review_error = None
        if self.publisher is not None:
            try:
                review_url = await self.publisher.publish(entry, caller.agent)
            except (PublishError, httpx.HTTPError, KeyError, ValueError) as exc:
                # Onboarding still succeeded; the entry is usable as provisional.
                review_error = f"could not open a review: {exc}"
                log.warning("publishing %s for review failed: %s", api_id, exc)
        await self.audit.record(
            AuditEvent(
                event="onboard",
                caller=caller.agent,
                principal=caller.principal,
                api=api_id,
                detail=f"{len(entry.operations)} operations ({reads} read, {review} need review)"
                f" from {entry.source.kind}",
                extra={"source": entry.source.url, "review_url": review_url}
                | ({"internal": internal} if internal else {}),
            )
        )
        summary = (
            f"{len(entry.operations)} operations ({reads} reads, {review} need review) from "
            f"{_learned_from(entry)}"
        )
        all_warnings = warnings + ([internal] if internal else [])
        try:
            if review_url:
                # The approve-or-deny step: a card with a link to the review page.
                await self.reviews.open(
                    api=entry.id,
                    api_name=entry.name,
                    host=entry.host,
                    review_url=review_url,
                    learned_by=caller.principal or caller.agent,
                    summary=summary,
                    auth=_auth_words(entry),
                    warnings=all_warnings,
                    needs_review=[op.id for op in entry.operations if op.access is Access.UNKNOWN],
                    channel=entry.owners.channel,
                    approver_group=entry.owners.approver_group,
                )
            else:
                await self.notifier.api_learned(
                    entry.id,
                    entry.name,
                    caller.principal or caller.agent,
                    f"{summary}; auth: {_auth_words(entry)}",
                    None,
                    entry.owners.channel,
                    all_warnings,
                    review_error,
                )
        except Exception as exc:  # a broken notifier must not undo a successful onboarding
            log.warning("could not announce the learned API %s: %s", api_id, exc)
        return OnboardResult(entry, path, reads, review, review_url, review_error)

    async def approve_review(
        self, review_id: str, by: str, decisions: dict[str, str]
    ) -> tuple[ReviewRequest, str | None]:
        """Apply the person's decisions on operations, then merge the proposal as the
        App. Returns the review and, when GitHub refused the merge, why (the review stays
        open so the person can merge on GitHub)."""
        review = await self.reviews.load(review_id)
        if review is None or review.state is not ReviewState.PENDING:
            raise LookupError(f"review {review_id!r} is not pending")
        for op_id, choice in decisions.items():
            if op_id in review.needs_review:
                await self.set_operation_access(review.api, op_id, choice, by)
        if self.publisher is None:
            raise LibrarianError("no registry publisher is configured")
        try:
            merged = await self.publisher.merge(review.api, by)
        except (PublishError, httpx.HTTPError, KeyError, ValueError) as exc:
            await self.audit.record(
                AuditEvent(
                    event="review_merge_failed",
                    caller="console",
                    principal=by,
                    api=review.api,
                    detail=str(exc)[:500],
                )
            )
            return review, str(exc)
        note = "merged; verified once the registry syncs" if merged else "nothing left to merge"
        return await self.reviews.decide(review_id, ReviewState.APPROVED, by, note), None

    async def deny_review(self, review_id: str, by: str) -> ReviewRequest:
        """Close the proposal and drop the API (a person said no)."""
        review = await self.reviews.load(review_id)
        if review is None or review.state is not ReviewState.PENDING:
            raise LookupError(f"review {review_id!r} is not pending")
        entry = self.registry.get(review.api)
        note = "closed"
        if entry is not None and entry.status is EntryStatus.PROVISIONAL:
            report = await self.forget(review.api, by, close_review=False)  # closes the PR too
            note = "closed and the API forgotten"
            if report.review_error:
                note += f" (could not close the proposal: {report.review_error})"
        elif self.publisher is not None:
            try:
                await self.publisher.withdraw(review.api, f"Denied by {by} (librarian).")
            except (PublishError, httpx.HTTPError, KeyError, ValueError) as exc:
                note = f"could not close the proposal: {exc}"
        return await self.reviews.decide(review_id, ReviewState.DENIED, by, note)

    async def _settle_auth(self, entry: ApiEntry) -> tuple[ApiEntry, list[str]]:
        """A spec that declares no authentication is common even for APIs that need a key
        (GitHub's does). Rather than leave that to a person editing YAML, ask the model what
        the API uses and write it into the proposed entry, for approval."""
        if not isinstance(entry.auth, NoAuth) or entry.source.kind != "openapi":
            return entry, []
        if self.extractor is None:
            return entry, [
                "No authentication: the spec declares none and no model is configured to "
                "check. If calls answer 401, choose the authentication in the console."
            ]
        extracted = await extract_from_knowledge(
            self.extractor, base_url=entry.base_url, api_name=entry.name
        )
        if extracted.auth == "none":
            note = "\nThe spec declares no authentication and the model agrees."
            return entry.model_copy(update={"notes": (entry.notes + note)[:8000]}), []
        auth = auth_from_extracted(extracted, f"{entry.id}/readonly")
        if isinstance(auth, NoAuth):
            return entry, [
                "No authentication in the spec, and the model could not tell what this API "
                "uses. If calls answer 401, choose the authentication in the console."
            ]
        return self._with_auth(entry, auth, "inferred from the model's knowledge", extracted), []

    def _with_auth(
        self, entry: ApiEntry, auth: Any, how: str, extracted: ExtractedApi | None = None
    ) -> ApiEntry:
        validation = guess_validation_operation(entry.operations)
        update: dict[str, Any] = {"validation_operation": validation}
        if extracted is not None:
            if extracted.credential_url and extracted.credential_url.startswith("https://"):
                update["create_url"] = extracted.credential_url[:500]
            if extracted.scopes:
                update["scopes"] = [s[:100] for s in extracted.scopes[:20]]
            if extracted.credential_instructions:
                update["instructions"] = extracted.credential_instructions[:400]
        guidance = entry.credential.model_copy(update=update)
        note = (
            f"\nAuthentication {how} (the spec declares none): {auth.type}. "
            "Approve if that is right; librarian asks for the key when it is first needed."
        )
        if validation:
            note += f" New credentials are tested with `{validation}`."
        return entry.model_copy(
            update={"auth": auth, "credential": guidance, "notes": (entry.notes + note)[:8000]}
        )

    async def set_auth(
        self, api_id: str, scheme: str, key_name: str | None, by: str
    ) -> tuple[ApiEntry, str | None]:
        """A person chose the authentication for a provisional API (the console). Returns
        the entry and the review link, if a review could be opened."""
        entry = self.registry.get(api_id)
        if entry is None:
            raise LibrarianError(f"unknown API {api_id!r}")
        if entry.status is not EntryStatus.PROVISIONAL:
            raise LibrarianError("only provisional entries change here; verified ones via review")
        extracted = ExtractedApi.model_validate({"auth": scheme, "auth_key_name": key_name})
        if scheme == "none":
            updated = entry.model_copy(
                update={"auth": NoAuth(), "credential": CredentialGuidance()}
            )
        else:
            auth = auth_from_extracted(extracted, f"{api_id}/readonly")
            if isinstance(auth, NoAuth):
                raise LibrarianError("that authentication needs a valid header or query name")
            updated = self._with_auth(entry, auth, f"chosen by {by}")
        await self.registry.save(updated)
        link = await self._propose(
            updated,
            f"{updated.name}: authentication {scheme}",
            f"{by} set the authentication to {scheme} in the console.",
        )
        await self.audit.record(
            AuditEvent(
                event="auth_set",
                caller="console",
                principal=by,
                api=api_id,
                detail=scheme,
                extra={"review_url": link},
            )
        )
        return updated, link

    async def _propose(self, entry: ApiEntry, title: str, body: str) -> str | None:
        """Carry a decision into the entry's review: a new commit on its open proposal
        when there is one (one pull request per entry, approve or deny), else a new
        proposal. None when no review could be opened (logged)."""
        if self.publisher is None:
            return None
        try:
            link = await self.publisher.refresh(entry, f"{title}\n\n{body}")
            if link is None:
                link = await self.publisher.propose_update(entry, title, body)
            return link
        except (PublishError, httpx.HTTPError, KeyError, ValueError) as exc:
            log.warning("could not carry %s into a review: %s", entry.id, exc)
            return None

    async def set_operation_access(
        self, api_id: str, op_id: str, choice: str, by: str
    ) -> tuple[ApiEntry, str | None]:
        """A person decided what an operation with unknown access is: `read` (safe to
        call), `write` (needs approval each time) or `exclude` (agents may not call it).
        Provisional entries change at once and the decision is carried into the open
        review; verified entries only get a proposal."""
        entry = self.registry.get(api_id)
        if entry is None:
            raise LibrarianError(f"unknown API {api_id!r}")
        op = entry.operation(op_id)
        if op is None:
            raise LibrarianError(f"{api_id!r} has no operation {op_id!r}")
        if choice == "read":
            changed = op.model_copy(
                update={"access": Access.READ, "read_basis": ReadBasis.REVIEWED}
            )
        elif choice == "write":
            changed = op.model_copy(update={"access": Access.WRITE, "writable": True})
        elif choice == "exclude":
            changed = op.model_copy(update={"access": Access.WRITE, "writable": False})
        else:
            raise LibrarianError("the choice is read, write or exclude")
        operations = [changed if o.id == op_id else o for o in entry.operations]
        note = f"\n{by} marked `{op_id}` as {choice} in the console."
        updated = entry.model_copy(
            update={"operations": operations, "notes": (entry.notes + note)[:8000]}
        )
        if entry.status is EntryStatus.PROVISIONAL:
            await self.registry.save(updated)
        link = await self._propose(
            updated,
            f"{entry.name}: {op_id} is {choice}",
            f"{by} decided in the console that `{op_id}` is {choice}.",
        )
        await self.audit.record(
            AuditEvent(
                event="operation_access_set",
                caller="console",
                principal=by,
                api=api_id,
                operation=op_id,
                detail=choice,
                extra={"review_url": link, "applied": entry.status is EntryStatus.PROVISIONAL},
            )
        )
        return updated, link

    async def _settle_auth_after_401(self, entry: ApiEntry, status: int) -> ApiEntry | None:
        """An API whose entry says "no authentication" answered 401. Work the scheme out
        and, for a provisional entry, apply it so the credential flow can ask for a key.
        A verified entry gets an update proposal instead; either way people are told once."""
        inferred: ApiEntry | None = None
        if self.extractor is not None:
            try:
                extracted = await extract_from_knowledge(
                    self.extractor, base_url=entry.base_url, api_name=entry.name
                )
                auth = auth_from_extracted(extracted, f"{entry.id}/readonly")
                if not isinstance(auth, NoAuth):
                    inferred = self._with_auth(entry, auth, "inferred after a 401", extracted)
            except (ContentFilterError, ModelAPIError, UnexpectedModelBehavior) as exc:
                log.warning("could not infer %s's authentication: %s", entry.id, exc)
        if inferred is not None and entry.status is EntryStatus.PROVISIONAL:
            await self.registry.save(inferred)
            await self.audit.record(
                AuditEvent(
                    event="auth_inferred",
                    caller="librarian",
                    api=entry.id,
                    detail=inferred.auth.type,
                )
            )
            return inferred
        if entry.id in self._auth_notified:
            return None
        self._auth_notified.add(entry.id)
        link = None
        details = [
            f"{entry.name} answered HTTP {status} (not authenticated), but its entry "
            "declares no authentication."
        ]
        if inferred is not None and self.publisher is not None:
            try:
                link = await self.publisher.propose_update(
                    inferred,
                    f"{entry.name}: needs authentication ({inferred.auth.type})",
                    f"The API answered {status}; librarian inferred {inferred.auth.type}.",
                )
                details.append(f"Proposed: {inferred.auth.type}. Approve the update.")
            except (PublishError, httpx.HTTPError, KeyError, ValueError) as exc:
                details.append(f"Could not open an update proposal: {exc}")
        else:
            details.append("Choose its authentication in the console (APIs → this API).")
        await self.notifier.api_changed(
            entry.id,
            "Needs a credential",
            details,
            link,
            entry.owners.channel,
            mention_group=entry.owners.approver_group,
        )
        return None

    async def _model_failed(self, exc: Exception, caller: Caller, what: str) -> LibrarianError:
        """Turn a model failure into something a caller can act on, and record it."""
        if isinstance(exc, ContentFilterError):
            message = (
                f"the model declined this request (content filter) while {what}: {exc}. "
                "Rephrase it, or call the operation directly with execute. A fallback_model "
                "setting makes librarian try another model once."
            )
            event = "model_refused"
        elif isinstance(exc, ModelAPIError):
            message = f"the model API failed while {what}: {exc}"
            event = "model_failed"
        else:
            message = f"the model gave an unusable answer while {what}: {exc}"
            event = "model_failed"
        await self.audit.record(
            AuditEvent(
                event=event, caller=caller.agent, principal=caller.principal, detail=str(exc)[:500]
            )
        )
        return LibrarianError(message)

    def _internal_note(self, entry: ApiEntry) -> str:
        """Says so when the API is reachable only because of an `internal_hosts` pattern,
        for the reviewer of the PR and the audit log."""
        host = urlsplit(entry.base_url).hostname or ""
        pattern = self.settings.network.internal_pattern(host)
        if pattern is None or self.settings.allow_private_networks:
            return ""
        return f"Internal API: {host} is reachable because it matches internal_hosts {pattern!r}."

    async def _learn(
        self,
        api_id: str,
        *,
        spec_url: str | None = None,
        base_url: str | None = None,
        docs_url: str | None = None,
        name: str | None = None,
    ) -> ApiEntry:
        net = self.settings.network
        client = self.source_client_factory()
        try:
            if spec_url is not None:
                spec = await fetch_spec(spec_url, client, net=net)
                return entry_from_spec(
                    spec, api_id=api_id, spec_url=spec_url, base_url=base_url, name=name
                )
            if docs_url is not None:
                if base_url is None:
                    raise LibrarianError(
                        "learning from documentation needs base_url: the API host is never "
                        "taken from the docs themselves"
                    )
                crawl = await crawl_docs(
                    docs_url,
                    client,
                    net=net,
                    max_pages=self.settings.learning.docs_max_pages,
                )
                if crawl.spec is not None:  # the docs link a real spec: use it
                    return entry_from_spec(
                        crawl.spec,
                        api_id=api_id,
                        spec_url=crawl.spec_url,
                        base_url=base_url,
                        name=name,
                    )
                extractor = self._require_extractor("learning from documentation")
                extracted, found = await extract_from_docs(
                    extractor, crawl, base_url=base_url, api_name=name
                )
                entry = entry_from_docs(
                    extracted, found, api_id=api_id, base_url=base_url, crawl=crawl, name=name
                ).entry
                trust = trust_of(docs_url, base_url)
                return entry.model_copy(
                    update={
                        "source": entry.source.model_copy(
                            update={"found_by": "given", "trust": trust}
                        )
                    }
                )
            if base_url is None:
                raise LibrarianError("onboarding needs spec_url, docs_url or base_url")
            try:
                found_url, spec = await discover_spec(base_url, client, net=net)
            except SpecError as no_spec:
                return await self._learn_without_spec(api_id, base_url, name, client, no_spec)
            return entry_from_spec(
                spec, api_id=api_id, spec_url=found_url, base_url=base_url, name=name
            )
        except (ContentFilterError, ModelAPIError, UnexpectedModelBehavior):
            raise  # explained by the caller (onboard), with an audit event
        except (SpecError, httpx.HTTPError, ValueError, AgentRunError, SearchDidNotRun) as exc:
            raise LibrarianError(f"could not learn {api_id!r}: {exc}") from exc
        finally:
            await client.aclose()

    async def _learn_without_spec(
        self,
        api_id: str,
        base_url: str,
        name: str | None,
        client: httpx.AsyncClient,
        no_spec: SpecError,
    ) -> ApiEntry:
        """No spec at the usual paths: find documentation (usual places, then a web
        search if configured), else fall back to the model's own knowledge."""
        learning = self.settings.learning
        net = self.settings.network
        notes: list[str] = [str(no_spec)]
        searcher = self._searcher(client)
        if learning.find_docs or searcher is not None:
            found, tried = await find_docs(
                base_url,
                client,
                name=name,
                searcher=searcher,
                net=net,
                allow_third_party=learning.search.allow_third_party,
                look_nearby=learning.find_docs,
            )
            notes += tried
            if found is not None:
                provenance = {"found_by": found.found_by, "trust": found.trust}
                if found.spec is not None:
                    entry = entry_from_spec(
                        found.spec, api_id=api_id, spec_url=found.url, base_url=base_url, name=name
                    )
                    return _with_source(entry, provenance)
                extractor = self._require_extractor("learning from documentation")
                crawl = await crawl_docs(
                    found.url, client, net=net, max_pages=learning.docs_max_pages
                )
                if crawl.spec is not None:
                    entry = entry_from_spec(
                        crawl.spec,
                        api_id=api_id,
                        spec_url=crawl.spec_url,
                        base_url=base_url,
                        name=name,
                    )
                    return _with_source(entry, provenance)
                extracted, pages = await extract_from_docs(
                    extractor, crawl, base_url=base_url, api_name=name
                )
                entry = entry_from_docs(
                    extracted, pages, api_id=api_id, base_url=base_url, crawl=crawl, name=name
                ).entry
                return _with_source(entry, provenance)
        if self.extractor is None or not learning.model_knowledge:
            raise SpecError("; ".join(notes))
        extracted = await extract_from_knowledge(
            self.extractor, base_url=base_url, api_name=name or api_id
        )
        return entry_from_knowledge(
            extracted, api_id=api_id, base_url=base_url, name=name or api_id
        ).entry

    def _searcher(self, client: httpx.AsyncClient) -> Searcher | None:
        search = self.settings.learning.search
        if search.provider == "searxng":
            if not search.url:
                raise LibrarianError("learning.search.provider searxng needs learning.search.url")
            return searxng_searcher(search.url, client, search.max_results)
        if search.provider == "brave":
            if search.api_key is None:
                raise LibrarianError("learning.search.provider brave needs an api_key")
            return brave_searcher(search.api_key.get_secret_value(), client, search.max_results)
        if search.provider == "model":
            if self.search_model is None:
                raise LibrarianError("learning.search.provider model needs `model` to be set")
            return model_searcher(self.search_model, search.max_results)
        return None

    def _require_extractor(self, what: str) -> Agent[None, Any]:
        if self.extractor is None:
            raise LibrarianError(f"{what} needs a model: set `model` in the configuration")
        return self.extractor

    # -- change detection --------------------------------------------------------

    # -- console actions (a signed-in person; every one is audited) --------------------

    async def block(self, api_id: str, by: str) -> ApiEntry:
        """Stop agents using an API until someone unblocks it. Open review proposals stay
        open, with a comment saying so."""
        entry = await self.registry.set_status(api_id, EntryStatus.BLOCKED)
        extra: dict[str, Any] = {}
        if self.publisher is not None:
            try:
                extra["commented"] = await self.publisher.comment(
                    api_id,
                    f"Blocked in the console by {by}: agents are refused until someone "
                    "unblocks it. This review stays open.",
                )
            except (PublishError, httpx.HTTPError, KeyError, ValueError) as exc:
                extra["review_error"] = str(exc)
                log.warning("could not comment on %s's reviews: %s", api_id, exc)
        await self.audit.record(
            AuditEvent(event="api_blocked", caller="console", principal=by, api=api_id, extra=extra)
        )
        return entry

    async def unblock(self, api_id: str, by: str) -> ApiEntry:
        """Back to its reviewed status, or provisional for a learned API."""
        reviewed = self.registry.reviewed(api_id)
        status = EntryStatus.PROVISIONAL
        if reviewed is not None and reviewed.status is not EntryStatus.BLOCKED:
            status = reviewed.status
        entry = await self.registry.set_status(api_id, status)
        await self.audit.record(
            AuditEvent(
                event="api_unblocked",
                caller="console",
                principal=by,
                api=api_id,
                detail=f"now {status.value}",
            )
        )
        return entry

    async def forget(self, api_id: str, by: str, *, close_review: bool = True) -> ForgetReport:
        """Drop a learned API altogether: the entry, what was observed about it, anything
        waiting on a person for it, and its open review proposals (closed as the App).
        The next agent that needs it teaches it again."""
        report = ForgetReport()
        review = self.reviews.pending_for(api_id)
        if review is not None and close_review:
            await self.reviews.decide(review.id, ReviewState.DENIED, by, "the API was forgotten")
        pending = self.broker.pending_for(api_id)
        if pending is not None:
            await self.broker.deny(pending.id, by)
            report.cancelled += 1
        for approval in [a for a in self.approvals.pending() if a.api == api_id]:
            await self.approvals.deny(approval.id, by)
            report.cancelled += 1
        await self.registry.forget(api_id)
        report.observations = await self.observations.forget(api_id)
        if self.publisher is not None:
            try:
                report.closed = await self.publisher.withdraw(
                    api_id, f"Forgotten in the console by {by}; this proposal is withdrawn."
                )
            except (PublishError, httpx.HTTPError, KeyError, ValueError) as exc:
                report.review_error = str(exc)
                log.warning("could not close %s's reviews: %s", api_id, exc)
        await self.audit.record(
            AuditEvent(
                event="api_forgotten",
                caller="console",
                principal=by,
                api=api_id,
                extra={
                    "closed_reviews": report.closed,
                    "cancelled": report.cancelled,
                    "observations": report.observations,
                }
                | ({"review_error": report.review_error} if report.review_error else {}),
            )
        )
        return report

    async def invalidate_credential(self, api_id: str, by: str) -> list[str]:
        """Overwrite the API's credential(s) with a useless value. The next call fails with
        401, which opens a fresh credential request, so the key gets replaced out of band."""
        entry = self.registry.get(api_id)
        if entry is None:
            raise LibrarianError(f"unknown API {api_id!r}")
        refs = [r for r in (entry.secret_ref, entry.write_secret_ref) if r]
        if not refs:
            raise LibrarianError(f"{api_id!r} has no credential")
        if not self.secrets.writable:
            raise LibrarianError("the secret store is read-only here")
        for ref in refs:
            await self.secrets.put(ref, f"invalidated-{pysecrets.token_hex(8)}")
        await self.audit.record(
            AuditEvent(
                event="credential_invalidated",
                caller="console",
                principal=by,
                api=api_id,
                extra={"refs": refs},
            )
        )
        return refs

    async def recheck(self, api_id: str) -> RecheckReport:
        """Re-read an entry's spec or docs; handle any change by risk (see drift.py)."""
        entry = self.registry.get(api_id)
        if entry is None:
            raise LibrarianError(f"unknown API {api_id!r}")
        source = entry.source
        if source.kind not in ("openapi", "docs") or not source.url:
            return RecheckReport(api_id, "skipped", detail="no spec or docs to re-read")
        last = await self._last_hash(entry)
        net = self.settings.network
        client = self.source_client_factory()
        try:
            if source.kind == "openapi":
                spec = await fetch_spec(source.url, client, net=net)
                current_hash = spec_hash(spec)
                if current_hash == last:
                    return RecheckReport(api_id, "unchanged")
                fresh = entry_from_spec(
                    spec,
                    api_id=api_id,
                    spec_url=source.url,
                    base_url=entry.base_url,
                    name=entry.name,
                )
            else:
                crawl = await crawl_docs(
                    source.url,
                    client,
                    net=net,
                    max_pages=max(len(source.pages), 1),
                )
                current_hash = spec_hash(crawl.spec) if crawl.spec else crawl.content_hash
                if current_hash == last:
                    return RecheckReport(api_id, "unchanged")
                if crawl.spec is not None:
                    fresh = entry_from_spec(
                        crawl.spec,
                        api_id=api_id,
                        spec_url=crawl.spec_url,
                        base_url=entry.base_url,
                        name=entry.name,
                    )
                elif self.extractor is None:
                    await self._remember_hash(api_id, current_hash)
                    await self._notify_change(
                        entry,
                        "documentation changed",
                        [f"{source.url} changed; no model is configured to re-read it"],
                        None,
                    )
                    return RecheckReport(api_id, "changed", detail="docs changed; no model")
                else:
                    extracted, found = await extract_from_docs(
                        self.extractor, crawl, base_url=entry.base_url, api_name=entry.name
                    )
                    fresh = entry_from_docs(
                        extracted,
                        found,
                        api_id=api_id,
                        base_url=entry.base_url,
                        crawl=crawl,
                        name=entry.name,
                    ).entry
        except (SpecError, httpx.HTTPError, ValueError, AgentRunError) as exc:
            return RecheckReport(api_id, "error", detail=f"could not re-read the source: {exc}")
        finally:
            await client.aclose()

        await self._remember_hash(api_id, current_hash)
        changes = diff(entry, fresh)
        if not changes:
            return RecheckReport(api_id, "unchanged", detail="source changed; operations did not")
        actions, review_url = await self._apply_changes(entry, fresh, changes)
        details = [f"[{c.severity}] {c.detail}" for c in changes] + actions
        await self._notify_change(
            entry,
            f"{entry.name} changed ({worst(changes)})",
            details,
            review_url,
            urgent=worst(changes) != "additive",
        )
        await self.audit.record(
            AuditEvent(
                event="api_changed",
                api=api_id,
                detail=f"{len(changes)} change(s), worst {worst(changes)}",
                extra={"review_url": review_url, "actions": actions},
            )
        )
        return RecheckReport(api_id, "changed", changes, actions, review_url)

    async def _apply_changes(
        self, entry: ApiEntry, fresh: ApiEntry, changes: list[Change]
    ) -> tuple[list[str], str | None]:
        actions: list[str] = []
        review_url: str | None = None
        if entry.status is EntryStatus.PROVISIONAL:
            # Never reviewed: take the fresh copy (keeping owners and auth) and re-propose.
            updated = _keep_human_fields(entry, fresh)
            await self.registry.save(updated)
            actions.append("the provisional entry was updated to match")
            if self.publisher is not None:
                try:
                    review_url = await self.publisher.publish(updated, "librarian (API changed)")
                except (PublishError, httpx.HTTPError, KeyError, ValueError) as exc:
                    actions.append(f"could not open a review: {exc}")
            return actions, review_url

        # Reviewed entries are not changed until a human merges the proposal; until
        # then, operations that may now do something else are held back.
        for change in changes:
            op = entry.operation(change.operation)
            if op is None:
                continue
            if change.severity == "safety" or (change.severity == "breaking" and op.writable):
                await self.observations.suspend(entry, op, change.detail)
                actions.append(f"suspended {op.id}: {change.detail}")
            elif change.severity == "breaking":
                await self.observations.warn(entry, op, f"API changed: {change.detail}")
                actions.append(f"callers of {op.id} are warned: {change.detail}")
        if self.publisher is not None:
            proposal = proposed_update(entry, fresh, changes)
            try:
                review_url = await self.publisher.propose_update(
                    proposal,
                    f"registry: `{entry.id}` changed at its source ({worst(changes)})",
                    review_body(entry, changes, actions),
                )
            except (PublishError, httpx.HTTPError, KeyError, ValueError) as exc:
                actions.append(f"could not open a review: {exc}")
        return actions, review_url

    async def recheck_all_if_due(self) -> list[RecheckReport]:
        """Re-check every entry once per `drift.recheck_hours`, on one replica only."""
        hours = self.settings.drift.recheck_hours
        if hours <= 0:
            return []
        bucket = int(datetime.now(UTC).timestamp() // (hours * 3600))
        if bucket <= self._last_recheck_bucket:
            return []
        self._last_recheck_bucket = bucket
        if self.state is not None:
            await self.state.create_doc_if_absent("lease", "recheck", "-1", "-1")
            previous = await self.state.get_doc("lease", "recheck") or "-1"
            if int(previous) >= bucket or not await self.state.transition_doc(
                "lease", "recheck", previous, str(bucket), str(bucket)
            ):
                return []  # another replica has this round
        reports: list[RecheckReport] = []
        for entry in self.registry.all():
            if entry.status is EntryStatus.BLOCKED or not entry.source.url:
                continue
            try:
                reports.append(await self.recheck(entry.id))
            except Exception:
                log.exception("re-checking %s failed", entry.id)
        return reports

    async def _last_hash(self, entry: ApiEntry) -> str | None:
        if self.state is not None:
            stored = await self.state.get_doc("source-hash", entry.id)
            if stored is not None:
                return stored
        return self._last_hashes.get(entry.id, entry.source.content_hash)

    async def _remember_hash(self, api_id: str, value: str) -> None:
        self._last_hashes[api_id] = value
        if self.state is not None:
            await self.state.save_doc("source-hash", api_id, "active", value)

    async def _report_finding(self, finding: Finding) -> None:
        entry = self.registry.get(finding.api)
        if entry is None:
            return
        await self.audit.record(
            AuditEvent(
                event="api_signal",
                api=finding.api,
                operation=finding.operation,
                detail=f"{finding.kind}: {finding.detail}",
            )
        )
        title = _FINDING_TITLES.get(finding.kind, finding.kind)
        await self._notify_change(
            entry, f"{entry.name}: {title}", [finding.detail], None, finding.severity != "info"
        )

    async def _notify_change(
        self,
        entry: ApiEntry,
        title: str,
        details: list[str],
        link: str | None,
        urgent: bool = False,
    ) -> None:
        group = entry.owners.approver_group or self.settings.notifier.slack.approver_group
        try:
            await self.notifier.api_changed(
                entry.id,
                title,
                details,
                link,
                entry.owners.channel or self.settings.notifier.slack.channel,
                mention_group=group if urgent else None,
            )
        except Exception:
            log.exception("notifying about %s failed", entry.id)

    async def discover(self, api_id: str) -> int:
        """Run an API's discovery reads and store what they teach as its glossary."""
        entry = self.registry.get(api_id)
        if entry is None:
            raise LibrarianError(f"unknown API {api_id!r}")
        if not entry.discovery:
            return 0
        glossary: dict[str, str] = {}
        for item in entry.discovery:
            result = await self.executor.execute(
                CallRequest(api=entry.id, operation=item.operation, max_pages=5), SYSTEM_CALLER
            )
            if not result.ok:
                raise LibrarianError(
                    f"discovery {item.operation} returned HTTP {result.status_code}"
                )
            data: Any = get_path(result.data, item.items_path) if item.items_path else result.data
            for record in data if isinstance(data, list) else []:
                if not isinstance(record, dict):
                    continue
                fields = cast(dict[str, Any], record)
                key, name = fields.get(item.key), fields.get(item.value)
                if key is None or name is None:
                    continue
                key_text = str(key)[:100]
                if item.key_prefix and not key_text.startswith(item.key_prefix):
                    continue
                glossary[key_text] = str(name)[:200]
                if len(glossary) >= 5000:
                    break
        current = self.registry.get(api_id) or entry
        data_out = current.model_dump(mode="json", by_alias=True)
        data_out["glossary"] = glossary
        await self.registry.save(ApiEntry.model_validate(data_out))
        await self.audit.record(
            AuditEvent(event="discovery", api=api_id, detail=f"{len(glossary)} glossary entries")
        )
        return len(glossary)

    async def _discover_quietly(self, api_id: str) -> None:
        entry = self.registry.get(api_id)
        if entry is None or not entry.discovery:
            return
        try:
            await self.discover(api_id)
        except (LibrarianError, PolicyDenied, CredentialMissing, httpx.HTTPError) as exc:
            log.warning("discovery for %s failed: %s", api_id, exc)

    # -- credentials ------------------------------------------------------------

    async def _await_credential(
        self,
        missing: CredentialMissing,
        caller: Caller,
        on_auth_required: AuthRequiredHook | None,
    ) -> None:
        if missing.principal:  # the person, not the approvers, has to act
            consent = await self.consents.open(missing.entry, caller, missing.reason)
            if on_auth_required is not None:
                await on_auth_required(consent)
            done = await self.consents.wait(
                consent.id, max_wait=self.settings.credential_wait_seconds
            )
            if done.state is not ConsentState.CONNECTED:
                raise LibrarianError(
                    f"{missing.entry.name}: {consent.principal} did not sign in ({done.state})"
                )
            return
        request = await self.broker.open(missing.entry, caller, missing.reason)
        if on_auth_required is not None:
            await on_auth_required(request)
        resolved = await self.broker.wait(
            request.id, max_wait=self.settings.credential_wait_seconds
        )
        if resolved.state is not RequestState.FULFILLED:
            raise CredentialUnavailable(resolved)

    async def _validate_credential(self, entry: ApiEntry) -> str | None:
        op_id = entry.credential.validation_operation
        if op_id is None:
            return None
        try:
            result = await self.executor.execute(
                CallRequest(api=entry.id, operation=op_id), SYSTEM_CALLER
            )
        except CredentialMissing as exc:
            return exc.reason
        except PolicyDenied as exc:
            return f"validation call refused: {exc.reason}"
        except httpx.HTTPError as exc:
            return f"validation call failed: {exc}"
        if not result.ok:
            return f"validation call returned HTTP {result.status_code}"
        return None


def _default_secrets(settings: Settings) -> SecretStore:
    stores: list[SecretStore] = []
    if settings.secrets_backend == "aws":
        aws = settings.aws_secrets
        stores.append(
            AwsSecretsManagerStore(
                prefix=aws.prefix, region=aws.region, cache_seconds=aws.cache_seconds
            )
        )
    elif settings.secrets_dir is not None:
        stores.append(FileSecretStore(settings.secrets_dir))
    stores.append(EnvSecretStore())
    return ChainSecretStore(stores)


def _default_publisher(settings: Settings) -> Publisher | None:
    config = settings.publish
    if config.type != "github":
        return None
    if not config.repo or (config.token is None) == (config.github_app is None):
        raise ValueError(
            "publish.type=github requires publish.repo and one of publish.github_app "
            "(recommended) or publish.token"
        )
    app = config.github_app
    auth: str | GitHubAppAuth = (
        GitHubAppAuth(
            app.app_id,
            app.key(),
            config.repo,
            installation_id=app.installation_id,
            api_url=config.api_url,
        )
        if app is not None
        else config.token.get_secret_value()  # pyright: ignore[reportOptionalMemberAccess]
    )
    return GitHubPublisher(
        config.repo,
        auth,
        base_branch=config.base_branch,
        directory=config.directory,
        api_url=config.api_url,
    )


def _default_notifier(settings: Settings) -> Notifier:
    config = settings.notifier
    if config.type == "slack":
        if config.slack.bot_token is None:
            raise ValueError("notifier.type=slack requires notifier.slack.bot_token")
        return SlackNotifier(config.slack.bot_token.get_secret_value(), config.slack.channel)
    if config.type == "webhook":
        if not config.webhook_url:
            raise ValueError("notifier.type=webhook requires notifier.webhook_url")
        return WebhookNotifier(config.webhook_url)
    return Notifier()


NO_GATEWAY_KEY = "no-key-configured"  # sent to a gateway that needs no key


def _with_fallback(model: Model | str, settings: Settings) -> Model | str:
    """Try `fallback_model` once when the main model fails or declines."""
    fallback = _model_from_settings(settings, settings.fallback_model)
    if fallback is None:
        return model
    return FallbackModel(model, fallback, fallback_on=(ModelAPIError, ContentFilterError))


def _learned_from(entry: ApiEntry) -> str:
    return {
        "openapi": "an OpenAPI spec",
        "docs": "documentation",
        "inferred": "the model's knowledge",
        "manual": "a person",
    }.get(entry.source.kind, entry.source.kind)


def _auth_words(entry: ApiEntry) -> str:
    if isinstance(entry.auth, NoAuth):
        return "none"
    return f"{entry.auth.type} ({entry.secret_ref})"


def _model_from_settings(settings: Settings, name: str | None = None) -> Model | str | None:
    """The configured model (`name` overrides the model name, with the same gateway)."""
    name = name or settings.model
    if name is None:
        return None
    if settings.model_base_url is None:
        return name  # e.g. "anthropic:claude-sonnet-5-5"; provider reads its own env
    # Only ever the key configured for the gateway. Left unset, the provider libraries
    # would pick up OPENAI_API_KEY / ANTHROPIC_API_KEY from the environment and send that
    # real provider key to the gateway URL.
    api_key = (
        settings.model_api_key.get_secret_value() if settings.model_api_key else NO_GATEWAY_KEY
    )
    name = name.split(":", 1)[1] if ":" in name else name
    if settings.model_api == "anthropic":
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider

        # LiteLLM and other gateways serve the Anthropic Messages API at the root.
        base = settings.model_base_url.removesuffix("/v1").rstrip("/")
        return AnthropicModel(name, provider=AnthropicProvider(base_url=base, api_key=api_key))
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    return OpenAIChatModel(
        name, provider=OpenAIProvider(base_url=settings.model_base_url, api_key=api_key)
    )
