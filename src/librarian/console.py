"""The operator console: what librarian knows, what is waiting on people, and a few
actions for the people who run it.

Served on the pages listener (behind the auth proxy) at `/`, under the same sign-in
rules as the approval pages (`approvers`). `approvers.console` opens it without
sign-in on a laptop, or switches it off. Every action is a plain form whose token is
bound to the signed-in person (so another site cannot submit it for them), and every
action is written to the audit log with who did it.
"""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime
from typing import Any

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from librarian.approvals import ApprovalError, Grant, WriteApproval
from librarian.audit import AuditEvent
from librarian.calls import Caller
from librarian.config import ApproverSettings
from librarian.credentials import CredentialRequest, RequestState
from librarian.opsearch import rank, tokens
from librarian.registry.models import Access, ApiEntry, EntryStatus, Operation
from librarian.registry.store import RegistryError
from librarian.service import Librarian, LibrarianError
from librarian.web import approver_identity, notice, page

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


def _overview(
    librarian: Librarian,
    credentials: list[CredentialRequest],
    approvals: list[WriteApproval],
    token: str,
    query: str = "",
) -> str:
    entries = librarian.registry.all()
    total = len(entries)
    if query:
        needle = query.lower()
        entries = [
            x for x in entries if needle in f"{x.id} {x.name} {x.host} {x.description}".lower()
        ]
    grants = [g for g in librarian.approvals.grants() if g.usable(datetime.now(UTC))]
    if credentials or approvals:
        rows = "".join(
            f"<tr><td>Key for <b>{e(r.api_name)}</b> <span class=muted>({e(r.host)})</span></td>"
            f"<td>{e(', '.join(r.requested_by) or 'an agent')}</td>"
            f"<td>{r.waiting} waiting, {_when(r.created_at)}</td>"
            f"<td><a href='{e(librarian.broker.link(r))}'>Review</a></td></tr>"
            for r in credentials
        ) + "".join(
            f"<tr><td>Write to <b>{e(a.api_name)}</b>: {_method_text(a)}"
            f"<code>{e(a.operation)}</code></td>"
            f"<td>{e(a.agent)}{' for ' + e(a.principal) if a.principal else ''}</td>"
            f"<td>{_when(a.created_at)}</td>"
            f"<td><a href='{e(librarian.approvals.link(a))}'>Review</a></td></tr>"
            for a in approvals
        )
        waiting = (
            f"<table><tr><th>What</th><th>Who asked</th><th>Since</th><th></th></tr>{rows}</table>"
        )
    else:
        waiting = "<p class=muted>Nothing is waiting on a person.</p>"
    api_rows = "".join(
        f"<tr><td><a href='/apis/{e(entry.id)}'>{e(entry.name)}</a> "
        f"<span class=muted>{e(entry.id)}</span></td><td>{_tag(entry.status)}</td>"
        f"<td>{e(entry.host)}</td>"
        f"<td>{_n(sum(1 for op in entry.operations if op.access is Access.READ), 'read')}, "
        f"{_n(sum(1 for op in entry.operations if op.writable), 'write')}</td>"
        f"<td>{_learned_from(entry)}</td><td>{', '.join(_flags(librarian, entry))}</td></tr>"
        for entry in entries
    )
    apis = (
        f"<table><tr><th>API</th><th>Status</th><th>Host</th><th>Operations</th>"
        f"<th>Learned from</th><th></th></tr>{api_rows}</table>"
        if entries
        else (
            f"<p class=muted>No API matches {e(query)!r}.</p>"
            if query
            else "<p class=muted>No APIs yet. The first agent that needs one teaches it.</p>"
        )
    )
    if total > 8 or query:
        apis = _search_form("/", query, "Filter APIs by name, id or host") + apis
    errors = "".join(
        f"<p class=err><code>{e(path)}</code>: {e(problem)}</p>"
        for path, problem in librarian.registry.errors.items()
    )
    grant_rows = "".join(
        f"<tr><td>{e(g.agent)}{' for ' + e(g.principal) if g.principal else ''}</td>"
        f"<td><code>{e(g.api)}.{e(g.operation)}</code></td>"
        f"<td>{_pins(g)}</td><td>{_expiry(g)}</td><td>{e(g.source)}, by {e(g.created_by)}</td>"
        f"<td>{_form(f'/grants/{g.id}/revoke', token, 'Revoke', 'deny small')}</td></tr>"
        for g in grants
    )
    grants_html = (
        f"<table><tr><th>Agent</th><th>Operation</th><th>Pinned</th><th>Limits</th>"
        f"<th>Source</th><th></th></tr>{grant_rows}</table>"
        if grants
        else "<p class=muted>No standing grants: every write is approved one by one.</p>"
    )
    return f"""<h1>What librarian knows</h1>
<p class=lede>{_n(total, "API")}, {len(credentials) + len(approvals)} waiting on a person,
{_n(len(grants), "standing grant")}.</p>
<h2>Waiting on a person</h2>{waiting}
<h2>APIs</h2>{errors}{apis}
<h2>Grants</h2><p class=muted>Revoking a grant is the kill switch: the agent's next such write
waits for a person again. Grants from the config file come back on restart.</p>{grants_html}"""


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


