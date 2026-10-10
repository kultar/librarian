"""Notifiers that tell humans a credential is needed.

Slack is the default. The Slack card carries a single "Review request" link to
the secure page; the key itself is never typed into Slack (Slack inputs cannot
be masked and would pass through Slack's servers).
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from librarian.credentials import CredentialRequest, Notifier, RequestState

log = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api"
_SLACK_GROUP_ID = re.compile(r"^S[A-Z0-9]{6,}$")


class WebhookNotifier(Notifier):
    """POSTs a JSON event to a URL. Use it to connect Teams, Discord, PagerDuty, ..."""

    def __init__(self, url: str, client: httpx.AsyncClient | None = None) -> None:
        self.url = url
        self.client = client or httpx.AsyncClient(timeout=10)

    async def _post(self, event: str, request: CredentialRequest, link: str | None) -> None:
        payload: dict[str, Any] = {
            "event": event,
            "request": request.model_dump(mode="json", exclude={"notifier_ref"}),
        }
        if link is not None:
            payload["link"] = link
        try:
            response = await self.client.post(self.url, json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.error("webhook notification failed: %s", exc)

    async def opened(self, request: CredentialRequest, link: str) -> None:
        await self._post("credential_request.opened", request, link)

    async def updated(self, request: CredentialRequest) -> None:
        await self._post("credential_request.updated", request, None)

    async def closed(self, request: CredentialRequest) -> None:
        await self._post("credential_request.closed", request, None)


class SlackNotifier(Notifier):
    """Posts one card per request and keeps it up to date with `chat.update`."""

    def __init__(
        self,
        bot_token: str,
        default_channel: str,
        client: httpx.AsyncClient | None = None,
        api_base: str = SLACK_API,
    ) -> None:
        self.bot_token = bot_token
        self.default_channel = default_channel
        self.client = client or httpx.AsyncClient(timeout=10)
        self.api_base = api_base
        self._links: dict[str, str] = {}

    async def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self.client.post(
                f"{self.api_base}/{method}",
                json=payload,
                headers={"Authorization": f"Bearer {self.bot_token}"},
            )
            data: dict[str, Any] = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.error("slack %s failed: %s", method, exc)
            return {"ok": False}
        if not data.get("ok"):
            log.error("slack %s error: %s", method, data.get("error"))
        return data

    async def opened(self, request: CredentialRequest, link: str) -> None:
        self._links[request.id] = link
        channel = request.channel or self.default_channel
        data = await self._call(
            "chat.postMessage",
            {
                "channel": channel,
                "text": f"Access needed: {request.api_name} (read-only)",
                "blocks": card_blocks(request, link),
            },
        )
        if data.get("ok"):
            request.notifier_ref = {"channel": str(data["channel"]), "ts": str(data["ts"])}

    async def updated(self, request: CredentialRequest) -> None:
        ref = request.notifier_ref
        if not ref:
            return
        await self._call(
            "chat.update",
            {
                "channel": ref["channel"],
                "ts": ref["ts"],
                "text": f"Access needed: {request.api_name} (read-only)",
                "blocks": card_blocks(request, self._links.get(request.id)),
            },
        )

    async def closed(self, request: CredentialRequest) -> None:
        ref = request.notifier_ref
        self._links.pop(request.id, None)
        if not ref:
            return
        await self._call(
            "chat.update",
            {
                "channel": ref["channel"],
                "ts": ref["ts"],
                "text": f"{request.api_name}: {request.state}",
                "blocks": card_blocks(request, None),
            },
        )
        await self._call(
            "chat.postMessage",
            {
                "channel": ref["channel"],
                "thread_ts": ref["ts"],
                "text": resolution_text(request),
            },
        )


def mention(group: str | None) -> str:
    if not group:
        return ""
    return f"<!subteam^{group}>" if _SLACK_GROUP_ID.match(group) else group


def resolution_text(request: CredentialRequest) -> str:
    icon = {
        RequestState.FULFILLED: ":white_check_mark:",
        RequestState.DENIED: ":no_entry:",
        RequestState.EXPIRED: ":hourglass:",
        RequestState.PENDING: ":key:",
    }[request.state]
    who = f" by {request.resolved_by}" if request.resolved_by else ""
    note = f" · {request.resolution_note}" if request.resolution_note else ""
    return f"{icon} {request.api_name} {request.state}{who}{note}"


def card_blocks(request: CredentialRequest, link: str | None) -> list[dict[str, Any]]:
    """Block Kit card for a credential request."""
    pending = request.state is RequestState.PENDING
    title = (
        f":key: {request.api_name} access needed (read-only)"
        if pending
        else resolution_text(request)
    )
    fields = [
        f"*Site:*\n{request.host}",
        f"*Credential:*\n`{request.secret_ref}` ({request.auth_type})",
        f"*Asked by:*\n{', '.join(request.requested_by[:5])}",
        f"*Waiting:*\n{request.waiting} request(s)",
    ]
    if request.scopes:
        fields.append(f"*Needs:*\n{', '.join(request.scopes)}")
    if request.reason != "missing":
        fields.append(f"*Why:*\n{request.reason}")
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*"}},
        {
            "type": "section",
            "fields": [{"type": "mrkdwn", "text": f} for f in fields[:10]],
        },
    ]
    if pending and request.instructions:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": request.instructions}]}
        )
    if pending and link:
        group = mention(request.approver_group)
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (f"{group} " if group else "")
                    + "Add a key, pick an existing secret, or deny. The key never goes"
                    " through Slack.",
                },
                "accessory": {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Review request"},
                    "url": link,
                    "style": "primary",
                    "action_id": "librarian_review",
                },
            }
        )
    return blocks
