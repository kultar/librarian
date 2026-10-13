"""Notifiers that tell humans a credential is needed.

Slack is the default. The Slack card carries a single "Review request" link to
the secure page; the key itself is never typed into Slack (Slack inputs cannot
be masked and would pass through Slack's servers).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from librarian.approvals import ApprovalState, Grant, WriteApproval
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

    async def _post_write(self, event: str, payload: dict[str, Any]) -> None:
        try:
            response = await self.client.post(self.url, json={"event": event, **payload})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.error("webhook notification failed: %s", exc)

    async def write_opened(self, approval: WriteApproval, link: str) -> None:
        data = approval.model_dump(mode="json", exclude={"notifier_ref", "result"})
        await self._post_write("write_approval.opened", {"approval": data, "link": link})

    async def write_closed(self, approval: WriteApproval) -> None:
        data = approval.model_dump(mode="json", exclude={"notifier_ref", "result"})
        await self._post_write("write_approval.closed", {"approval": data})

    async def api_changed(
        self,
        api: str,
        title: str,
        details: list[str],
        link: str | None,
        channel: str | None,
        mention_group: str | None = None,
    ) -> None:
        await self._post_write(
            "api.changed", {"api": api, "title": title, "details": details, "link": link}
        )

    async def api_learned(
        self,
        api: str,
        name: str,
        learned_by: str,
        summary: str,
        link: str | None,
        channel: str | None,
        warnings: list[str],
        error: str | None = None,
    ) -> None:
        await self._post_write(
            "api.learned",
            {
                "api": api,
                "name": name,
                "learned_by": learned_by,
                "summary": summary,
                "link": link,
                "warnings": warnings,
                "error": error,
            },
        )

    async def review_opened(self, review: Any, link: str) -> None:
        await self._post_write(
            "review.opened", {"review": review.model_dump(mode="json"), "link": link}
        )

    async def review_closed(self, review: Any) -> None:
        await self._post_write("review.closed", {"review": review.model_dump(mode="json")})

    async def consent_opened(self, request: Any, link: str) -> None:
        data = request.model_dump(mode="json", exclude={"code_verifier"})
        await self._post_write("consent.opened", {"request": data, "link": link})

    async def consent_closed(self, request: Any) -> None:
        data = request.model_dump(mode="json", exclude={"code_verifier"})
        await self._post_write("consent.closed", {"request": data})

    async def grant_used(self, grant: Grant, approval_like: WriteApproval) -> None:
        await self._post_write(
            "grant.used",
            {
                "grant": grant.model_dump(mode="json"),
                "request": approval_like.request.model_dump(mode="json"),
            },
        )


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
        self._user_ids: dict[str, str | None] = {}

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
                "text": resolution_reply(request),
            },
        )

    async def _user_mention(self, email: str) -> str:
        """`<@U123>` for a workspace member's email (needs users:read.email), else the email."""
        if "@" not in email:
            return email
        if email not in self._user_ids:
            try:
                response = await self.client.get(
                    f"{self.api_base}/users.lookupByEmail",
                    params={"email": email},
                    headers={"Authorization": f"Bearer {self.bot_token}"},
                )
                data: dict[str, Any] = response.json()
                user: Any = data.get("user")
                user_id = user.get("id") if data.get("ok") and isinstance(user, dict) else None
                self._user_ids[email] = str(user_id) if user_id else None  # pyright: ignore[reportUnknownArgumentType]
            except (httpx.HTTPError, ValueError):
                self._user_ids[email] = None
        user_id = self._user_ids[email]
        return f"<@{user_id}>" if user_id else email

    async def write_opened(self, approval: WriteApproval, link: str) -> None:
        self._links[approval.id] = link
        people = [await self._user_mention(who) for who in approval.requesters]
        data = await self._call(
            "chat.postMessage",
            {
                "channel": approval.channel or self.default_channel,
                "text": f"Write approval needed: {approval.api_name} {approval.operation}",
                "blocks": write_blocks(approval, link, people),
            },
        )
        if data.get("ok"):
            approval.notifier_ref = {"channel": str(data["channel"]), "ts": str(data["ts"])}

    async def write_closed(self, approval: WriteApproval) -> None:
        ref = approval.notifier_ref
        self._links.pop(approval.id, None)
        if not ref:
            return
        await self._call(
            "chat.update",
            {
                "channel": ref["channel"],
                "ts": ref["ts"],
                "text": f"{approval.api_name} {approval.operation}: {approval.state}",
                "blocks": write_blocks(approval, None, []),
            },
        )
        await self._call(
            "chat.postMessage",
            {
                "channel": ref["channel"],
                "thread_ts": ref["ts"],
                "text": write_reply(approval),
            },
        )

    async def grant_used(self, grant: Grant, approval_like: WriteApproval) -> None:
        if not grant.notify_owner:
            return
        uses = f"{grant.uses}" + (f"/{grant.max_uses}" if grant.max_uses else "")
        await self._call(
            "chat.postMessage",
            {
                "channel": approval_like.channel or self.default_channel,
                "text": (
                    f":robot_face: `{grant.agent}` ran `{grant.operation}` on "
                    f"{approval_like.api_name} under a standing grant (use {uses}). Args: "
                    f"`{json.dumps(approval_like.request.args)[:300]}`"
                ),
            },
        )

    async def api_changed(
        self,
        api: str,
        title: str,
        details: list[str],
        link: str | None,
        channel: str | None,
        mention_group: str | None = None,
    ) -> None:
        text = _api_changed_text(api, title, details, link)
        if mention_group:
            text = f"{mention(mention_group)} {text}"
        await self._call(
            "chat.postMessage", {"channel": channel or self.default_channel, "text": text}
        )

    async def api_learned(
        self,
        api: str,
        name: str,
        learned_by: str,
        summary: str,
        link: str | None,
        channel: str | None,
        warnings: list[str],
        error: str | None = None,
    ) -> None:
        await self._call(
            "chat.postMessage",
            {
                "channel": channel or self.default_channel,
                "text": api_learned_text(api, name, learned_by, summary, link, warnings, error),
            },
        )

    async def review_opened(self, review: Any, link: str) -> None:
        self._links[review.id] = link
        data = await self._call(
            "chat.postMessage",
            {
                "channel": review.channel or self.default_channel,
                "text": f"New API to review: {review.api_name}",
                "blocks": review_blocks(review, link),
            },
        )
        if data.get("ok"):
            review.notifier_ref = {"channel": str(data["channel"]), "ts": str(data["ts"])}

    async def review_closed(self, review: Any) -> None:
        ref = review.notifier_ref
        if not ref:
            return
        await self._call(
            "chat.update",
            {
                "channel": ref["channel"],
                "ts": ref["ts"],
                "text": review_outcome_text(review),
                "blocks": review_blocks(review, None),
            },
        )
        await self._call(
            "chat.postMessage",
            {
                "channel": ref["channel"],
                "thread_ts": ref["ts"],
                "text": review_outcome_text(review),
            },
        )

    async def consent_opened(self, request: Any, link: str) -> None:
        """A direct message to the person; the channel (with a mention) when Slack does
        not know their address."""
        user = await self._user_id(request.principal)
        text = (
            f":key: *{request.api_name}* needs you to sign in, so that *{request.agent}* can "
            f"act as you there. <{link}|Sign in to {request.api_name}> (one-time link, "
            f"{int(CONSENT_MINUTES)} minutes). Only what you can see is shared; agents never "
            "see your token."
        )
        target = user or self.default_channel
        if user is None:
            text = f"{await self._user_mention(request.principal)} {text}"
        await self._call("chat.postMessage", {"channel": target, "text": text})

    async def consent_closed(self, request: Any) -> None:
        user = await self._user_id(request.principal)
        if user is None or str(request.state) == "pending":
            return
        done = {"connected": ":white_check_mark: Signed in", "denied": ":no_entry: Not signed in"}
        text = done.get(str(request.state), f":hourglass: {request.state}")
        await self._call(
            "chat.postMessage",
            {"channel": user, "text": f"{text} to {request.api_name}; your agent carries on."},
        )

    async def _user_id(self, email: str) -> str | None:
        mention = await self._user_mention(email)
        return mention[2:-1] if mention.startswith("<@") else None


