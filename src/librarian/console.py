"""The operator console: what librarian knows, what is waiting on people, and a few
actions for the people who run it.

Served on the pages listener (behind the auth proxy) at `/`, under the same sign-in
rules as the approval pages (`approvers`). `approvers.console` opens it without
sign-in on a laptop, or switches it off. Every action is a plain form whose token is
bound to the signed-in person (so another site cannot submit it for them), and every
action is written to the audit log with who did it.
"""

from __future__ import annotations

import hmac
import html
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from librarian.approvals import ApprovalError, Grant, WriteApproval
from librarian.audit import AuditEvent
from librarian.calls import Caller
from librarian.config import ApproverSettings
from librarian.credentials import CredentialRequest, RequestState
from librarian.enablement import EnableWriteRequest
from librarian.oauth import ConsentRequest, OAuthError, authorize_url
from librarian.opsearch import rank, tokens
from librarian.registry.models import Access, ApiEntry, EntryStatus, OAuth2UserAuth, Operation
from librarian.registry.store import RegistryError
from librarian.reviews import ReviewRequest
from librarian.service import Librarian, LibrarianError
from librarian.web import approver_identity, expired, notice, page

e = html.escape

OPEN_IDENTITY = "someone (console open)"


def _viewer(request: Request, settings: ApproverSettings) -> tuple[str | None, Response | None]:
    """(who is looking, None) when the request may use the console, else (None, the page
    to answer with)."""
    if settings.console == "off":
        return None, notice("note", "The console is off", "approvers.console is 'off'.", 404)
    if settings.console == "open":
        return OPEN_IDENTITY, None
    if settings.mode == "token":
        return None, notice(
            "bad",
            "Sign-in needed",
            "The console shows every API and credential reference librarian knows, so it needs "
            "a signed-in person: run the pages listener behind your auth proxy with "
            "approvers.mode: trusted_header. On a laptop, approvers.console: open serves it "
            "without sign-in.",
            403,
        )
    identity, problem = approver_identity(request, settings)
    if problem or identity is None:
        return None, notice("bad", "Not allowed", e(problem or ""), 403)
    return identity, None


def _tag(status: EntryStatus) -> str:
    return f"<span class='tag {e(status.value)}'>{e(status.value)}</span>"


def _method(op: Operation) -> str:
    return f"<span class='method {e(op.method.lower())}'>{e(op.method.upper())}</span>"


def _n(count: int, noun: str, plural: str | None = None) -> str:
    return f"{count} {noun if count == 1 else plural or noun + 's'}"


