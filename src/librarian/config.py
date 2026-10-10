"""Configuration from environment variables (LIBRARIAN_*) and/or a YAML file."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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


class AwsSecretsSettings(BaseModel):
    region: str | None = None  # default: the usual AWS environment/config chain
    prefix: str = ""  # e.g. "librarian/" → ref jira/readonly = secret librarian/jira/readonly
    cache_seconds: float = 60


class PublishSettings(BaseModel):
    """Where learned entries are proposed for human review."""

    type: Literal["none", "github"] = "none"
    repo: str | None = None  # owner/name
    base_branch: str = "main"
    directory: str = "registry"
    token: SecretStr | None = None  # needs contents:write + pull_requests:write on the repo
    api_url: str = "https://api.github.com"


class CallerKey(BaseModel):
    """A calling agent and the secret reference holding its API key."""

    agent: str
    key_ref: str
    # Glob patterns of API ids this agent may use, e.g. ["jira", "github-*"].
    # Narrow this in shared deployments; "*" lets the agent use every API.
    apis: list[str] = Field(default_factory=lambda: ["*"])
    can_onboard: bool = True

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
    public_url: str = "http://127.0.0.1:8080"

    registry_dir: Path = Path("registry")
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
    max_calls_per_fetch: int = 8

    rate_limit_per_minute: int = 60
    request_timeout_seconds: float = 30
    max_response_bytes: int = 2_000_000
    allow_provisional: bool = True
    # Let onboarding and provisional entries reach private/internal addresses.
    # Keep off unless every caller is trusted (SSRF protection).
    allow_private_networks: bool = False
    onboarding_enabled: bool = True
    # How long an A2A task waits for a human to provide a missing credential.
    credential_wait_seconds: float = 900

    # Caller authentication. Empty = open (local mode only).
    callers: list[CallerKey] = Field(default_factory=list[CallerKey])

    notifier: NotifierSettings = Field(default_factory=NotifierSettings)
    publish: PublishSettings = Field(default_factory=PublishSettings)
    approvers: ApproverSettings = Field(default_factory=ApproverSettings)

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