def _detail(librarian: Librarian, entry: ApiEntry, token: str, query: str = "") -> str:
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
    details = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows)
    notes = f"<h2>Notes</h2><pre>{e(entry.notes)}</pre>" if entry.notes else ""
    return f"""<p><a href='/'>All APIs</a></p>
<h1>{e(entry.name)}</h1>
<p class=lede>{e(entry.description or entry.id)}</p>
<div class=card><dl>{details}</dl></div>
<h2>Operations</h2>{_operations(librarian, entry, token, query)}{notes}
<h2>Actions</h2>{_actions(librarian, entry, token)}"""


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


def _actions(librarian: Librarian, entry: ApiEntry, token: str) -> str:
    base = f"/apis/{entry.id}"
    cards: list[str] = []
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
    if entry.status is EntryStatus.BLOCKED:
        cards.append("<p class=muted>Unblock this API to re-learn it.</p>")
    else:
        cards.append("<p class=muted>Verified entries change through the registry repo.</p>")
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
    return "".join(cards) + "<div class=zone><h2>Danger</h2>" + "".join(zone) + "</div>"


def _auth(entry: ApiEntry) -> str:
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
    if op.writable:
        return "<b>write</b> (enabled)"
    if op.access is Access.WRITE:
        return "write (not enabled)"
    return "unknown (needs review)"


def _op_flags(librarian: Librarian, entry: ApiEntry, op: Operation, token: str) -> str:
    o = librarian.observations
    flags: list[str] = []
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
        credentials = await _pending_credentials(librarian)
        query = request.query_params.get("q", "").strip()[:100]
        body = _overview(
            librarian, credentials, librarian.approvals.pending(), token_for(identity), query
        )
        return page("librarian", body, kind="console")

    async def api(request: Request) -> Response:
        identity, denied = await guard(request)
        if denied is not None or identity is None:
            return denied or notice("bad", "Error", "Something went wrong.", 400)
        entry = librarian.registry.get(request.path_params["api_id"])
        if entry is None:
            return notice("note", "Unknown API", "No API has that id.", 404)
        query = request.query_params.get("q", "").strip()[:100]
        body = _detail(librarian, entry, token_for(identity), query)
        return page(entry.name, body, kind="console")

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
        Route("/apis/{api_id}", api, methods=["GET"]),
        Route("/grants/{grant_id}/{action}", act, methods=["POST"]),
        Route("/apis/{api_id}/ops/{op_id}/{action}", act, methods=["POST"]),
        Route("/apis/{api_id}/{action}", act, methods=["POST"]),
    ]