def _when(moment: datetime | None) -> str:
    if moment is None:
        return ""
    delta = datetime.now(UTC) - moment
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{int(delta.total_seconds() // 60)} min ago"
    if hours < 48:
        return f"{int(hours)} h ago"
    return f"{int(hours // 24)} days ago"


def _learned_from(entry: ApiEntry) -> str:
    source = entry.source
    kind = {
        "openapi": "an OpenAPI spec",
        "docs": "documentation",
        "manual": "a person",
        "inferred": "the model's knowledge",
    }.get(source.kind, source.kind)
    trust = e(source.trust or "unrated")
    how = f" (found by {e(source.found_by)}, {trust})" if source.found_by else ""
    return f"{e(kind)}{how}"


def _flags(librarian: Librarian, entry: ApiEntry) -> list[str]:
    obs = librarian.observations
    suspended = sum(1 for op in entry.operations if obs.suspended(entry, op))
    unconfirmed = sum(
        1 for op in entry.operations if op.access is Access.READ and not obs.confirmed(entry, op)
    )
    flags: list[str] = []
    if suspended:
        flags.append(f"<span class=bad>{suspended} suspended</span>")
    if unconfirmed:
        flags.append(f"{unconfirmed} unconfirmed")
    if librarian.broker.pending_for(entry.id) is not None:
        flags.append("<span class=note>waiting for a key</span>")
    return flags


async def _pending_credentials(librarian: Librarian) -> list[CredentialRequest]:
    store = librarian.broker.store
    if store is not None:  # every replica's requests
        return list(await store.list_requests(RequestState.PENDING))
    return [r for r in librarian.broker.all() if r.state is RequestState.PENDING]


def _form(action: str, token: str, button: str, cls: str = "", fields: str = "") -> str:
    """A one-button form. `fields` is extra inline HTML (inputs, labels)."""
    return (
        f"<form method=post action='{e(action)}' class=inline>"
        f"<input type=hidden name=t value='{e(token)}'>{fields}"
        f"<button class='{e(cls)}'>{e(button)}</button></form>"
    )


def _search_form(action: str, query: str, placeholder: str) -> str:
    return (
        f"<form method=get action='{e(action)}' class=search>"
        f"<input name=q value='{e(query)}' placeholder='{e(placeholder)}' aria-label='Search'>"
        "<button class=quiet>Search</button>"
        + (f"<a href='{e(action)}'>Clear</a>" if query else "")
        + "</form>"
    )


async def _pending(librarian: Librarian) -> Pending:
    await librarian.enablement.refresh()
    return (
        await _pending_credentials(librarian),
        librarian.approvals.pending(),
        librarian.reviews.pending(),
        librarian.consents.pending(),
        librarian.enablement.pending(),
    )


def _waiting_rows(
    librarian: Librarian,
    credentials: list[CredentialRequest],
    approvals: list[WriteApproval],
    reviews: list[ReviewRequest],
    consents: list[ConsentRequest],
    enables: list[EnableWriteRequest],
    limit: int | None = None,
) -> str:
    rows: list[str] = []
    for en in enables:
        more = (
            f" and {_n(len(en.others), 'more operation', 'more operations')}" if en.others else ""
        )
        rows.append(
            f"<tr><td>Enable writes on <b>{e(en.api_name)}</b>: {e(en.method)} "
            f"<code>{e(en.operation)}</code>{more}</td>"
            f"<td>{e(', '.join(en.requested_by) or 'an agent')}</td>"
            f"<td>{_n(en.asks, 'ask')}, {_when(en.created_at)}</td>"
            f"<td><a href='{e(librarian.enablement.link(en))}'>Decide</a></td></tr>"
        )
    for r in reviews:
        rows.append(
            f"<tr><td>Review <b>{e(r.api_name)}</b>: {e(r.summary)}</td>"
            f"<td>{e(r.learned_by)}</td><td>{_when(r.created_at)}</td>"
            f"<td><a href='{e(librarian.reviews.link(r))}'>Review</a></td></tr>"
        )
    for c in credentials:
        rows.append(
            f"<tr><td>Key for <b>{e(c.api_name)}</b> <span class=muted>({e(c.host)})</span></td>"
            f"<td>{e(', '.join(c.requested_by) or 'an agent')}</td>"
            f"<td>{c.waiting} waiting, {_when(c.created_at)}</td>"
            f"<td><a href='{e(librarian.broker.link(c))}'>Add a key</a></td></tr>"
        )
    for a in approvals:
        rows.append(
            f"<tr><td>Write to <b>{e(a.api_name)}</b>: {_method_text(a)}"
            f"<code>{e(a.operation)}</code></td>"
            f"<td>{e(a.agent)}{' for ' + e(a.principal) if a.principal else ''}</td>"
            f"<td>{_when(a.created_at)}</td>"
            f"<td><a href='{e(librarian.approvals.link(a))}'>Decide</a></td></tr>"
        )
    for s in consents:
        rows.append(
            f"<tr><td>Sign-in to <b>{e(s.api_name)}</b> by {e(s.principal)}</td>"
            f"<td>{e(s.agent)}</td><td>{_when(s.created_at)}</td>"
            "<td><span class=muted>waiting for the person</span></td></tr>"
        )
    if not rows:
        return "<p class=empty>Nothing is waiting on a person.</p>"
    shown = rows if limit is None else rows[:limit]
    table = (
        "<table><tr><th>What</th><th>Who</th><th>Since</th><th></th></tr>"
        + "".join(shown)
        + "</table>"
    )
    if limit is not None and len(rows) > limit:
        table += f"<p class=more><a href='/waiting'>All {len(rows)} waiting</a></p>"
    return table


def _api_rows(librarian: Librarian, entries: list[ApiEntry]) -> str:
    return "".join(
        f"<tr><td><a href='/apis/{e(entry.id)}'>{e(entry.name)}</a> "
        f"<span class=muted>{e(entry.id)}</span></td><td>{_tag(entry.status)}</td>"
        f"<td>{e(entry.host)}</td>"
        f"<td>{_n(sum(1 for op in entry.operations if op.access is Access.READ), 'read')}, "
        f"{_n(sum(1 for op in entry.operations if op.writable), 'write')}</td>"
        f"<td>{_learned_from(entry)}</td><td>{', '.join(_flags(librarian, entry))}</td></tr>"
        for entry in entries
    )


API_TABLE_HEAD = (
    "<table><tr><th>API</th><th>Status</th><th>Host</th><th>Operations</th>"
    "<th>Learned from</th><th></th></tr>"
)

Pending = tuple[
    list[CredentialRequest],
    list[WriteApproval],
    list[ReviewRequest],
    list[ConsentRequest],
    list[EnableWriteRequest],
]


EVENT_LABELS = {
    "call": "Read",
    "write": "Write",
    "denied": "Denied",
    "write_denied": "Write denied",
    "write_requested": "Write asked",
    "write_decided": "Write decided",
    "credential_request": "Key asked",
    "credential_resolved": "Key added",
    "credential_invalidated": "Key invalidated",
    "consent_request": "Sign-in asked",
    "consent_resolved": "Signed in",
    "onboard": "Learned",
    "api_forgotten": "Forgotten",
    "api_changed": "Changed",
    "api_blocked": "Blocked",
    "api_unblocked": "Unblocked",
    "recipe_learned": "Recipe saved",
    "grant_created": "Grant",
    "grant_revoked": "Grant revoked",
    "review_approved": "Approved",
    "review_denied": "Review denied",
    "enable_write_requested": "Write enable asked",
    "enable_write_enabled": "Writes enabled",
    "enable_write_declined": "Writes declined",
    "enable_write_expired": "Write enable lapsed",
    "writes_enabled": "Write key allowed",
    "slashes_allowed": "Slashes allowed",
    "decisions_reapplied": "Decisions re-applied",
    "write_auto": "Write (automatic)",
    "write_auto_capped": "Automatic writes capped",
    "proposal_merge_failed": "Merge refused",
    "operation_access_set": "Operation decided",
    "auth_set": "Authentication set",
    "person_disconnected": "Signed out",
    "signins_checked": "Sign-ins checked",
}


def _event_label(event: AuditEvent) -> str:
    if event.event == "fetch":
        return "Answered from a recipe" if event.detail == "recipe" else "Answered by the model"
    return EVENT_LABELS.get(event.event, event.event.replace("_", " ").capitalize())


def _size(count: int | None) -> str:
    if not count:
        return ""
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""  # pragma: no cover


def _event_detail(event: AuditEvent) -> str:
    parts: list[str] = []
    if event.status_code is not None:
        parts.append(f"HTTP {event.status_code}")
    if event.bytes:
        parts.append(_size(event.bytes))
    if event.duration_ms is not None:
        parts.append(f"{event.duration_ms} ms")
    if event.event == "fetch":
        calls = event.extra.get("calls")
        if calls is not None:
            parts.append(_n(int(calls), "call"))
    elif event.detail:
        parts.append(event.detail)
    return e(", ".join(parts))


def _activity_rows(events: list[AuditEvent], *, show_api: bool = True) -> str:
    if not events:
        return "<p class=empty>Nothing yet.</p>"
    rows: list[str] = []
    for ev in events:
        who = e(ev.caller or "")
        if ev.principal:
            who += f" <span class=muted>for {e(ev.principal)}</span>"
        what = e(ev.operation or "")
        if show_api and ev.api:
            what = f"<a href='/apis/{e(ev.api)}'>{e(ev.api)}</a>" + (f" / {what}" if what else "")
        bad = ev.event in ("denied", "write_denied") or (ev.status_code or 0) >= 400
        cls = " class=bad" if bad else ""
        rows.append(
            f"<tr><td>{_when(ev.ts)}</td><td{cls}>{e(_event_label(ev))}</td><td>{who}</td>"
            f"<td>{what}</td><td class=muted>{_event_detail(ev)}</td></tr>"
        )
    return (
        "<table class=activity><tr><th>When</th><th>What</th><th>Who</th><th>Where</th>"
        f"<th></th></tr>{''.join(rows)}</table>"
    )


def _summary_tiles(summary: dict[str, Any]) -> str:
    answered = summary["fetch_planner"] + summary["fetch_recipe"]
    recipe_share = (
        f"{round(100 * summary['fetch_recipe'] / answered)}% without a model" if answered else ""
    )
    tiles: list[tuple[str, str, str]] = [
        (
            str(summary["calls"]),
            "reads",
            _size(summary["bytes"]) + " fetched" if summary["bytes"] else "",
        ),
        (str(summary["writes"]), "writes", "each one approved or granted"),
        (str(summary["denied"]), "denied", "policy, limits, unknown operations"),
        (
            str(answered),
            "plain-language requests",
            recipe_share or "recipes replay without a model",
        ),
    ]
    return (
        "<div class=tiles>"
        + "".join(
            f"<div class=tile><b>{e(count)}</b><em>{e(label)}</em><span>{e(note)}</span></div>"
            for count, label, note in tiles
        )
        + "</div>"
    )


NO_ACTIVITY_DB = (
    "<p class=empty>Showing activity here needs a database. Every event is still written "
    "to the audit stream.</p>"
)


def _activity_page(
    librarian: Librarian,
    events: list[AuditEvent] | None,
    summary: dict[str, Any] | None,
    api: str,
) -> str:
    if events is None or summary is None:
        body = NO_ACTIVITY_DB
    else:
        scope = f" for <code>{e(api)}</code>" if api else ""
        by_api = "".join(
            f"<li><a href='/activity?api={e(name)}'>{e(name)}</a> <span class=muted>"
            f"{_n(count, 'read')}</span></li>"
            for name, count in summary["by_api"]
        )
        busiest = (
            f"<h2>Busiest APIs, last 7 days</h2><ul class=plain>{by_api}</ul>" if by_api else ""
        )
        clear = " <a class=muted href='/activity'>Show all</a>" if api else ""
        body = (
            f"<h2>Last 7 days</h2>{_summary_tiles(summary)}{busiest}"
            f"<h2>Recent events{scope}{clear}</h2>{_activity_rows(events)}"
        )
    return f"""<div class=pagehead><h1>Activity</h1></div>
<p class=lede>What agents did through librarian, and what it cost.</p>
{body}"""


def _dashboard(
    librarian: Librarian,
    pending: Pending,
    people: int | None,
    summary: dict[str, Any] | None = None,
    recent_events: list[AuditEvent] | None = None,
) -> str:
    credentials, approvals, reviews, consents, enables = pending
    entries = librarian.registry.all()
    grants = [g for g in librarian.approvals.grants() if g.usable(datetime.now(UTC))]
    waiting = len(credentials) + len(approvals) + len(reviews) + len(consents) + len(enables)
    attention = sum(1 for x in entries if _flags(librarian, x))
    tiles: list[tuple[str, str, str, str]] = [
        (
            "/apis",
            str(len(entries)),
            "APIs",
            f"{attention} need attention" if attention else "all quiet",
        ),
        ("/waiting", str(waiting), "waiting on a person", "keys, writes, reviews, sign-ins"),
        ("/grants", str(len(grants)), "standing grants", "writes allowed without asking"),
        (
            "/people",
            "n/a" if people is None else str(people),
            "signed-in people",
            "needs a database" if people is None else "per-person APIs",
        ),
    ]
    tiles_html = "".join(
        f"<a class=tile href='{href}'><b>{e(count)}</b><em>{e(label)}</em>"
        f"<span>{e(note)}</span></a>"
        for href, count, label, note in tiles
    )
    recent = sorted(entries, key=lambda x: x.updated_at or datetime.min.replace(tzinfo=UTC))
    recent = list(reversed(recent))[:8]
    errors = "".join(
        f"<p class=err><code>{e(path)}</code>: {e(problem)}</p>"
        for path, problem in librarian.registry.errors.items()
    )
    if entries:
        apis = API_TABLE_HEAD + _api_rows(librarian, recent) + "</table>"
        if len(entries) > 8:
            apis += f"<p class=more><a href='/apis'>All {len(entries)} APIs</a></p>"
    else:
        apis = "<p class=empty>No APIs yet. The first agent that needs one teaches it.</p>"
    if summary is None or recent_events is None:
        activity = f"<h2>Last 7 days</h2>{NO_ACTIVITY_DB}"
    else:
        activity = (
            f"<h2>Last 7 days</h2>{_summary_tiles(summary)}"
            f"<h2>Recent activity</h2>{_activity_rows(recent_events)}"
            "<p class=more><a href='/activity'>All activity</a></p>"
        )
    waiting_rows = _waiting_rows(librarian, credentials, approvals, reviews, consents, enables, 8)
    return f"""<div class=pagehead><h1>Overview</h1></div>
<p class=lede>What librarian knows, and what waits on a person.</p>
<div class=tiles>{tiles_html}</div>
<h2>Waiting on a person</h2>{waiting_rows}
{activity}
<h2>Recently changed APIs</h2>{errors}{apis}"""


def _apis_page(librarian: Librarian, query: str) -> str:
    entries = librarian.registry.all()
    total = len(entries)
    if query:
        needle = query.lower()
        entries = [
            x for x in entries if needle in f"{x.id} {x.name} {x.host} {x.description}".lower()
        ]
    errors = "".join(
        f"<p class=err><code>{e(path)}</code>: {e(problem)}</p>"
        for path, problem in librarian.registry.errors.items()
    )
    if entries:
        table = API_TABLE_HEAD + _api_rows(librarian, entries) + "</table>"
    elif query:
        table = f"<p class=empty>No API matches {e(query)!r}.</p>"
    else:
        table = "<p class=empty>No APIs yet. The first agent that needs one teaches it.</p>"
    count = _n(total, "entry", "entries")
    return f"""<div class=pagehead><h1>APIs</h1><span class=muted>{count}</span></div>
<p class=lede>Every API librarian knows: reviewed ones and the ones learned on demand.</p>
{_search_form("/apis", query, "Filter by name, id or host")}{errors}{table}"""


def _waiting_page(librarian: Librarian, pending: Pending) -> str:
    credentials, approvals, reviews, consents, enables = pending
    return f"""<div class=pagehead><h1>Waiting on a person</h1></div>
<p class=lede>Keys to add, writes to decide, learned APIs to review, and people who still have
to sign in. The links are the same one-time links the notifications carry.</p>
{_waiting_rows(librarian, credentials, approvals, reviews, consents, enables)}"""


def _grants_page(librarian: Librarian, token: str, include_inactive: bool) -> str:
    now = datetime.now(UTC)
    grants = librarian.approvals.grants(include_inactive=include_inactive)

    def last(g: Grant) -> str:
        if g.usable(now):
            return f"<td>{_form(f'/grants/{g.id}/revoke', token, 'Revoke', 'deny small')}</td>"
        if g.revoked:
            return "<td><span class=muted>revoked</span></td>"
        return "<td><span class=muted>expired or used up</span></td>"

    rows = "".join(
        f"<tr><td>{e(g.agent)}{' for ' + e(g.principal) if g.principal else ''}</td>"
        f"<td><code>{e(g.api)}.{e(g.operation)}</code></td>"
        f"<td>{_pins(g)}</td><td>{_expiry(g)}</td><td>{e(g.source)}, by {e(g.created_by)}</td>"
        f"{last(g)}</tr>"
        for g in grants
    )
    table = (
        f"<table><tr><th>Agent</th><th>Operation</th><th>Pinned</th><th>Limits</th>"
        f"<th>Source</th><th></th></tr>{rows}</table>"
        if grants
        else "<p class=empty>No standing grants: every write is approved one by one.</p>"
    )
    toggle = (
        "<p class=more><a href='/grants'>Only active grants</a></p>"
        if include_inactive
        else "<p class=more><a href='/grants?all=1'>Include revoked and expired</a></p>"
    )
    return f"""<div class=pagehead><h1>Grants</h1></div>
<p class=lede>Writes an agent may make without asking each time. Revoking one is the kill
switch: the next such write waits for a person again. Grants from the config file come back
on restart.</p>{table}{toggle}"""


def _people_page(librarian: Librarian, token: str, connections: list[Any]) -> str:
    if librarian.consents.store is None:
        body = (
            "<p class=empty>Listing who is signed in needs a database (Postgres). Sign-ins "
            "still work without one; they just cannot be listed here.</p>"
        )
    elif not connections:
        body = "<p class=empty>Nobody has signed in to a per-person API yet.</p>"
    else:
        rows = "".join(
            f"<tr><td>{e(c.principal)}</td><td><a href='/apis/{e(c.api)}'>{e(c.api)}</a></td>"
            f"<td>{e(' '.join(c.scopes))}</td><td>{_when(c.connected_at)}</td><td>"
            + _form(
                f"/apis/{c.api}/disconnect",
                token,
                "Disconnect",
                "deny small",
                f"<input type=hidden name=principal value='{e(c.principal)}'>",
            )
            + "</td></tr>"
            for c in connections
        )
        body = (
            "<table><tr><th>Person</th><th>API</th><th>Scopes</th><th>Since</th><th></th></tr>"
            f"{rows}</table>"
        )
    return f"""<div class=pagehead><h1>People</h1></div>
<p class=lede>Who has signed in to an API that acts as the person. Disconnecting drops their
sign-in here (and revokes it at the provider when the entry says where); their next request
asks them to sign in again.</p>{body}"""


def _me_page(librarian: Librarian, identity: str | None, connections: list[Any], token: str) -> str:
    if identity is None:
        body = (
            "<p class=empty>Your sign-ins are shown when the pages run behind your sign-in "
            "proxy, which tells librarian who you are.</p>"
        )
    elif librarian.consents.store is None:
        body = "<p class=empty>Listing sign-ins needs a database.</p>"
    elif not connections:
        body = "<p class=empty>You have not signed in to any API through librarian.</p>"
    else:
        rows = "".join(
            f"<tr><td><a href='/apis/{e(c.api)}'>{e(c.api)}</a></td>"
            f"<td>{e(' '.join(c.scopes))}</td><td>{_when(c.connected_at)}</td><td>"
            + _form(
                "/me/disconnect",
                token,
                "Disconnect",
                "deny small",
                f"<input type=hidden name=api value='{e(c.api)}'>",
            )
            + "</td></tr>"
            for c in connections
        )
        body = f"<table><tr><th>API</th><th>Scopes</th><th>Since</th><th></th></tr>{rows}</table>"
    who = f" <span class=muted>{e(identity)}</span>" if identity else ""
    return f"""<div class=pagehead><h1>Your sign-ins</h1>{who}</div>
<p class=lede>APIs where agents act as you, with the access you granted. Disconnect to stop
that; the next request for you asks you to sign in again.</p>{body}"""


def _method_text(approval: WriteApproval) -> str:
    cls = e(approval.method.lower())
    return f"<span class='method {cls}'>{e(approval.method.upper())}</span>"


def _pins(grant: Grant) -> str:
    if not grant.pinned_args:
        return "<span class=muted>any arguments</span>"
    return ", ".join(
        f"<code>{e(k)}</code> = {e(json.dumps(v))}" for k, v in grant.pinned_args.items()
    )


def _expiry(grant: Grant) -> str:
    parts: list[str] = []
    if grant.expires_at is not None:
        left = grant.expires_at - datetime.now(UTC)
        parts.append(f"{max(0, int(left.total_seconds() // 86400))} days left")
    if grant.max_uses is not None:
        parts.append(f"{grant.uses}/{grant.max_uses} uses")
    if grant.max_uses_per_day is not None:
        parts.append(f"{grant.uses_today}/{grant.max_uses_per_day} today")
    return ", ".join(parts) or "no limit"


def _detail(
    librarian: Librarian,
    entry: ApiEntry,
    token: str,
    query: str = "",
    connections: list[Any] | None = None,
    events: list[AuditEvent] | None = None,
    stale: list[EnableWriteRequest] | None = None,
) -> str:
    connections = connections or []
    source_url = f": <code>{e(entry.source.url)}</code>" if entry.source.url else ""
    updated = e(entry.updated_at.isoformat(timespec="minutes")) if entry.updated_at else ""
    rows = [
        ("Base URL", f"<code>{e(entry.base_url)}</code>"),
        ("Status", _tag(entry.status)),
        ("Learned from", _learned_from(entry) + source_url),
        ("Credential", _auth(entry)),
        ("Updated", updated),
    ]
    owners = " / ".join(filter(None, [entry.owners.channel, entry.owners.approver_group]))
    if owners:
        rows.append(("Owners", e(owners)))
    pending = librarian.broker.pending_for(entry.id)
    if pending is not None:
        rows.append(
            (
                "Waiting",
                f"a key, asked {_when(pending.created_at)}: "
                f"<a href='{e(librarian.broker.link(pending))}'>Review</a>",
            )
        )
    asks = [r for r in librarian.enablement.pending() if r.api == entry.id]
    if asks:
        rows.append(
            (
                "Wanted writes",
                "; ".join(
                    ", ".join(f"<code>{e(o)}</code>" for o in r.operations)
                    + f" by {e(', '.join(r.requested_by))}: "
                    f"<a href='{e(librarian.enablement.link(r))}'>Decide</a>"
                    for r in asks
                ),
            )
        )
    review = librarian.reviews.pending_for(entry.id)
    if review is not None:
        rows.append(
            (
                "Review",
                f"open since {_when(review.created_at)}: "
                f"<a href='{e(librarian.reviews.link(review))}'>Approve or deny</a> "
                f"(<a href='{e(review.review_url)}'>the pull request</a>)",
            )
        )
    rows = [r for r in rows if r[0] != "Status"]  # the header shows it
    details = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows)
    notes = f"<h2 id=notes>Notes</h2><pre>{e(entry.notes)}</pre>" if entry.notes else ""
    people = ""
    if entry.per_person:
        rows_html = "".join(
            f"<tr><td>{e(c.principal)}</td><td>{e(' '.join(c.scopes))}</td>"
            f"<td>{_when(c.connected_at)}</td><td>"
            + _form(
                f"/apis/{entry.id}/disconnect",
                token,
                "Disconnect",
                "deny small",
                f"<input type=hidden name=principal value='{e(c.principal)}'>",
            )
            + "</td></tr>"
            for c in connections
        )
        if librarian.consents.store is None:
            listing = "<p class=empty>Listing who is signed in needs a database.</p>"
        elif connections:
            listing = (
                "<table><tr><th>Person</th><th>Scopes</th><th>Since</th><th></th></tr>"
                f"{rows_html}</table>"
            )
        else:
            listing = (
                "<p class=empty>Nobody has signed in yet. The first request for a person "
                "sends them a link.</p>"
            )
        people = f"<h2 id=people>Signed-in people</h2>{listing}"
    cards, danger = _actions(librarian, entry, token, stale or [])
    if events is None:
        activity = f"<h2 id=activity>Activity</h2>{NO_ACTIVITY_DB}"
    else:
        activity = (
            f"<h2 id=activity>Activity</h2>{_activity_rows(events, show_api=False)}"
            f"<p class=more><a href='/activity?api={e(entry.id)}'>All activity for this API</a></p>"
        )
    links = ["<a href='#operations'>Operations</a>", "<a href='#activity'>Activity</a>"]
    if entry.per_person:
        links.append("<a href='#people'>People</a>")
    if entry.notes:
        links.append("<a href='#notes'>Notes</a>")
    links.append("<a href='#actions'>Actions</a>")
    return f"""<p class=crumbs><a href='/apis'>APIs</a> / {e(entry.id)}</p>
<div class=pagehead><h1>{e(entry.name)}</h1>{_tag(entry.status)}
<span class=muted>{e(entry.id)}</span></div>
<p class=lede>{e(entry.description or entry.host)}</p>
<div class=card><dl>{details}</dl></div>
<nav class=subnav aria-label='On this page'>{"".join(links)}</nav>
<h2 id=operations>Operations</h2>{_operations(librarian, entry, token, query)}
{activity}{people}{notes}
<h2 id=actions>Actions</h2><div class=grid2>{cards}</div>{danger}"""


GROUP_THRESHOLD = 25  # above this many operations, group them


def _group_of(op: Operation) -> str:
    if op.tags:
        return op.tags[0].strip().lower()[:40] or "other"
    segments = [s for s in op.path.split("/") if s and not s.startswith("{")]
    return segments[0].lower()[:40] if segments else "other"


def _needs_attention(librarian: Librarian, entry: ApiEntry, op: Operation) -> bool:
    return op.access is Access.UNKNOWN or librarian.observations.suspended(entry, op) is not None


def _op_table(
    librarian: Librarian, entry: ApiEntry, ops: list[Operation], token: str, origins: bool
) -> str:
    rows = "".join(
        f"<tr><td>{_method(op)}</td>"
        f"<td><code>{e(op.id)}</code>"
        + (f"<br><span class=muted>{e(op.summary[:120])}</span>" if op.summary else "")
        + f"</td><td><code>{e(op.path)}</code></td><td>{_access(op)}</td>"
        + (f"<td>{e(op.origin.value)}</td>" if origins else "")
        + f"<td>{_op_flags(librarian, entry, op, token)}</td></tr>"
        for op in ops
    )
    head = "<th></th><th>Operation</th><th>Path</th><th>Access</th>"
    head += "<th>From</th>" if origins else ""
    return f"<table><tr>{head}<th></th></tr>{rows}</table>"


def _operations(librarian: Librarian, entry: ApiEntry, token: str, query: str) -> str:
    ops = entry.operations
    if not ops:
        return "<p class=muted>No operations.</p>"
    obs = librarian.observations
    origins = len({op.origin for op in ops}) > 1
    reads = sum(1 for op in ops if op.access is Access.READ)
    writes = sum(1 for op in ops if op.writable)
    unknown = sum(1 for op in ops if op.access is Access.UNKNOWN)
    suspended = sum(1 for op in ops if obs.suspended(entry, op))
    unconfirmed = sum(1 for op in ops if op.access is Access.READ and not obs.confirmed(entry, op))
    strip = [f"<b>{_n(len(ops), 'operation')}</b>", f"{reads} reads", f"{writes} writes enabled"]
    if unknown:
        strip.append(f"<span class=note>{unknown} need review</span>")
    if suspended:
        strip.append(f"<span class=bad>{suspended} suspended</span>")
    if unconfirmed:
        strip.append(f"{unconfirmed} unconfirmed")
    parts = [f"<p class=strip>{' · '.join(strip)}</p>"]
    if len(ops) > GROUP_THRESHOLD or query:
        parts.append(_search_form(f"/apis/{entry.id}", query, "Search operations, paths, words"))
    if query:
        terms = set(tokens(query))
        matches = [
            op
            for op in rank(entry, query, ops, limit=50)
            if terms & set(tokens(f"{op.id} {op.path} {op.summary} {' '.join(op.tags)}"))
        ]
        parts.append(f"<p class=muted>{_n(len(matches), 'match', 'matches')} for {e(query)!r}.</p>")
        if matches:
            parts.append(_op_table(librarian, entry, matches, token, origins))
        return "".join(parts)
    attention = [op for op in ops if _needs_attention(librarian, entry, op)]
    if attention and len(ops) > GROUP_THRESHOLD:
        parts.append(
            "<h3>Needs attention</h3>" + _op_table(librarian, entry, attention, token, origins)
        )
    if len(ops) <= GROUP_THRESHOLD:
        parts.append(_op_table(librarian, entry, ops, token, origins))
        return "".join(parts)
    groups: dict[str, list[Operation]] = {}
    for op in ops:
        groups.setdefault(_group_of(op), []).append(op)
    for name in sorted(groups):
        members = groups[name]
        open_ = any(_needs_attention(librarian, entry, op) for op in members)
        parts.append(
            f"<details{' open' if open_ else ''}><summary>{e(name)} "
            f"<span class=muted>({len(members)})</span></summary>"
            + _op_table(librarian, entry, members, token, origins)
            + "</details>"
        )
    return "".join(parts)


def _actions(
    librarian: Librarian, entry: ApiEntry, token: str, stale: list[EnableWriteRequest]
) -> tuple[str, str]:
    base = f"/apis/{entry.id}"
    cards: list[str] = []
    if stale:
        ops = sorted({name for r in stale for name in (r.enabled or r.operations)})
        listed = ", ".join(f"<code>{e(n)}</code>" for n in ops[:8])
        more = f" and {len(ops) - 8} more" if len(ops) > 8 else ""
        cards.append(
            "<div class=card><h2>Enabled writes not live yet</h2><p class=muted>"
            f"{_n(len(stale), 'enabled write request')} ({listed}{more}) are not in the live "
            "entry: the proposal is waiting to be merged or the registry to sync. If the "
            "proposal lost them (a later decision overwrote it), put them back and merge.</p>"
            f"{_form(f'{base}/reapply', token, 'Re-apply and merge', 'quiet')}</div>"
        )
    if entry.source.kind in ("openapi", "docs"):
        cards.append(
            "<div class=card><h2>Re-check the source</h2><p class=muted>Re-read the spec or "
            "documentation now and handle any change (new operations are proposed for review)."
            f"</p>{_form(f'{base}/recheck', token, 'Re-check now', 'quiet')}</div>"
        )
    if entry.status is EntryStatus.PROVISIONAL:
        cards.append(
            "<div class=card><h2>Re-learn from documentation</h2><p class=muted>When the "
            "model couldn't find or read the right documentation, give it the page (or a spec "
            "URL). The entry is replaced and a new review pull request is opened.</p>"
            f"<form method=post action='{e(base)}/relearn'>"
            f"<input type=hidden name=t value='{e(token)}'>"
            "<label>Documentation URL<input name=docs_url placeholder='https://...' "
            "inputmode=url></label>"
            "<label>Or an OpenAPI spec URL<input name=spec_url placeholder='https://...' "
            "inputmode=url></label>"
            f"<label>Base URL<input name=base_url value='{e(entry.base_url)}' required></label>"
            "<button>Re-learn</button></form></div>"
        )
    if entry.status is EntryStatus.PROVISIONAL:
        current = e(entry.auth.type)
        options = "".join(
            f"<option value={v}{' selected' if v == entry.auth.type else ''}>{label}</option>"
            for v, label in (
                ("none", "none (public API)"),
                ("bearer", "bearer token (Authorization: Bearer ...)"),
                ("basic", "basic (username:password)"),
                ("api_key_header", "API key in a header"),
                ("api_key_query", "API key in a query parameter"),
            )
        )
        cards.append(
            f"<div class=card><h2>Authentication</h2><p class=muted>Now: <b>{current}</b>. "
            "Specs often declare none even when the API needs a key; choose what it really "
            "uses and librarian asks for the key when it is first needed. Verified entries "
            "change through review.</p>"
            f"<form method=post action='{e(base)}/auth'>"
            f"<input type=hidden name=t value='{e(token)}'>"
            f"<label>Scheme<select name=scheme>{options}</select></label>"
            "<label>Header or query parameter name (API keys only)"
            "<input name=key_name placeholder='X-Api-Key'></label>"
            "<button class=quiet>Set authentication</button></form></div>"
        )
    candidates = librarian.slash_candidates(entry)
    if candidates:
        names = ", ".join(f"<code>{e(op.id)}.{e(p.name)}</code>" for op, p in candidates[:8])
        more = f" and {len(candidates) - 8} more" if len(candidates) > 8 else ""
        cards.append(
            "<div class=card><h2>Slashes in path arguments</h2><p class=muted>"
            f"{_n(len(candidates), 'path parameter')} look like file paths or refs, which "
            f"contain '/': {names}{more}. Allowing it lets agents pass such values; empty, "
            "'.' and '..' segments stay refused. Verified entries change through review.</p>"
            f"{_form(f'{base}/slashes', token, 'Allow slashes', 'quiet')}</div>"
        )
    if entry.status is EntryStatus.BLOCKED:
        cards.append(
            "<div class=card><h2>Re-learn</h2><p class=muted>Unblock this API to re-learn it."
            "</p></div>"
        )
    else:
        cards.append(
            "<div class=card><h2>Re-learn</h2><p class=muted>Verified entries change through "
            "the registry repo.</p></div>"
        )
    zone: list[str] = []
    if entry.status is EntryStatus.BLOCKED:
        zone.append(
            "<p class=muted>Blocked: agents are refused and nobody is asked for a key.</p>"
            + _form(f"{base}/unblock", token, "Unblock", "quiet")
        )
    else:
        zone.append(
            "<p class=muted>Block: agents are refused and credential requests stop, until "
            "someone unblocks it.</p>" + _form(f"{base}/block", token, "Block this API", "deny")
        )
    if entry.secret_ref:
        zone.append(
            "<p class=muted>Invalidate the credential: its stored value is overwritten, so the "
            "next call fails and a fresh key is asked for out of band. Revoke the key at the "
            "provider as well.</p>"
            + _form(f"{base}/invalidate", token, "Invalidate the credential", "deny")
        )
    if librarian.registry.reviewed(entry.id) is None:
        zone.append(
            "<p class=muted>Forget: drop this learned API altogether. The next agent that "
            "needs it teaches it again.</p>"
            + _form(f"{base}/forget", token, "Forget this API", "deny")
        )
    if entry.per_person:
        cards.append(
            "<div class=card><h2>Sign-ins</h2><p class=muted>Each person signs in themselves. "
            "Check that every sign-in still works (each is renewed now; broken ones are dropped "
            "and the person is asked again).</p>"
            + _form(f"{base}/check-signins", token, "Check all sign-ins now", "quiet")
            + "</div>"
        )
        zone.append(
            "<p class=muted>Disconnect everyone: after rotating the OAuth client secret or "
            "changing scopes. Each person signs in again on their next request.</p>"
            + _form(f"{base}/disconnect-all", token, "Disconnect everyone", "deny")
        )
    return "".join(cards), "<div class=zone><h2>Danger</h2>" + "".join(zone) + "</div>"


def _auth(entry: ApiEntry) -> str:
    if isinstance(entry.auth, OAuth2UserAuth):
        return f"each person signs in themselves (OAuth, scopes: {e(' '.join(entry.auth.scopes))})"
    if not entry.secret_ref:
        return "none (no authentication)"
    kind = e(entry.auth.type)
    ref = f"<code>{e(entry.secret_ref)}</code>"
    write = ""
    if entry.write_secret_ref:
        write = f", writes with <code>{e(entry.write_secret_ref)}</code>"
    return f"{kind}: {ref}{write}"


def _access(op: Operation) -> str:
    if op.access is Access.READ:
        return f"read ({e(op.read_basis.value if op.read_basis else 'method')})"
    if op.writable and op.approval == "auto":
        return f"<b>write</b> (automatic, up to {op.auto_limit_per_day}/day)"
    if op.writable:
        return "<b>write</b> (enabled)"
    if op.access is Access.WRITE:
        return "write (not enabled)"
    return "unknown (needs review)"


def _current_choice(op: Operation) -> str | None:
    if op.access is Access.UNKNOWN:
        return None
    if op.access is Access.READ:
        return "read"
    if not op.writable:
        return "exclude"
    return "auto" if op.approval == "auto" else "write"


def _op_flags(librarian: Librarian, entry: ApiEntry, op: Operation, token: str) -> str:
    o = librarian.observations
    flags: list[str] = []
    asked = librarian.enablement.pending_for(entry.id, op.id) if token else None
    if asked is not None:
        flags.append(
            f"<a href='{e(librarian.enablement.link(asked))}'>"
            f"{e(', '.join(asked.requested_by))} wants to write: decide</a>"
        )
    elif token:  # no token: a page with its own decision form
        current = _current_choice(op)
        choices = [
            ("read", "read: safe to call"),
            ("write", "write: needs approval each time"),
        ]
        if op.method != "DELETE":
            choices.append(("auto", "write: approved automatically"))
        choices.append(("exclude", "exclude: agents may not call it"))
        options = "".join(
            f"<option value={v}{' selected' if v == current else ''}>{label}</option>"
            for v, label in choices
        )
        cap = (
            f"<input type=number name=cap value='{op.auto_limit_per_day}' min=1 max=100000 "
            f"class=cap aria-label='Automatic writes per day' title='Automatic writes per day'>"
            if op.method != "DELETE" and op.access is not Access.READ
            else ""
        )
        verb = "Decide" if current is None else "Change"
        flags.append(
            f"<form method=post action='/apis/{e(entry.id)}/ops/{e(op.id)}/access' class=inline>"
            f"<input type=hidden name=t value='{e(token)}'>"
            f"<select name=choice aria-label='{verb} {e(op.id)}'>{options}</select>{cap}"
            f"<button class='quiet small'>{verb}</button></form>"
        )
        if op.writable and entry.write_auth is None:
            flags.append(
                _form(
                    f"/apis/{entry.id}/ops/{op.id}/enable",
                    token,
                    "Add a write credential",
                    "quiet small",
                )
            )
    why = o.suspended(entry, op)
    if why:
        clear = _form(f"/apis/{entry.id}/ops/{op.id}/clear", token, "Clear", "quiet small")
        flags.append(f"<span class=bad>suspended: {e(why)}</span> {clear}")
    for warning in o.warnings(entry, op):
        flags.append(f"<span class=note>{e(warning)}</span>")
    return "<br>".join(flags)


def console_routes(librarian: Librarian) -> list[Route]:
    def token_for(identity: str) -> str:
        return librarian.approvals.page_token("console", identity)

    async def guard(request: Request) -> tuple[str | None, Response | None]:
        """The signed-in person; for a POST, after checking the form token."""
        identity, denied = _viewer(request, librarian.settings.approvers)
        if denied is not None or identity is None:
            return None, denied
        if request.method == "POST":
            form: Any = await request.form()
            if str(form.get("t", "")) != token_for(identity):
                return None, notice(
                    "bad", "Not allowed", "This form was not issued to you. Reload the page.", 403
                )
        return identity, None

    async def overview(request: Request) -> Response:
        identity, denied = await guard(request)
        if denied is not None or identity is None:
            return denied or notice("bad", "Error", "Something went wrong.", 400)
        pending = await _pending(librarian)
        people = len(await librarian.consents.connections()) if librarian.consents.store else None
        summary = recent = None
        if librarian.state is not None:
            summary = await librarian.state.audit_summary(datetime.now(UTC) - timedelta(days=7))
            recent = await librarian.state.recent_audit(limit=8)
        body = _dashboard(librarian, pending, people, summary, recent)
        return page("librarian", body, kind="console", active="overview")

    async def activity(request: Request) -> Response:
        identity, denied = await guard(request)
        if denied is not None or identity is None:
            return denied or notice("bad", "Error", "Something went wrong.", 400)
        api_id = request.query_params.get("api", "").strip()[:64]
        events = summary = None
        if librarian.state is not None:
            events = await librarian.state.recent_audit(api_id or None, limit=100)
            summary = await librarian.state.audit_summary(datetime.now(UTC) - timedelta(days=7))
        body = _activity_page(librarian, events, summary, api_id)
        return page("Activity", body, kind="console", active="activity")

    async def apis(request: Request) -> Response:
        identity, denied = await guard(request)
        if denied is not None or identity is None:
            return denied or notice("bad", "Error", "Something went wrong.", 400)
        query = request.query_params.get("q", "").strip()[:100]
        return page("APIs", _apis_page(librarian, query), kind="console", active="apis")

    async def waiting(request: Request) -> Response:
        identity, denied = await guard(request)
        if denied is not None or identity is None:
            return denied or notice("bad", "Error", "Something went wrong.", 400)
        body = _waiting_page(librarian, await _pending(librarian))
        return page("Waiting on a person", body, kind="console", active="waiting")

    async def grants(request: Request) -> Response:
        identity, denied = await guard(request)
        if denied is not None or identity is None:
            return denied or notice("bad", "Error", "Something went wrong.", 400)
        include = request.query_params.get("all") == "1"
        body = _grants_page(librarian, token_for(identity), include)
        return page("Grants", body, kind="console", active="grants")

    async def people(request: Request) -> Response:
        identity, denied = await guard(request)
        if denied is not None or identity is None:
            return denied or notice("bad", "Error", "Something went wrong.", 400)
        connections = await librarian.consents.connections()
        body = _people_page(librarian, token_for(identity), connections)
        return page("People", body, kind="console", active="people")

    def person(request: Request) -> str | None:
        """The signed-in person, from the proxy (None when the console is open)."""
        settings = librarian.settings.approvers
        if settings.mode != "trusted_header":
            return None
        return request.headers.get(settings.user_header, "").strip() or None

    async def me(request: Request) -> Response:
        identity, denied = await guard(request)
        if denied is not None or identity is None:
            return denied or notice("bad", "Error", "Something went wrong.", 400)
        who = person(request)
        connections = await librarian.consents.connections_of(who) if who else []
        body = _me_page(librarian, who, connections, token_for(identity))
        return page("Your sign-ins", body, kind="console", active="")

    async def me_disconnect(request: Request) -> Response:
        identity, denied = await guard(request)
        if denied is not None or identity is None:
            return denied or notice("bad", "Error", "Something went wrong.", 400)
        who = person(request)
        if who is None:
            return notice("bad", "Not signed in", "No identity from the proxy.", 403)
        form: Any = await request.form()
        api_id = str(form.get("api", "")).strip()
        entry = librarian.registry.get(api_id)
        auth = entry.auth if entry is not None and isinstance(entry.auth, OAuth2UserAuth) else None
        await librarian.consents.forget_tokens(api_id, who, auth)
        await librarian.audit.record(
            AuditEvent(
                event="person_disconnected",
                caller="console",
                principal=who,
                api=api_id,
                detail=who,
            )
        )
        return notice(
            "ok",
            "Disconnected",
            f"Your sign-in to {e(api_id)} was dropped. <a href='/me'>Back</a>.",
        )

    async def api(request: Request) -> Response:
        identity, denied = await guard(request)
        if denied is not None or identity is None:
            return denied or notice("bad", "Error", "Something went wrong.", 400)
        entry = librarian.registry.get(request.path_params["api_id"])
        if entry is None:
            return notice("note", "Unknown API", "No API has that id.", 404)
        query = request.query_params.get("q", "").strip()[:100]
        connections = await librarian.consents.connections(entry.id) if entry.per_person else []
        events = (
            await librarian.state.recent_audit(entry.id, limit=20)
            if librarian.state is not None
            else None
        )
        stale = await librarian.unapplied_enables(entry)
        body = _detail(librarian, entry, token_for(identity), query, connections, events, stale)
        return page(entry.name, body, kind="console", active="apis")

    def back(api_id: str) -> str:
        return f" <a href='/apis/{e(api_id)}'>Back to the API</a>."

    async def act(request: Request) -> Response:
        """One handler for every action; the route's last segment says which."""
        identity, denied = await guard(request)
        if denied is not None or identity is None:
            return denied or notice("bad", "Error", "Something went wrong.", 400)
        action = request.path_params["action"]
        api_id = request.path_params.get("api_id", "")
        try:
            return await _do(librarian, action, api_id, identity, request)
        except (LibrarianError, RegistryError, ApprovalError) as exc:
            where = back(api_id) if api_id else " <a href='/'>Back</a>."
            return notice("bad", "Could not do that", e(str(exc)) + where, 400)

    async def _do(
        librarian: Librarian, action: str, api_id: str, identity: str, request: Request
    ) -> Response:
        if action == "revoke":
            grant_id = request.path_params["grant_id"]
            grant = await librarian.approvals.revoke_grant(grant_id, identity)
            return notice(
                "ok",
                "Grant revoked",
                f"{e(grant.agent)} must ask again for {e(grant.api)}.{e(grant.operation)}. "
                "<a href='/'>Back</a>.",
            )
        if action == "block":
            await librarian.block(api_id, identity)
            return notice("ok", "Blocked", f"Agents are refused {e(api_id)}.{back(api_id)}")
        if action == "unblock":
            entry = await librarian.unblock(api_id, identity)
            status = e(entry.status.value)
            return notice("ok", "Unblocked", f"{e(api_id)} is {status} again.{back(api_id)}")
        if action == "forget":
            report = await librarian.forget(api_id, identity)
            what = [f"{e(api_id)} is gone."]
            if report.closed:
                links = ", ".join(f"<a href='{e(u)}'>{e(u)}</a>" for u in report.closed)
                what.append(f"Closed its review: {links}.")
            if report.review_error:
                what.append(
                    f"<span class=bad>Its review could not be closed: "
                    f"{e(report.review_error)}. Close it by hand.</span>"
                )
            if report.cancelled:
                what.append(f"{report.cancelled} waiting request(s) denied.")
            return notice("ok", "Forgotten", " ".join(what) + " <a href='/'>Back</a>.")
        if action == "invalidate":
            refs = await librarian.invalidate_credential(api_id, identity)
            return notice(
                "ok",
                "Credential invalidated",
                f"{e(', '.join(refs))} overwritten; the next call asks for a fresh key. "
                f"Revoke the old key at the provider too.{back(api_id)}",
            )
        if action == "auth":
            form = await request.form()
            scheme = str(form.get("scheme", "")).strip()
            key_name = str(form.get("key_name", "")).strip() or None
            if scheme not in ("none", "bearer", "basic", "api_key_header", "api_key_query"):
                return notice("bad", "Unknown scheme", f"No such scheme.{back(api_id)}", 400)
            updated, link = await librarian.set_auth(api_id, scheme, key_name, identity)
            review = f" Review: <a href='{e(link)}'>{e(link)}</a>." if link else ""
            return notice(
                "ok",
                "Authentication set",
                f"{e(api_id)} now uses {e(updated.auth.type)}; a key is asked for when it is "
                f"first needed.{review}{back(api_id)}",
            )
        if action == "recheck":
            report = await librarian.recheck(api_id)
            what = e(report.detail or ", ".join(report.actions) or report.status)
            icon = "bad" if report.status == "error" else "ok"
            return notice(icon, f"Re-check: {report.status}", what + back(api_id))
        if action == "relearn":
            form: Any = await request.form()
            docs_url = str(form.get("docs_url", "")).strip() or None
            spec_url = str(form.get("spec_url", "")).strip() or None
            base_url = str(form.get("base_url", "")).strip() or None
            if docs_url is None and spec_url is None:
                what = f"Give a documentation or spec URL.{back(api_id)}"
                return notice("bad", "Nothing to learn from", what, 400)
            entry = librarian.registry.get(api_id)
            result = await librarian.onboard(
                api_id,
                spec_url=spec_url,
                docs_url=docs_url,
                base_url=base_url,
                name=entry.name if entry else None,
                caller=Caller(agent="console", principal=identity, authenticated=True),
            )
            review = ""
            if result.review_url:
                url = e(result.review_url)
                review = f" Review: <a href='{url}'>{url}</a>."
            return notice(
                "ok",
                "Re-learned",
                f"{_n(len(result.entry.operations), 'operation')} "
                f"({result.read_operations} reads, {result.needs_review} need review) "
                f"from {e(result.entry.source.kind)}.{review}{back(api_id)}",
            )
        if action == "access":
            form = await request.form()
            op_id = request.path_params["op_id"]
            choice = str(form.get("choice", ""))
            cap = _int_field(form.get("cap"), 100)
            entry = librarian.registry.get(api_id)
            op = entry.operation(op_id) if entry is not None else None
            if (
                entry is not None
                and op is not None
                and choice in ("write", "auto")
                and (not op.writable or entry.write_auth is None)
            ):
                # Enabling writes: the same path as the enable page, so a missing write
                # credential is asked for.
                key = str(form.get("key", "dedicated"))
                try:
                    done, link = await librarian.enable_operation(
                        api_id, op_id, identity, key, approval=choice, cap=cap
                    )
                except LookupError:
                    return notice("note", "Already decided", "Someone else got there first.", 409)
                return notice(
                    "ok",
                    f"Writes enabled for {op_id}",
                    _enabled_text(librarian, done, link) + back(api_id),
                )
            updated, proposal = await librarian.set_operation_access(
                api_id, op_id, choice, identity, cap
            )
            return notice(
                "ok",
                f"{op_id} decided",
                f"{_proposal_text(updated, proposal)}{back(api_id)}",
            )
        if action == "slashes":
            updated, proposal, names = await librarian.allow_slashes(api_id, identity)
            return notice(
                "ok",
                "Slashes allowed",
                f"{e(', '.join(names))}. {_proposal_text(updated, proposal)}{back(api_id)}",
            )
        if action == "reapply":
            stale, proposal = await librarian.reapply_decisions(api_id, identity)
            entry = librarian.registry.get(api_id)
            ops = sorted({n for r in stale for n in (r.enabled or r.operations)})
            return notice(
                "ok",
                "Re-applied",
                f"{e(', '.join(ops))}. "
                + (_proposal_text(entry, proposal) if entry else e(proposal.note))
                + back(api_id),
            )
        if action == "enable":
            form = await request.form()
            op_id = request.path_params["op_id"]
            key = str(form.get("key", "dedicated"))
            try:
                done, link = await librarian.enable_operation(api_id, op_id, identity, key)
            except LookupError:
                return notice("note", "Already decided", "Someone else got there first.", 409)
            return notice(
                "ok",
                f"Writes enabled for {op_id}",
                _enabled_text(librarian, done, link) + back(api_id),
            )
        if action in ("disconnect-all", "check-signins"):
            entry = librarian.registry.get(api_id)
            auth = entry.auth if entry is not None else None
            if entry is None or not isinstance(auth, OAuth2UserAuth):
                return notice("bad", "Not a per-person API", f"Nothing to do.{back(api_id)}", 400)
            connections = await librarian.consents.connections(api_id)
            if action == "disconnect-all":
                for c in connections:
                    await librarian.consents.forget_tokens(api_id, c.principal, auth)
                await librarian.audit.record(
                    AuditEvent(
                        event="people_disconnected",
                        caller="console",
                        principal=identity,
                        api=api_id,
                        detail=f"{len(connections)} sign-ins",
                    )
                )
                return notice(
                    "ok",
                    "Everyone disconnected",
                    f"{_n(len(connections), 'sign-in')} dropped; each person signs in again "
                    f"on their next request.{back(api_id)}",
                )
            results = [
                (c.principal, await librarian.consents.refresh_now(entry, auth, c.principal))
                for c in connections
            ]
            broken = [(who, why) for who, why in results if why]
            await librarian.audit.record(
                AuditEvent(
                    event="signins_checked",
                    caller="console",
                    principal=identity,
                    api=api_id,
                    detail=f"{len(results) - len(broken)} ok, {len(broken)} dropped",
                )
            )
            lines = "".join(
                f"<li><span class=bad>{e(who)}</span>: {e(why or '')} (dropped)</li>"
                for who, why in broken
            )
            return notice(
                "ok" if not broken else "note",
                "Sign-ins checked",
                f"{len(results) - len(broken)} still work."
                + (f" Dropped: <ul>{lines}</ul>" if broken else "")
                + back(api_id),
            )
        if action == "disconnect":
            form = await request.form()
            principal = str(form.get("principal", "")).strip()
            if not principal:
                return notice(
                    "bad", "No person given", f"Nothing to disconnect.{back(api_id)}", 400
                )
            entry = librarian.registry.get(api_id)
            auth = (
                entry.auth if entry is not None and isinstance(entry.auth, OAuth2UserAuth) else None
            )
            await librarian.consents.forget_tokens(api_id, principal, auth)
            await librarian.audit.record(
                AuditEvent(
                    event="person_disconnected",
                    caller="console",
                    principal=identity,
                    api=api_id,
                    detail=principal,
                )
            )
            return notice(
                "ok",
                "Disconnected",
                f"{e(principal)}'s sign-in to {e(api_id)} was dropped; their next request "
                f"asks them to sign in again. Revoke it at the provider too if needed."
                f"{back(api_id)}",
            )
        if action == "clear":
            entry = librarian.registry.get(api_id)
            op = entry.operation(request.path_params["op_id"]) if entry else None
            if entry is None or op is None:
                return notice("note", "Unknown operation", "No such operation.", 404)
            if op.suspended:
                return notice(
                    "note",
                    "Suspended in the entry itself",
                    "This suspension is written into the registry entry; lift it through "
                    f"review.{back(api_id)}",
                )
            cleared = await librarian.observations.clear(entry, op)
            await librarian.audit.record(
                AuditEvent(
                    event="suspension_cleared",
                    caller="console",
                    principal=identity,
                    api=api_id,
                    operation=op.id,
                )
            )
            title = "Cleared" if cleared else "Nothing to clear"
            return notice("ok", title, f"{e(op.id)}.{back(api_id)}")
        return notice("note", "Unknown action", "No such action.", 404)

    return [
        Route("/", overview, methods=["GET"]),
        Route("/apis", apis, methods=["GET"]),
        Route("/waiting", waiting, methods=["GET"]),
        Route("/grants", grants, methods=["GET"]),
        Route("/people", people, methods=["GET"]),
        Route("/me", me, methods=["GET"]),
        Route("/activity", activity, methods=["GET"]),
        Route("/me/disconnect", me_disconnect, methods=["POST"]),
        Route("/apis/{api_id}", api, methods=["GET"]),
        Route("/grants/{grant_id}/{action}", act, methods=["POST"]),
        Route("/apis/{api_id}/ops/{op_id}/{action}", act, methods=["POST"]),
        Route("/apis/{api_id}/{action}", act, methods=["POST"]),
    ]


# -- the review page: approve or deny a learned API ----------------------------------------


def _review_body(librarian: Librarian, review: ReviewRequest, token: str, error: str | None) -> str:
    entry = librarian.registry.get(review.api)
    rows = [
        ("Site", e(review.host)),
        ("Learned by", e(review.learned_by)),
        ("What", e(review.summary)),
        ("Authentication", e(review.auth)),
        ("Pull request", f"<a href='{e(review.review_url)}'>{e(review.review_url)}</a>"),
    ]
    if entry is not None and entry.credential.create_url:
        rows.append(
            (
                "Key from",
                f"<a href='{e(entry.credential.create_url)}'>{e(entry.credential.create_url)}</a>",
            )
        )
    if entry is not None and entry.credential.scopes:
        rows.append(("Scopes", e(", ".join(entry.credential.scopes))))
    details = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows)
    warnings = "".join(f"<p class=err>{e(w)}</p>" for w in review.warnings)
    err = f"<p class=err>{e(error)}</p>" if error else ""
    hidden = f"<input type=hidden name=t value='{e(token)}'>"
    decisions = ""
    if entry is not None and review.needs_review:
        options = "".join(
            f"<option value={v}>{label}</option>"
            for v, label in (
                ("", "decide later (stays refused)"),
                ("read", "read: safe to call"),
                ("write", "write: needs approval each time"),
                ("exclude", "exclude: agents may not call it"),
            )
        )
        items = "".join(
            f"<tr><td>{_method(op)}</td><td><code>{e(op.id)}</code>"
            + (f"<br><span class=muted>{e(op.summary[:120])}</span>" if op.summary else "")
            + f"</td><td><code>{e(op.path)}</code></td>"
            f"<td><select name='decide_{e(op.id)}' aria-label='Decide {e(op.id)}'>{options}"
            "</select></td></tr>"
            for op_id in review.needs_review
            if (op := entry.operation(op_id)) is not None and op.access is Access.UNKNOWN
        )
        if items:
            decisions = (
                "<h2>These operations need a decision</h2><p class=muted>POST operations that "
                "look like reads. Undecided ones stay refused; you can decide them later in "
                "the console.</p><table><tr><th></th><th>Operation</th><th>Path</th>"
                f"<th>Decision</th></tr>{items}</table>"
            )
    operations = ""
    if entry is not None:
        operations = (
            f"<h2>Operations</h2>{_operations(librarian, entry, '', '')}"
            f"<p class=muted><a href='/apis/{e(entry.id)}'>More in the console</a></p>"
        )
    else:
        operations = "<p class=err>This API is no longer in the registry.</p>"
    return f"""<h1>Review {e(review.api_name)}</h1>
<p class=lede>{e(review.learned_by)} taught librarian this API. Approving merges the proposal as
the App and the API becomes verified once the registry syncs; denying closes it and forgets the
API.</p>{err}
<div class=card><dl>{details}</dl>{warnings}</div>
<form method=post>{hidden}<input type=hidden name=action value=approve>
{decisions}
<div class=card><h2>Approve</h2><p class=muted>Merges the pull request now, with the decisions
above.</p><button>Approve and merge</button></div></form>
{operations}
<div class=zone><h2>Deny</h2><p class=muted>Closes the pull request and forgets the API. The next
agent that needs it would teach it again.</p>
<form method=post>{hidden}<input type=hidden name=action value=deny>
<button class=deny>Deny and forget</button></form></div>"""


