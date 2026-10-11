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

import httpx
from pydantic_ai import Agent
from pydantic_ai.exceptions import AgentRunError, UsageLimitExceeded
from pydantic_ai.models import Model

from librarian import telemetry
from librarian.approvals import ApprovalBroker, ApprovalState, Grant, WriteApproval
from librarian.audit import AuditEvent, AuditSink, JsonlAuditSink
from librarian.calls import Caller, CallRequest, CallResult, WriteRequest
from librarian.config import Settings
from librarian.credentials import CredentialBroker, CredentialRequest, Notifier, RequestState
from librarian.docs import (
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
from librarian.observations import Finding, Observations
from librarian.openapi import SpecError, discover_spec, entry_from_spec, fetch_spec, spec_hash
from librarian.opsearch import rank
from librarian.planner import FetchAnswer, FetchOutcome, Planner
from librarian.policy import PolicyEngine
from librarian.publish import GitHubPublisher, Publisher, PublishError
from librarian.recipes import Recipe, recipe_summary
from librarian.recipes import learn as learn_recipe
from librarian.registry.models import Access, ApiEntry, EntryStatus, Operation
from librarian.registry.store import Registry
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

AuthRequiredHook = Callable[[CredentialRequest], Awaitable[None]]


class LibrarianError(Exception):
    """An error safe to show to the calling agent."""


class CredentialUnavailable(LibrarianError):
    def __init__(self, request: CredentialRequest) -> None:
        super().__init__(
            f"no credential for {request.api!r}: request {request.state}"
            + (f" ({request.resolution_note})" if request.resolution_note else "")
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
            allow_private_provisional=settings.allow_private_networks,
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
        self.extractor: Agent[None, Any] | None = (
            build_extractor(model, mode) if model is not None else None
        )
        self.search_model: Model | str | None = model
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
        existing = self.registry.get(api_id)
        if existing is not None and (
            existing.status is not EntryStatus.PROVISIONAL or not replace_provisional
        ):
            raise LibrarianError(
                f"API {api_id!r} already exists ({existing.status}); edit it via review"
            )
        if spec_url is None and base_url is None and docs_url is None:
            raise LibrarianError("onboarding needs spec_url, docs_url or base_url")
        entry = await self._learn(
            api_id, spec_url=spec_url, base_url=base_url, docs_url=docs_url, name=name
        )
        if existing is not None:
            entry = _keep_human_fields(existing, entry)
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
                extra={"source": entry.source.url, "review_url": review_url},
            )
        )
        return OnboardResult(entry, path, reads, review, review_url, review_error)

    async def _learn(
        self,
        api_id: str,
        *,
        spec_url: str | None = None,
        base_url: str | None = None,
        docs_url: str | None = None,
        name: str | None = None,
    ) -> ApiEntry:
        private = self.settings.allow_private_networks
        client = self.source_client_factory()
        try:
            if spec_url is not None:
                spec = await fetch_spec(spec_url, client, allow_private=private)
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
                    allow_private=private,
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
                found_url, spec = await discover_spec(base_url, client, allow_private=private)
            except SpecError as no_spec:
                return await self._learn_without_spec(api_id, base_url, name, client, no_spec)
            return entry_from_spec(
                spec, api_id=api_id, spec_url=found_url, base_url=base_url, name=name
            )
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
        private = self.settings.allow_private_networks
        notes: list[str] = [str(no_spec)]
        searcher = self._searcher(client)
        if learning.find_docs or searcher is not None:
            found, tried = await find_docs(
                base_url,
                client,
                name=name,
                searcher=searcher,
                allow_private=private,
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
                    found.url, client, allow_private=private, max_pages=learning.docs_max_pages
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

    async def recheck(self, api_id: str) -> RecheckReport:
        """Re-read an entry's spec or docs; handle any change by risk (see drift.py)."""
        entry = self.registry.get(api_id)
        if entry is None:
            raise LibrarianError(f"unknown API {api_id!r}")
        source = entry.source
        if source.kind not in ("openapi", "docs") or not source.url:
            return RecheckReport(api_id, "skipped", detail="no spec or docs to re-read")
        last = await self._last_hash(entry)
        private = self.settings.allow_private_networks
        client = self.source_client_factory()
        try:
            if source.kind == "openapi":
                spec = await fetch_spec(source.url, client, allow_private=private)
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
                    allow_private=private,
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


def _model_from_settings(settings: Settings) -> Model | str | None:
    if settings.model is None:
        return None
    if settings.model_base_url is None:
        return settings.model  # e.g. "anthropic:claude-sonnet-5-5"; provider reads its own env
    # Only ever the key configured for the gateway. Left unset, the provider libraries
    # would pick up OPENAI_API_KEY / ANTHROPIC_API_KEY from the environment and send that
    # real provider key to the gateway URL.
    api_key = (
        settings.model_api_key.get_secret_value() if settings.model_api_key else NO_GATEWAY_KEY
    )
    name = settings.model.split(":", 1)[1] if ":" in settings.model else settings.model
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