CONSENT_MINUTES = 30


def review_outcome_text(review: Any) -> str:
    icon = {"approved": ":white_check_mark:", "denied": ":no_entry:"}.get(
        str(review.state), ":pencil2:"
    )
    who = f" by {review.decided_by}" if review.decided_by else ""
    note = f" · {review.note}" if review.note else ""
    return f"{icon} {review.api_name} review {review.state}{who}{note}"


def review_blocks(review: Any, link: str | None) -> list[dict[str, Any]]:
    """Block Kit card for a learned API awaiting approval."""
    pending = str(review.state) == "pending"
    title = (
        f":books: New API to review: {review.api_name}" if pending else review_outcome_text(review)
    )
    fields = [
        f"*Site:*\n{review.host}",
        f"*Learned by:*\n{review.learned_by}",
        f"*What:*\n{review.summary}",
        f"*Authentication:*\n{review.auth}",
    ]
    if review.needs_review:
        fields.append(f"*Need a decision:*\n{len(review.needs_review)} operation(s)")
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*"}},
        {"type": "section", "fields": [{"type": "mrkdwn", "text": f} for f in fields[:10]]},
    ]
    for warning in list(review.warnings)[:3]:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": f":warning: {warning}"}]}
        )
    if pending and link:
        group = mention(review.approver_group)
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (f"{group} " if group else "")
                    + "Approve merges the proposal as the App and the API becomes verified; "
                    "deny closes it and drops the API.",
                },
                "accessory": {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Review"},
                    "url": link,
                    "style": "primary",
                },
            }
        )
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"<{review.review_url}|The pull request>"}],
        }
    )
    return blocks