def review_routes(librarian: Librarian) -> list[Route]:
    async def authorized(
        request: Request, token: str
    ) -> tuple[ReviewRequest | None, str | None, Response | None]:
        review = await librarian.reviews.load(request.path_params["review_id"])
        if review is None or not librarian.reviews.check_token(review, token):
            return None, None, expired()
        identity, problem = approver_identity(request, librarian.settings.approvers)
        if problem or identity is None:
            return None, None, notice("bad", "Not allowed", e(problem or ""), 403)
        return review, identity, None

    async def show(request: Request) -> Response:
        token = request.query_params.get("t", "")
        review, _, denied = await authorized(request, token)
        if denied is not None or review is None:
            return denied or notice("bad", "Error", "Something went wrong.", 400)
        body = _review_body(librarian, review, token, None)
        return page(f"Review: {review.api_name}", body, kind="review")

    async def submit(request: Request) -> Response:
        form: Any = await request.form()
        token = str(form.get("t", ""))
        review, identity, denied = await authorized(request, token)
        if denied is not None or review is None or identity is None:
            return denied or notice("bad", "Error", "Something went wrong.", 400)
        action = str(form.get("action", ""))
        try:
            if action == "deny":
                done = await librarian.deny_review(review.id, identity)
                return notice("bad", "Denied", f"{e(done.note or 'closed')}.")
            if action == "approve":
                decisions = {
                    key.removeprefix("decide_"): str(value)
                    for key, value in form.items()
                    if key.startswith("decide_") and str(value) in ("read", "write", "exclude")
                }
                done, problem = await librarian.approve_review(review.id, identity, decisions)
                if problem:
                    body = _review_body(
                        librarian,
                        done,
                        token,
                        f"{problem}. The review stays open: merge it on GitHub, then it is done.",
                    )
                    return page(f"Review: {review.api_name}", body, 502, kind="review")
                return notice(
                    "ok",
                    "Approved",
                    f"{e(done.note or 'merged')}. "
                    f"<a href='{e(done.review_url)}'>The pull request</a>.",
                )
        except LookupError:
            return notice("note", "Already decided", "Someone else got there first.", 409)
        except (LibrarianError, RegistryError) as exc:
            body = _review_body(librarian, review, token, str(exc))
            return page(f"Review: {review.api_name}", body, 400, kind="review")
        return notice("bad", "Unknown action", "No such action.", 400)

    return [
        Route("/reviews/{review_id}", show, methods=["GET"]),
        Route("/reviews/{review_id}", submit, methods=["POST"]),
    ]


