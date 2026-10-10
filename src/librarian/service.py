"""The Librarian service: wires registry, policy, executor, credentials and planner."""

from __future__ import annotations

import json
import logging
import secrets as pysecrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.models import Model

from librarian import telemetry
from librarian.approvals import ApprovalBroker, ApprovalState, Grant, WriteApproval
from librarian.audit import AuditEvent, AuditSink, JsonlAuditSink
from librarian.calls import Caller, CallRequest, CallResult, WriteRequest
from librarian.config import Settings
from librarian.credentials import CredentialBroker, CredentialRequest, Notifier, RequestState
from librarian.executor import CredentialMissing, Executor, PolicyDenied
from librarian.notifiers import SlackNotifier, WebhookNotifier
from librarian.openapi import SpecError, discover_spec, entry_from_spec, fetch_spec
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
        self.policy = PolicyEngine(
            self.registry,
            rate_limit_per_minute=settings.rate_limit_per_minute,
            write_rate_limit_per_minute=settings.writes.rate_limit_per_minute,
            allow_provisional=settings.allow_provisional,
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
        model = planner_model if planner_model is not None else _model_from_settings(settings)
        mode = settings.planner_output
        if mode == "auto":
            mode = "prompted" if settings.model_base_url else "tool"
        self.planner = (
            Planner(model, settings.max_calls_per_fetch, mode) if model is not None else None
        )

    def load(self) -> None:
        self.registry.load()

    async def start(self) -> None:
        """Load the registry and durable state; call once before serving."""
        self.load()
        if self.state is not None:
            await self.state.initialize()
            restored = await self.broker.restore()
            if restored:
                log.info("restored %d pending credential request(s)", restored)
            await self.approvals.restore()
            for data in await self.state.list_docs("recipe", "active"):
                recipe = Recipe.model_validate_json(data)
                self.recipes[recipe.id] = recipe
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
            await self.secrets.put(ref, key)
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
    ) -> CallResult:
        """Run one read operation; if a credential is missing, ask a human and wait."""
        try:
            return await self.executor.execute(request, caller)
        except CredentialMissing as missing:
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
        approval = await self.approvals.open(entry, op, request, caller)
        if on_approval_required is not None:
            await on_approval_required(approval)
        approval = await self.approvals.wait(approval.id, max_wait=caller.approval_wait_seconds)
        if approval.state is ApprovalState.PENDING:
            await self.approvals.expire(approval.id, f"{caller.agent} stopped waiting")
        if approval.state is not ApprovalState.APPROVED:
            telemetry.write_requests.add(1, {"librarian.api": entry.id, "outcome": approval.state})
            raise WriteRefused(
                f"write {approval.state}" + (f" ({approval.note})" if approval.note else "")
            )

        # Run exactly what was approved, against the entry as it is now.
        current = self.registry.get(entry.id)
        current_op = current.operation(op.id) if current is not None else None
        if current is None or current_op is None or not current_op.writable:
            await self.approvals.finish(approval, False, None, "operation no longer writable")
            raise WriteRefused("the operation is no longer enabled for writes")
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
        telemetry.write_requests.add(1, {"librarian.api": entry.id, "outcome": "executed"})
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
            reads = [op for op in entry.operations if op.access is Access.READ]
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
            }
            for e in entries
        ]

    async def onboard(
        self,
        api_id: str,
        *,
        spec_url: str | None = None,
        base_url: str | None = None,
        name: str | None = None,
        caller: Caller = SYSTEM_CALLER,
        replace_provisional: bool = True,
    ) -> OnboardResult:
        """Learn an API from its OpenAPI spec and save it as a provisional entry."""
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
        if spec_url is None and base_url is None:
            raise LibrarianError("onboarding needs spec_url or base_url")
        private = self.settings.allow_private_networks
        spec_client = httpx.AsyncClient(timeout=30, follow_redirects=False)
        try:
            if spec_url is not None:
                spec = await fetch_spec(spec_url, spec_client, allow_private=private)
            elif base_url is not None:
                spec_url, spec = await discover_spec(base_url, spec_client, allow_private=private)
            else:
                raise LibrarianError("onboarding needs spec_url or base_url")
            entry = entry_from_spec(
                spec, api_id=api_id, spec_url=spec_url, base_url=base_url, name=name
            )
        except (SpecError, httpx.HTTPError, ValueError) as exc:
            raise LibrarianError(f"could not learn {api_id!r}: {exc}") from exc
        finally:
            await spec_client.aclose()
        if existing is not None:
            # Keep human-provided fields when re-learning a provisional entry.
            entry = entry.model_copy(
                update={"owners": existing.owners, "auth": existing.auth}
                if existing.secret_ref
                else {"owners": existing.owners}
            )
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
                detail=f"{len(entry.operations)} operations ({reads} read, {review} need review)",
                extra={"spec_url": spec_url, "review_url": review_url},
            )
        )
        return OnboardResult(entry, path, reads, review, review_url, review_error)

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
    if not config.repo or config.token is None:
        raise ValueError("publish.type=github requires publish.repo and publish.token")
    return GitHubPublisher(
        config.repo,
        config.token.get_secret_value(),
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


def _model_from_settings(settings: Settings) -> Model | str | None:
    if settings.model is None:
        return None
    if settings.model_base_url is None:
        return settings.model  # e.g. "anthropic:claude-sonnet-5-5"; provider reads its own env
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    api_key = settings.model_api_key.get_secret_value() if settings.model_api_key else None
    name = (
        settings.model.split(":", 1)[1] if settings.model.startswith("openai:") else settings.model
    )
    return OpenAIChatModel(
        name, provider=OpenAIProvider(base_url=settings.model_base_url, api_key=api_key)
    )