def api_learned_text(
    api: str,
    name: str,
    learned_by: str,
    summary: str,
    link: str | None,
    warnings: list[str],
    error: str | None,
) -> str:
    lines = [f":books: *{name}* (`{api}`) learned by {learned_by}: {summary}"]
    lines += [f":warning: {w}" for w in warnings]
    if link:
        lines.append(f"<{link}|Review the entry> (merging it makes the API verified)")
    if error:
        lines.append(f":x: No review was opened: {error}. The entry is usable as provisional.")
    return "\n".join(lines)


def _api_changed_text(api: str, title: str, details: list[str], link: str | None) -> str:
    lines = [f":warning: *{title}* (`{api}`)", *[f"• {d}" for d in details[:10]]]
    if len(details) > 10:
        lines.append(f"• ... and {len(details) - 10} more")
    if link:
        lines.append(f"<{link}|Review the proposed update>")
    return "\n".join(lines)


def write_outcome_text(approval: WriteApproval) -> str:
    icon = {
        ApprovalState.EXECUTED: ":white_check_mark:",
        ApprovalState.FAILED: ":x:",
        ApprovalState.DENIED: ":no_entry:",
        ApprovalState.EXPIRED: ":hourglass:",
        ApprovalState.APPROVED: ":ballot_box_with_check:",
        ApprovalState.EXECUTING: ":hourglass_flowing_sand:",
        ApprovalState.PENDING: ":pencil2:",
    }[approval.state]
    who = f" by {approval.decided_by}" if approval.decided_by else ""
    remembered = " (similar writes allowed by a new grant)" if approval.decision == "grant" else ""
    note = f" · {approval.note}" if approval.note else ""
    return (
        f"{icon} {approval.api_name} `{approval.operation}` {approval.state}{who}{remembered}{note}"
    )


def write_reply(approval: WriteApproval) -> str:
    """The thread reply under a write card: what actually happened, in a few words."""
    who = f" by {approval.decided_by}" if approval.decided_by else ""
    if approval.state is ApprovalState.EXECUTED:
        status = (approval.result or {}).get("status_code")
        code = f"HTTP {int(status)}" if isinstance(status, int | float) else "a response"
        grant = (
            " Similar writes are now allowed by a grant." if approval.decision == "grant" else ""
        )
        return f":white_check_mark: Done: {approval.api_name} answered {code}.{grant}"
    if approval.state is ApprovalState.FAILED:
        return f":x: Approved{who}, but the write failed: {approval.note or 'unknown error'}"
    if approval.state is ApprovalState.DENIED:
        return f":no_entry: Denied{who}. Nothing was sent."
    if approval.state is ApprovalState.EXPIRED:
        return ":hourglass: Expired without a decision. Nothing was sent."
    return write_outcome_text(approval)


def write_blocks(
    approval: WriteApproval, link: str | None, people: list[str]
) -> list[dict[str, Any]]:
    pending = approval.state is ApprovalState.PENDING
    title = (
        f":pencil2: Write approval needed: {approval.api_name}"
        if pending
        else write_outcome_text(approval)
    )
    request = approval.request
    fields = [
        f"*Operation:*\n`{approval.operation}`\n{approval.method} {approval.path}",
        f"*Agent:*\n{approval.agent}",
        f"*For:*\n{approval.principal or '(no person; autonomous)'}",
        f"*Arguments:*\n`{json.dumps(request.args)[:500]}`",
    ]
    if request.reason:
        fields.append(f"*Why:*\n{request.reason[:500]}")
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*"}},
        {"type": "section", "fields": [{"type": "mrkdwn", "text": f} for f in fields]},
    ]
    if request.body is not None:
        body = json.dumps(request.body, indent=2, default=str)
        if len(body) > 1500:
            body = body[:1500] + "\n…(truncated; full body on the review page)"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"```{body}```"}})
    if pending and link:
        who = " ".join(
            people + ([mention(approval.approver_group)] if approval.approver_group else [])
        )
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (f"{who} " if who else "")
                    + "Approve once, approve and allow similar writes, or deny.",
                },
                "accessory": {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Review write"},
                    "url": link,
                    "style": "danger",
                    "action_id": "librarian_review_write",
                },
            }
        )
    return blocks


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


def resolution_reply(request: CredentialRequest) -> str:
    """The thread reply under a credential card: the outcome, in a few words."""
    who = f" by {request.resolved_by}" if request.resolved_by else ""
    if request.state is RequestState.FULFILLED:
        note = request.resolution_note or "credential provided"
        waiting = f" {request.waiting} waiting request(s) resumed." if request.waiting else ""
        return f":white_check_mark: Done{who}: {note}.{waiting}"
    if request.state is RequestState.DENIED:
        return f":no_entry: Denied{who}. Agents asking for {request.api_name} are told no."
    if request.state is RequestState.EXPIRED:
        return ":hourglass: Expired without a decision. Agents can ask again."
    return resolution_text(request)


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
