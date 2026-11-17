from __future__ import annotations

import asyncio
import json
import socket
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from librarian.config import Settings
from librarian.credentials import CredentialRequest, RequestState
from librarian.net import PUBLIC, BlockedAddress, NetworkPolicy, check_url, host_pattern_ok, resolve
from librarian.notifiers import (
    SlackNotifier,
    WebhookNotifier,
    card_blocks,
    mention,
    own,
    slack_payload,
)


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


PRIVATE = NetworkPolicy(allow_private=True)


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
    # The thread reply says what happened, rather than repeating the card's headline.
    assert calls[3][1]["text"] == (
        ":white_check_mark: Done by sam: new key added; tested OK. 2 waiting request(s) resumed."
    )


def test_thread_replies_say_what_happened() -> None:
    from librarian.approvals import ApprovalState, WriteApproval
    from librarian.calls import WriteRequest
    from librarian.notifiers import resolution_reply, write_reply

    approval = WriteApproval(
        fingerprint="f",
        api="tracker",
        api_name="Tracker",
        host="t.test",
        operation="addComment",
        method="POST",
        path="/c",
        request=WriteRequest(api="tracker", operation="addComment"),
        agent="bot",
        expires_at=datetime.now(UTC),
    )
    approval.state, approval.result, approval.decision = (
        ApprovalState.EXECUTED,
        {"status_code": 201.0},
        "grant",
    )
    assert write_reply(approval) == (
        ":white_check_mark: Done: Tracker answered HTTP 201. "
        "Similar writes are now allowed by a grant."
    )
    approval.state, approval.decided_by, approval.note = ApprovalState.FAILED, "kim", "HTTP 409"
    assert write_reply(approval) == ":x: Approved by kim, but the write failed: HTTP 409"
    approval.state = ApprovalState.DENIED
    assert "Nothing was sent" in write_reply(approval)
    approval.state = ApprovalState.EXECUTING  # rendering any state must not fail
    assert write_reply(approval)
    request = make_request(state=RequestState.DENIED, resolved_by="sam")
    assert (
        resolution_reply(request) == ":no_entry: Denied by sam. Agents asking for Jira are told no."
    )


async def test_urgent_api_change_notices_mention_the_group() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "channel": "C1", "ts": "1.2"})

    notifier = SlackNotifier(
        "xoxb-test", "#api", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    await notifier.api_changed("jira", "Jira changed", ["x"], None, "#jira")
    await notifier.api_changed("jira", "Jira changed", ["x"], None, "#jira", "S0APPROVE1")
    assert not sent[0]["text"].startswith("<!subteam")
    assert sent[1]["text"].startswith("<!subteam^S0APPROVE1> ")


HOSTILE = (
    "<!channel> approved by security: <https://evil.example/approve|Open in librarian> & <@U999>"
)
AS_TEXT = (
    "&lt;!channel&gt; approved by security: &lt;https://evil.example/approve|Open in "
    "librarian&gt; &amp; &lt;@U999&gt;"
)


async def test_nothing_an_agent_or_an_api_wrote_becomes_slack_markup() -> None:
    """Slack reads "<...>" in a message as markup: a mention that pings a whole channel, a
    link with any words over it. A card asks a person to approve something, so what the
    agent wrote into it has to stay text. librarian's own mentions and links stay markup."""
    from librarian.approvals import WriteApproval
    from librarian.calls import WriteRequest

    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("users.lookupByEmail"):
            return httpx.Response(200, json={"ok": True, "user": {"id": "U777"}})
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "channel": "C1", "ts": "1.2"})

    notifier = SlackNotifier(
        "xoxb-test", "#writes", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    approval = WriteApproval(
        fingerprint="f",
        api="tracker",
        api_name="Tracker <!here>",
        host="t.test",
        operation="addComment",
        method="POST",
        path="/issues/{key}/comments",
        request=WriteRequest(
            api="tracker", operation="addComment", args={"key": "`<!channel>`"}, reason=HOSTILE
        ),
        agent="bot",
        claimed_for="<!everyone> kim",
        requesters=["kim@example.com"],
        approver_group="S0APPROVE1",
        expires_at=datetime.now(UTC),
    )
    link = "https://lib.test/approvals/abc?t=tok&next=1"
    await notifier.write_opened(approval, link)
    await notifier.auto_write_used(approval, 1, 5, link)
    await notifier.api_changed("tracker", HOSTILE, [HOSTILE], link, None, "S0APPROVE1")
    card, auto, changed = (json.dumps(body, ensure_ascii=False) for body in sent)

    for body in (card, auto, changed):  # what an agent or an API wrote is text...
        for markup in ("<!channel>", "<!here>", "<!everyone>", "<https://evil", "<@U999>"):
            assert markup not in body
        assert "\x00" not in body and "\\u0000" not in body  # no mark of librarian's own is left
    assert AS_TEXT in card and AS_TEXT in changed
    assert "the agent's platform names &lt;!everyone&gt; kim, unverified" in card
    assert "Tracker &lt;!here&gt;" in card and "Tracker &lt;!here&gt;" in auto
    # ...and what librarian wrote is markup still: the requester and the group are pinged,
    # its links are links, and the address behind a button is as it was.
    assert "<@U777>" in card and "<!subteam^S0APPROVE1>" in card
    assert f"<{link}|Require approval again>" in auto
    assert f"<{link}|Review the proposed update>" in changed
    assert changed.count("<!subteam^S0APPROVE1>") == 1
    assert f'"url": "{link}"' in card


def test_only_what_librarian_marked_is_left_as_markup() -> None:
    made = slack_payload(
        {
            "text": f"{own('<@U1>')} says <b> & {own('<https://lib.test|open>')}",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": f"a < b {own('<!here>')}"}},
                {"type": "button", "text": {"type": "plain_text", "text": "A < B"}, "url": "u?a&b"},
            ],
        }
    )
    assert made == {
        "text": "<@U1> says &lt;b&gt; &amp; <https://lib.test|open>",
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "a &lt; b <!here>"}},
            {"type": "button", "text": {"type": "plain_text", "text": "A < B"}, "url": "u?a&b"},
        ],
    }
    # A mark with no partner (a text cut short) opens nothing.
    cut = own("<!channel>")[:-5]
    assert slack_payload({"text": f"x {cut}"})["text"].endswith("&lt;!channel&gt;")


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
        await check_url(url)
    await check_url(url, PRIVATE)  # explicitly allowed


