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
# A secret reference, optionally selecting one key of a JSON secret: `service/jira#api_token`.
SECRET_REF = re.compile(r"^[a-z0-9][a-z0-9_./-]{0,127}(#[A-Za-z0-9_.-]{1,128})?$")
# Internal secrets (caller keys, the link-signing key) live under this prefix.
# API entries may never reference them, so no registry entry can make the
# librarian send an internal secret to an outside host.
RESERVED_SECRET_PREFIX = "librarian/"  # noqa: S105 - a name prefix, not a secret


def is_reserved_secret(ref: str) -> bool:
    return ref.startswith(RESERVED_SECRET_PREFIX)


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


class Pagination(StrictModel):
    """How to fetch more pages of an operation's results.

    - link:   follow `Link: <...>; rel="next"` headers (the default)
    - cursor: send the value at `next_cursor_path` back as `cursor_param`
              (e.g. Jira: nextPageToken → nextPageToken)
    - offset: advance `offset_param` by the number of items received
              (e.g. startAt), optionally stopping at `total_path`
    - page:   increment `page_param` until a page has no items
    `items_path` names the list in each page; when set, pages are merged into
    one list at that path.
    """

    style: Literal["link", "cursor", "offset", "page"] = "link"
    items_path: str | None = None
    cursor_param: str | None = None
    next_cursor_path: str | None = None
    offset_param: str | None = None
    total_path: str | None = None
    page_param: str | None = None
    first_page: int = 1

    @model_validator(mode="after")
    def _check(self) -> Pagination:
        required = {
            "cursor": ("cursor_param", "next_cursor_path"),
            "offset": ("offset_param", "items_path"),
            "page": ("page_param", "items_path"),
            "link": (),
        }[self.style]
        missing = [name for name in required if getattr(self, name) is None]
        if missing:
            raise ValueError(f"{self.style} pagination needs {', '.join(missing)}")
        return self


class Origin(StrEnum):
    """Where an operation's description came from."""

    SPEC = "spec"  # a machine-readable OpenAPI/Swagger document
    DOCS = "docs"  # extracted from documentation pages by a model, with a verified quote
    MODEL = "model"  # the model's own knowledge of the API: unconfirmed until it works
    MANUAL = "manual"  # written by a person


MAX_SHAPE_PATHS = 200


class Operation(StrictModel):
    id: str
    method: str
    path: str = Field(min_length=1, max_length=2048)
    summary: str = Field(default="", max_length=2000)
    tags: list[str] = Field(default_factory=list[str], max_length=20)
    parameters: list[Parameter] = Field(default_factory=list[Parameter])
    access: Access = Access.UNKNOWN
    read_basis: ReadBasis | None = None
    # Only meaningful for reviewed reads that need a JSON body (POST search).
    accepts_body: bool = False
    pagination: Pagination | None = None  # overrides the entry's default
    # Writes: a reviewer must opt each write operation in. Even then every call
    # needs human approval (or a matching grant) and a write credential.
    writable: bool = False
    # May an approval of this operation be remembered as a grant? Never for DELETE.
    rememberable: bool | None = None
    origin: Origin = Origin.MANUAL
    # For docs-learned operations: the verbatim passage that describes it, and where.
    evidence: str | None = Field(default=None, max_length=1000)
    evidence_url: str | None = None
    # When a real call first succeeded. Docs/model operations are unconfirmed until then.
    confirmed_at: datetime | None = None
    # Field paths seen in a successful response (e.g. "issues[].key"); the baseline
    # for noticing that the API's responses changed shape.
    response_shape: list[str] | None = Field(default=None, max_length=MAX_SHAPE_PATHS)
    # Set when the API changed in a way that needs a human; the operation is refused
    # until a reviewer clears it.
    suspended: str | None = Field(default=None, max_length=500)
    # Shown to callers with every result (e.g. "the spec no longer lists this").
    warning: str | None = Field(default=None, max_length=500)

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
        if self.writable and self.access is not Access.WRITE:
            raise ValueError(
                f"operation {self.id!r}: only access: write operations can be writable"
            )
        if self.rememberable is None:
            self.rememberable = self.method != "DELETE"
        elif self.rememberable and self.method == "DELETE":
            raise ValueError(f"operation {self.id!r}: DELETE approvals can never be remembered")
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


class OAuth2UserAuth(StrictModel):
    """Acting as the person: OAuth 2.0 authorization code (PKCE) per principal.

    Each person signs in once through librarian's pages; their tokens live under
    `<api>/users/<hash of the principal>` and every call for them carries their own
    token, so the API enforces their access. librarian is the OAuth client: register
    it with the provider once and put the client secret under `client_secret_ref`.
    Only verified entries may use this (the URLs are where tokens and secrets go)."""

    type: Literal["oauth2_user"] = "oauth2_user"
    authorization_url: str
    token_url: str
    scopes: list[str] = Field(default_factory=list[str], max_length=50)
    client_id: str = Field(min_length=1, max_length=256)
    client_secret_ref: str | None = None  # None: a public client (PKCE only)
    revocation_url: str | None = None  # RFC 7009 endpoint: disconnecting revokes there too
    pkce: bool = True
    # Provider extras on the authorization request, e.g. Google's
    # {"access_type": "offline", "prompt": "consent"} to get a refresh token.
    extra_authorize_params: dict[str, str] = Field(default_factory=dict[str, str])

    @field_validator("authorization_url", "token_url", "revocation_url")
    @classmethod
    def _https(cls, url: str | None) -> str | None:
        if url is None:
            return None
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("OAuth endpoints must be https URLs")
        return url