# -- the enable-writes page: an agent wants a write nobody has enabled yet -----------------


def _int_field(value: Any, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _proposal_text(entry: ApiEntry, proposal: Any) -> str:
    """What happened to a console decision, for the notice."""
    if entry.status is EntryStatus.PROVISIONAL:
        text = "Applied now."
        if proposal.link:
            text += (
                f" Carried into the review: <a href='{e(proposal.link)}'>{e(proposal.link)}</a>."
            )
        return text
    text = e(proposal.note.capitalize()) + "."
    if proposal.link:
        text += f" Proposal: <a href='{e(proposal.link)}'>{e(proposal.link)}</a>."
    return text


def _enabled_text(librarian: Librarian, done: EnableWriteRequest, link: str | None) -> str:
    text = e((done.note or "enabled").capitalize()) + "."
    if link:
        text += f" Review: <a href='{e(link)}'>{e(link)}</a>."
    if done.key_request:
        key = librarian.broker.get(done.key_request)
        if key is not None:
            text += f" <a href='{e(librarian.broker.link(key))}'>Add the write credential</a>."
    return text


def _enable_body(
    librarian: Librarian, request: EnableWriteRequest, token: str, error: str | None
) -> str:
    entry = librarian.registry.get(request.api)
    op = entry.operation(request.operation) if entry is not None else None
    operation = f"{e(request.method)} <code>{e(request.path)}</code>"
    if request.summary:
        operation += f"<br><span class=muted>{e(request.summary)}</span>"
    rows = [("Site", e(request.host))]
    ticks = ""
    if request.others:
        items: list[str] = []
        for name in request.operations:
            found = entry.operation(name) if entry is not None else None
            what = (
                f"{e(found.method)} <code>{e(found.path)}</code>"
                + (f" <span class=muted>{e(found.summary[:100])}</span>" if found.summary else "")
                if found is not None
                else "<span class=bad>no longer in the registry</span>"
            )
            items.append(
                f"<label><input type=checkbox name=op value='{e(name)}' checked> "
                f"<code>{e(name)}</code>: {what}</label>"
            )
        ticks = (
            f"<h3>{_n(len(request.operations), 'operation')} in this flow</h3>"
            "<p class=muted>Untick any that should stay disabled.</p>" + "".join(items)
        )
    else:
        rows.append(("Operation", f"<code>{e(request.operation)}</code>: {operation}"))
    rows += [
        ("Asked by", e(", ".join(request.requested_by) or "an agent")),
        ("Asked", f"{_n(request.asks, 'time')}, first {_when(request.created_at)}"),
    ]
    if request.reasons:
        rows.append(("Why", "<br>".join(e(r) for r in request.reasons)))
    key_choice = ""
    if entry is None:
        credential = "<span class=bad>the API is no longer in the registry</span>"
    elif entry.per_person:
        credential = "each person's own sign-in, like reads"
    elif entry.write_secret_ref:
        credential = f"<code>{e(entry.write_secret_ref)}</code>, already configured"
    else:
        credential = "<b>none yet</b>; choose below"
        read_key = (
            f"<label><input type=radio name=key value=read> Use the read credential "
            f"<code>{e(entry.secret_ref)}</code> for writes too.</label>"
            if entry.secret_ref
            else ""
        )
        key_choice = (
            "<h3>Write credential</h3><p class=choice><label><input type=radio name=key "
            f"value=dedicated checked> Ask for a dedicated write credential <code>{e(entry.id)}"
            "/write</code> (recommended): a key page follows, saying the least permission these "
            f"writes need.</label>{read_key}</p>"
        )
    rows.append(("Write credential", credential))
    if op is not None and op.writable:
        rows.append(("Already enabled", "yes; only the credential is missing"))
    approval_choice = (
        "<h3>Approval</h3><p class=choice><label><input type=radio name=approval value=each "
        "checked> Approve each write (a card every time, or a grant).</label>"
        "<label><input type=radio name=approval value=auto> Approve writes automatically, up to "
        "<input type=number name=cap value=100 min=1 max=100000 class=cap aria-label='per day'> "
        "a day; a person is asked again past that, DELETE operations always need approval, and "
        "one click puts approval back.</label></p>"
    )
    details = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows)
    err = f"<p class=err>{e(error)}</p>" if error else ""
    hidden = f"<input type=hidden name=t value='{e(token)}'>"
    who = e(request.requested_by[0] if request.requested_by else "An agent")
    what = (
        f"the {_n(len(request.operations), 'operation')} ticked below"
        if request.others
        else f"<code>{e(request.operation)}</code>"
    )
    button = (
        f"Enable writes for {_n(len(request.operations), 'operation')}"
        if request.others
        else f"Enable writes for {e(request.operation)}"
    )
    return f"""<h1>{who} wants to write to {e(request.api_name)}</h1>
<p class=lede>Enabling carries {what} into the entry's review
proposal (approve or deny, as always); every write still needs a person's approval or a grant.
Declining keeps it refused and quiet for a week.</p>{err}
<div class=card><dl>{details}</dl></div>
<form method=post>{hidden}<input type=hidden name=action value=enable>
<div class=card><h2>Enable</h2>{ticks}{approval_choice}{key_choice}
<button>{button}</button></div></form>
<div class=zone><h2>Decline</h2><p class=muted>The agents are told no, and this operation is not
asked about again for a week.</p>
<form method=post>{hidden}<input type=hidden name=action value=decline>
<input name=note placeholder="Why (optional, shown to the agents)" maxlength=500
aria-label="Reason"> <button class=deny>Decline</button></form></div>"""