async def test_public_literal_allowed_and_schemes_checked() -> None:
    assert await resolve("93.184.215.14") == ["93.184.215.14"]
    with pytest.raises(BlockedAddress):
        await check_url("file:///etc/passwd", PRIVATE)


def test_internal_host_patterns() -> None:
    policy = NetworkPolicy(internal_hosts=("*.internal.example.test", "api.corp.test"))
    assert policy.internal_pattern("jira.internal.example.test") == "*.internal.example.test"
    assert policy.internal_pattern("a.b.internal.example.test") == "*.internal.example.test"
    assert policy.internal_pattern("API.CORP.TEST.") == "api.corp.test"  # case, trailing dot
    assert policy.internal_pattern("internal.example.test") is None  # the wildcard needs a label
    assert policy.internal_pattern("jira.internal.example.test.attacker.test") is None
    assert policy.internal_pattern("xapi.corp.test") is None
    assert PUBLIC.internal_pattern("jira.internal.example.test") is None
    for bad in ("*", "*.", "", "http://x.test", "a b", "x/y", "*.*"):
        assert not host_pattern_ok(bad), bad
    assert host_pattern_ok("*.internal.example.test") and host_pattern_ok("api-1.corp.test")


def fake_dns(answers: dict[str, list[str]]) -> Any:
    async def getaddrinfo(host: str, port: Any, *args: Any, **kwargs: Any) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0)) for a in answers[host]]

    return getaddrinfo


async def test_internal_hosts_allow_private_but_never_loopback_or_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = NetworkPolicy(internal_hosts=("*.internal.example.test",))
    answers = {
        "jira.internal.example.test": ["10.1.2.3"],
        "pub.internal.example.test": ["93.184.215.14"],
        "meta.internal.example.test": ["169.254.169.254"],
        "lo.internal.example.test": ["127.0.0.1"],
        "mapped.internal.example.test": ["::ffff:127.0.0.1"],
        "mixed.internal.example.test": ["10.1.2.3", "169.254.169.254"],
        "other.example.test": ["10.1.2.3"],
    }
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake_dns(answers))
    assert await resolve("jira.internal.example.test", policy) == ["10.1.2.3"]
    assert await resolve("pub.internal.example.test", policy) == ["93.184.215.14"]
    for host in ("meta", "lo", "mapped", "mixed"):
        with pytest.raises(BlockedAddress, match="disallowed"):
            await resolve(f"{host}.internal.example.test", policy)
    with pytest.raises(BlockedAddress, match="non-public"):
        await resolve("other.example.test", policy)  # not listed: public only, as before
    with pytest.raises(BlockedAddress, match="non-public"):
        await resolve("jira.internal.example.test")  # PUBLIC policy ignores the listing
    pinned = await check_url("https://jira.internal.example.test/x", policy)
    assert pinned is None


def test_internal_hosts_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="allow_private_networks"):
        Settings(internal_hosts=["*"])
    with pytest.raises(ValueError, match="hostname pattern"):
        Settings(internal_hosts=["https://x.test"])
    settings = Settings(internal_hosts=[" *.Internal.Example.com "])
    assert settings.network == NetworkPolicy(False, ("*.internal.example.com",))
    monkeypatch.setenv("LIBRARIAN_INTERNAL_HOSTS", '["*.corp.test"]')
    assert Settings().network.internal_hosts == ("*.corp.test",)
    assert Settings(allow_private_networks=True).network.allow_private


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


async def test_pin_formats_ipv6_and_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    from librarian import net

    async def fake_resolve(host: str, policy: NetworkPolicy = PUBLIC) -> list[str]:
        return ["2606:2800:21f:cb07:6820:80da:af6b:8b2c"]

    monkeypatch.setattr(net, "resolve", fake_resolve)
    target = await net.pin("https://api.example.test:8443/v1/x?q=1")
    assert target.url == "https://[2606:2800:21f:cb07:6820:80da:af6b:8b2c]:8443/v1/x?q=1"
    assert target.host_header == "api.example.test:8443"
    assert target.extensions == {"sni_hostname": "api.example.test"}
    plain = await net.pin("http://api.example.test/x")
    assert plain.extensions == {}