AuthSpec = Annotated[
    NoAuth | BearerAuth | ApiKeyAuth | BasicAuth | OAuth2UserAuth,
    Field(discriminator="type"),
]


class CredentialGuidance(StrictModel):
    """Help shown to the human who provides a credential for this API."""

    create_url: str | None = None  # where to create a token, ideally prefilled
    scopes: list[str] = Field(default_factory=list[str])
    instructions: str = Field(default="", max_length=4000)
    validation_operation: str | None = None  # a cheap read used to test a new credential


class Discovery(StrictModel):
    """A read that teaches the librarian instance-specific names.

    Example (Jira): operation getFields, key "id", value "name", key_prefix
    "customfield_" fills the glossary with customfield_10020 -> Sprint.
    """

    operation: str
    items_path: str | None = None  # where the list is; None = the response itself
    key: str = "id"
    value: str = "name"
    key_prefix: str | None = None  # keep only keys starting with this


class SpecSource(StrictModel):
    """Where an entry was learned from, and a fingerprint for noticing changes.

    kind: openapi (a spec), docs (documentation pages), inferred (the model's own
    knowledge), manual (written by a person).
    """

    kind: Literal["openapi", "docs", "manual", "inferred"] = "manual"
    url: str | None = None
    pages: list[str] = Field(default_factory=list[str], max_length=20)  # docs pages read
    fetched_at: datetime | None = None
    content_hash: str | None = None  # of the spec / docs text when last learned or checked
    checked_at: datetime | None = None
    # How the spec/docs were found when nobody gave their URL: well-known (usual places),
    # homepage (a link on the API's site) or search; and how far the source is trusted:
    # official (the API's own domain), docs-host (GitHub, ReadMe, ...) or third-party.
    found_by: Literal["given", "well-known", "homepage", "search"] | None = None
    trust: Literal["official", "docs-host", "third-party"] | None = None


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
    # Credential used for writes. Kept separate so the read path never holds a key
    # that can change data; without it the API cannot be written to.
    write_auth: AuthSpec | None = None
    credential: CredentialGuidance = Field(default_factory=CredentialGuidance)
    operations: list[Operation] = Field(default_factory=list[Operation])
    pagination: Pagination | None = None  # default for all operations
    discovery: list[Discovery] = Field(default_factory=list[Discovery], max_length=10)
    # Instance-specific names learned by discovery (id -> human name), shown to the planner.
    glossary: dict[str, str] = Field(default_factory=dict[str, str])
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
        for auth in (self.auth, self.write_auth):
            if auth is None or isinstance(auth, NoAuth):
                continue
            if isinstance(auth, OAuth2UserAuth):
                ref = auth.client_secret_ref
                if ref is not None and (not SECRET_REF.match(ref) or is_reserved_secret(ref)):
                    raise ValueError(f"invalid client_secret_ref {ref!r}")
                continue
            if not SECRET_REF.match(auth.secret_ref):
                raise ValueError(f"invalid secret_ref {auth.secret_ref!r}")
            if is_reserved_secret(auth.secret_ref):
                raise ValueError(
                    f"secret_ref {auth.secret_ref!r} uses the reserved prefix "
                    f"{RESERVED_SECRET_PREFIX!r} (internal secrets)"
                )
        for item in self.discovery:
            found = self.operation(item.operation)
            if found is None or found.access is not Access.READ:
                raise ValueError(f"discovery operation {item.operation!r} must be a read operation")
        if len(self.glossary) > 5000:
            raise ValueError("glossary is limited to 5000 entries")
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
        """The shared read credential's reference (None for no auth, and for per-person
        sign-in, whose tokens live under `<id>/users/...`)."""
        if isinstance(self.auth, NoAuth | OAuth2UserAuth):
            return None
        return self.auth.secret_ref

    @property
    def write_secret_ref(self) -> str | None:
        auth = self.write_auth
        if auth is None or isinstance(auth, NoAuth | OAuth2UserAuth):
            return None
        return auth.secret_ref

    @property
    def per_person(self) -> bool:
        """Reads use each person's own sign-in rather than a shared credential."""
        return isinstance(self.auth, OAuth2UserAuth)

    @property
    def has_writable_operations(self) -> bool:
        return any(op.writable for op in self.operations)

    def pagination_for(self, op: Operation) -> Pagination:
        return op.pagination or self.pagination or Pagination()

    def operation(self, operation_id: str) -> Operation | None:
        return next((op for op in self.operations if op.id == operation_id), None)
