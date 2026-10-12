"""The secure credential and approval pages: the only places a person ever enters a key
or decides on a write.

A page is reached from the one-time link in the notification. The key goes from the
form straight into the secret store; it is never echoed, logged or passed to any agent.

All styling is inline, because the pages send a strict CSP (no external resources).
The colour tokens are shared verbatim with the project website (site/theme.css);
a test keeps the two copies identical.
"""

from __future__ import annotations

import html
import json
from typing import Any, Literal

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from librarian.approvals import ApprovalBroker, ApprovalError, WriteApproval
from librarian.config import ApproverSettings
from librarian.credentials import CredentialBroker, CredentialRequest

_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",  # the URL carries the one-time token
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
}

# One theme for the website, the README logo and these pages. Keep this block identical
# to the first two rules of site/theme.css (tests/test_web.py checks).
_TOKENS = (
    ":root{--ink:#2b3a67;--ink-deep:#1f2b50;--brass:#d9a441;--brass-ink:#8a6420;--paper:#f6f4ee;"
    "--surface:#ffffff;--line:#e3dfd5;--text:#1c1f2b;--muted:#5d6170;--ok:#2e7d4f;"
    '--danger:#b3261e;--link:#2b3a67;--serif:Georgia,"Iowan Old Style","Times New Roman",serif;'
    '--sans:system-ui,-apple-system,"Segoe UI",Helvetica,Arial,sans-serif;'
    '--mono:ui-monospace,"Cascadia Mono",Menlo,Consolas,monospace;--radius:10px}\n'
    "@media (prefers-color-scheme:dark){:root{--ink:#4f67b8;--ink-deep:#5f78c9;--brass:#e0b254;"
    "--brass-ink:#e0b254;--paper:#0f1420;--surface:#171d2d;--line:#2a3247;--text:#e9e7e0;"
    "--muted:#a3a8b8;--ok:#5bbf86;--danger:#f08a80;--link:#9fb1f0}}"
)

