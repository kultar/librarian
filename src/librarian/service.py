"""The Librarian service: wires registry, policy, executor, credentials and planner."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets as pysecrets
import time
import traceback
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
import yaml
from pydantic import ValidationError
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
from librarian.audit import (
    AuditEvent,
    AuditSink,
    DatabaseAuditSink,
    FanoutAuditSink,
    JsonlAuditSink,
)
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
    extract_write_guidance,
)
from librarian.drift import Change, diff, proposed_update, worst
from librarian.enablement import EnableState, EnableWriteBroker, EnableWriteRequest
from librarian.errors import ApiUnavailable, LibrarianError
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
from librarian.record import Recorder, RecordStatus
from librarian.registry.models import (
    KEYLESS,
    MADE_PER_CALL,
    Access,
    ApiEntry,
    AuthSpec,
    AwsSigV4Auth,
    BearerAuth,
    CredentialGuidance,
    EntryStatus,
    NoAuth,
    OAuth2UserAuth,
    Operation,
    Parameter,
    ReadBasis,
    looks_like_path,
)
from librarian.registry.store import Registry, RegistryError
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

EntryChange = Callable[[ApiEntry], ApiEntry]
"""A decision as a function of the entry: applied to the entry as the database has it
at that moment, so a decision made on another replica a moment earlier is kept."""

UPDATE_KIND = "source-update"  # the freshly learned entry a pending update review is about


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


class WriteNotEnabled(WriteRefused):
    """The operation is not enabled for writes (or the API has no write credential);
    the approvers have been asked to enable it. Retry once they have."""

    def __init__(self, request: EnableWriteRequest) -> None:
        missing = (
            "is not enabled for writes" if request.needs_enabling else "has no write credential yet"
        )
        more = f" (and {len(request.others)} more on this request)" if request.others else ""
        super().__init__(
            f"{request.api_name}: {request.operation} {missing}{more}; approvers have been "
            f"asked to enable it (request {request.id}). Retry once they have."
        )
        self.request = request


@dataclass
class Applied:
    """What happened to a decision. It is saved in the database, so it is in effect on
    every replica now; `recorded` says whether it is also being written to the external
    record (an approved API, with a record configured)."""

    recorded: bool = False

    @property
    def note(self) -> str:
        return "in effect now" + ("; being written to the record" if self.recorded else "")


@dataclass
class WriteOutcome:
    result: CallResult
    how: str  # "approved" | "grant" | "auto" | "idempotent"
    approval_id: str | None = None
    grant_id: str | None = None
    message: str = ""  # how it came to run, for the agent


ApprovalHook = Callable[[WriteApproval], Awaitable[None]]


@dataclass
class OnboardResult:
    entry: ApiEntry
    path: Path
    read_operations: int
    needs_review: int
    review: str | None = None  # the review request approvers were sent, when one opened


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

    recorded: bool = False  # its file is being removed from the external record
    cancelled: int = 0  # credential requests and write approvals denied
    observations: int = 0  # operations whose observations were dropped


@dataclass
class RecheckReport:
    api: str
    status: str  # unchanged | changed | skipped | error
    changes: list[Change] = field(default_factory=list[Change])
    actions: list[str] = field(default_factory=list[str])
    review: str | None = None  # the update review opened for a verified entry
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
        self.audit: AuditSink = audit or JsonlAuditSink(settings.audit_file)
        self.http = http_client or httpx.AsyncClient(
            timeout=settings.request_timeout_seconds, follow_redirects=False
        )
        self.engine = make_engine(settings.database_url) if settings.database_url else None
        self.state = StateStore(self.engine) if self.engine is not None else None
        if audit is None and self.state is not None:
            # The JSONL stream stays the system of record; the database copy feeds the console.
            self.audit = FanoutAuditSink([self.audit, DatabaseAuditSink(self.state)])
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
            shared_limits=self.state,
        )
        self.executor = Executor(
            self.policy,
            self.secrets,
            self.audit,
            client=self.http,
            timeout_seconds=settings.request_timeout_seconds,
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
        self.enablement = EnableWriteBroker(
            self.notifier,
            self.audit,
            public_url=settings.public_url,
            signing_key=pysecrets.token_bytes(32),
            default_channel=settings.notifier.slack.channel,
            default_approver_group=settings.notifier.slack.approver_group,
            store=self.state,
        )
        self.approvals = ApprovalBroker(
            self.notifier,
            self.audit,
            public_url=settings.public_url,
            signing_key=pysecrets.token_bytes(32),
            approver_mode=settings.writes.approvers,
            expire_after=timedelta(hours=settings.writes.approval_expire_hours),
            default_channel=settings.notifier.slack.channel,
            default_approver_group=settings.notifier.slack.approver_group,
            deciding_group=settings.approvers.group,
            max_grant_days=settings.writes.max_grant_days,
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
        self.recorder = Recorder(
            self.registry,
            self.audit,
            publisher=publisher if publisher is not None else _default_publisher(settings),
            store=self.state,
        )
        self._updates: dict[str, ApiEntry] = {}  # pending source updates, without a database
        self.recipes: dict[str, Recipe] = {}
        self._background: set[asyncio.Task[None]] = set()
        self._auto_uses: dict[tuple[str, str, int], int] = {}  # without a database
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

    @property
    def publisher(self) -> Publisher | None:
        """Where decisions are written down (the external record); None: nowhere."""
        return self.recorder.publisher

    @publisher.setter
    def publisher(self, value: Publisher | None) -> None:
        self.recorder.publisher = value

    def load(self) -> None:
        self.registry.load()

    async def start(self) -> None:
        """Load the registry and durable state; call once before serving."""
        self.load()
        if self.state is not None:
            await self.state.initialize()
            await self.registry.refresh_stored(force=True)
            # The database is the registry: files are only a way in.
            await self.registry.import_files()
            await self.observations.load()
            restored = await self.broker.restore()
            if restored:
                log.info("restored %d pending credential request(s)", restored)
            await self.approvals.restore()
            await self.approvals.close_interrupted()  # writes a stopped replica left open
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
        self.enablement.set_signing_key(key)
        await self.reviews.restore()
        await self.enablement.restore()
        if self.publisher is not None:
            self._spawn(self._close_old_proposals())

    def _spawn(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(work)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _close_old_proposals(self) -> None:
        """Before 0.20 an approval was a merged pull request. Those still open would
        suggest GitHub decides; it no longer does, so close them. What they asked for
        is in the console: learned APIs wait for approval there, and write requests
        that were enabled but never merged can be applied from the API's page."""
        publisher = self.publisher
        if publisher is None:
            return
        try:
            closed = await publisher.close_proposals(
                "librarian now approves in its own database (0.20): this pull request is "
                "no longer how the change is approved, so it is closed. Nothing is lost: "
                "the review continues in librarian's console, and this repository keeps "
                "receiving every approved decision as a commit."
            )
        except (PublishError, httpx.HTTPError, KeyError, ValueError) as exc:
            log.warning("could not close review pull requests from before 0.20: %s", exc)
            return
        if closed:
            log.info("closed %d review pull request(s) from before 0.20: %s", len(closed), closed)
            await self.audit.record(
                AuditEvent(
                    event="old_proposals_closed",
                    caller="librarian",
                    detail=f"{len(closed)} pull request(s)",
                    extra={"closed": closed},
                )
            )

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
        await self.recorder.drain()  # decisions still on their way to the record
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
            await self._audit_fetch(caller, "recipe", replayed)
            return replayed
        if self.planner is None:
            raise LibrarianError("the fetch skill is disabled: no model is configured")
        started = time.monotonic()
        for _attempt in range(2):
            try:
                outcome = await self.planner.fetch(
                    intent,
                    self.registry,
                    self.executor,
                    caller,
                    budget_seconds=self.settings.fetch_timeout_seconds,
                )
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
            if outcome.partial:
                log.warning(
                    "fetch for %s stopped after %d calls: the time budget (%s s) ran out",
                    caller.agent,
                    len(outcome.results),
                    self.settings.fetch_timeout_seconds,
                )
            else:
                await self._learn_recipe(intent, outcome)
            await self._audit_fetch(
                caller, "planner", outcome, duration_ms=int((time.monotonic() - started) * 1000)
            )
            return outcome
        raise LibrarianError("credential still not usable after it was provided")

    async def _audit_fetch(
        self,
        caller: Caller,
        how: str,
        outcome: FetchOutcome,
        duration_ms: int | None = None,
    ) -> None:
        """One event per plain-language request, saying whether a model was involved."""
        await self.audit.record(
            AuditEvent(
                event="fetch",
                caller=caller.agent,
                principal=caller.principal,
                detail=how,
                duration_ms=duration_ms,
                extra={
                    "calls": len(outcome.results),
                    "recipe": outcome.recipe,
                    "partial": outcome.partial,
                },
            )
        )

    async def prune_audit(self) -> int:
        """Drop database audit events older than `audit_retention_days` (the JSONL stream
        keeps everything). Returns how many went."""
        if self.state is None:
            return 0
        cutoff = datetime.now(UTC) - timedelta(days=self.settings.audit_retention_days)
        return await self.state.prune_audit(cutoff)

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
        if approval.state is ApprovalState.EXECUTED:
            when = f" at {approval.executed_at:%Y-%m-%d %H:%M} UTC" if approval.executed_at else ""
            status["result_as_of"] = (
                approval.executed_at.isoformat() if approval.executed_at else None
            )
            status["message"] = (
                f"The result is {approval.api}'s response when the write ran{when}; the "
                "resource may have changed since. Read it again for its current state."
            )
        return status

    async def _prior_write(
        self, idempotency_key: str, caller: Caller
    ) -> WriteOutcome | WriteApproval | None:
        """What this agent already did with this key: the earlier result (never sent
        again), or the approval still deciding or running."""
        prior = await self.approvals.find_by_key(caller, idempotency_key)
        if prior is None:
            return None
        deadline = time.monotonic() + 5
        while prior.state in (ApprovalState.APPROVED, ApprovalState.EXECUTING):
            # decided, running on some replica: its result is moments away
            if time.monotonic() > deadline:
                break
            await asyncio.sleep(0.2)
            prior = await self.approvals.load(prior.id) or prior
        if prior.state is ApprovalState.EXECUTED and prior.result is not None:
            when = f" at {prior.executed_at:%Y-%m-%d %H:%M} UTC" if prior.executed_at else ""
            return WriteOutcome(
                CallResult.model_validate(prior.result),
                "idempotent",
                prior.id,
                prior.grant_id,
                f"Not sent again: this agent already ran this write with idempotency key "
                f"{idempotency_key!r}{when}; this is that result.",
            )
        if prior.state is ApprovalState.PENDING:
            return prior
        if prior.state in (ApprovalState.APPROVED, ApprovalState.EXECUTING):
            # Still being sent (or its replica stopped mid-way and it has not been closed
            # yet): a second copy must not go out beside it.
            raise WriteRefused(
                f"a write with idempotency key {idempotency_key!r} is still being run "
                f"(approval {prior.id}); ask write_status for its outcome before retrying"
            )
        return None  # failed, denied or expired: a fresh request

    def _finish_in_background(self, approval: WriteApproval, caller: Caller) -> None:
        async def finish() -> None:
            decided = await self.approvals.wait(approval.id)
            try:
                await self._run_approved(decided, caller)
            except LibrarianError as exc:  # refused, or the API gave no answer
                log.info("detached write %s not run: %s", approval.id, exc)

        task = asyncio.create_task(finish())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _start_write(
        self, request: WriteRequest, caller: Caller
    ) -> WriteOutcome | WriteApproval:
        """Policy, idempotency and grants; opens an approval when a human must decide."""
        if request.idempotency_key:
            prior = await self._prior_write(request.idempotency_key, caller)
            if prior is not None:
                return prior
        if self.registry.get(request.api) is None:
            await self.registry.refresh_stored()  # learned on another replica?
        decision = self.policy.check_write(request, caller)
        if not decision.allowed or decision.entry is None or decision.operation is None:
            if decision.askable and decision.entry is not None and decision.operation is not None:
                raise await self._ask_to_enable(
                    decision.entry, decision.operation, request, caller, decision.reason
                )
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
        limited = await self.policy.rate_limited(caller, entry, write=True)
        if limited:
            await self.audit.record(
                AuditEvent(
                    event="write_denied",
                    caller=caller.agent,
                    principal=caller.principal,
                    api=request.api,
                    operation=request.operation,
                    detail=limited,
                )
            )
            telemetry.write_requests.add(1, {"librarian.api": request.api, "outcome": "denied"})
            raise PolicyDenied(limited)

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
            if self.settings.writes.announce_grants and grant.notify_owner:
                await self.notifier.grant_used(grant, notice)
            else:
                log.info(
                    "write by %s ran under grant %s: %s.%s (use %d%s)",
                    caller.agent,
                    grant.id,
                    entry.id,
                    op.id,
                    grant.uses,
                    f"/{grant.max_uses}" if grant.max_uses else "",
                )
            notice.grant_id = grant.id
            await self.approvals.record_executed(
                notice, result.model_dump(mode="json"), f"ran under grant {grant.id}"
            )
            lasts = (
                f"until {grant.expires_at:%Y-%m-%d}"
                if grant.expires_at is not None
                else "until it is revoked"
            )
            return WriteOutcome(
                result,
                "grant",
                notice.id,
                grant.id,
                f"Ran at once under a standing grant ({grant.id}, created by {grant.created_by}, "
                f"{lasts}): similar writes by this agent run without asking a person.",
            )

        if op.approval == "auto" and op.method != "DELETE":
            used = await self._auto_count(entry, op)
            if used <= op.auto_limit_per_day:
                result = await self.executor.execute_write(entry, op, request, caller)
                telemetry.write_requests.add(1, {"librarian.api": entry.id, "outcome": "auto"})
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
                await self.audit.record(
                    AuditEvent(
                        event="write_auto",
                        caller=caller.agent,
                        principal=caller.principal,
                        api=entry.id,
                        operation=op.id,
                        detail=f"auto-approved ({used} of {op.auto_limit_per_day} today)",
                    )
                )
                link = self.auto_link(entry.id, op.id)
                if self.settings.writes.announce_auto:
                    await self.notifier.auto_write_used(notice, used, op.auto_limit_per_day, link)
                else:
                    log.info(
                        "auto-approved write by %s: %s.%s (%d of %d today); require approval "
                        "again at %s",
                        caller.agent,
                        entry.id,
                        op.id,
                        used,
                        op.auto_limit_per_day,
                        link,
                    )
                await self.approvals.record_executed(
                    notice, result.model_dump(mode="json"), "approved automatically"
                )
                return WriteOutcome(
                    result,
                    "auto",
                    notice.id,
                    message=(
                        f"Approved automatically: an approver chose that for {entry.id}."
                        f"{op.id} ({used} of {op.auto_limit_per_day} today)."
                    ),
                )
            await self.audit.record(
                AuditEvent(
                    event="write_auto_capped",
                    caller=caller.agent,
                    principal=caller.principal,
                    api=entry.id,
                    operation=op.id,
                    detail=f"{op.auto_limit_per_day} automatic writes today; asking a person",
                )
            )

        problem = self.approvals.approval_route(
            caller.requesters(), link_holders=self.settings.approvers.mode == "token"
        )
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
        # From the claim on, this approval must reach an end whatever happens: left as
        # "executing", nobody could tell whether the write was made or may be retried.
        try:
            result = await self.executor.execute_write(
                current, current_op, approval.request, caller
            )
        except ApiUnavailable as gone:
            await self.approvals.finish(approval, False, None, str(gone))
            raise
        except (CredentialMissing, PolicyDenied) as exc:
            await self.approvals.finish(approval, False, None, str(exc))
            raise WriteRefused(f"approved write failed: {exc}") from exc
        except Exception as exc:
            note = (
                f"librarian failed while making this write ({_where(exc)}). It may have "
                f"reached {approval.api_name}: check before sending it again."
            )
            log.error("approved write %s: %s", approval.id, note)
            await self.approvals.finish(approval, False, None, note)
            raise WriteRefused(note) from None
        note = "done" if result.ok else (result.error or f"HTTP {result.status_code}")
        try:
            stored = result.model_dump(mode="json")
            if len(json.dumps(stored, default=str)) > 65_536:
                stored = {**stored, "data": None, "truncated": True}
            await self.approvals.finish(approval, result.ok, stored, note)
        except Exception as exc:
            # The write was made and answered; only keeping the answer failed.
            log.error(
                "approved write %s: its result could not be kept (%s)", approval.id, _where(exc)
            )
            kept = (
                f"{note}; {approval.api_name} answered HTTP {result.status_code}, but the "
                "answer could not be kept"
            )
            await self.approvals.finish(approval, result.ok, None, kept)
        telemetry.write_requests.add(1, {"librarian.api": approval.api, "outcome": "executed"})
        who = f" by {approval.decided_by}" if approval.decided_by else ""
        return WriteOutcome(
            result, "approved", approval.id, approval.grant_id, f"Approved{who} and run once."
        )

    def capabilities(
        self, query: str | None = None, caller: Caller = SYSTEM_CALLER
    ) -> list[dict[str, Any]]:
        entries = (
            [h.entry for h in self.registry.search(query, limit=10)]
            if query
            else [e for e in self.registry.all() if e.status is not EntryStatus.BLOCKED]
        )
        entries = [e for e in entries if caller.may_use(e.id)]

        def reads(entry: ApiEntry) -> list[Operation]:
            """The reads this caller can make there: its limits may leave some out."""
            limits = caller.limits_for(entry.id)
            return [
                op
                for op in entry.operations
                if op.access is Access.READ
                and not self.observations.suspended(entry, op)
                and (limits is None or limits.reaches(op))
            ]

        def shown(entry: ApiEntry) -> list[Operation]:
            return rank(entry, query, reads(entry), limit=20) if query else reads(entry)[:50]

        def limited(entry: ApiEntry) -> dict[str, Any]:
            limits = caller.limits_for(entry.id)
            return {"limits": limits.told()} if limits is not None else {}

        return [
            {
                "api": e.id,
                "name": e.name,
                "description": e.description[:300],
                "status": e.status.value,
                "base_url": e.base_url,
                "read_operations": [
                    {"operation": op.id, "summary": op.summary[:160]} for op in shown(e)
                ],
                "total_read_operations": len(reads(e)),
                "credential_ready": None if e.secret_ref is None else "unknown",
                "learned_from": e.source.kind,
                **limited(e),
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
                f"API {api_id!r} already exists ({existing.status}); an approver changes it "
                "in the console"
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
        await self.audit.record(
            AuditEvent(
                event="onboard",
                caller=caller.agent,
                principal=caller.principal,
                api=api_id,
                detail=f"{len(entry.operations)} operations ({reads} read, {review} need review)"
                f" from {entry.source.kind}",
                extra={"source": entry.source.url} | ({"internal": internal} if internal else {}),
            )
        )
        summary = (
            f"{len(entry.operations)} operations ({reads} reads, {review} need review) from "
            f"{_learned_from(entry)}"
        )
        opened: ReviewRequest | None = None
        try:
            # The approve-or-deny step: a card with a link to the review page.
            opened = await self.reviews.open(
                api=entry.id,
                api_name=entry.name,
                host=entry.host,
                learned_by=caller.principal or caller.agent,
                summary=summary,
                auth=_auth_words(entry),
                warnings=warnings + ([internal] if internal else []),
                needs_review=[op.id for op in entry.operations if op.access is Access.UNKNOWN],
                channel=entry.owners.channel,
                approver_group=entry.owners.approver_group,
            )
        except Exception as exc:  # a broken notifier must not undo a successful onboarding
            log.warning("could not announce the learned API %s: %s", api_id, exc)
            opened = self.reviews.pending_for(entry.id)
        return OnboardResult(entry, path, reads, review, opened.id if opened else None)

    async def approve_api(self, api_id: str, by: str) -> tuple[ApiEntry, Applied]:
        """An approver approves a learned API: it is verified from now on, on every
        replica. Operations whose access is unknown stay refused until decided."""
        await self.registry.refresh_stored()
        entry = self.registry.get(api_id)
        if entry is None:
            raise LibrarianError(f"unknown API {api_id!r}")
        if entry.status is EntryStatus.BLOCKED:
            raise LibrarianError(f"{api_id!r} is blocked; unblock it first")
        if entry.status is EntryStatus.VERIFIED:
            return entry, Applied(recorded=self.recorder.enabled)
        await self.reviews.refresh()  # the review may have been opened on another replica
        note = f"\nApproved by {by}."
        updated, applied = await self._decide(
            api_id,
            lambda base: base.model_copy(
                update={"status": EntryStatus.VERIFIED, "notes": (base.notes + note)[:8000]}
            ),
            f"approved ({entry.name})",
            f"{by} approved {entry.name}: {len(entry.operations)} operations learned from "
            f"{_learned_from(entry)}, authentication {_auth_words(entry)}.",
            by,
        )
        await self.audit.record(
            AuditEvent(
                event="api_approved",
                caller="console",
                principal=by,
                api=api_id,
                extra={"recorded": applied.recorded},
            )
        )
        pending = self.reviews.pending_for(api_id)
        if pending is not None and pending.kind == "learned":
            await self.reviews.decide(
                pending.id, ReviewState.APPROVED, by, "verified; " + applied.note
            )
        return updated, applied

    async def approve_review(
        self, review_id: str, by: str, decisions: dict[str, str]
    ) -> ReviewRequest:
        """Apply the person's decisions on operations, then the approval itself: a
        learned API becomes verified; an update to a verified API is applied. Both take
        effect at once, in the database."""
        review = await self.reviews.load(review_id)
        if review is None or review.state is not ReviewState.PENDING:
            raise LookupError(f"review {review_id!r} is not pending")
        if review.kind == "update":
            await self._apply_update(review, by)
            note = "the update was applied"
        else:
            note = "verified"
        for op_id, choice in decisions.items():
            if op_id in review.needs_review:
                await self.set_operation_access(review.api, op_id, choice, by)
        if review.kind == "update":
            return await self.reviews.decide(review_id, ReviewState.APPROVED, by, note)
        _, applied = await self.approve_api(review.api, by)  # closes the review too
        done = await self.reviews.load(review_id)
        if done is not None and done.state is ReviewState.PENDING:  # e.g. already verified
            done = await self.reviews.decide(
                review_id, ReviewState.APPROVED, by, f"{note}; {applied.note}"
            )
        return done or review

    async def deny_review(self, review_id: str, by: str) -> ReviewRequest:
        """A person said no. A learned API is dropped; for an update, the entry stays
        as it is (operations suspended meanwhile stay suspended until someone clears
        them in the console)."""
        review = await self.reviews.load(review_id)
        if review is None or review.state is not ReviewState.PENDING:
            raise LookupError(f"review {review_id!r} is not pending")
        entry = self.registry.get(review.api)
        note = "denied"
        if review.kind == "update":
            await self._drop_update(review.api)
            note = "the entry stays as it is"
        elif entry is not None and entry.status is EntryStatus.PROVISIONAL:
            await self.forget(review.api, by, close_review=False)
            note = "the API was forgotten"
        return await self.reviews.decide(review_id, ReviewState.DENIED, by, note)

    # -- a verified API changed at its source: the update waits for a person --------------

    async def _save_update(self, api_id: str, fresh: ApiEntry) -> None:
        self._updates[api_id] = fresh
        if self.state is not None:
            await self.state.save_doc(
                UPDATE_KIND, api_id, "pending", fresh.model_dump_json(by_alias=True)
            )

    async def _load_update(self, api_id: str) -> ApiEntry | None:
        if self.state is not None:
            data = await self.state.get_doc(UPDATE_KIND, api_id)
            return None if data is None or data == "{}" else ApiEntry.model_validate_json(data)
        return self._updates.get(api_id)

    async def _drop_update(self, api_id: str) -> None:
        self._updates.pop(api_id, None)
        if self.state is not None and await self.state.get_doc(UPDATE_KIND, api_id) is not None:
            await self.state.save_doc(UPDATE_KIND, api_id, "done", "{}")

    async def pending_update(self, api_id: str) -> tuple[ApiEntry, list[Change]] | None:
        """What approving the API's pending update would make of the entry, and the
        changes it carries, worked out against the entry as it is now."""
        entry = self.registry.get(api_id)
        fresh = await self._load_update(api_id)
        if entry is None or fresh is None:
            return None
        changes = diff(entry, fresh)
        return proposed_update(entry, fresh, changes), changes

    async def _apply_update(self, review: ReviewRequest, by: str) -> None:
        await self.registry.refresh_stored()
        fresh = await self._load_update(review.api)
        if fresh is None:
            raise LibrarianError("this update is no longer there; re-check the API's source")
        count = 0

        def change(base: ApiEntry) -> ApiEntry:
            # Against the entry as it is now, so decisions made since are kept.
            nonlocal count
            changes = diff(base, fresh)
            count = len(changes)
            updated = proposed_update(base, fresh, changes)
            note = f"\n{by} approved an update from the API's source ({count} change(s))."
            return updated.model_copy(update={"notes": (updated.notes + note)[:8000]})

        _, applied = await self._decide(
            review.api,
            change,
            "updated from its source",
            f"{by} approved the update librarian found at the API's source:\n"
            + "\n".join(f"- {c}" for c in review.changes[:50]),
            by,
        )
        await self._drop_update(review.api)
        await self.audit.record(
            AuditEvent(
                event="api_updated",
                caller="console",
                principal=by,
                api=review.api,
                detail=f"{count} change(s) applied",
                extra={"recorded": applied.recorded},
            )
        )

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

    async def set_auth_spec(
        self, api_id: str, which: str, spec: str, by: str
    ) -> tuple[ApiEntry, Applied]:
        """A person described the authentication of an API in full (the console), as a
        registry entry would: for the kinds the simple choice does not cover (tokens made
        per call, per-person sign-in). `which` is `auth` (reads) or `write_auth`; an
        empty `write_auth` removes the write credential. In effect at once.

        A secret may only be referred to under the API's own name (or where the entry
        already refers to): through this form nobody can point one API at another's key.
        """
        entry = self.registry.get(api_id)
        if entry is None:
            raise LibrarianError(f"unknown API {api_id!r}")
        if entry.status is EntryStatus.BLOCKED:
            raise LibrarianError(f"{api_id!r} is blocked; unblock it first")
        if which not in ("auth", "write_auth"):
            raise LibrarianError("choose reads or writes")
        try:
            described: Any = yaml.safe_load(spec) if spec.strip() else None
        except yaml.YAMLError as exc:
            problem = getattr(exc, "problem", None) or "it does not parse"
            raise LibrarianError(f"that is not valid YAML: {problem}") from None
        if described is None and which == "auth":
            described = {"type": "none"}
        if described is not None and not isinstance(described, dict):
            raise LibrarianError("describe the authentication as `key: value` lines")
        data = entry.model_dump(mode="json", by_alias=True)
        data[which] = described
        try:
            chosen = ApiEntry.model_validate(data)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(part) for part in first["loc"] if part != which)
            raise LibrarianError(
                f"that authentication is not valid: {where + ': ' if where else ''}{first['msg']}"
            ) from None
        new = chosen.auth if which == "auth" else chosen.write_auth
        allowed = {
            ref
            for auth in (entry.auth, entry.write_auth)
            for name in ("secret_ref", "client_secret_ref")
            if isinstance(ref := getattr(auth, name, None), str)
        }
        for name in ("secret_ref", "client_secret_ref"):
            ref = getattr(new, name, None)
            if isinstance(ref, str) and ref not in allowed and not ref.startswith(f"{api_id}/"):
                raise LibrarianError(
                    f"{name} {ref!r} is not under this API's own name: use `{api_id}/...` "
                    "(a secret kept elsewhere can be named in a registry file)"
                )
        kind = "none" if new is None else new.type
        what = "reads" if which == "auth" else "writes"
        updated, applied = await self._decide(
            api_id,
            lambda base: base.model_copy(update={which: new}),
            f"authentication for {what}: {kind}",
            f"{by} set the authentication for {what} to {kind} in the console.",
            by,
        )
        self._auth_notified.discard(api_id)
        await self.audit.record(
            AuditEvent(
                event="auth_set",
                caller="console",
                principal=by,
                api=api_id,
                detail=f"{what}: {kind}",
                extra={"recorded": applied.recorded},
            )
        )
        return updated, applied

    async def set_auth(
        self, api_id: str, scheme: str, key_name: str | None, by: str
    ) -> tuple[ApiEntry, Applied]:
        """A person chose the authentication for an API (the console): in effect at
        once. Returns the entry and what happened to the decision."""
        entry = self.registry.get(api_id)
        if entry is None:
            raise LibrarianError(f"unknown API {api_id!r}")
        if entry.status is EntryStatus.BLOCKED:
            raise LibrarianError(f"{api_id!r} is blocked; unblock it first")
        extracted = ExtractedApi.model_validate({"auth": scheme, "auth_key_name": key_name})
        if scheme == "none":
            chosen = entry.model_copy(update={"auth": NoAuth(), "credential": CredentialGuidance()})
        else:
            auth = auth_from_extracted(extracted, f"{api_id}/readonly")
            if isinstance(auth, NoAuth):
                raise LibrarianError("that authentication needs a valid header or query name")
            chosen = self._with_auth(entry, auth, f"chosen by {by}")
        updated, applied = await self._decide(
            api_id,
            lambda base: base.model_copy(
                update={
                    "auth": chosen.auth,
                    "credential": chosen.credential,
                    "notes": chosen.notes,
                }
            ),
            f"authentication {scheme}",
            f"{by} set the authentication to {scheme} in the console.",
            by,
        )
        self._auth_notified.discard(api_id)
        await self.audit.record(
            AuditEvent(
                event="auth_set",
                caller="console",
                principal=by,
                api=api_id,
                detail=scheme,
                extra={"recorded": applied.recorded},
            )
        )
        return updated, applied

    async def _decide(
        self, api_id: str, change: EntryChange, title: str, body: str, by: str
    ) -> tuple[ApiEntry, Applied]:
        """Apply a person's decision to an entry. It is saved to the database, which
        puts it in effect on every replica at once; for an approved API it is then
        written to the external record, in the background."""
        await self.registry.refresh_stored()  # decisions made on other replicas
        entry = self.registry.get(api_id)
        if entry is None:
            raise LibrarianError(f"unknown API {api_id!r}")
        await self.registry.save(change(entry))
        saved = self.registry.get(api_id) or entry
        return saved, await self._record(api_id, title, body, by)

    async def _record(self, api_id: str, title: str, body: str, by: str) -> Applied:
        """Write a decision about an approved API to the external record (when one is
        configured). An API nobody approved yet is not written down."""
        if not self.recorder.enabled or not self.registry.approved(api_id):
            return Applied()
        message = f"{api_id}: {title}\n\n{body}\n\nDecided by {by} in librarian."
        return Applied(recorded=await self.recorder.note(api_id, message))

    async def record_now(self, api_id: str, by: str) -> RecordStatus | None:
        """Write the API's entry to the record now (the console, after a failed write)."""
        if not self.recorder.enabled:
            raise LibrarianError("no external record is configured (publish.type)")
        status = await self.recorder.write_now(api_id)
        await self.audit.record(
            AuditEvent(
                event="record_requested",
                caller="console",
                principal=by,
                api=api_id,
                detail=(status.error or status.url or "") if status else "",
            )
        )
        return status

    async def restore_from_record(self, by: str, *, replace: bool = False) -> dict[str, Any]:
        """Bring the entries in the external record into the database: after losing
        the database, or when moving to a new one. APIs the database already has are
        left alone unless `replace`. The record is read only here, on request."""
        publisher = self.publisher
        if publisher is None:
            raise LibrarianError("no external record is configured (publish.type)")
        try:
            entries, errors = await publisher.read_all()
        except (PublishError, httpx.HTTPError, KeyError, ValueError) as exc:
            raise LibrarianError(f"could not read the record: {exc}") from exc
        await self.registry.refresh_stored()
        restored: list[str] = []
        kept: list[str] = []
        for entry in entries:
            if self.registry.get(entry.id) is not None and not replace:
                kept.append(entry.id)
                continue
            await self.registry.save(entry)
            restored.append(entry.id)
        await self.audit.record(
            AuditEvent(
                event="registry_restored",
                caller="cli",
                principal=by,
                detail=f"{len(restored)} restored, {len(kept)} already there",
                extra={"restored": restored, "kept": kept, "unreadable": errors},
            )
        )
        return {"restored": restored, "kept": kept, "unreadable": errors}

    # -- enabling writes --------------------------------------------------------

    async def _ask_to_enable(
        self,
        entry: ApiEntry,
        op: Operation,
        request: WriteRequest,
        caller: Caller,
        reason: str,
    ) -> Exception:
        """A write the policy refused because nobody has enabled it yet: ask the
        approvers (once per operation) instead of denying silently. Returns the error
        to raise: the request, or the refusal when approvers declined recently."""
        declined = self.enablement.declined_for(entry.id, op.id)
        if declined is not None:
            until = declined.expires_at.date().isoformat()
            await self.audit.record(
                AuditEvent(
                    event="write_denied",
                    caller=caller.agent,
                    principal=caller.principal,
                    api=entry.id,
                    operation=op.id,
                    detail=f"{reason}; approvers declined to enable it until {until}",
                )
            )
            telemetry.write_requests.add(1, {"librarian.api": entry.id, "outcome": "denied"})
            return PolicyDenied(
                f"operation {op.id!r} is not enabled for writes: approvers declined to enable "
                f"it ({declined.note or 'no note'}); it can be asked again after {until}"
            )
        who = caller.agent + (f" (for {caller.principal})" if caller.principal else "")
        ops = [op]
        for other in request.also_needs:
            extra = entry.operation(other)
            if (
                extra is not None
                and extra.id != op.id
                and extra not in ops
                and extra.access is not Access.READ
                and (not extra.writable or entry.write_auth is None)
                and self.enablement.declined_for(entry.id, extra.id) is None
            ):
                ops.append(extra)
        asked = await self.enablement.open(entry, ops, who=who, reason=request.reason)
        await self.audit.record(
            AuditEvent(
                event="enable_write_requested",
                caller=caller.agent,
                principal=caller.principal,
                api=entry.id,
                operation=op.id,
                detail=reason,
                extra={"request": asked.id, "asks": asked.asks, "operations": asked.operations},
            )
        )
        telemetry.write_requests.add(1, {"librarian.api": entry.id, "outcome": "asked"})
        return WriteNotEnabled(asked)

    async def enable_writes(
        self,
        request_id: str,
        by: str,
        key: str = "dedicated",
        operations: list[str] | None = None,
        approval: str = "each",
        cap: int = 100,
    ) -> EnableWriteRequest:
        """An approver enables the operations an agent asked to write: in effect at
        once (each write still needs approval, a grant, or automatic approval).
        `operations` narrows it to some of the request's operations; the rest stay
        disabled. When the entry has no write credential, `key` says where it comes
        from: `dedicated` asks for a new `<api>/write` credential through the secure
        page; `read` uses the read key for writes too."""
        request = await self.enablement.load(request_id)
        if request is None or request.state is not EnableState.PENDING:
            raise LookupError("this request is no longer pending")
        entry = self.registry.get(request.api)
        chosen = [
            op
            for name in request.operations
            if (operations is None or name in operations)
            and entry is not None
            and (op := entry.operation(name)) is not None
        ]
        if entry is None or not chosen:
            if entry is None or not any(entry.operation(o) for o in request.operations):
                await self.enablement.decide(
                    request.id, EnableState.EXPIRED, by, "the operations are no longer there"
                )
                raise LibrarianError(f"{request.api!r} no longer has these operations")
            raise LibrarianError("tick at least one operation to enable")
        asked_by = ", ".join(request.requested_by) or by
        listed = ", ".join(f"`{op.id}` ({op.method} {op.path})" for op in chosen)
        lines = [f"{by} enabled writes for {listed}, asked by {asked_by}."]
        chosen_ids = {op.id for op in chosen}
        left = [o for o in request.operations if o not in chosen_ids]
        if left:
            lines.append("Left disabled: " + ", ".join(f"`{o}`" for o in left) + ".")
        if request.reasons:
            lines.append("Reason given: " + " / ".join(request.reasons))
        key_request: CredentialRequest | None = None
        write_auth: AuthSpec | None = None
        if entry.write_auth is None:
            if isinstance(entry.auth, OAuth2UserAuth):
                write_auth = entry.auth
                lines.append("Writes use each person's own sign-in, like reads.")
            elif isinstance(entry.auth, MADE_PER_CALL):
                # Nothing to ask a person for: what a write is sent with is made for it.
                write_auth = entry.auth
                lines.append(
                    f"Writes are authenticated like reads ({entry.auth.type}): the "
                    "credential is made for each write. Give writes their own scopes, "
                    "permissions or role under Authentication in the console if they need them."
                )
            elif key == "read" and entry.secret_ref is not None:
                write_auth = entry.auth
                lines.append(
                    f"Writes use the read credential `{entry.secret_ref}` too (chosen by {by})."
                )
            else:
                write_auth = _write_auth_like(entry.auth, f"{entry.id}/write")
                guidance = await self._write_guidance(entry, chosen)
                key_request = await self.broker.open_write(
                    entry,
                    write_auth,
                    by=by,
                    reason="writes enabled for " + ", ".join(o.id for o in chosen),
                    scopes=guidance.scopes,
                    create_url=guidance.create_url,
                    instructions=guidance.instructions,
                )
                lines.append(
                    f"Writes use a dedicated credential `{entry.id}/write`; "
                    f"{by} was asked to add it."
                )
        auto_ops: set[str] = set()
        if approval == "auto":
            auto_ops = {op.id for op in chosen if op.method != "DELETE"}
            skipped = [op.id for op in chosen if op.method == "DELETE"]
            lines.append(
                f"Writes to {', '.join(f'`{o}`' for o in sorted(auto_ops))} are approved "
                f"automatically, up to {cap} a day (chosen by {by})."
                + (
                    f" DELETE operations always need approval: {', '.join(skipped)}."
                    if skipped
                    else ""
                )
            )
        note = "\n" + " ".join(lines)
        named = ", ".join(o.id for o in chosen[:3]) + (
            f" and {len(chosen) - 3} more" if len(chosen) > 3 else ""
        )
        _, applied = await self._decide(
            entry.id,
            _enable_change(chosen_ids, write_auth, note, auto_ops, cap),
            f"enable writes for {named}",
            " ".join(lines),
            by,
        )
        outcome = applied.note
        if key_request is not None:
            outcome += "; a write credential was asked for"
        return await self.enablement.decide(
            request.id,
            EnableState.ENABLED,
            by,
            outcome,
            key_request=key_request.id if key_request else None,
            enabled=[o.id for o in chosen],
        )

    async def enable_operation(
        self,
        api_id: str,
        op_id: str,
        by: str,
        key: str = "dedicated",
        approval: str = "each",
        cap: int = 100,
    ) -> EnableWriteRequest:
        """Enable writes for an operation from the console, without waiting for an agent
        to ask. Joins the pending request for it when there is one."""
        entry = self.registry.get(api_id)
        op = entry.operation(op_id) if entry is not None else None
        if entry is None or op is None:
            raise LibrarianError(f"unknown operation {op_id!r} on {api_id!r}")
        if op.access is Access.READ:
            raise LibrarianError(f"{op_id!r} is a read operation")
        if op.writable and entry.write_auth is not None:
            raise LibrarianError(f"{op_id!r} is already enabled for writes")
        request = self.enablement.pending_for(api_id, op_id)
        if request is None:
            request = await self.enablement.open(
                entry, [op], who=by, reason="enabled from the console", notify=False, join=False
            )
        return await self.enable_writes(request.id, by, key, approval=approval, cap=cap)

    async def enable_status(self, request_id: str, caller: Caller) -> dict[str, Any]:
        """Where an enable-write request stands, for the agent that asked: pending,
        enabled (decided, but waiting for the write credential), live (in effect: writes
        go through normal approval), declined until a date, or expired."""
        request = await self.enablement.load(request_id)
        mine = request is not None and any(
            who == caller.agent or who.startswith(f"{caller.agent} (")
            for who in request.requested_by
        )
        if request is None or not mine:
            raise LibrarianError(f"no enable request {request_id!r} for this agent")
        entry = self.registry.get(request.api)
        live = [
            name
            for name in request.operations
            if entry is not None
            and entry.write_auth is not None
            and (op := entry.operation(name)) is not None
            and op.writable
        ]
        status: dict[str, Any] = {
            "enable_request": request.id,
            "api": request.api,
            "operations": request.operations,
            "live_operations": live,
            "decided_by": request.decided_by,
            "note": request.note,
        }
        if request.state is EnableState.PENDING:
            status["state"] = "pending"
            status["message"] = "approvers have been asked; retry once it is enabled"
        elif request.state is EnableState.ENABLED:
            if set(live) >= set(request.enabled or request.operations):
                status["state"] = "live"
                status["message"] = "enabled and in effect; writes go through normal approval"
            else:
                waits: list[str] = []
                if request.review_url:
                    # Decided before 0.20, in a pull request that was never merged.
                    waits.append("an approver to apply it in the console")
                key = self.broker.get(request.key_request) if request.key_request else None
                if key is not None and key.state is RequestState.PENDING:
                    waits.append("the write credential to be added")
                status["state"] = "enabled"
                status["message"] = f"enabled by {request.decided_by}; waiting for " + (
                    " and ".join(waits) or "the write credential"
                )
        elif request.state is EnableState.DECLINED:
            status["state"] = "declined"
            status["until"] = request.expires_at.isoformat()
            status["message"] = (
                f"declined by {request.decided_by}: {request.note}; "
                f"it can be asked again after {request.expires_at.date().isoformat()}"
            )
        else:
            status["state"] = "expired"
            status["message"] = "nobody decided in time; asking again opens a new request"
        return status

    async def unapplied_enables(self, entry: ApiEntry) -> list[EnableWriteRequest]:
        """Enable-write requests an approver said yes to before 0.20 that never took
        effect: back then the decision went into a pull request, and that one was not
        merged. They can be applied from the console."""

        def live(request: EnableWriteRequest) -> bool:
            wanted = request.enabled or request.operations
            return entry.write_auth is not None and all(
                (op := entry.operation(name)) is not None and op.writable for name in wanted
            )

        return [
            r for r in await self.enablement.enabled_for(entry.id) if r.review_url and not live(r)
        ]

    async def reapply_decisions(
        self, api_id: str, by: str
    ) -> tuple[list[EnableWriteRequest], Applied]:
        """Put write requests that were enabled before 0.20 but never merged into
        effect. Returns the requests applied and what happened to the decision."""
        entry = self.registry.get(api_id)
        if entry is None:
            raise LibrarianError(f"unknown API {api_id!r}")
        stale = await self.unapplied_enables(entry)
        if not stale:
            raise LibrarianError("every enabled write request is already in effect")
        wanted: set[str] = set()
        write_auth: AuthSpec | None = None
        for request in stale:
            wanted |= set(request.enabled or request.operations)
            if request.key_request:
                key = await self.broker.load(request.key_request)
                if key is not None:
                    write_auth = _write_auth_like(entry.auth, key.secret_ref)
        if entry.write_auth is None and write_auth is None:
            # The approver chose the read credential (or the API has none / per-person).
            write_auth = (
                BearerAuth(secret_ref=f"{entry.id}/write")
                if isinstance(entry.auth, NoAuth)
                else entry.auth
            )
        names = sorted(wanted)
        note = (
            f"\n{by} applied write requests enabled earlier: "
            + ", ".join(f"`{n}`" for n in names)
            + "."
        )
        named = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
        _, applied = await self._decide(
            api_id,
            _enable_change(wanted, write_auth, note),
            f"enable writes for {named}",
            f"{by} applied {len(stale)} write request(s) that were enabled before librarian "
            f"0.20 but never merged: {', '.join(names)}.",
            by,
        )
        for request in stale:
            await self.enablement.mark_applied(request, applied.note)
        await self.audit.record(
            AuditEvent(
                event="decisions_reapplied",
                caller="console",
                principal=by,
                api=api_id,
                detail=", ".join(names)[:2000],
                extra={"recorded": applied.recorded},
            )
        )
        return stale, applied

    async def decline_writes(self, request_id: str, by: str, note: str = "") -> EnableWriteRequest:
        """An approver declines: the operation stays refused and the same agents are
        not asked about it again for a while."""
        return await self.enablement.decide(
            request_id, EnableState.DECLINED, by, note.strip()[:500] or "declined by approver"
        )

    async def _write_guidance(self, entry: ApiEntry, ops: list[Operation]) -> CredentialGuidance:
        """What the person adding the write key should create: the least permission for
        these operations, from the model when one is configured."""
        listed = [
            f"{op.method} {op.path}" + (f": {op.summary[:120]}" if op.summary else "") for op in ops
        ]
        fallback = CredentialGuidance(
            create_url=entry.credential.create_url,
            instructions=(
                "Create a credential that may only " + "; ".join(listed[:5]) + ". Nothing more."
            )[:4000],
        )
        if self.extractor is None:
            return fallback
        try:
            extracted = await extract_write_guidance(
                self.extractor, base_url=entry.base_url, api_name=entry.name, operations=listed
            )
        except Exception as exc:  # the model is a convenience here, never a blocker
            log.warning("write credential guidance for %s failed: %s", entry.id, exc)
            return fallback
        url = extracted.credential_url
        return CredentialGuidance(
            create_url=url[:500] if url and url.startswith("https://") else fallback.create_url,
            scopes=[s[:100] for s in extracted.scopes[:20]],
            instructions=(extracted.credential_instructions or fallback.instructions)[:4000],
        )

    def slash_candidates(self, entry: ApiEntry) -> list[tuple[Operation, Parameter]]:
        """Path parameters that look like file paths or refs but do not allow "/" yet."""
        return [
            (op, p)
            for op in entry.operations
            for p in op.path_params
            if not p.allow_slash and looks_like_path(p.name, p.description)
        ]

    async def allow_slashes(self, api_id: str, by: str) -> tuple[ApiEntry, Applied, list[str]]:
        """A person allows "/" in the path parameters that look like paths or refs (the
        console): in effect at once. Returns the entry, what happened to the decision
        and the `operation.parameter` names changed."""
        entry = self.registry.get(api_id)
        if entry is None:
            raise LibrarianError(f"unknown API {api_id!r}")
        candidates = self.slash_candidates(entry)
        if not candidates:
            raise LibrarianError("no path parameter looks like a file path or a ref")
        wanted = {(op.id, p.name) for op, p in candidates}
        names = [f"{op.id}.{p.name}" for op, p in candidates]
        note = f"\n{by} allowed '/' in path arguments that are paths or refs: {', '.join(names)}."

        def change(base: ApiEntry) -> ApiEntry:
            operations = [
                op.model_copy(
                    update={
                        "parameters": [
                            p.model_copy(update={"allow_slash": True})
                            if (op.id, p.name) in wanted
                            else p
                            for p in op.parameters
                        ]
                    }
                )
                for op in base.operations
            ]
            return base.model_copy(
                update={"operations": operations, "notes": (base.notes + note)[:8000]}
            )

        updated, applied = await self._decide(
            api_id,
            change,
            f"allow slashes in {len(names)} path argument(s)",
            f"{by} allowed '/' in path arguments that are file paths or refs (segments are "
            f"still checked): {', '.join(names)}.",
            by,
        )
        await self.audit.record(
            AuditEvent(
                event="slashes_allowed",
                caller="console",
                principal=by,
                api=api_id,
                detail=", ".join(names)[:2000],
                extra={"recorded": applied.recorded},
            )
        )
        return updated, applied, names

    async def _auto_count(self, entry: ApiEntry, op: Operation) -> int:
        """Count one automatic write today for this operation, across replicas."""
        day = int(time.time() // 86400)
        if self.state is not None:
            return await self.state.count_hit(f"auto:{entry.id}:{op.id}"[:200], day)
        key = (entry.id, op.id, day)
        self._auto_uses[key] = self._auto_uses.get(key, 0) + 1
        return self._auto_uses[key]

    def auto_link(self, api_id: str, op_id: str) -> str:
        """The page where an approver puts per-write approval back with one click."""
        token = self.approvals.page_token("auto", f"{api_id}/{op_id}")
        return f"{self.settings.public_url.rstrip('/')}/auto/{api_id}/{op_id}?t={token}"

    async def set_operation_access(
        self, api_id: str, op_id: str, choice: str, by: str, cap: int | None = None
    ) -> tuple[ApiEntry, Applied]:
        """A person decided what an operation is: `read` (safe to call), `write` (needs
        approval each time), `auto` (writable, approved automatically up to `cap` a day)
        or `exclude` (agents may not call it). In effect at once."""
        entry = self.registry.get(api_id)
        if entry is None:
            raise LibrarianError(f"unknown API {api_id!r}")
        op = entry.operation(op_id)
        if op is None:
            raise LibrarianError(f"{api_id!r} has no operation {op_id!r}")
        if choice == "read":
            fields: dict[str, Any] = {
                "access": Access.READ,
                "read_basis": ReadBasis.REVIEWED,
                "writable": False,
                "approval": "each",
            }
        elif choice == "write":
            fields = {"access": Access.WRITE, "writable": True, "approval": "each"}
        elif choice == "auto":
            if op.method == "DELETE":
                raise LibrarianError("DELETE writes are never approved automatically")
            fields = {"access": Access.WRITE, "writable": True, "approval": "auto"}
            if cap is not None:
                fields["auto_limit_per_day"] = max(1, min(cap, 100_000))
        elif choice == "exclude":
            fields = {"access": Access.WRITE, "writable": False, "approval": "each"}
        else:
            raise LibrarianError("the choice is read, write, auto or exclude")
        what = {
            "read": "read",
            "write": "write (approved each time)",
            "auto": f"write, approved automatically (up to {cap or op.auto_limit_per_day}/day)",
            "exclude": "exclude",
        }[choice]
        note = f"\n{by} marked `{op_id}` as {what} in the console."

        def change(base: ApiEntry) -> ApiEntry:
            operations = [
                o.model_copy(update=fields) if o.id == op_id else o for o in base.operations
            ]
            return base.model_copy(
                update={"operations": operations, "notes": (base.notes + note)[:8000]}
            )

        updated, applied = await self._decide(
            api_id,
            change,
            f"{op_id} is {choice}",
            f"{by} decided in the console that `{op_id}` is {what}.",
            by,
        )
        await self.audit.record(
            AuditEvent(
                event="operation_access_set",
                caller="console",
                principal=by,
                api=api_id,
                operation=op_id,
                detail=choice,
                extra={"recorded": applied.recorded},
            )
        )
        return updated, applied

    async def _settle_auth_after_401(self, entry: ApiEntry, status: int) -> ApiEntry | None:
        """An API whose entry says "no authentication" answered 401. Work the scheme out
        and, for a provisional entry, apply it so the credential flow can ask for a key.
        A verified entry is a person's decision: they are told once, with what librarian
        thinks it is, and choose in the console."""
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
        details = [
            f"{entry.name} answered HTTP {status} (not authenticated), but its entry "
            "declares no authentication."
        ]
        if inferred is not None:
            details.append(f"librarian thinks it uses {inferred.auth.type}.")
        details.append("Choose its authentication in the console (APIs → this API).")
        await self.notifier.api_changed(
            entry.id,
            "Needs a credential",
            details,
            f"{self.settings.public_url.rstrip('/')}/apis/{entry.id}",
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
        """Stop agents using an API until someone unblocks it."""
        try:
            entry = await self.registry.block(api_id)
        except RegistryError as exc:
            raise LibrarianError(str(exc)) from exc
        applied = await self._record(
            api_id, "blocked", f"{by} blocked {entry.name}: agents are refused.", by
        )
        await self.audit.record(
            AuditEvent(
                event="api_blocked",
                caller="console",
                principal=by,
                api=api_id,
                extra={"recorded": applied.recorded},
            )
        )
        return entry

    async def unblock(self, api_id: str, by: str) -> ApiEntry:
        """Back to the status it had before it was blocked."""
        try:
            entry = await self.registry.unblock(api_id)
        except RegistryError as exc:
            raise LibrarianError(str(exc)) from exc
        applied = await self._record(
            api_id, "unblocked", f"{by} unblocked {entry.name}: {entry.status.value} again.", by
        )
        await self.audit.record(
            AuditEvent(
                event="api_unblocked",
                caller="console",
                principal=by,
                api=api_id,
                detail=f"now {entry.status.value}",
                extra={"recorded": applied.recorded},
            )
        )
        return entry

    async def forget(self, api_id: str, by: str, *, close_review: bool = True) -> ForgetReport:
        """Drop an API altogether: the entry, what was observed about it, anything
        waiting on a person for it, and its file in the external record. The next agent
        that needs it teaches it again."""
        report = ForgetReport()
        await self.reviews.refresh()
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
        was_approved = self.registry.approved(api_id)
        await self.registry.forget(api_id)
        await self._drop_update(api_id)
        report.observations = await self.observations.forget(api_id)
        if was_approved or await self.recorder.status(api_id) is not None:
            report.recorded = await self.recorder.note(
                api_id, f"{api_id}: forgotten\n\n{by} forgot this API in librarian."
            )
        await self.audit.record(
            AuditEvent(
                event="api_forgotten",
                caller="console",
                principal=by,
                api=api_id,
                extra={
                    "recorded": report.recorded,
                    "cancelled": report.cancelled,
                    "observations": report.observations,
                },
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
        actions, review = await self._apply_changes(entry, fresh, changes)
        if review is None:
            # No review card went out (a provisional entry, or it could not be sent).
            await self._notify_change(
                entry,
                f"{entry.name} changed ({worst(changes)})",
                [f"[{c.severity}] {c.detail}" for c in changes] + actions,
                f"{self.settings.public_url.rstrip('/')}/apis/{entry.id}",
                urgent=worst(changes) != "additive",
            )
        await self.audit.record(
            AuditEvent(
                event="api_changed",
                api=api_id,
                detail=f"{len(changes)} change(s), worst {worst(changes)}",
                extra={"review": review, "actions": actions},
            )
        )
        return RecheckReport(api_id, "changed", changes, actions, review)

    async def _apply_changes(
        self, entry: ApiEntry, fresh: ApiEntry, changes: list[Change]
    ) -> tuple[list[str], str | None]:
        """Handle a change at an API's source. Returns what was done at once and the
        id of the review opened for an approver, if any."""
        actions: list[str] = []
        if entry.status is EntryStatus.PROVISIONAL:
            # Nobody approved it yet: take the fresh copy (keeping owners and auth). The
            # review still open for it shows the entry as it is now.
            updated = _keep_human_fields(entry, fresh)
            await self.registry.save(updated)
            actions.append("the provisional entry was updated to match")
            return actions, None

        # An approved entry changes only when a person approves the update; until
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
        await self._save_update(entry.id, fresh)
        proposal = proposed_update(entry, fresh, changes)
        ranked = sorted(changes, key=lambda c: c.severity != "safety")
        try:
            review = await self.reviews.open(
                kind="update",
                api=entry.id,
                api_name=entry.name,
                host=entry.host,
                learned_by="librarian (re-check of the source)",
                summary=f"{len(changes)} change(s) at {entry.source.url or 'its source'}; "
                f"the worst is {worst(changes)}",
                auth=_auth_words(entry),
                warnings=actions,
                changes=[f"[{c.severity}] {c.detail}" for c in ranked][:200],
                needs_review=[op.id for op in proposal.operations if op.access is Access.UNKNOWN],
                channel=entry.owners.channel,
                approver_group=entry.owners.approver_group,
            )
        except Exception as exc:  # the update is saved; the console still offers it
            log.warning("could not announce the update to %s: %s", entry.id, exc)
            pending = self.reviews.pending_for(entry.id)
            return actions, pending.id if pending else None
        return actions, review.id

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

    async def _validate_credential(self, entry: ApiEntry, request: CredentialRequest) -> str | None:
        op_id = entry.credential.validation_operation
        if op_id is None:
            return None
        auth: AuthSpec | None = None
        if request.purpose == "write":
            # The write key is tested with the same read; it is not the entry's own
            # credential, so the call is sent with the key's shape explicitly.
            auth = _write_auth_like(entry.auth, request.secret_ref)
        try:
            result = await self.executor.execute(
                CallRequest(api=entry.id, operation=op_id), SYSTEM_CALLER, auth=auth
            )
        except CredentialMissing as exc:
            return exc.reason
        except PolicyDenied as exc:
            return f"validation call refused: {exc.reason}"
        except LibrarianError as exc:  # the API gave no answer
            return f"validation call failed: {exc}"
        if not result.ok:
            return f"validation call returned HTTP {result.status_code}"
        return None


def _where(exc: BaseException) -> str:
    """An unexpected error as its kind and where it was raised, without its message: a
    message can quote whatever the code was handling at that moment."""
    frames = traceback.extract_tb(exc.__traceback__)
    place = f" at {frames[-1].filename.rsplit('/', 1)[-1]}:{frames[-1].lineno}" if frames else ""
    return f"{type(exc).__name__}{place}"


def _enable_change(
    ops: set[str],
    write_auth: AuthSpec | None,
    note: str,
    auto_ops: set[str] | None = None,
    cap: int = 100,
) -> EntryChange:
    """The decision "these operations are writable (and this is the write credential,
    when the entry has none; and these are approved automatically)" as a function of
    whatever the proposal already holds."""
    auto = auto_ops or set()

    def fields(op: Operation) -> dict[str, Any]:
        update: dict[str, Any] = {"access": Access.WRITE, "writable": True}
        if op.id in auto and op.method != "DELETE":
            update |= {"approval": "auto", "auto_limit_per_day": max(1, min(cap, 100_000))}
        return update

    def change(base: ApiEntry) -> ApiEntry:
        operations = [o.model_copy(update=fields(o)) if o.id in ops else o for o in base.operations]
        update: dict[str, Any] = {"operations": operations, "notes": (base.notes + note)[:8000]}
        if base.write_auth is None and write_auth is not None:
            update["write_auth"] = write_auth
        return base.model_copy(update=update)

    return change


def _write_auth_like(auth: AuthSpec, secret_ref: str) -> AuthSpec:
    """The write credential's shape: the read authentication with another secret. An
    API that needs no key for reads is assumed to take a bearer token for writes."""
    if isinstance(auth, KEYLESS):
        return BearerAuth(secret_ref=secret_ref)
    return auth.model_copy(update={"secret_ref": secret_ref})


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
    if isinstance(entry.auth, AwsSigV4Auth):
        return f"aws_sigv4 ({entry.auth.service}, {entry.auth.region})"
    if isinstance(entry.auth, OAuth2UserAuth):
        return "oauth2_user (each person signs in)"
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