def enable_routes(librarian: Librarian) -> list[Route]:
    async def authorized(
        request: Request, token: str
    ) -> tuple[EnableWriteRequest | None, str | None, Response | None]:
        asked = await librarian.enablement.load(request.path_params["request_id"])
        if asked is None or not librarian.enablement.check_token(asked, token):
            return None, None, expired()
        identity, problem = approver_identity(request, librarian.settings.approvers)
        if problem or identity is None:
            return None, None, notice("bad", "Not allowed", e(problem or ""), 403)
        return asked, identity, None

    async def show(request: Request) -> Response:
        token = request.query_params.get("t", "")
        asked, _, denied = await authorized(request, token)
        if denied is not None or asked is None:
            return denied or notice("bad", "Error", "Something went wrong.", 400)
        body = _enable_body(librarian, asked, token, None)
        return page(f"Enable writes: {asked.api_name}", body, kind="review")

    async def submit(request: Request) -> Response:
        form: Any = await request.form()
        token = str(form.get("t", ""))
        asked, identity, denied = await authorized(request, token)
        if denied is not None or asked is None or identity is None:
            return denied or notice("bad", "Error", "Something went wrong.", 400)
        action = str(form.get("action", ""))
        try:
            if action == "decline":
                done = await librarian.decline_writes(asked.id, identity, str(form.get("note", "")))
                return notice(
                    "bad", "Declined", f"{e(done.note or 'declined')}. The agents are told."
                )
            if action == "enable":
                key = str(form.get("key", "dedicated"))
                ticked = [str(v) for v in form.getlist("op")] if asked.others else None
                approval = "auto" if str(form.get("approval", "each")) == "auto" else "each"
                done, link = await librarian.enable_writes(
                    asked.id,
                    identity,
                    key,
                    ticked,
                    approval=approval,
                    cap=_int_field(form.get("cap"), 100),
                )
                return notice(
                    "ok",
                    f"Writes enabled for {done.enabled[0] if done.enabled else done.operation}"
                    + (f" and {len(done.enabled) - 1} more" if len(done.enabled) > 1 else ""),
                    _enabled_text(librarian, done, link),
                )
        except LookupError:
            return notice("note", "Already decided", "Someone else got there first.", 409)
        except (LibrarianError, RegistryError) as exc:
            body = _enable_body(librarian, asked, token, str(exc))
            return page(f"Enable writes: {asked.api_name}", body, 400, kind="review")
        return notice("bad", "Unknown action", "No such action.", 400)

    return [
        Route("/enable/{request_id}", show, methods=["GET"]),
        Route("/enable/{request_id}", submit, methods=["POST"]),
    ]


