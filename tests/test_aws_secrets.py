from __future__ import annotations

import json
from typing import Any

import pytest
from botocore.exceptions import ClientError  # pyright: ignore[reportMissingTypeStubs]

from librarian.secrets import AwsSecretsManagerStore, SecretStoreError


def not_found(op: str) -> ClientError:
    return ClientError({"Error": {"Code": "ResourceNotFoundException"}}, op)


class FakeSecretsManager:
    """Just enough of the boto3 secretsmanager client."""

    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        self.secrets = dict(secrets or {})
        self.gets = 0
        self.created: list[dict[str, Any]] = []
        self.denied = False

    def get_secret_value(self, SecretId: str) -> dict[str, Any]:
        self.gets += 1
        if self.denied:
            raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "GetSecretValue")
        if SecretId not in self.secrets:
            raise not_found("GetSecretValue")
        return {"SecretString": self.secrets[SecretId]}

    def put_secret_value(self, SecretId: str, SecretString: str) -> None:
        if SecretId not in self.secrets:
            raise not_found("PutSecretValue")
        self.secrets[SecretId] = SecretString

    def create_secret(self, Name: str, SecretString: str, **kwargs: Any) -> None:
        self.created.append({"Name": Name, **kwargs})
        self.secrets[Name] = SecretString

    def get_paginator(self, name: str) -> Any:
        assert name == "list_secrets"
        secrets = self.secrets

        class Paginator:
            def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
                prefix = kwargs.get("Filters", [{"Values": [""]}])[0]["Values"][0]
                names = [{"Name": n} for n in secrets if n.startswith(prefix)]
                return [{"SecretList": names[:1]}, {"SecretList": names[1:]}]

        return Paginator()


async def test_plain_and_json_key_secrets() -> None:
    fake = FakeSecretsManager(
        {
            "agents/jira/readonly": "plain-token",
            "agents/service/jira": json.dumps({"api_token": "json-token", "empty": ""}),
        }
    )
    store = AwsSecretsManagerStore(prefix="agents/", client=fake)
    assert await store.get("jira/readonly") == "plain-token"
    assert await store.get("service/jira#api_token") == "json-token"
    assert await store.get("service/jira#missing") is None
    assert await store.get("service/jira#empty") is None
    assert await store.get("nope/missing") is None


async def test_values_are_cached_and_writes_invalidate() -> None:
    fake = FakeSecretsManager({"jira/readonly": "v1"})
    store = AwsSecretsManagerStore(client=fake, cache_seconds=60)
    assert await store.get("jira/readonly") == "v1"
    assert await store.get("jira/readonly") == "v1"
    assert fake.gets == 1
    await store.put("jira/readonly", "v2")
    assert await store.get("jira/readonly") == "v2"


async def test_put_creates_missing_secrets_and_merges_json_keys() -> None:
    fake = FakeSecretsManager({"service/jira": json.dumps({"other": "keep"})})
    store = AwsSecretsManagerStore(client=fake)
    await store.put("new/secret", "abc")
    assert fake.secrets["new/secret"] == "abc"
    assert fake.created[0]["Tags"] == [{"Key": "managed-by", "Value": "librarian"}]
    await store.put("service/jira#api_token", "tok")
    assert json.loads(fake.secrets["service/jira"]) == {"other": "keep", "api_token": "tok"}


async def test_list_names_strips_prefix_and_paginates() -> None:
    fake = FakeSecretsManager({"agents/a/one": "1", "agents/b/two": "2", "other/x": "3"})
    store = AwsSecretsManagerStore(prefix="agents/", client=fake)
    assert await store.list_names() == ["a/one", "b/two"]


async def test_access_denied_is_an_error_not_a_missing_secret() -> None:
    fake = FakeSecretsManager({"jira/readonly": "v"})
    fake.denied = True
    store = AwsSecretsManagerStore(client=fake)
    with pytest.raises(SecretStoreError, match="AccessDenied"):
        await store.get("jira/readonly")


async def test_put_if_absent_first_writer_wins() -> None:
    fake = FakeSecretsManager()

    def create_secret(Name: str, SecretString: str, **kwargs: Any) -> None:
        if Name in fake.secrets:
            raise ClientError({"Error": {"Code": "ResourceExistsException"}}, "CreateSecret")
        fake.secrets[Name] = SecretString

    fake.create_secret = create_secret  # type: ignore[method-assign]
    store = AwsSecretsManagerStore(client=fake, cache_seconds=0)
    assert await store.put_if_absent("librarian/signing-key", "first") == "first"
    assert await store.put_if_absent("librarian/signing-key", "second") == "first"
