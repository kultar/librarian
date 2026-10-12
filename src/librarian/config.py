"""Configuration from environment variables (LIBRARIAN_*) and/or a YAML file."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_core import PydanticUseDefault
from pydantic_settings import BaseSettings, SettingsConfigDict

from librarian.net import NetworkPolicy, host_pattern_ok
from librarian.registry.models import RESERVED_SECRET_PREFIX


class SlackSettings(BaseModel):
    bot_token: SecretStr | None = None
    channel: str = "#api-access-requests"
    # A Slack user-group ID (S0123ABCD) is rendered as a real @mention.
    approver_group: str | None = None


class NotifierSettings(BaseModel):
    type: Literal["log", "slack", "webhook"] = "log"
    slack: SlackSettings = Field(default_factory=SlackSettings)
    webhook_url: str | None = None
    remind_after_hours: float = 4
    expire_after_hours: float = 24


class ApproverSettings(BaseModel):
    """How the secure credential page identifies approvers.

    - `token`: possession of the one-time link is enough (local/dev only)
    - `trusted_header`: an auth proxy in front (e.g. oauth2-proxy) sets identity
      and group headers; the approver must be in `group`.
    """

    mode: Literal["token", "trusted_header"] = "token"
    user_header: str = "X-Auth-Request-Email"
    groups_header: str = "X-Auth-Request-Groups"
    group: str | None = None
    # The read-only console at the pages listener's root: "signed_in" (needs
    # trusted_header, and `group` when set), "open" (no sign-in: a laptop), or "off".
    console: Literal["signed_in", "open", "off"] = "signed_in"


class AwsSecretsSettings(BaseModel):
    region: str | None = None  # default: the usual AWS environment/config chain
    prefix: str = ""  # e.g. "librarian/" → ref jira/readonly = secret librarian/jira/readonly
    cache_seconds: float = 60


class GitHubAppSettings(BaseModel):
    """Sign in as a GitHub App (recommended): owned by the organization, short-lived
    tokens. Create one with `librarian github-app create --repo owner/name`."""

    app_id: str
    # The App's private key (PEM): inline (e.g. via LIBRARIAN_PUBLISH__GITHUB_APP__PRIVATE_KEY)
    # or a file (e.g. a mounted Kubernetes Secret, shared with git-sync).
    private_key: SecretStr | None = None
    private_key_file: Path | None = None
    installation_id: int | None = None  # found automatically when unset

    @model_validator(mode="after")
    def _one_key(self) -> GitHubAppSettings:
        if (self.private_key is None) == (self.private_key_file is None):
            raise ValueError("github_app needs exactly one of private_key or private_key_file")
        return self

    def key(self) -> str:
        if self.private_key is not None:
            return self.private_key.get_secret_value()
        if self.private_key_file is None:  # excluded by the validator
            raise ValueError("github_app has no private key")
        return self.private_key_file.read_text("utf-8")


class PublishSettings(BaseModel):
    """Where learned entries are proposed for human review."""

    type: Literal["none", "github"] = "none"
    repo: str | None = None  # owner/name
    base_branch: str = "main"
    directory: str = "registry"
    token: SecretStr | None = None  # needs contents:write + pull_requests:write on the repo
    github_app: GitHubAppSettings | None = None  # instead of token (recommended)
    api_url: str = "https://api.github.com"


class IssuerSettings(BaseModel):
    issuer: str  # e.g. https://acme.okta.com/oauth2/default
    audience: str  # the audience tokens must be minted for (librarian)
    jwks_url: str | None = None  # default: from the issuer's OIDC discovery document
    claim: str = "email"  # claim that names the person
    algorithms: list[str] = Field(default_factory=lambda: ["RS256", "ES256"])
    leeway_seconds: int = 30


class PrincipalSettings(BaseModel):
    """Who agents act for.

    - off:      the agent's claimed `principal` is recorded (and trusted for approvals)
    - optional: a token in `header` is verified when present; a claimed principal
                without one is recorded but not trusted for approvals
    - required: a claimed principal without a valid token is refused
    """

    mode: Literal["off", "optional", "required"] = "off"
    header: str = "X-Principal-Token"
    issuers: list[IssuerSettings] = Field(default_factory=list[IssuerSettings])

    @field_validator("mode", mode="before")
    @classmethod
    def _yaml_off(cls, value: object) -> object:
        # YAML reads a bare `off` as false.
        return "off" if value is False else value


class GrantSettings(BaseModel):
    """A standing grant declared by the operator (typically for autonomous agents)."""

    agent: str
    api: str
    operation: str
    principal: str | None = None
    pinned_args: dict[str, str] = Field(default_factory=dict[str, str])
    max_uses_per_day: int | None = None
    notify_owner: bool = True


class WriteSettings(BaseModel):
    # Who may approve a write: the requester (the person the agent acts for, or the
    # agent's owner), members of the approver group, or either.
    approvers: Literal["requester", "group", "requester_or_group"] = "requester_or_group"
    approval_expire_hours: float = 24
    rate_limit_per_minute: int = 10
    grants: list[GrantSettings] = Field(default_factory=list[GrantSettings])


class SearchSettings(BaseModel):
    """Finding an API's documentation on the web when it is not in the usual places.

    - off:     never search
    - model:   the model's own web search (Anthropic models; through a gateway, set
               model_api: anthropic)
    - searxng: a SearXNG instance (`url`), e.g. self-hosted
    - brave:   the Brave Search API (`api_key`)
    """

    provider: Literal["off", "model", "searxng", "brave"] = "off"
    url: str | None = None
    api_key: SecretStr | None = None
    # Only use docs on the API's own domain or a well-known docs host (GitHub, ReadMe,
    # GitBook, ...). Pages elsewhere could be planted to steer what is learned.
    allow_third_party: bool = False
    max_results: int = Field(default=8, ge=1, le=20)


class LearningSettings(BaseModel):
    """Learning APIs that have no machine-readable spec (needs `model`)."""

    # The model used for learning (reading documentation, web search), when it should differ
    # from `model` (which `fetch` uses on every plain-language request). Learning happens once
    # per API, so a stronger model is cheap here; unset = the same as `model`. Same gateway
    # settings (model_base_url, model_api, model_api_key); just the model name.
    model: str | None = None
    docs_max_pages: int = Field(default=8, ge=1, le=20)
    # With only a base URL: look for documentation in the usual places (/docs,
    # docs.<domain>, /llms.txt, links on the homepage) before anything else.
    find_docs: bool = True
    search: SearchSettings = Field(default_factory=SearchSettings)
    # With only a base URL and no spec or docs found: describe the API from the model's
    # own knowledge (reads only, unconfirmed until a call succeeds).
    model_knowledge: bool = True


class DriftSettings(BaseModel):
    """Noticing API changes."""

    # Re-check each entry's spec or docs this often (0 = never). Changes to verified
    # entries are proposed for review; risky operations are suspended meanwhile.
    recheck_hours: float = Field(default=24, ge=0)


class CallerKey(BaseModel):
    """A calling agent and the secret reference holding its API key."""

    agent: str
    key_ref: str
    # Glob patterns of API ids this agent may use, e.g. ["jira", "github-*"].
    # Narrow this in shared deployments; "*" lets the agent use every API.
    apis: list[str] = Field(default_factory=lambda: ["*"])
    can_onboard: bool = True
    # APIs this agent may request writes on (each write still needs approval or a
    # grant). Empty by default: no writes.
    write_apis: list[str] = Field(default_factory=list[str])
    # Human (email) responsible for this agent. Required for autonomous agents that
    # write: approvals for requests with no principal are routed to the owner.
    owner: str | None = None
    # How long this agent's write requests wait for approval; 0 returns "pending"
    # at once (autonomous agents poll the A2A task). None = the global default.
    approval_wait_seconds: float | None = None

    @model_validator(mode="after")
    def _warn_unowned_writer(self) -> CallerKey:
        if self.write_apis and not self.owner:
            logging.getLogger(__name__).warning(
                "caller %r may request writes but has no owner: its writes can only be "
                "approved by an approver group (or run under grants)",
                self.agent,
            )
        return self

    @field_validator("key_ref")
    @classmethod
    def _internal(cls, value: str) -> str:
        if not value.startswith(RESERVED_SECRET_PREFIX):
            raise ValueError(f"caller key_ref must start with {RESERVED_SECRET_PREFIX!r}")
        return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LIBRARIAN_", env_nested_delimiter="__", extra="ignore"
    )

    host: str = "127.0.0.1"
    port: int = 8080
    # Where approvers' browsers reach the credential/approval pages (used in links).
    public_url: str = "http://127.0.0.1:8080"
    # The A2A endpoint advertised in the agent card (where agents connect). Defaults
    # to public_url; set it when agents and people reach librarian at different
    # addresses (e.g. in-cluster Service vs. SSO-protected ingress).
    a2a_url: str | None = None
    # Serve the human pages (/credentials, /approvals) on their own listener.
    # Required with approvers.mode=trusted_header: identity headers must only be
    # accepted from the auth proxy, so the pages listener should be reachable only
    # by the proxy (e.g. bound to 127.0.0.1 behind an oauth2-proxy sidecar), while
    # agents use the A2A listener, which then does not serve the pages at all.
    pages_host: str = "127.0.0.1"
    pages_port: int | None = None

    registry_dir: Path = Path("registry")
    # Where the librarian writes its own registry changes when registry_dir is
    # managed elsewhere (e.g. a git-sync checkout). None = write into registry_dir.
    registry_overlay_dir: Path | None = None
    # Where credentials live: "file" (secrets_dir) or "aws" (Secrets Manager, `aws`
    # extra). Environment variables (LIBRARIAN_SECRET_*) are always consulted last.
    secrets_backend: Literal["file", "aws"] = "file"
    secrets_dir: Path | None = Path(".librarian/secrets")
    aws_secrets: AwsSecretsSettings = Field(default_factory=AwsSecretsSettings)
    audit_file: Path | None = None  # stdout when unset
    state_dir: Path = Path(".librarian")
    # Durable state (credential requests, A2A tasks). SQLite for one instance;
    # postgresql+asyncpg://... (install the `postgres` extra) for several replicas.
    # Set to null to keep everything in memory.
    database_url: str | None = "sqlite+aiosqlite:///.librarian/librarian.db"
    # Secret holding the key that signs approval links. Generated on first start
    # if the secret store is writable, so links survive restarts and are valid
    # on every replica.
    signing_key_ref: str = "librarian/signing-key"

    @field_validator("signing_key_ref")
    @classmethod
    def _signing_key_internal(cls, value: str) -> str:
        if not value.startswith(RESERVED_SECRET_PREFIX):
            raise ValueError(f"signing_key_ref must start with {RESERVED_SECRET_PREFIX!r}")
        return value

    # LLM for the `fetch` skill. Unset disables `fetch` (execute still works).
    # Examples: "anthropic:claude-sonnet-5-5", or any model name served by an
    # OpenAI-compatible gateway such as LiteLLM when `model_base_url` is set.
    model: str | None = None
    model_base_url: str | None = None
    model_api_key: SecretStr | None = None
    # The API format used with model_base_url (a gateway such as LiteLLM): "openai" (Chat
    # Completions) or "anthropic" (Messages). Web search through a gateway needs "anthropic".
    model_api: Literal["openai", "anthropic"] = "openai"
    # Tried once when `model` (or learning.model) fails or declines a request (a content
    # filter false positive, an outage). Same gateway settings; just the model name.
    fallback_model: str | None = None
    max_calls_per_fetch: int = 8
    # Save successful plain-language plans as templates and replay them without the LLM.
    recipes_enabled: bool = True
    # How the planner gets structured answers: "tool" (forced output tool), "native"
    # (provider JSON mode) or "prompted" (JSON in the reply). "auto" = prompted when
    # going through an OpenAI-compatible gateway (model_base_url), else tool.
    planner_output: Literal["auto", "tool", "native", "prompted"] = "auto"

    rate_limit_per_minute: int = 60
    request_timeout_seconds: float = 30
    max_response_bytes: int = 2_000_000
    allow_provisional: bool = True
    # Let onboarding and provisional entries reach private/internal addresses.
    # Keep off unless every caller is trusted (SSRF protection).
    allow_private_networks: bool = False
    # Narrower than allow_private_networks: hostname patterns (e.g. "*.internal.example.com")
    # that onboarding and provisional entries may reach even when they resolve to private
    # addresses. Everything else stays public-only, and a listed host may still never
    # resolve to loopback or link-local (cloud metadata) addresses.
    internal_hosts: list[str] = Field(default_factory=list[str])
    onboarding_enabled: bool = True
    # Serve librarian's skills as MCP tools at /mcp (Streamable HTTP) on the A2A listener.
    # How many replicas run (the Helm chart sets this). Above 1, all state must be shared:
    # librarian refuses to start with SQLite or file secrets rather than diverge silently.
    replicas: int = Field(default=1, ge=1)
    mcp_enabled: bool = True
    # Host headers accepted on /mcp (the MCP SDK's DNS-rebinding guard, e.g.
    # ["librarian.internal:8080"]). Empty = any host: every MCP request already needs a
    # caller key, which a rebinding web page cannot have.
    mcp_allowed_hosts: list[str] = Field(default_factory=list[str])
    # How long an A2A task waits for a human to provide a missing credential.
    credential_wait_seconds: float = 900

    @field_validator("internal_hosts")
    @classmethod
    def _internal_hosts(cls, patterns: list[str]) -> list[str]:
        for pattern in patterns:
            if not host_pattern_ok(pattern):
                raise ValueError(
                    f"internal_hosts: {pattern!r} is not a hostname pattern (letters, digits, "
                    "dots, dashes and *; to allow every private address set "
                    "allow_private_networks instead)"
                )
        return [p.strip().lower() for p in patterns]

    @property
    def network(self) -> NetworkPolicy:
        """What onboarding and provisional entries may reach (see net.py)."""
        return NetworkPolicy(self.allow_private_networks, tuple(self.internal_hosts))

    # Caller authentication. Empty = open (local mode only).
    callers: list[CallerKey] = Field(default_factory=list[CallerKey])

    # OpenTelemetry (`otel` extra). Exporter endpoint/headers come from the standard
    # OTEL_EXPORTER_OTLP_* environment variables.
    otel_enabled: bool = False
    otel_service_name: str = "librarian"

    notifier: NotifierSettings = Field(default_factory=NotifierSettings)
    publish: PublishSettings = Field(default_factory=PublishSettings)
    writes: WriteSettings = Field(default_factory=WriteSettings)
    learning: LearningSettings = Field(default_factory=LearningSettings)
    drift: DriftSettings = Field(default_factory=DriftSettings)
    principals: PrincipalSettings = Field(default_factory=PrincipalSettings)
    approvers: ApproverSettings = Field(default_factory=ApproverSettings)

    @field_validator("port", mode="before")
    @classmethod
    def _not_a_service_link(cls, value: object) -> object:
        # Kubernetes injects LIBRARIAN_PORT=tcp://10.0.0.1:8080 for a Service named
        # "librarian". That is not our setting: keep the default.
        if isinstance(value, str) and value.startswith("tcp://"):
            raise PydanticUseDefault
        return value

    @model_validator(mode="after")
    def _shared_state_for_replicas(self) -> Settings:
        if self.replicas > 1:
            url = self.database_url or ""
            if not url.startswith("postgresql"):
                raise ValueError(
                    f"replicas={self.replicas} needs a shared database: set database_url to "
                    "postgresql+asyncpg://... (SQLite is per replica)"
                )
            if self.secrets_backend != "aws":
                raise ValueError(
                    f"replicas={self.replicas} needs a shared secret store: set "
                    "secrets_backend: aws (file secrets are per replica)"
                )
        return self

    @classmethod
    def load(cls, config_file: Path | None = None) -> Settings:
        """Environment variables override values from the YAML file."""
        file_values: dict[str, Any] = {}
        if config_file is not None:
            loaded: Any = yaml.safe_load(config_file.read_text("utf-8")) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"{config_file}: expected a mapping")
            file_values = loaded
        env_only = cls()
        merged = _deep_merge(file_values, env_only.model_dump(exclude_unset=True))
        return cls.model_validate(merged)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = _deep_merge(current, value)  # pyright: ignore[reportUnknownArgumentType]
        else:
            result[key] = value
    return result
