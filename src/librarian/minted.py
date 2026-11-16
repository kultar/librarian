"""Credentials made for a call instead of stored.

A stored key is the same key for every call, for as long as nobody replaces it: whoever
gets hold of it has everything it can do, until someone notices. Three kinds of
authentication avoid sending such a key to an API at all:

- OAuth client credentials: the client secret goes to the token endpoint only; the API
  gets an access token that expires by itself.
- A GitHub App: the private key signs a ten-minute App token, which buys an installation
  token for an hour, limited to the repository the call names and read-only for a read.
- AWS Signature Version 4: nothing is sent but a signature that fits one request, made
  with librarian's own identity or with a role it assumes for that API.

What is made is kept, per process, until shortly before it expires. Errors here never
quote what a token endpoint or GitHub answered beyond a status and a short error code:
such answers can repeat what they were sent.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable, Hashable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import parse_qsl, quote

import httpx

from librarian.errors import ApiUnavailable, why_no_answer
from librarian.github_app import GitHubAppError, app_jwt
from librarian.registry.models import (
    ApiEntry,
    AwsSigV4Auth,
    GitHubAppTokenAuth,
    OAuth2ClientAuth,
    Operation,
)

log = logging.getLogger(__name__)

# Stop using a token this long before it expires: a call must not carry one that runs
# out on the way.
MARGIN = timedelta(seconds=60)
# How long a looked-up fact about a GitHub App is trusted (where it is installed, what it
# may do there): these change when a person changes them, which is rare.
FACTS_LAST = timedelta(minutes=10)
MAX_KEPT = 2000
# GitHub permissions that only exist as "write": left out of a read-only token.
WRITE_ONLY_PERMISSIONS = frozenset({"workflows"})
GITHUB_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
_OAUTH_ERROR = re.compile(r"^[a-z_]{1,40}$")
_PEM = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----(.+?)-----END \1-----", re.DOTALL)


class MintError(Exception):
    """A credential could not be made for a call."""


class SecretRejected(MintError):
    """What is in the secret store was refused (a client secret, an App's key): a
    person has to store the right one."""


class SetupProblem(MintError):
    """The secret is fine, or there is none; what the entry says about its
    authentication does not work. A new key would not help."""


class Kept[T]:
    """What was made, until shortly before it expires; made once at a time per key."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._values: dict[Hashable, tuple[T, datetime]] = {}
        self._locks: dict[Hashable, asyncio.Lock] = {}

    def _fresh(self, key: Hashable) -> T | None:
        kept = self._values.get(key)
        if kept is not None and self._clock() < kept[1] - MARGIN:
            return kept[0]
        return None

    async def get(self, key: Hashable, make: Callable[[], Awaitable[tuple[T, datetime]]]) -> T:
        value = self._fresh(key)
        if value is not None:
            return value
        async with self._locks.setdefault(key, asyncio.Lock()):
            value = self._fresh(key)  # another call made it while this one waited
            if value is not None:
                return value
            made, expires = await make()
            if len(self._values) >= MAX_KEPT:
                now = self._clock()
                self._values = {k: v for k, v in self._values.items() if now < v[1]}
            if len(self._values) < MAX_KEPT:
                self._values[key] = (made, expires)
            return made

    def forget(self, key: Hashable) -> None:
        self._values.pop(key, None)


def _mark(secret: str) -> str:
    """Tells one stored secret from the next in a cache key, without keeping it there."""
    return hashlib.sha256(secret.encode()).hexdigest()[:16]


def pem(key: str) -> str:
    """A private key as PEM, also when its line breaks were lost on the way into the
    secret store: pasted into a one-line field, or stored with `\\n` written out."""
    text = key.replace("\\n", "\n").strip()
    match = _PEM.search(text)
    if match is None:
        return text
    label, body = match.group(1), "".join(match.group(2).split())
    lines = "\n".join(body[i : i + 64] for i in range(0, len(body), 64))
    return f"-----BEGIN {label}-----\n{lines}\n-----END {label}-----\n"


def _said(response: httpx.Response) -> str:
    """The `message` of a GitHub error, as one short line without anything a terminal or
    a later reader would act on; nothing else from the body."""
    try:
        payload: Any = response.json()
    except ValueError:
        return ""
    message = cast(dict[str, Any], payload).get("message") if isinstance(payload, dict) else None
    if not isinstance(message, str):
        return ""
    text = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", " ".join(message.split()))[:160]
    return f": {text}" if text else ""


@dataclass(frozen=True)
class Made:
    """A token made for a call, with what to say about it in the audit trail and how to
    drop it if the API refuses it."""

    token: str
    note: str
    forget: Callable[[], None]


class AwsSigner:
    """Signs requests with AWS Signature Version 4."""

    def __init__(self, credentials: Any, service: str, region: str, note: str) -> None:
        self._credentials = credentials  # botocore ReadOnlyCredentials
        self.service = service
        self.region = region
        self.note = note
        # What the signed requests carried that must not come back in an answer.
        self.sent: set[str] = set()

    def sign(self, request: httpx.Request) -> None:
        from botocore.auth import (  # pyright: ignore[reportMissingTypeStubs]
            S3SigV4Auth,
            SigV4Auth,
        )
        from botocore.awsrequest import AWSRequest  # pyright: ignore[reportMissingTypeStubs]

        # AWS signs the query in one exact encoding (everything but letters, digits and
        # -_.~ escaped, a space as %20), whatever arrives; the signing library signs it
        # as given. So the query is first put into that encoding, on the wire as well.
        query = request.url.query.decode()
        strict = "&".join(
            f"{quote(name, safe='-_.~')}={quote(value, safe='-_.~')}"
            for name, value in parse_qsl(query, keep_blank_values=True)
        )
        if strict != query:
            request.url = request.url.copy_with(query=strict.encode())
        headers = {}
        if "content-type" in request.headers:
            headers["Content-Type"] = request.headers["content-type"]
        signed = AWSRequest(
            method=request.method, url=str(request.url), data=request.content, headers=headers
        )
        # S3 signs the path as it is; every other service signs it encoded once more.
        signer = S3SigV4Auth if self.service == "s3" else SigV4Auth
        signer(self._credentials, self.service, self.region).add_auth(signed)  # pyright: ignore[reportUnknownMemberType]
        for name in ("Authorization", "X-Amz-Date", "X-Amz-Security-Token", "X-Amz-Content-SHA256"):
            value = signed.headers.get(name)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            if value is not None:
                request.headers[name] = str(value)  # pyright: ignore[reportUnknownArgumentType]
        authorization = request.headers.get("Authorization", "")
        signature = authorization.rpartition("Signature=")[2]
        token = request.headers.get("X-Amz-Security-Token", "")
        self.sent.update(value for value in (authorization, signature, token) if value)


class Minter:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        deadline_seconds: float = 30,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.client = client
        self.deadline_seconds = deadline_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._tokens: Kept[str] = Kept(self._clock)
        self._facts: Kept[Any] = Kept(self._clock)
        self._aws: Kept[Any] = Kept(self._clock)

    # -- asking -------------------------------------------------------------------------

    async def _ask(
        self, entry: ApiEntry, whom: str, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        """One request to whoever makes tokens, with the same time limit and the same
        wording of a failure as a call to the API itself."""
        try:
            async with asyncio.timeout(self.deadline_seconds):
                return await self.client.request(method, url, **kwargs)
        except (httpx.HTTPError, TimeoutError) as exc:
            reason, detail, _ = why_no_answer(exc)
            raise ApiUnavailable(
                f"{whom} gave no answer: {reason}. Nothing was sent to {entry.name}: retry later.",
                sent=False,
                reason=f"{whom}: {reason}",
                detail=detail,
            ) from None

    # -- OAuth client credentials ---------------------------------------------------

    async def client_token(self, entry: ApiEntry, auth: OAuth2ClientAuth, secret: str) -> Made:
        key = (
            "oauth",
            entry.id,
            auth.token_url,
            auth.client_id,
            tuple(auth.scopes),
            auth.audience,
            auth.client_auth,
            _mark(secret),
        )
        token = await self._tokens.get(key, lambda: self._client_token(entry, auth, secret))
        scopes = f" ({' '.join(auth.scopes)})" if auth.scopes else ""
        return Made(token, f"client-credentials token{scopes}", lambda: self._tokens.forget(key))

    async def _client_token(
        self, entry: ApiEntry, auth: OAuth2ClientAuth, secret: str
    ) -> tuple[str, datetime]:
        form = {**auth.extra_token_params, "grant_type": "client_credentials"}
        if auth.scopes:
            form["scope"] = " ".join(auth.scopes)
        if auth.audience:
            form["audience"] = auth.audience
        sign_in: httpx.BasicAuth | None = None
        if auth.client_auth == "basic":
            # RFC 6749 2.3.1: the id and secret are form-encoded before they become Basic.
            sign_in = httpx.BasicAuth(quote(auth.client_id, safe=""), quote(secret, safe=""))
        else:
            form |= {"client_id": auth.client_id, "client_secret": secret}
        whom = f"{entry.name}'s token endpoint"
        response = await self._ask(
            entry,
            whom,
            "POST",
            auth.token_url,
            data=form,
            headers={"Accept": "application/json"},
            auth=sign_in,
        )
        try:
            payload: Any = response.json()
        except ValueError:
            payload = None
        answer: dict[str, Any] = cast(dict[str, Any], payload) if isinstance(payload, dict) else {}
        token = answer.get("access_token")
        if response.status_code != 200 or not isinstance(token, str) or not token:
            code = answer.get("error")
            named = f", {code}" if isinstance(code, str) and _OAUTH_ERROR.match(code) else ""
            said = f"HTTP {response.status_code}{named}"
            if response.status_code == 401 or code in ("invalid_client", "unauthorized_client"):
                raise SecretRejected(
                    f"the token endpoint refused the client id and secret ({said})"
                )
            raise SetupProblem(
                f"the token endpoint did not issue a token ({said}): check the token URL, the "
                "scopes and the audience of its authentication"
            )
        kind = answer.get("token_type", "bearer")
        if not isinstance(kind, str) or kind.lower() != "bearer":
            raise SetupProblem("the token endpoint issued a token that is not a bearer token")
        seconds = answer.get("expires_in")
        try:
            lifetime = float(seconds) if seconds is not None else 300.0
        except (TypeError, ValueError):
            lifetime = 300.0
        # Never trusted for longer than an hour, whatever the endpoint says.
        return token, self._clock() + timedelta(seconds=max(0.0, min(lifetime, 3600.0)))

    # -- GitHub App -----------------------------------------------------------------

    async def github_token(
        self,
        entry: ApiEntry,
        auth: GitHubAppTokenAuth,
        private_key: str,
        op: Operation,
        args: Mapping[str, Any],
        *,
        write: bool,
    ) -> Made:
        key_pem = pem(private_key)
        in_path = {p.name for p in op.path_params}

        def named(param: str) -> str | None:
            value = args.get(param) if param in in_path else None
            return str(value) if value is not None and str(value) else None

        owner, repo, org = named(auth.owner_param), named(auth.repo_param), named(auth.org_param)
        installation = auth.installation_id
        if owner or org:
            installation = await self._installation(entry, auth, key_pem, owner, repo, org)
        if installation is None:
            raise SetupProblem(
                "this operation names no repository or organisation, so librarian cannot tell "
                "which installation of the GitHub App to use: set installation_id in its "
                "authentication"
            )
        only = repo if owner and repo else None
        key = ("github", entry.id, auth.app_id, installation, only, write, _mark(key_pem))

        async def make() -> tuple[str, datetime]:
            return await self._github_token(entry, auth, key_pem, installation, only, write)

        token = await self._tokens.get(key, make)
        where = f"{owner}/{repo}" if only else (org or owner or f"installation {installation}")
        note = f"installation token for {where}, {'as installed' if write else 'read-only'}"
        return Made(token, note, lambda: self._tokens.forget(key))

    def _as_the_app(self, auth: GitHubAppTokenAuth, key_pem: str) -> dict[str, str]:
        try:
            token = app_jwt(auth.app_id, key_pem)
        except GitHubAppError:
            # Its message names the key's defect in the library's words: not passed on.
            raise SecretRejected("the GitHub App's private key is not usable") from None
        return {**GITHUB_HEADERS, "Authorization": f"Bearer {token}"}

    async def _github(
        self,
        entry: ApiEntry,
        auth: GitHubAppTokenAuth,
        key_pem: str,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        response = await self._ask(
            entry,
            f"{entry.name} (as the GitHub App)",
            method,
            entry.base_url + path,
            headers=self._as_the_app(auth, key_pem),
            **kwargs,
        )
        if response.status_code == 401:
            raise SecretRejected(
                "GitHub refused the App's private key (HTTP 401): check app_id and the key"
            )
        return response

    async def _installation(
        self,
        entry: ApiEntry,
        auth: GitHubAppTokenAuth,
        key_pem: str,
        owner: str | None,
        repo: str | None,
        org: str | None,
    ) -> int:
        if owner and repo:
            paths = [f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/installation"]
            where = f"{owner}/{repo}"
        else:
            account = org or owner or ""
            safe = quote(account, safe="")
            paths = [f"/orgs/{safe}/installation", f"/users/{safe}/installation"]
            where = account

        async def find() -> tuple[int, datetime]:
            for path in paths:
                response = await self._github(entry, auth, key_pem, "GET", path)
                if response.status_code == 200:
                    return int(response.json()["id"]), self._clock() + FACTS_LAST + MARGIN
                if response.status_code != 404:
                    raise SetupProblem(
                        f"GitHub would not say where the App is installed "
                        f"(HTTP {response.status_code}{_said(response)})"
                    )
            raise SetupProblem(f"the GitHub App is not installed on {where}")

        key = ("installation", entry.id, auth.app_id, tuple(paths), _mark(key_pem))
        return int(await self._facts.get(key, find))

    async def _read_only(
        self, entry: ApiEntry, auth: GitHubAppTokenAuth, key_pem: str, installation: int
    ) -> dict[str, str]:
        """What the installation may do, with every permission turned down to "read"."""

        async def look() -> tuple[dict[str, str], datetime]:
            response = await self._github(
                entry, auth, key_pem, "GET", f"/app/installations/{installation}"
            )
            if response.status_code != 200:
                raise SetupProblem(
                    f"GitHub would not describe the App's installation "
                    f"(HTTP {response.status_code}{_said(response)})"
                )
            granted: Any = response.json().get("permissions") or {}
            names = [str(name) for name in cast(dict[str, Any], granted)]
            return (
                {name: "read" for name in names if name not in WRITE_ONLY_PERMISSIONS},
                self._clock() + FACTS_LAST + MARGIN,
            )

        key = ("permissions", entry.id, auth.app_id, installation, _mark(key_pem))
        return dict(await self._facts.get(key, look))

    async def _github_token(
        self,
        entry: ApiEntry,
        auth: GitHubAppTokenAuth,
        key_pem: str,
        installation: int,
        only: str | None,
        write: bool,
    ) -> tuple[str, datetime]:
        body: dict[str, Any] = {}
        if only:
            body["repositories"] = [only]  # this repository, not all the App can reach
        if write:
            if auth.permissions:
                body["permissions"] = dict(auth.permissions)
        else:
            # A read never gets a token that could write, whatever the entry asks for.
            wanted = auth.permissions or await self._read_only(entry, auth, key_pem, installation)
            body["permissions"] = {
                name: "read" for name in wanted if name not in WRITE_ONLY_PERMISSIONS
            }
            if not body["permissions"]:
                raise SetupProblem("the GitHub App has no permission a read could use")
        response = await self._github(
            entry,
            auth,
            key_pem,
            "POST",
            f"/app/installations/{installation}/access_tokens",
            json=body,
        )
        if response.status_code != 201:
            raise SetupProblem(
                f"GitHub did not issue a token (HTTP {response.status_code}{_said(response)})"
            )
        answer: Any = response.json()
        token = answer.get("token") if isinstance(answer, dict) else None
        if not isinstance(token, str) or not token:
            raise SetupProblem("GitHub's answer held no token")
        try:
            expires = datetime.fromisoformat(str(answer.get("expires_at")).replace("Z", "+00:00"))
        except ValueError:
            expires = self._clock() + timedelta(minutes=10)
        return token, min(expires, self._clock() + timedelta(hours=1))

    # -- AWS ------------------------------------------------------------------------------

    async def aws_signer(self, entry: ApiEntry, auth: AwsSigV4Auth) -> AwsSigner:
        if auth.role_arn is None:
            credentials = await asyncio.to_thread(_own_aws_credentials)
            return AwsSigner(
                credentials, auth.service, auth.region, "signed with librarian's own AWS identity"
            )

        async def assume() -> tuple[Any, datetime]:
            return await asyncio.to_thread(_assume_role, entry, auth)

        key = ("aws", entry.id, auth.role_arn, auth.external_id, auth.region)
        credentials = await self._aws.get(key, assume)
        return AwsSigner(
            credentials, auth.service, auth.region, f"signed as {auth.role_arn.rsplit('/', 1)[-1]}"
        )


def _boto3() -> Any:
    try:
        import boto3  # pyright: ignore[reportMissingTypeStubs]
    except ImportError:
        raise SetupProblem(
            "AWS request signing needs the aws extra (pip install 'librarian-a2a[aws]')"
        ) from None
    return boto3


def _own_aws_credentials() -> Any:
    credentials = _boto3().Session().get_credentials()
    if credentials is None:
        raise SetupProblem(
            "librarian has no AWS identity to sign with (no role for its pod, no credentials "
            "in its environment)"
        )
    return credentials.get_frozen_credentials()


def _assume_role(entry: ApiEntry, auth: AwsSigV4Auth) -> tuple[Any, datetime]:
    from botocore.credentials import (  # pyright: ignore[reportMissingTypeStubs]
        ReadOnlyCredentials,
    )
    from botocore.exceptions import (  # pyright: ignore[reportMissingTypeStubs]
        BotoCoreError,
        ClientError,
    )

    request: dict[str, Any] = {
        "RoleArn": auth.role_arn,
        "RoleSessionName": re.sub(r"[^\w+=,.@-]", "-", f"librarian-{entry.id}")[:64],
        "DurationSeconds": 900,  # the shortest AWS allows
    }
    if auth.external_id:
        request["ExternalId"] = auth.external_id
    try:
        sts = _boto3().Session().client("sts", region_name=auth.region)
        made = sts.assume_role(**request)["Credentials"]
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", "refused"))  # pyright: ignore[reportUnknownMemberType]
        raise SetupProblem(f"librarian may not assume {auth.role_arn} ({code})") from None
    except BotoCoreError as exc:
        raise SetupProblem(
            f"librarian could not ask AWS for the role's credentials ({type(exc).__name__})"
        ) from None
    credentials = ReadOnlyCredentials(
        made["AccessKeyId"], made["SecretAccessKey"], made["SessionToken"]
    )
    expires: Any = made["Expiration"]
    if not isinstance(expires, datetime):
        expires = datetime.fromisoformat(json.dumps(expires).strip('"').replace("Z", "+00:00"))
    return credentials, expires
