"""The secure credential page: the only place a human ever enters a key.

The page is reached from the one-time link in the notification. The key goes
from the form straight into the secret store; it is never echoed, logged or
passed to any agent.
"""

from __future__ import annotations

import html
import json
from typing import Any

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

_STYLE = """
body{font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;max-width:640px;margin:40px auto;
padding:0 16px;color:#1d1d1f;background:#fafafa}
h1{font-size:22px}.card{background:#fff;border:1px solid #ddd;border-radius:10px;padding:20px;
margin:16px 0}dt{font-weight:600;margin-top:8px}dd{margin:0}code{background:#f0f0f0;padding:1px 4px;
border-radius:4px}input,select,button{font:inherit;padding:8px 10px;border-radius:6px;
border:1px solid #bbb;width:100%;box-sizing:border-box;margin-top:6px}button{cursor:pointer;
background:#1d6ef7;color:#fff;border:0;margin-top:10px}button.deny{background:#c0392b}
.muted{color:#666;font-size:13px}
pre{background:#f0f0f0;padding:10px;border-radius:6px;overflow:auto;max-height:400px}
table{border-collapse:collapse}td{padding:2px 8px;border-bottom:1px solid #ddd}
label{display:block;margin-top:6px}.err{color:#b00020}.ok{color:#137333}
@media (prefers-color-scheme:dark){body{background:#111;color:#eee}.card{background:#1b1b1b;
border-color:#333}code{background:#2a2a2a}.muted{color:#aaa}}
"""


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body>{body}</body></html>"
    )
    return HTMLResponse(doc, status_code=status, headers=_SECURITY_HEADERS)


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
        ("API", f"{e(request_obj.api_name)} <span class=muted>({e(request_obj.host)})</span>"),
        ("Stored as", f"<code>{e(request_obj.secret_ref)}</code> ({e(request_obj.auth_type)})"),
        ("Asked by", e(", ".join(request_obj.requested_by))),
        ("Waiting", f"{request_obj.waiting} request(s)"),
    ]
    if request_obj.scopes:
        rows.append(("Needs (read-only)", e(", ".join(request_obj.scopes))))
    if request_obj.reason != "missing":
        rows.append(("Why", e(request_obj.reason)))
    details = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows)
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
        "<label class=muted><input type=checkbox name=allow_writes value=1 style='width:auto'> "
        "Also allow <b>approved writes</b> with this key (every write is still approved "
        "individually or by a grant)</label>"
        if request_obj.writes_possible
        else ""
    )
    options = "".join(f"<option>{e(n)}</option>" for n in names)
    existing = (
        f"""<div class=card><h2>Use an existing secret</h2>
<form method=post><input type=hidden name=t value="{e(token)}">
<input type=hidden name=action value=existing>
<select name=existing_ref required>{options}</select>{writes}
<button>Use this secret</button></form></div>"""
        if names
        else ""
    )
    err = f"<p class=err>{e(error)}</p>" if error else ""
    return f"""<h1>Access needed: {e(request_obj.api_name)}</h1>{err}
<div class=card><dl>{details}</dl></div>
<div class=card><h2>Add a key</h2>{instructions}{create}{hint}
<form method=post autocomplete=off><input type=hidden name=t value="{e(token)}">
<input type=hidden name=action value=add>
<input type=password name=value required placeholder="Paste the key" autocomplete=off>
{writes}<button>Save and test</button></form>
<p class=muted>The key goes straight to the secret store. It is never shown again,
logged, or shared with any agent. Prefer a dedicated read-only account.</p></div>
{existing}
<div class=card><h2>Deny</h2><p class=muted>Waiting agents are told access was denied and the API
is blocked until someone re-enables it.</p>
<form method=post><input type=hidden name=t value="{e(token)}">
<input type=hidden name=action value=deny><button class=deny>Deny access</button></form></div>"""