# -- the "require approval again" page: one click from an FYI ----------------------------


def auto_routes(librarian: Librarian) -> list[Route]:
    def allowed(
        request: Request, token: str
    ) -> tuple[Operation | None, ApiEntry | None, str | None, Response | None]:
        api_id = request.path_params["api_id"]
        op_id = request.path_params["op_id"]
        entry = librarian.registry.get(api_id)
        op = entry.operation(op_id) if entry is not None else None
        expected = librarian.approvals.page_token("auto", f"{api_id}/{op_id}")
        if entry is None or op is None or not hmac.compare_digest(expected, token):
            return None, None, None, expired()
        identity, problem = approver_identity(request, librarian.settings.approvers)
        if problem or identity is None:
            return None, None, None, notice("bad", "Not allowed", e(problem or ""), 403)
        return op, entry, identity, None

    async def show(request: Request) -> Response:
        token = request.query_params.get("t", "")
        op, entry, _, denied = allowed(request, token)
        if denied is not None or op is None or entry is None:
            return denied or notice("bad", "Error", "Something went wrong.", 400)
        state = (
            f"approved automatically, up to {op.auto_limit_per_day} a day"
            if op.writable and op.approval == "auto"
            else "already approved each time"
        )
        body = f"""<h1>Require approval again for {e(op.id)}</h1>
<p class=lede>{e(op.method)} <code>{e(op.path)}</code> on {e(entry.name)} is {e(state)}.
Putting approval back means every write to it waits for a person (or a grant) from now on;
the change lands in the entry's review and is merged.</p>
<form method=post><input type=hidden name=t value='{e(token)}'>
<button class=deny>Require approval again</button></form>
<p class=muted><a href='/apis/{e(entry.id)}'>The API in the console</a></p>"""
        return page(f"Approval: {op.id}", body, kind="review")

    async def submit(request: Request) -> Response:
        form: Any = await request.form()
        op, entry, identity, denied = allowed(request, str(form.get("t", "")))
        if denied is not None or op is None or entry is None or identity is None:
            return denied or notice("bad", "Error", "Something went wrong.", 400)
        try:
            updated, proposal = await librarian.set_operation_access(
                entry.id, op.id, "write", identity
            )
        except (LibrarianError, RegistryError) as exc:
            return notice("bad", "Could not do that", e(str(exc)), 400)
        return notice(
            "ok",
            f"{op.id} needs approval again",
            _proposal_text(updated, proposal) + f" <a href='/apis/{e(entry.id)}'>The API</a>.",
        )

    return [
        Route("/auto/{api_id}/{op_id}", show, methods=["GET"]),
        Route("/auto/{api_id}/{op_id}", submit, methods=["POST"]),
    ]


