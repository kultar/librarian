"""Notifiers that tell humans a credential is needed.

Slack is the default. The Slack card carries a single "Review request" link to
the secure page; the key itself is never typed into Slack (Slack inputs cannot
be masked and would pass through Slack's servers).
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from typing import Any, cast

import httpx

from librarian.approvals import ApprovalState, Grant, WriteApproval
from librarian.credentials import CredentialRequest, Notifier, RequestState

log = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api"

# Slack reads "<...>" in a message as markup: a mention that pings people, a link with any
# words over it. What an agent, an API or a spec wrote must never become that, so every
# text is escaped where it leaves for Slack. librarian's own markup is wrapped in a mark
# that only this process knows, and is the one thing left as it is.
_MARK = f"\x00{secrets.token_hex(8)}\x00"


def own(markup: str) -> str:
    """Slack markup librarian itself wrote: a mention, a link of its own."""
    return f"{_MARK}{markup}{_MARK}"


def slack_text(text: str) -> str:
    """A text as it may be sent to Slack: nothing in it is markup but what `own` marked."""
    parts = text.split(_MARK)
    return "".join(
        part if n % 2 and n < len(parts) - 1 else _as_text(part)  # odd: between two marks
        for n, part in enumerate(parts)
    )


def _as_text(part: str) -> str:
    part = part.split("\x00", 1)[0]  # what is left of a mark that a text was cut short in
    return part.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_payload(node: Any, *, top: bool = True) -> Any:
    """What is sent to Slack's API, with every text it will read as markup made safe."""
    if isinstance(node, dict):
        fields = cast(dict[str, Any], node)
        read_as_markup = top or fields.get("type") == "mrkdwn"
        return {
            key: slack_text(value)
            if key == "text" and isinstance(value, str) and read_as_markup
            else slack_payload(value, top=False)
            for key, value in fields.items()
        }
    if isinstance(node, list):
        return [slack_payload(item, top=False) for item in cast(list[Any], node)]
    return node.replace(_MARK, "") if isinstance(node, str) else node


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

    async def review_opened(self, review: Any, link: str) -> None:
        await self._post_write(
            "review.opened", {"review": review.model_dump(mode="json"), "link": link}
        )

    async def review_closed(self, review: Any) -> None:
        await self._post_write("review.closed", {"review": review.model_dump(mode="json")})

    async def enable_opened(self, request: Any, link: str) -> None:
        await self._post_write(
            "enable_write.opened",
            {"request": request.model_dump(mode="json", exclude={"notifier_ref"}), "link": link},
        )

    async def enable_updated(self, request: Any) -> None:
        await self._post_write(
            "enable_write.updated",
            {"request": request.model_dump(mode="json", exclude={"notifier_ref"})},
        )

    async def enable_closed(self, request: Any) -> None:
        await self._post_write(
            "enable_write.closed",
            {"request": request.model_dump(mode="json", exclude={"notifier_ref"})},
        )

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

    async def auto_write_used(
        self, approval_like: WriteApproval, used: int, cap: int, link: str
    ) -> None:
        await self._post_write(
            "auto_write.used",
            {
                "agent": approval_like.agent,
                "principal": approval_like.principal,
                "api": approval_like.api,
                "operation": approval_like.operation,
                "used_today": used,
                "limit_per_day": cap,
                "request": approval_like.request.model_dump(mode="json"),
                "link": link,
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
                json=slack_payload(payload),
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
                "text": access_text(request),
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
                "text": access_text(request),
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
        return own(f"<@{user_id}>") if user_id else email

    async def write_opened(self, approval: WriteApproval, link: str) -> None:
        self._links[approval.id] = link
        people = [await self._user_mention(who) for who in approval.requesters]
        data = await self._call(
            "chat.postMessage",
            {
                "channel": approval.channel or self.default_channel,
                "text": f"{_needed(approval)}: {approval.api_name} {_what(approval)}",
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
                "text": f"{approval.api_name} {_what(approval)}: {approval.state}",
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

    async def auto_write_used(
        self, approval_like: WriteApproval, used: int, cap: int, link: str
    ) -> None:
        who = approval_like.agent + (
            f" for {approval_like.principal}" if approval_like.principal else ""
        )
        await self._call(
            "chat.postMessage",
            {
                "channel": approval_like.channel or self.default_channel,
                "text": (
                    f":zap: Auto-approved write: {who} ran {approval_like.method} "
                    f"`{approval_like.operation}` on {approval_like.api_name} "
                    f"({used} of {cap} today). " + own(f"<{link}|Require approval again>")
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

    async def review_opened(self, review: Any, link: str) -> None:
        self._links[review.id] = link
        data = await self._call(
            "chat.postMessage",
            {
                "channel": review.channel or self.default_channel,
                "text": review_title(review),
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

    async def enable_opened(self, request: Any, link: str) -> None:
        self._links[request.id] = link
        data = await self._call(
            "chat.postMessage",
            {
                "channel": request.channel or self.default_channel,
                "text": enable_text(request),
                "blocks": enable_blocks(request, link),
            },
        )
        if data.get("ok"):
            request.notifier_ref = {"channel": str(data["channel"]), "ts": str(data["ts"])}

    async def enable_updated(self, request: Any) -> None:
        ref = request.notifier_ref
        if not ref:
            return
        await self._call(
            "chat.update",
            {
                "channel": ref["channel"],
                "ts": ref["ts"],
                "text": enable_text(request),
                "blocks": enable_blocks(request, self._links.get(request.id)),
            },
        )

    async def enable_closed(self, request: Any) -> None:
        ref = request.notifier_ref
        self._links.pop(request.id, None)
        if not ref:
            return
        await self._call(
            "chat.update",
            {
                "channel": ref["channel"],
                "ts": ref["ts"],
                "text": enable_outcome_text(request),
                "blocks": enable_blocks(request, None),
            },
        )
        await self._call(
            "chat.postMessage",
            {
                "channel": ref["channel"],
                "thread_ts": ref["ts"],
                "text": enable_outcome_text(request),
            },
        )

    async def consent_opened(self, request: Any, link: str) -> None:
        """A direct message to the person; the channel (with a mention) when Slack does
        not know their address."""
        user = await self._user_id(request.principal)
        text = (
            f":key: *{request.api_name}* needs you to sign in, so that *{request.agent}* can "
            f"act as you there. {own(f'<{link}|')}Sign in to {request.api_name}{own('>')} "
            f"(one-time link, {int(CONSENT_MINUTES)} minutes). Only what you can see is "
            "shared; agents never see your token."
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
        await self._user_mention(email)  # looks the person up, once
        return self._user_ids.get(email)


CONSENT_MINUTES = 30


def enable_text(request: Any) -> str:
    who = ", ".join(request.requested_by[:3]) or "an agent"
    others = len(request.operations) - 1
    more = f" (+{others} more)" if others > 0 else ""
    return f"{who} wants to write to {request.api_name}: {request.operation}{more}"


def enable_outcome_text(request: Any) -> str:
    icon = {
        "enabled": ":white_check_mark:",
        "declined": ":no_entry:",
        "expired": ":hourglass:",
    }.get(str(request.state), ":pencil2:")
    who = f" by {request.decided_by}" if request.decided_by else ""
    note = f" · {request.note}" if request.note else ""
    return f"{icon} {request.api_name}: writes for {request.operation} {request.state}{who}{note}"


def enable_blocks(request: Any, link: str | None) -> list[dict[str, Any]]:
    """Block Kit card for a write an agent wants that nobody has enabled yet."""
    pending = str(request.state) == "pending"
    title = f":pencil2: {enable_text(request)}" if pending else enable_outcome_text(request)
    operation = f"`{request.method} {request.path}`"
    if request.summary:
        operation += f"\n{request.summary}"
    others = [o for o in request.operations if o != request.operation]
    if others:
        operation += "\nand " + ", ".join(f"`{o}`" for o in others[:10])
        if len(others) > 10:
            operation += f" and {len(others) - 10} more"
    fields = [
        f"*Operation{'s' if others else ''}:*\n{operation}",
        f"*Asked by:*\n{', '.join(request.requested_by[:5])}",
        "*Why:*\n" + ("; ".join(request.reasons[:3]) or "no reason given"),
        f"*Asked:*\n{request.asks} time(s)",
    ]
    if request.needs_credential:
        fields.append("*Write credential:*\nnone yet; enabling asks for one")
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*"}},
        {"type": "section", "fields": [{"type": "mrkdwn", "text": f} for f in fields[:10]]},
    ]
    if pending and link:
        group = mention(request.approver_group)
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (f"{group} " if group else "")
                    + "Enable carries it into the entry's review (every write still needs "
                    "approval or a grant); decline keeps it refused and quiet for a week.",
                },
                "accessory": {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Decide"},
                    "url": link,
                    "style": "primary",
                },
            }
        )
    return blocks


def review_outcome_text(review: Any) -> str:
    icon = {"approved": ":white_check_mark:", "denied": ":no_entry:"}.get(
        str(review.state), ":pencil2:"
    )
    who = f" by {review.decided_by}" if review.decided_by else ""
    note = f" · {review.note}" if review.note else ""
    return f"{icon} {review.api_name} review {review.state}{who}{note}"


def review_title(review: Any) -> str:
    if getattr(review, "kind", "learned") == "update":
        return f"{review.api_name} changed at its source: review the update"
    return f"New API to review: {review.api_name}"


def review_blocks(review: Any, link: str | None) -> list[dict[str, Any]]:
    """Block Kit card for a learned API (or an update to a verified one) awaiting
    approval."""
    pending = str(review.state) == "pending"
    update = getattr(review, "kind", "learned") == "update"
    icon = ":warning:" if update else ":books:"
    title = f"{icon} {review_title(review)}" if pending else review_outcome_text(review)
    fields = [
        f"*Site:*\n{review.host}",
        f"*Noticed by:*\n{review.learned_by}" if update else f"*Learned by:*\n{review.learned_by}",
        f"*What:*\n{review.summary}",
        f"*Authentication:*\n{review.auth}",
    ]
    if review.needs_review:
        fields.append(f"*Need a decision:*\n{len(review.needs_review)} operation(s)")
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*"}},
        {"type": "section", "fields": [{"type": "mrkdwn", "text": f} for f in fields[:10]]},
    ]
    changes = list(getattr(review, "changes", []))
    if changes:
        shown = "\n".join(f"• {c}" for c in changes[:8])
        more = f"\n• ... and {len(changes) - 8} more" if len(changes) > 8 else ""
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": shown + more}})
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
                    + (
                        "Approve applies the update; deny keeps the entry as it is "
                        "(operations suspended meanwhile stay suspended)."
                        if update
                        else "Approve makes the API verified, at once; deny drops it."
                    ),
                },
                "accessory": {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Review"},
                    "url": link,
                    "style": "primary",
                },
            }
        )
    return blocks


def _api_changed_text(api: str, title: str, details: list[str], link: str | None) -> str:
    lines = [f":warning: *{title}* (`{api}`)", *[f"• {d}" for d in details[:10]]]
    if len(details) > 10:
        lines.append(f"• ... and {len(details) - 10} more")
    if link:
        lines.append(own(f"<{link}|Review the proposed update>"))
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
    what = _what(approval) if approval.steps else f"`{approval.operation}`"
    return f"{icon} {approval.api_name} {what} {approval.state}{who}{remembered}{note}"


def _what(approval: WriteApproval) -> str:
    """What an approval is for, in a few words: the operation, or how big a plan."""
    return f"plan of {len(approval.steps)} writes" if approval.steps else approval.operation


def _for_whom(approval: WriteApproval) -> str:
    """Who a write is for. A name the agent's platform sent without proof is said to be
    exactly that."""
    if approval.principal:
        return approval.principal
    if approval.claimed_for:
        return (
            f"(no person with proof; the agent's platform names {approval.claimed_for}, unverified)"
        )
    return "(no person; autonomous)"


def _needed(approval: WriteApproval) -> str:
    return "Plan approval needed" if approval.steps else "Write approval needed"


def write_reply(approval: WriteApproval) -> str:
    """The thread reply under a write card: what actually happened, in a few words."""
    who = f" by {approval.decided_by}" if approval.decided_by else ""
    if approval.state is ApprovalState.EXECUTED and approval.steps:
        return (
            f":white_check_mark: Done: all {len(approval.steps)} writes of the plan were "
            f"made on {approval.api_name}, in order."
        )
    if approval.state is ApprovalState.EXECUTED:
        status = (approval.result or {}).get("status_code")
        code = f"HTTP {int(status)}" if isinstance(status, int | float) else "a response"
        grant = (
            " Similar writes are now allowed by a grant." if approval.decision == "grant" else ""
        )
        return f":white_check_mark: Done: {approval.api_name} answered {code}.{grant}"
    if approval.state is ApprovalState.FAILED:
        what = "the plan stopped" if approval.steps else "the write failed"
        return f":x: Approved{who}, but {what}: {approval.note or 'unknown error'}"
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
        f":pencil2: {_needed(approval)}: {approval.api_name}"
        if pending
        else write_outcome_text(approval)
    )
    request = approval.request
    if approval.steps:
        listed = "\n".join(
            f"{number}. `{step.operation}` {step.method} {step.path}"[:150]
            for number, step in enumerate(approval.steps[:10], start=1)
        )
        more = len(approval.steps) - 10
        fields = [
            f"*Plan:* {len(approval.steps)} writes, in order\n{listed}"
            + (f"\n…and {more} more" if more > 0 else ""),
            f"*Agent:*\n{approval.agent}",
            f"*For:*\n{_for_whom(approval)}",
        ]
    else:
        fields = [
            f"*Operation:*\n`{approval.operation}`\n{approval.method} {approval.path}",
            f"*Agent:*\n{approval.agent}",
            f"*For:*\n{_for_whom(approval)}",
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
                    + (
                        "Every write is on the review page. Approve the plan as it stands, "
                        "or deny it."
                        if approval.steps
                        else "Approve once, approve and allow similar writes, or deny."
                    ),
                },
                "accessory": {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Review plan" if approval.steps else "Review write",
                    },
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
    return own(f"<!subteam^{group}>") if _SLACK_GROUP_ID.match(group) else group


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


def access_text(request: CredentialRequest) -> str:
    kind = "write credential" if request.purpose == "write" else "read-only"
    return f"Access needed: {request.api_name} ({kind})"


def card_blocks(request: CredentialRequest, link: str | None) -> list[dict[str, Any]]:
    """Block Kit card for a credential request."""
    pending = request.state is RequestState.PENDING
    kind = "write credential" if request.purpose == "write" else "read-only"
    title = (
        f":key: {request.api_name} access needed ({kind})" if pending else resolution_text(request)
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
                    + (
                        "Add the write credential, or deny. It never goes through Slack."
                        if request.purpose == "write"
                        else "Add a key, pick an existing secret, or deny. The key never goes"
                        " through Slack."
                    ),
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
