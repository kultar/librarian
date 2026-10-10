from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from librarian.config import Settings
from librarian.credentials import CredentialRequest, RequestState
from librarian.net import BlockedAddress, check_url, resolve_public
from librarian.notifiers import SlackNotifier, WebhookNotifier, card_blocks, mention


def make_request(**overrides: Any) -> CredentialRequest:
    values: dict[str, Any] = {
        "api": "jira",
        "api_name": "Jira",
        "host": "acme.atlassian.net",
        "secret_ref": "jira/readonly",
        "auth_type": "basic",
        "reason": "missing",
        "scopes": ["read:jira-work"],
        "requested_by": ["release-notes-agent (for kim)"],
        "waiting": 2,
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
        "approver_group": "S0APPROVE1",
    }
    values.update(overrides)
    return CredentialRequest.model_validate(values)


async def test_slack_posts_card_then_updates_and_threads() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append((request.url.path.rsplit("/", 1)[-1], body))
        assert request.headers["authorization"] == "Bearer xoxb-test"
        return httpx.Response(200, json={"ok": True, "channel": "C1", "ts": "123.456"})

    notifier = SlackNotifier(
        "xoxb-test", "#api-access", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    request = make_request()
    await notifier.opened(request, "https://lib.test/credentials/abc?t=tok")
    assert request.notifier_ref == {"channel": "C1", "ts": "123.456"}
    method, body = calls[0]
    assert method == "chat.postMessage" and body["channel"] == "#api-access"
    rendered = json.dumps(body["blocks"])
    assert "Review request" in rendered and "<!subteam^S0APPROVE1>" in rendered

    await notifier.updated(request)
    assert calls[1][0] == "chat.update"

    request.state = RequestState.FULFILLED
    request.resolved_by = "sam"
    request.resolution_note = "new key added; tested OK"
    await notifier.closed(request)
    assert [c[0] for c in calls[2:]] == ["chat.update", "chat.postMessage"]
    assert "Review request" not in json.dumps(calls[2][1]["blocks"])  # button removed
    assert calls[3][1]["thread_ts"] == "123.456"
    assert "sam" in calls[3][1]["text"]


def test_card_never_contains_secret_material() -> None:
    blocks = card_blocks(make_request(), "https://lib.test/credentials/abc?t=tok")
    text = json.dumps(blocks)
    assert "jira/readonly" in text  # the reference is fine
    assert mention("@api-approvers") == "@api-approvers"
    assert mention(None) == ""


async def test_webhook_payload() -> None:
    received: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(json.loads(request.content))
        return httpx.Response(204)

    notifier = WebhookNotifier(
        "https://hooks.test/x", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    await notifier.opened(make_request(), "https://lib.test/link")
    assert received[0]["event"] == "credential_request.opened"
    assert received[0]["link"] == "https://lib.test/link"
    assert "notifier_ref" not in received[0]["request"]


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://10.1.2.3/x",
        "http://169.254.169.254/latest",
        "http://[::1]/x",
        "http://[::ffff:127.0.0.1]/x",
        "http://0.0.0.0/x",
    ],
)
async def test_private_addresses_blocked(url: str) -> None:
    with pytest.raises(BlockedAddress):
        await check_url(url, allow_private=False)
    await check_url(url, allow_private=True)  # explicitly allowed


async def test_public_literal_allowed_and_schemes_checked() -> None:
    assert await resolve_public("93.184.215.14") == ["93.184.215.14"]
    with pytest.raises(BlockedAddress):
        await check_url("file:///etc/passwd", allow_private=True)


def test_settings_from_yaml_and_env(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "librarian.yaml"
    config.write_text(
        "port: 9000\nmodel: anthropic:claude-sonnet-5-5\n"
        "notifier:\n  type: slack\n  slack:\n    channel: '#x'\n"
    )
    monkeypatch.setenv("LIBRARIAN_PORT", "9100")
    monkeypatch.setenv("LIBRARIAN_NOTIFIER__SLACK__BOT_TOKEN", "xoxb-env")
    settings = Settings.load(config)
    assert settings.port == 9100  # env wins
    assert settings.model == "anthropic:claude-sonnet-5-5"
    assert settings.notifier.type == "slack"
    assert settings.notifier.slack.bot_token is not None
    assert settings.notifier.slack.bot_token.get_secret_value() == "xoxb-env"
    assert "xoxb-env" not in repr(settings)