# -- the sign-in pages: a person connects their own account to an API -------------------


def _consent_body(request: ConsentRequest, token: str, go: str) -> str:
    """`go` is the provider's authorization URL: a plain link, because the pages' CSP
    (form-action 'self') would block a form submission that redirects there."""
    scopes = "".join(f"<li><code>{e(s)}</code></li>" for s in request.scopes) or "<li>none</li>"
    return f"""<h1>Sign in to {e(request.api_name)}</h1>
<p class=lede>Your agent <b>{e(request.agent)}</b> needs {e(request.api_name)} as
<b>{e(request.principal)}</b>. Sign in there once and librarian keeps your sign-in; every request
your agents make for you carries your own access, and only what you can see is shared. Agents
never see your token.</p>
<div class=card><dl><dt>Site</dt><dd>{e(request.host)}</dd><dt>Access asked</dt>
<dd><ul>{scopes}</ul></dd><dt>Waiting</dt><dd>{request.waiting} request(s)</dd></dl>
<p><a class=btn href='{e(go)}'>Sign in to {e(request.api_name)}</a></p>
<p class=muted>You will be sent to {e(request.host)}'s own sign-in page and back here.</p></div>
<div class=zone><h2>Not now</h2><p class=muted>Your agent is told you did not sign in.</p>
<form method=post><input type=hidden name=t value='{e(token)}'><input type=hidden name=action
value=deny><button class=deny>Don't sign in</button></form></div>"""