_STYLE = (
    _TOKENS
    + """
*{box-sizing:border-box}
body{margin:0;font:16px/1.55 var(--sans);color:var(--text);background:var(--paper)}
.top{max-width:680px;margin:0 auto;padding:18px 20px 0;display:flex;align-items:center;gap:10px;
font-family:var(--serif);font-size:1.3rem}
.top svg{width:28px;height:28px;flex:none}.top .muted{margin-left:auto;font-family:var(--sans)}
main{max-width:680px;margin:0 auto;padding:16px 20px 56px}
h1{font:400 clamp(1.7rem,4vw,2.2rem)/1.15 var(--serif);margin:12px 0 6px;letter-spacing:-.01em}
h2{font:400 1.3rem/1.2 var(--serif);margin:0 0 8px}
p{margin:0 0 .8em}.lede{font-size:1.05rem;color:var(--muted);margin-bottom:20px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);
padding:20px 22px;margin:16px 0}
dl{margin:0;display:grid;grid-template-columns:max-content 1fr;gap:6px 18px}
dt{color:var(--muted)}dd{margin:0;overflow-wrap:anywhere}
code{font-family:var(--mono);font-size:.92em;background:var(--paper);border:1px solid var(--line);
border-radius:5px;padding:1px 5px}
pre{font-family:var(--mono);font-size:.9em;background:var(--paper);border:1px solid var(--line);
border-radius:8px;padding:12px 14px;overflow:auto;max-height:400px;margin:8px 0 0}
table{border-collapse:collapse;width:100%;margin-top:8px}td:first-child{width:1%;white-space:nowrap}
.card dl+h2,.card table+h2,.card pre+h2{margin-top:20px}form+p{margin-top:14px}
td{padding:6px 10px 6px 0;border-top:1px solid var(--line);vertical-align:top;
overflow-wrap:anywhere}
.method{display:inline-block;font:600 12px/1 var(--mono);letter-spacing:.04em;padding:5px 8px;
border-radius:6px;background:var(--ink);color:#fff;vertical-align:middle;margin-right:6px}
.method.get{background:var(--ok)}.method.delete{background:var(--danger)}
.pg{fill:var(--ink)}.hb{fill:var(--brass)}
label{display:block;margin-top:10px}
input,select{font:inherit;color:inherit;padding:9px 11px;border-radius:8px;
border:1px solid var(--line);background:var(--paper);width:100%;margin-top:6px}
input[type=checkbox]{width:auto;margin:0 6px 0 0;vertical-align:middle}
input[type=number]{width:7em;display:inline-block}
:focus-visible{outline:2px solid var(--brass);outline-offset:2px}
button{font:inherit;font-weight:500;cursor:pointer;padding:10px 16px;border-radius:8px;
border:1px solid var(--ink);background:var(--ink);color:#fff;margin-top:14px;width:100%}
button:hover{background:var(--ink-deep);border-color:var(--ink-deep)}
button.quiet{background:transparent;color:var(--text);border-color:var(--line)}
button.quiet:hover{border-color:var(--ink);background:transparent}
button.deny{background:transparent;color:var(--danger);border-color:var(--danger)}
button.deny:hover{background:var(--danger);color:#fff}
.zone{margin-top:28px;padding-top:18px;border-top:1px solid var(--line)}
.muted{color:var(--muted);font-size:.93em}
.err{color:var(--danger);background:var(--surface);border:1px solid var(--danger);
border-radius:8px;padding:10px 14px;margin:0 0 16px}
.ok{color:var(--ok)}.bad{color:var(--danger)}.note{color:var(--muted)}
.status{display:flex;align-items:center;gap:14px;margin:28px 0 8px}
.status svg{width:44px;height:44px;flex:none}.status h1{margin:0}
a{color:var(--link)}
main.console,.top.console{max-width:1040px}main.console h2{margin-top:28px}
th{text-align:left;font-weight:600;padding:6px 10px 6px 0;border-bottom:1px solid var(--line);
color:var(--muted);font-size:.9em}
.tag{display:inline-block;font-size:12px;padding:3px 8px;border-radius:6px;background:var(--line)}
.tag.verified{background:var(--ok);color:#fff}.tag.blocked{background:var(--danger);color:#fff}
.tag.provisional{background:var(--brass);color:#1c1f2b}
form.inline{display:inline}
button.small{width:auto;margin:0 0 0 6px;padding:4px 10px;font-size:.85em}
main.console .zone button,main.console .card button{width:auto;min-width:200px}
main.console label{margin-top:10px}
details{border-top:1px solid var(--line);padding:4px 0}
details:last-of-type{border-bottom:1px solid var(--line)}
summary{cursor:pointer;font-family:var(--serif);font-size:1.1rem;padding:10px 0;
list-style-position:inside}
details table{margin:0 0 10px}.strip{color:var(--muted);margin:6px 0 14px}
.strip b{color:var(--text)}
form.search{display:flex;gap:8px;align-items:center;margin:4px 0 16px}
form.search input{margin:0;flex:1}
form.search button{margin:0;width:auto;min-width:0}
@media (max-width:480px){dl{grid-template-columns:1fr;gap:2px 0}dt{margin-top:8px}}
"""
)

# The logo mark (site/assets/mark.svg) inline, so the strict CSP needs no image source.
_MARK = (
    '<svg viewBox="0 0 512 512" aria-hidden="true">'
    '<path class=pg d="M96 212Q176 192 240 236V404Q176 372 96 380Z"/>'
    '<path class=pg d="M416 212Q336 192 272 236V404Q336 372 416 380Z"/>'
    '<rect class=hb x="248" y="130" width="16" height="282" rx="8"/>'
    '<circle class=hb cx="256" cy="104" r="42"/></svg>'
)

_ICONS = {
    "ok": '<svg viewBox="0 0 48 48" aria-hidden="true"><circle cx="24" cy="24" r="22" fill="none" '
    'stroke="currentColor" stroke-width="3"/><path d="M14 25l7 7 13-15" fill="none" '
    'stroke="currentColor" stroke-width="3.5" stroke-linecap="round" '
    'stroke-linejoin="round"/></svg>',
    "bad": '<svg viewBox="0 0 48 48" aria-hidden="true"><circle cx="24" cy="24" r="22" fill="none" '
    'stroke="currentColor" stroke-width="3"/><path d="M16 16l16 16M32 16L16 32" fill="none" '
    'stroke="currentColor" stroke-width="3.5" stroke-linecap="round"/></svg>',
    "note": '<svg viewBox="0 0 48 48" aria-hidden="true"><circle cx="24" cy="24" r="22" '
    'fill="none" '
    'stroke="currentColor" stroke-width="3"/><path d="M24 22v13M24 14v1" fill="none" '
    'stroke="currentColor" stroke-width="3.5" stroke-linecap="round"/></svg>',
}


