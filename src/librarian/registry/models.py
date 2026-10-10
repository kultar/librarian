"""Registry data models.

A registry entry describes one API: where it lives, how to authenticate, and
which operations exist. Entries are stored as YAML and are untrusted input
(community entries, agent-learned entries), so every field is validated.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
OPERATION_ID = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")
SECRET_REF = re.compile(r"^[a-z0-9][a-z0-9_./-]{0,127}$")

READ_METHODS = frozenset({"GET", "HEAD"})
HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})


class StrictModel(BaseModel):
    """Base for models loaded from untrusted files: unknown fields are errors."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EntryStatus(StrEnum):
    """Lifecycle of a registry entry."""

    PROVISIONAL = "provisional"  # learned automatically, not yet reviewed
    VERIFIED = "verified"  # reviewed by a human
    BLOCKED = "blocked"  # explicitly disabled (e.g. credential request denied)


class Access(StrEnum):
    """Whether an operation reads or changes data."""

    READ = "read"
    WRITE = "write"
    UNKNOWN = "unknown"


class ReadBasis(StrEnum):
    """Why an operation is considered a read.

    `method` reads (GET/HEAD) are safe to use from provisional entries. A
    `reviewed` read (e.g. a POST search endpoint) must be confirmed by a human
    and is only usable once the entry is verified.
    """

    METHOD = "method"
    REVIEWED = "reviewed"


class ParamLocation(StrEnum):
    PATH = "path"
    QUERY = "query"
    HEADER = "header"


class Parameter(StrictModel):
    name: str = Field(min_length=1, max_length=128)
    location: ParamLocation = Field(alias="in")
    required: bool = False
    type: str = "string"
    description: str = Field(default="", max_length=2000)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="after")
    def _path_params_are_required(self) -> Parameter:
        if self.location is ParamLocation.PATH:
            self.required = True
        return self


class Operation(StrictModel):
    id: str
    method: str
    path: str = Field(min_length=1, max_length=2048)
    summary: str = Field(default="", max_length=2000)
    parameters: list[Parameter] = Field(default_factory=list[Parameter])
    access: Access = Access.UNKNOWN
    read_basis: ReadBasis | None = None
    # Only meaningful for reviewed reads that need a JSON body (POST search).
    accepts_body: bool = False

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not OPERATION_ID.match(value):
            raise ValueError(f"invalid operation id {value!r}")
        return value

    @field_validator("method")
    @classmethod
    def _check_method(cls, value: str) -> str:
        method = value.upper()
        if method not in HTTP_METHODS:
            raise ValueError(f"unsupported HTTP method {value!r}")
        return method

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("operation path must start with '/'")
        if "://" in value or value.startswith("//"):
            raise ValueError("operation path must be relative to the API base URL")
        return value

    @model_validator(mode="after")
    def _check_read_basis(self) -> Operation:
        if self.access is Access.READ:
            if self.read_basis is None:
                self.read_basis = (
                    ReadBasis.METHOD if self.method in READ_METHODS else ReadBasis.REVIEWED
                )
            if self.read_basis is ReadBasis.METHOD and self.method not in READ_METHODS:
                raise ValueError(
                    f"operation {self.id!r}: only GET/HEAD can be reads by method; "
                    "mark it read_basis: reviewed after human review"
                )
        else:
            self.read_basis = None
        if self.accepts_body and self.method in READ_METHODS:
            raise ValueError(f"operation {self.id!r}: GET/HEAD operations cannot take a body")
        return self

    @property
    def path_params(self) -> list[Parameter]:
        return [p for p in self.parameters if p.location is ParamLocation.PATH]


class NoAuth(StrictModel):
    type: Literal["none"] = "none"


class BearerAuth(StrictModel):
    type: Literal["bearer"] = "bearer"
    secret_ref: str


class ApiKeyAuth(StrictModel):
    type: Literal["api_key"] = "api_key"
    secret_ref: str
    location: Literal["header", "query"] = Field(default="header", alias="in")
    name: str = Field(min_length=1, max_length=128)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class BasicAuth(StrictModel):
    """HTTP basic auth. The secret holds `username:password` (e.g. Jira email:token)."""

    type: Literal["basic"] = "basic"
    secret_ref: str


AuthSpec = Annotated[
    NoAuth | BearerAuth | ApiKeyAuth | BasicAuth,
    Field(discriminator="type"),
]


class CredentialGuidance(StrictModel):
    """Help shown to the human who provides a credential for this API."""

    create_url: str | None = None  # where to create a token, ideally prefilled
    scopes: list[str] = Field(default_factory=list[str])
    instructions: str = Field(default="", max_length=4000)
    validation_operation: str | None = None  # a cheap read used to test a new credential


class SpecSource(StrictModel):
    kind: Literal["openapi", "manual", "inferred"] = "manual"
    url: str | None = None
    fetched_at: datetime | None = None


class Owners(StrictModel):
    """Who approves credentials and reviews this entry (notifier-specific names)."""

    channel: str | None = None
    approver_group: str | None = None


class ApiEntry(StrictModel):
    id: str
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    base_url: str
    status: EntryStatus = EntryStatus.PROVISIONAL
    auth: AuthSpec = Field(default_factory=NoAuth)
    credential: CredentialGuidance = Field(default_factory=CredentialGuidance)
    operations: list[Operation] = Field(default_factory=list[Operation])
    source: SpecSource = Field(default_factory=SpecSource)
    owners: Owners = Field(default_factory=Owners)
    notes: str = Field(default="", max_length=8000)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not SLUG.match(value):
            raise ValueError(f"invalid API id {value!r} (lowercase letters, digits, '-')")
        return value

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValueError("base_url must be an absolute http(s) URL")
        if parts.username or parts.password:
            raise ValueError("base_url must not embed credentials")
        if parts.query or parts.fragment:
            raise ValueError("base_url must not have a query or fragment")
        return value.rstrip("/")

    @model_validator(mode="after")
    def _check_entry(self) -> ApiEntry:
        seen: set[str] = set()
        for op in self.operations:
            if op.id in seen:
                raise ValueError(f"duplicate operation id {op.id!r}")
            seen.add(op.id)
        auth = self.auth
        if not isinstance(auth, NoAuth) and not SECRET_REF.match(auth.secret_ref):
            raise ValueError(f"invalid secret_ref {auth.secret_ref!r}")
        validation = self.credential.validation_operation
        if validation is not None:
            op = self.operation(validation)
            if op is None or op.access is not Access.READ:
                raise ValueError(f"validation_operation {validation!r} must be a read operation")
        return self

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).hostname or ""

    @property
    def secret_ref(self) -> str | None:
        return None if isinstance(self.auth, NoAuth) else self.auth.secret_ref

    def operation(self, operation_id: str) -> Operation | None:
        return next((op for op in self.operations if op.id == operation_id), None)