def credential_routes(broker: CredentialBroker, approvers: ApproverSettings) -> list[Route]:
    async def show(request: Request) -> Response:
        req = await broker.load(request.path_params["request_id"])
        token = request.query_params.get("t", "")
        if req is None or not broker.check_token(req, token):
            return _page("Link expired", "<h1>This link is no longer valid</h1>", 404)
        _, problem = _approver(request, approvers)
        if problem:
            return _page("Not allowed", f"<h1>Not allowed</h1><p>{html.escape(problem)}</p>", 403)
        names = await broker.offerable_secrets()
        return _page(f"Access needed: {req.api_name}", _form(req, token, names, None))

    async def submit(request: Request) -> Response:
        req = await broker.load(request.path_params["request_id"])
        form: Any = await request.form()
        token = str(form.get("t", ""))
        if req is None or not broker.check_token(req, token):
            return _page("Link expired", "<h1>This link is no longer valid</h1>", 404)
        approver, problem = _approver(request, approvers)
        if problem or approver is None:
            return _page(
                "Not allowed", f"<h1>Not allowed</h1><p>{html.escape(problem or '')}</p>", 403
            )
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
                return _page("Denied", "<h1>Access denied</h1><p>Waiting agents were told.</p>")
            else:
                error = "Unknown action."
        except LookupError:
            return _page("Already handled", "<h1>This request was already handled</h1>", 409)
        if error:
            names = await broker.offerable_secrets()
            return _page(f"Access needed: {req.api_name}", _form(req, token, names, error), 400)
        note = html.escape(req.resolution_note or "")
        return _page(
            "Connected",
            f"<h1 class=ok>{html.escape(req.api_name)} connected</h1><p>{note}</p>"
            "<p>Waiting requests have resumed. You can close this page.</p>",
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
    rows = [
        ("API", f"{e(approval.api_name)} <span class=muted>({e(approval.host)})</span>"),
        (
            "Operation",
            f"<code>{e(approval.operation)}</code> {e(approval.method)} {e(approval.path)}",
        ),
        ("Agent", e(approval.agent)),
        ("On behalf of", e(approval.principal or "nobody (autonomous agent)")),
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
    remember = ""
    if approval.rememberable:
        pins = "".join(
            f"<label><input type=checkbox name=pin value='{e(k)}' checked style='width:auto'> "
            f"keep <code>{e(k)}</code> = {e(json.dumps(v))}</label><br>"
            for k, v in req.args.items()
        )
        for_whom = f" for {e(approval.principal)}" if approval.principal else ""
        remember = f"""<div class=card><h2>Approve and allow similar writes</h2>
<p class=muted>Lets <b>{e(approval.agent)}</b> run <code>{e(approval.operation)}</code>
again without asking{for_whom}. Unchecked arguments may differ; the request body may
always differ. You can revoke this at any time.</p>
<form method=post>{hidden}<input type=hidden name=action value=grant>{pins}
<label>For <select name=days><option value=1>1 day</option><option value=7 selected>7 days</option>
<option value=30>30 days</option></select></label>
<label>At most <input name=max_uses type=number min=1 placeholder="unlimited"> uses</label>
<button>Approve and allow similar</button></form></div>"""
    err = f"<p class=err>{e(error)}</p>" if error else ""
    return f"""<h1>Approve a write: {e(approval.api_name)}</h1>{err}
<div class=card><dl>{details}</dl>{args_html}{body_html}</div>
<div class=card><h2>Approve once</h2><p class=muted>Runs exactly this request, once.</p>
<form method=post>{hidden}<input type=hidden name=action value=once>
<button>Approve this write</button></form></div>
{remember}
<div class=card><h2>Deny</h2><form method=post>{hidden}<input type=hidden name=action value=deny>
<button class=deny>Deny</button></form></div>"""


def approval_routes(approvals: ApprovalBroker, settings: ApproverSettings) -> list[Route]:
    async def authorized(
        request: Request, token: str
    ) -> tuple[WriteApproval | None, str | None, Response | None]:
        approval = await approvals.load(request.path_params["approval_id"])
        if approval is None or not approvals.check_token(approval, token):
            return None, None, _page("Link expired", "<h1>This link is no longer valid</h1>", 404)
        identity, groups, problem = _identity(request, settings)
        problem = problem or approvals.may_approve(approval, identity, groups)
        if problem:
            return (
                None,
                None,
                _page("Not allowed", f"<h1>Not allowed</h1><p>{html.escape(problem)}</p>", 403),
            )
        return approval, identity or "link holder", None

    async def show(request: Request) -> Response:
        token = request.query_params.get("t", "")
        approval, _, denied = await authorized(request, token)
        if denied is not None or approval is None:
            return denied or _page("Error", "<h1>Error</h1>", 400)
        return _page(f"Approve a write: {approval.api_name}", _approval_form(approval, token, None))

    async def submit(request: Request) -> Response:
        form: Any = await request.form()
        token = str(form.get("t", ""))
        approval, approver, denied = await authorized(request, token)
        if denied is not None or approval is None or approver is None:
            return denied or _page("Error", "<h1>Error</h1>", 400)
        action = str(form.get("action", ""))
        try:
            if action == "deny":
                await approvals.deny(approval.id, approver)
                return _page("Denied", "<h1>Write denied</h1><p>The agent was told.</p>")
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
            return _page("Already handled", "<h1>This write was already decided</h1>", 409)
        except (ApprovalError, ValueError) as exc:
            return _page("Error", _approval_form(approval, token, str(exc)), 400)
        extra = " Similar writes are now allowed by a grant." if action == "grant" else ""
        return _page(
            "Approved",
            f"<h1 class=ok>Approved</h1><p>The write is being made now.{extra} "
            "The outcome is posted to the notification thread.</p>",
        )

    return [
        Route("/approvals/{approval_id}", show, methods=["GET"]),
        Route("/approvals/{approval_id}", submit, methods=["POST"]),
    ]