def _page(title: str, body: str, status: int = 200, kind: str = "approval") -> HTMLResponse:
    doc = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body><header class='top {html.escape(kind)}'>{_MARK}librarian"
        f"<span class=muted>{html.escape(kind)}</span>"
        f"</header><main class={html.escape(kind)}>{body}</main></body></html>"
    )
    return HTMLResponse(doc, status_code=status, headers=_SECURITY_HEADERS)


def _notice(
    icon: Literal["ok", "bad", "note"], title: str, text: str, status: int = 200
) -> HTMLResponse:
    """A result page: one icon, one heading, one sentence."""
    body = (
        f"<div class=status><span class={icon}>{_ICONS[icon]}</span><h1>{html.escape(title)}</h1>"
        f"</div><p>{text}</p>"
    )
    return _page(title, body, status)


def _expired() -> HTMLResponse:
    return _notice(
        "note",
        "This link is no longer valid",
        "It was used already, or it expired. If the request is still open, "
        "the notification has a fresh link.",
        404,
    )


def _not_allowed(problem: str) -> HTMLResponse:
    return _notice("bad", "Not allowed", html.escape(problem), 403)


def _approver(request: Request, settings: ApproverSettings) -> tuple[str | None, str | None]:
    """Return (approver identity, error)."""
    if settings.mode == "token":
        return "link holder", None
    user = request.headers.get(settings.user_header, "").strip()
    if not user:
        return None, "You are not signed in (no identity from the auth proxy)."
    if settings.group:
        groups = {g.strip() for g in request.headers.get(settings.groups_header, "").split(",")}
        if settings.group not in groups:
            return None, f"Only members of {settings.group} can approve credential requests."
    return user, None


def _form(request_obj: CredentialRequest, token: str, names: list[str], error: str | None) -> str:
    e = html.escape
    rows = [
        ("Host", e(request_obj.host)),
        ("Stored as", f"<code>{e(request_obj.secret_ref)}</code> ({e(request_obj.auth_type)})"),
        ("Waiting", f"{request_obj.waiting} request(s)"),
    ]
    if request_obj.scopes:
        rows.append(("Needs (read-only)", e(", ".join(request_obj.scopes))))
    if request_obj.reason != "missing":
        rows.append(("Why", e(request_obj.reason)))
    details = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows)
    asked_by = e(", ".join(request_obj.requested_by)) or "an agent"
    create = ""
    if request_obj.create_url:
        create = (
            f'<p>1. <a href="{e(request_obj.create_url)}" target=_blank rel="noopener noreferrer">'
            "Create a read-only key</a> (opens the provider's page).</p><p>2. Paste it below.</p>"
        )
    hint = (
        "<p class=muted>Basic auth: paste <code>username:password</code> "
        "(e.g. <code>you@example.com:API_TOKEN</code>).</p>"
        if request_obj.auth_type == "basic"
        else ""
    )
    instructions = f"<p>{e(request_obj.instructions)}</p>" if request_obj.instructions else ""
    writes = (
        "<label class=muted><input type=checkbox name=allow_writes value=1> "
        "Also allow <b>approved writes</b> with this key (every write is still approved "
        "individually or by a grant)</label>"
        if request_obj.writes_possible
        else ""
    )
    options = "".join(f"<option>{e(n)}</option>" for n in names)
    existing = (
        f"""<div class=card><h2>Or use an existing secret</h2>
<form method=post><input type=hidden name=t value="{e(token)}">
<input type=hidden name=action value=existing>
<select name=existing_ref required aria-label="Existing secret">{options}</select>{writes}
<button class=quiet>Use this secret</button></form></div>"""
        if names
        else ""
    )
    err = f"<p class=err>{e(error)}</p>" if error else ""
    return f"""<h1>{e(request_obj.api_name)} needs a key</h1>
<p class=lede>Asked by {asked_by}. The key goes straight to the secret store; the waiting
requests resume on their own.</p>{err}
<div class=card><dl>{details}</dl></div>
<div class=card><h2>Add a key</h2>{instructions}{create}{hint}
<form method=post autocomplete=off><input type=hidden name=t value="{e(token)}">
<input type=hidden name=action value=add>
<input type=password name=value required placeholder="Paste the key" autocomplete=off
aria-label="Key">
{writes}<button>Save and test</button></form>
<p class=muted>It is never shown again, logged, or shared with any agent.
Prefer a dedicated read-only account.</p></div>
{existing}
<div class=zone><h2>Deny</h2><p class=muted>Waiting agents are told access was denied and the API
is blocked until someone re-enables it.</p>
<form method=post><input type=hidden name=t value="{e(token)}">
<input type=hidden name=action value=deny><button class=deny>Deny access</button></form></div>"""


