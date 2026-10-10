"""The Librarian service: wires registry, policy, executor, credentials and planner."""

from __future__ import annotations

import logging
import secrets as pysecrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from pydantic_ai.models import Model

from librarian.audit import AuditEvent, AuditSink, JsonlAuditSink
from librarian.calls import Caller, CallRequest, CallResult
from librarian.config import Settings
from librarian.credentials import CredentialBroker, CredentialRequest, Notifier, RequestState
from librarian.executor import CredentialMissing, Executor, PolicyDenied
from librarian.notifiers import SlackNotifier, WebhookNotifier
from librarian.openapi import SpecError, discover_spec, entry_from_spec, fetch_spec
from librarian.planner import FetchOutcome, Planner
from librarian.policy import PolicyEngine
from librarian.registry.models import Access, ApiEntry, EntryStatus
from librarian.registry.store import Registry
from librarian.secrets import ChainSecretStore, EnvSecretStore, FileSecretStore, SecretStore

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


@dataclass
class OnboardResult:
    entry: ApiEntry
    path: Path
    read_operations: int
    needs_review: int


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
    ) -> None:
        self.settings = settings
        self.registry = registry or Registry(settings.registry_dir)
        self.secrets = secrets_store or _default_secrets(settings)
        self.audit = audit or JsonlAuditSink(settings.audit_file)
        self.http = http_client or httpx.AsyncClient(
            timeout=settings.request_timeout_seconds, follow_redirects=False
        )
        self.policy = PolicyEngine(
            self.registry,
            rate_limit_per_minute=settings.rate_limit_per_minute,
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
        self.broker = CredentialBroker(
            self.registry,
            self.secrets,
            notifier or _default_notifier(settings),
            self.audit,
            public_url=settings.public_url,
            signing_key=pysecrets.token_bytes(32),
            expire_after=timedelta(hours=settings.notifier.expire_after_hours),
            remind_after=timedelta(hours=settings.notifier.remind_after_hours),
            default_channel=settings.notifier.slack.channel,
            default_approver_group=settings.notifier.slack.approver_group,
        )
        self.broker.validator = self._validate_credential
        model = planner_model if planner_model is not None else _model_from_settings(settings)
        self.planner = Planner(model, settings.max_calls_per_fetch) if model is not None else None

    def load(self) -> None:
        self.registry.load()

    async def aclose(self) -> None:
        await self.http.aclose()

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
        if self.planner is None:
            raise LibrarianError("the fetch skill is disabled: no model is configured")
        for _attempt in range(2):
            try:
                return await self.planner.fetch(intent, self.registry, self.executor, caller)
            except CredentialMissing as missing:
                await self._await_credential(missing, caller, on_auth_required)
        raise LibrarianError("credential still not usable after it was provided")

    def capabilities(self, query: str | None = None) -> list[dict[str, Any]]:
        entries = (
            [h.entry for h in self.registry.search(query, limit=10)]
            if query
            else [e for e in self.registry.all() if e.status is not EntryStatus.BLOCKED]
        )
        return [
            {
                "api": e.id,
                "name": e.name,
                "description": e.description[:300],
                "status": e.status.value,
                "base_url": e.base_url,
                "read_operations": [
                    {"operation": op.id, "summary": op.summary[:160]}
                    for op in e.operations
                    if op.access is Access.READ
                ][:50],
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
        await self.audit.record(
            AuditEvent(
                event="onboard",
                caller=caller.agent,
                principal=caller.principal,
                api=api_id,
                detail=f"{len(entry.operations)} operations ({reads} read, {review} need review)",
                extra={"spec_url": spec_url},
            )
        )
        return OnboardResult(entry, path, reads, review)

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
    if settings.secrets_dir is not None:
        stores.append(FileSecretStore(settings.secrets_dir))
    stores.append(EnvSecretStore())
    return ChainSecretStore(stores)


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