def consent_routes(librarian: Librarian) -> list[Route]:
    async def authorized(
        request: Request, token: str
    ) -> tuple[ConsentRequest | None, Response | None]:
        consent = await librarian.consents.load(request.path_params["consent_id"])
        if consent is None or not librarian.consents.check_token(consent, token):
            return None, expired()
        settings = librarian.settings.approvers
        if settings.mode == "trusted_header":
            # Only the person themselves may connect their account.
            who = request.headers.get(settings.user_header, "").strip().lower()
            if not who:
                return None, notice("bad", "Not signed in", "No identity from the proxy.", 403)
            if who != consent.principal.lower():
                return None, notice(
                    "bad",
                    "This sign-in is for someone else",
                    f"It was sent to {e(consent.principal)}; you are {e(who)}.",
                    403,
                )
        return consent, None

    def provider_url(consent: ConsentRequest) -> str | None:
        entry = librarian.registry.get(consent.api)
        auth = entry.auth if entry is not None else None
        if not isinstance(auth, OAuth2UserAuth):
            auth = entry.write_auth if entry is not None else None
        if entry is None or not isinstance(auth, OAuth2UserAuth):
            return None
        return authorize_url(
            auth,
            librarian.consents.redirect_uri,
            librarian.consents.state_for(consent),
            consent.code_verifier,
        )

    async def show(request: Request) -> Response:
        token = request.query_params.get("t", "")
        consent, denied = await authorized(request, token)
        if denied is not None or consent is None:
            return denied or notice("bad", "Error", "Something went wrong.", 400)
        go = provider_url(consent)
        if go is None:
            return notice("bad", "Not available", "This API no longer uses sign-in.", 404)
        body = _consent_body(consent, token, go)
        return page(f"Sign in to {consent.api_name}", body, kind="sign-in")

    async def submit(request: Request) -> Response:
        form: Any = await request.form()
        token = str(form.get("t", ""))
        consent, denied = await authorized(request, token)
        if denied is not None or consent is None:
            return denied or notice("bad", "Error", "Something went wrong.", 400)
        if str(form.get("action", "")) == "deny":
            await librarian.consents.deny(consent)
            return notice("note", "Not signed in", "Your agent was told.")
        return notice("bad", "Unknown action", "No such action.", 400)

    async def callback(request: Request) -> Response:
        state = request.query_params.get("state", "")
        consent_id, _, token = state.partition(".")
        consent = await librarian.consents.load(consent_id) if consent_id else None
        if consent is None or not librarian.consents.check_token(consent, token):
            return expired()
        error = request.query_params.get("error")
        if error:
            await librarian.consents.deny(consent, f"the provider said: {error}")
            return notice("bad", "Not signed in", f"{e(consent.api_name)} said: {e(error)}.")
        code = request.query_params.get("code", "")
        entry = librarian.registry.get(consent.api)
        if not code or entry is None:
            return notice("bad", "Not signed in", "No code came back from the provider.", 400)
        try:
            await librarian.consents.complete(consent, entry, code)
        except OAuthError as exc:
            return notice("bad", "Sign-in failed", e(str(exc)), 502)
        return notice(
            "ok",
            f"Signed in to {consent.api_name}",
            "Your agent carries on; librarian keeps your sign-in and never shows it to agents. "
            "You can close this page.",
        )

    return [
        Route("/connect/{consent_id}", show, methods=["GET"]),
        Route("/connect/{consent_id}", submit, methods=["POST"]),
        Route("/oauth/callback", callback, methods=["GET"]),
    ]