def credential_routes(broker: CredentialBroker, approvers: ApproverSettings) -> list[Route]:
    async def show(request: Request) -> Response:
        req = await broker.load(request.path_params["request_id"])
        token = request.query_params.get("t", "")
        if req is None or not broker.check_token(req, token):
            return _expired()
        _, problem = _approver(request, approvers)
        if problem:
            return _not_allowed(problem)
        names = await broker.offerable_secrets()
        return _page(f"Access needed: {req.api_name}", _form(req, token, names, None))

    async def submit(request: Request) -> Response:
        req = await broker.load(request.path_params["request_id"])
        form: Any = await request.form()
        token = str(form.get("t", ""))
        if req is None or not broker.check_token(req, token):
            return _expired()
        approver, problem = _approver(request, approvers)
        if problem or approver is None:
            return _not_allowed(problem or "")
        action = str(form.get("action", ""))
        try:
            allow_writes = req.writes_possible and form.get("allow_writes") == "1"
            if action == "add":
                error = await broker.provide_value(
                    req.id, str(form.get("value", "")), approver, allow_writes
                )
            elif action == "existing":
                error = await broker.use_existing(
                    req.id, str(form.get("existing_ref", "")), approver, allow_writes
                )
            elif action == "deny":
                await broker.deny(req.id, approver)
                return _notice("bad", "Access denied", "Waiting agents were told.")
            else:
                error = "Unknown action."
        except LookupError:
            return _notice(
                "note", "This request was already handled", "Someone else got there first.", 409
            )
        if error:
            names = await broker.offerable_secrets()
            return _page(f"Access needed: {req.api_name}", _form(req, token, names, error), 400)
        note = html.escape(req.resolution_note or "")
        return _notice(
            "ok",
            f"{req.api_name} connected",
            f"{note} Waiting requests have resumed. You can close this page.".strip(),
        )

    return [
        Route("/credentials/{request_id}", show, methods=["GET"]),
        Route("/credentials/{request_id}", submit, methods=["POST"]),
    ]


def _identity(
    request: Request, settings: ApproverSettings
) -> tuple[str | None, set[str], str | None]:
    """(identity, groups, error). Identity is None in `token` mode (link possession)."""
    if settings.mode == "token":
        return None, set(), None
    user = request.headers.get(settings.user_header, "").strip()
    if not user:
        return None, set(), "You are not signed in (no identity from the auth proxy)."
    groups = {g.strip() for g in request.headers.get(settings.groups_header, "").split(",")}
    return user, groups - {""}, None


def _approval_form(approval: WriteApproval, token: str, error: str | None) -> str:
    e = html.escape
    req = approval.request
    method = approval.method.upper()
    badge = f"<span class='method {e(method.lower())}'>{e(method)}</span>"
    rows = [
        ("Request", f"{badge}<code>{e(approval.path)}</code>"),
        ("Operation", f"<code>{e(approval.operation)}</code>"),
        ("API", f"{e(approval.api_name)} <span class=muted>({e(approval.host)})</span>"),
        ("Agent", e(approval.agent)),
        ("On behalf of", e(approval.principal or "nobody (autonomous agent)")),
        ("Requester", e(", ".join(approval.requesters) or "none (approver group decides)")),
    ]
    if approval.summary:
        rows.append(("What it does", e(approval.summary)))
    if req.reason:
        rows.append(("Agent's reason", e(req.reason)))
    details = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows)
    args = "".join(
        f"<tr><td><code>{e(k)}</code></td><td>{e(json.dumps(v))}</td></tr>"
        for k, v in req.args.items()
    )
    args_html = f"<h2>Arguments</h2><table>{args}</table>" if args else ""
    body_html = (
        f"<h2>Request body</h2><pre>{e(json.dumps(req.body, indent=2, default=str))}</pre>"
        if req.body is not None
        else ""
    )
    hidden = f'<input type=hidden name=t value="{e(token)}">'
    for_whom = f" for {e(approval.principal)}" if approval.principal else ""
    remember = ""
    if approval.rememberable:
        pins = "".join(
            f"<label><input type=checkbox name=pin value='{e(k)}' checked> "
            f"keep <code>{e(k)}</code> = {e(json.dumps(v))}</label>"
            for k, v in req.args.items()
        )
        remember = f"""<div class=card><h2>Approve and allow similar writes</h2>
<p class=muted>Lets <b>{e(approval.agent)}</b> run <code>{e(approval.operation)}</code>
again without asking{for_whom}. Unchecked arguments may differ; the request body may
always differ. You can revoke this at any time.</p>
<form method=post>{hidden}<input type=hidden name=action value=grant>{pins}
<label>For <select name=days><option value=1>1 day</option><option value=7 selected>7 days</option>
<option value=30>30 days</option></select></label>
<label>At most <input name=max_uses type=number min=1 placeholder="unlimited"> uses</label>
<button class=quiet>Approve and allow similar</button></form></div>"""
    elif method == "DELETE":
        remember = (
            "<p class=muted>This deletes data, so it can only ever be approved one request "
            "at a time.</p>"
        )
    err = f"<p class=err>{e(error)}</p>" if error else ""
    return f"""<h1>Approve a write to {e(approval.api_name)}</h1>
<p class=lede><b>{e(approval.agent)}</b> wants to run <code>{e(approval.operation)}</code>
{for_whom}. Nothing is sent until you decide.</p>{err}
<div class=card><dl>{details}</dl>{args_html}{body_html}</div>
<div class=card><h2>Approve once</h2><p class=muted>Runs exactly this request, once.</p>
<form method=post>{hidden}<input type=hidden name=action value=once>
<button>Approve this write</button></form></div>
{remember}
<div class=zone><h2>Deny</h2><p class=muted>The agent is told the write was refused.</p>
<form method=post>{hidden}<input type=hidden name=action value=deny>
<button class=deny>Deny</button></form></div>"""


def approval_routes(approvals: ApprovalBroker, settings: ApproverSettings) -> list[Route]:
    async def authorized(
        request: Request, token: str
    ) -> tuple[WriteApproval | None, str | None, Response | None]:
        approval = await approvals.load(request.path_params["approval_id"])
        if approval is None or not approvals.check_token(approval, token):
            return None, None, _expired()
        identity, groups, problem = _identity(request, settings)
        problem = problem or approvals.may_approve(approval, identity, groups)
        if problem:
            return None, None, _not_allowed(problem)
        return approval, identity or "link holder", None

    async def show(request: Request) -> Response:
        token = request.query_params.get("t", "")
        approval, _, denied = await authorized(request, token)
        if denied is not None or approval is None:
            return denied or _notice("bad", "Error", "Something went wrong.", 400)
        return _page(f"Approve a write: {approval.api_name}", _approval_form(approval, token, None))

    async def submit(request: Request) -> Response:
        form: Any = await request.form()
        token = str(form.get("t", ""))
        approval, approver, denied = await authorized(request, token)
        if denied is not None or approval is None or approver is None:
            return denied or _notice("bad", "Error", "Something went wrong.", 400)
        action = str(form.get("action", ""))
        try:
            if action == "deny":
                await approvals.deny(approval.id, approver)
                return _notice("bad", "Write denied", "The agent was told.")
            if action == "once":
                await approvals.approve(approval.id, approver)
            elif action == "grant":
                max_uses = str(form.get("max_uses", "")).strip()
                await approvals.approve(
                    approval.id,
                    approver,
                    remember=True,
                    pin=[str(p) for p in form.getlist("pin")],
                    grant_days=int(str(form.get("days", "7")) or 7),
                    grant_uses=int(max_uses) if max_uses.isdigit() else None,
                )
            else:
                return _page("Error", _approval_form(approval, token, "Unknown action."), 400)
        except LookupError:
            return _notice(
                "note", "This write was already decided", "Someone else got there first.", 409
            )
        except (ApprovalError, ValueError) as exc:
            return _page("Error", _approval_form(approval, token, str(exc)), 400)
        extra = " Similar writes are now allowed by a grant." if action == "grant" else ""
        return _notice(
            "ok",
            "Approved",
            f"The write is being made now.{extra} "
            "The outcome is posted to the notification thread.",
        )

    return [
        Route("/approvals/{approval_id}", show, methods=["GET"]),
        Route("/approvals/{approval_id}", submit, methods=["POST"]),
    ]


# Public names for the console (console.py), which shares this chrome and these rules.
page = _page
notice = _notice
approver_identity = _approver
