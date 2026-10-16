# Troubleshooting

By symptom. Most answers start with librarian's log: every call, refusal and decision is a JSON
line with `"audit"`, and problems are logged as warnings.

## Agents and calls

**An agent gets HTTP 401 from librarian.**
Its key does not match a caller. Check the caller's `key_ref` and the secret it names, e.g.
`librarian/callers/my-agent` → `LIBRARIAN_SECRET_LIBRARIAN_CALLERS_MY_AGENT` (upper-cased, other
characters become `_`). With no `callers` configured, librarian accepts everyone: fine locally,
never shared.

**"caller ... is not allowed to use API ..." / "... may not write ..."**
The caller's `apis` / `write_apis` patterns don't include that API.

**"operation ... is not a read" / "is not enabled for writes"**
Reads only run operations classified as reads (GET, or reviewed). Writes need a verified entry,
`writable: true` on the operation, a `write_auth` credential, and the caller's `write_apis`.
When the entry is verified and only the enabling or the write credential is missing, the agent
gets `write_not_enabled` instead and the approvers are asked: the card (Slack) and the console's
waiting list lead to a page with Enable or Decline. "approvers declined to enable it" means
someone declined within the last week; the console's API page can still enable it.

**"path argument ... is not a valid path segment"**
Path arguments are single segments: no `/`, `.` or `..`. A parameter that is a file path or a git
ref can carry `allow_slash: true` (inferred when the API is learned, or *Allow slashes* on the
console's API page, which lands in the entry's review); segments are still checked.

**A plain-language request comes back `partial`.**
The planner hit `fetch_timeout_seconds` (default 100 s) and returned the calls it had made. Narrow
the request, or raise the budget while keeping it below the caller's own timeout.

**"operation ... is suspended until reviewed"**
librarian held it back: it was learned from docs or model knowledge and kept failing, or the API
changed under a reviewed entry. Fix the entry by pull request (or merge the update librarian
proposed), or, after checking by hand, `librarian registry clear <api> <operation>`.

**A task stays in `TASK_STATE_AUTH_REQUIRED`.**
It is waiting for a person: a key to add, or a write to approve. The link is in Slack (or in the
log with the default `log` notifier). The task resumes by itself; agents poll with `GetTask`.

**Numbers come back as `2529.0` over A2A.**
A2A carries JSON numbers as floating point. Over MCP they arrive as written (`2529`).

**MCP calls get HTTP 421 (older versions).**
Fixed in 0.7.0. If you restrict hosts with `mcp_allowed_hosts`, include the name clients use
(e.g. `librarian.librarian.svc:8080`).

## Learning APIs

**Calls return 401, or empty lists for private data, and no credential request appears.**
The API's spec declared no authentication (GitHub's does), so the entry says `none`. Since
0.12.1 librarian asks the model what the API uses when it learns it, and again on the first
401; if it can't tell, the console (APIs → the API → Authentication) lets you choose, and the
learned-API Slack notice says so. The next call then asks for a key.

**`fetch` fails with "the model declined this request (content filter)".**
A refusal by the model, usually a false positive on wording like "authenticated" or
"private". Rephrase, call the operation directly with `execute`, or set `fallback_model` so
librarian tries another model once. The audit log has a `model_refused` event.

**"no OpenAPI document found at well-known paths".**
Give the spec (`spec_url`) or a docs page (`docs_url` + `base_url`). With a `model` set, librarian
also looks for docs itself, and searches the web if `learning.search.provider` is set.

**"... needs a model".**
Learning from documentation, or from model knowledge, needs `model`.

**"Web search needs an Anthropic model ..." / "the model answered without searching".**
Through LiteLLM, web search only works with `model_api: anthropic`. With the OpenAI formats the
gateway refuses or silently drops the search; librarian refuses rather than use an answer from
memory. Or use `learning.search.provider: searxng` / `brave`.

**An internal API can't be reached: "... resolves to non-public address(es) ...".**
By default librarian only learns and calls public addresses, because any agent can point it at a
URL. List the domains agents may learn in `internal_hosts` (e.g. `["*.internal.example.com"]`);
`allow_private_networks: true` allows every private address and is for a laptop.

**Learned APIs disappear after a restart.**
With Postgres they are stored there. Without a database they are files on the pod's volume
(`/data/registry`): keep `persistence.enabled: true`.

## Slack

**Nothing is posted.** Check the log for `slack chat.postMessage error: <code>`:
- `invalid_auth` / `not_authed`: the bot token is wrong (`LIBRARIAN_NOTIFIER__SLACK__BOT_TOKEN`).
- `channel_not_found` / `not_in_channel`: a private channel; `/invite @librarian` there.

**Cards stay "pending" after a decision.**
Fixed in 0.8.1: upgrade.

**Nobody is @mentioned.**
`notifier.slack.approver_group` (or an entry's `owners.approver_group`) must be the user group's
**ID** (`S...`), not its handle. It is optional: without it cards still post.

**Saving the bot token with `read -rs` prints an error.**
Non-interactive shells (an editor's or assistant's `!` command runner) have no terminal to read
from. Save tokens from a real terminal, or write the file with your password manager's CLI.

## Sign-in and approvals

**The browser tab shows the identity provider's icon instead of librarian's.**
Fixed in 0.13.1: the pages now serve their own icon. Before that, the sign-in redirect made the
browser cache the provider's icon for librarian's address; reload the page after upgrading.

**"Only ... can approve this write." / "Only members of ... can approve credential requests."**
Approvals follow `writes.approvers`; adding keys needs the approver group. The group is
`approvers.group` (the chart's `oauth2Proxy.approverGroup`), as your identity provider names it
in the `groups` claim (oauth2-proxy `--oidc-groups-claim` if it is named differently; Entra ID
sends group object ids, and only when the app registration's *groupMembershipClaims* is set).
The Slack user group (`notifier.slack.approver_group`) is who gets pinged, not who may decide.
If the message names the right group and you are in it, the proxy is not forwarding the
claim: check the groups header the pod receives. Before 0.18.2 the page compared your groups
with the Slack group and said "members of None" when there was none; upgrade.

**"no one can approve: ... (approvers.group)"** when an agent asks to write.
The agent has no `owner` (or acts for no one) and no approver group is configured, so nobody
could ever approve: set `approvers.group`, or give the agent an owner who signs in.

**Approval pages show "executed by link holder".**
`approvers.mode: token` (local mode): whoever has the link decides, so no name is known. In a
deployment enable oauth2-proxy (`oauth2Proxy.enabled`), which records the signed-in person.

## The registry repo (GitHub App)

**`github-app create` says the account is not found, or the name is taken.**
Check `--repo owner/name`; pick another name with `--name`.

**"the GitHub App is not installed on owner/repo".**
Install it (github.com/apps/<app>/installations/new) on that repo. `librarian github-app check
--repo ... --app-id ... --key-file ...` confirms access.

**git-sync can't clone, or prints its usage; review pull requests fail with "not found".**
`kubectl logs deploy/librarian -c git-sync`. The repo must exist **with at least one commit**
(an empty repo has no branch) and the App must be installed on it. Give it its first commit as
the App: `librarian github-app init-repo --repo acme/api-registry --app-id ... --key-file ...`
(or `github-app create --init-repo` from the start). That creates `main`, so it works unless a
ruleset restricts creating the default branch; if it reports a rule violation, make the first
commit as a person or exclude the App from the rule for a moment.

## Kubernetes

**git-sync init container in `Init:CrashLoopBackOff`, printing its usage or
`strconv.ParseInt: parsing "9.87654321e+08"`.**
Chart versions before 0.11.2 rendered a numeric `githubApp.installationId` (or `appId`) from a
values file in scientific notation. Upgrade the chart, or quote the ids: `installationId: "987654321"`.

**The pod crashes with `port ... unable to parse string as an integer ... 'tcp://...'`.**
Kubernetes put `LIBRARIAN_PORT=tcp://...` into the pod for a Service named `librarian`, which
collides with librarian's settings. Fixed in 0.10.0 (the chart sets `enableServiceLinks: false`,
and librarian ignores such a value). Without the chart, set `enableServiceLinks: false`.

**Crash at start: `connect() got an unexpected keyword argument 'sslmode'`.**
The database driver is asyncpg, which takes `ssl=require` rather than `psql`'s `sslmode=require`.
librarian translates `sslmode` since 0.11.2; on earlier versions change the URL to `?ssl=require`.

**Requests fail with `connection is closed` after a database failover or restart.**
Fixed in 0.10.0 (connections are checked before use). Also make sure the URL is the writer
endpoint: read replicas are not supported.

**"replicas=N needs a shared database / a shared secret store".**
With more than one replica, set `LIBRARIAN_DATABASE_URL` (Postgres) and
`secrets_backend: aws` ([deploy.md, Replicas](deploy.md#replicas)).

**Helm says "replicaCount > 1: set persistence.enabled=false ...".**
Replicas need no volume ([deploy.md, Replicas](deploy.md#replicas)).

**The pod can't write keys to Secrets Manager: `AccessDeniedException`.**
The pod's role needs the policy in [deploy.md, step 4](deploy.md#4-where-keys-are-stored),
scoped to your `aws_secrets.prefix`.

**Secrets Manager calls fail with "not authorized to perform sts:AssumeRoleWithWebIdentity"
or use the node's role.** The role's trust policy must name the service account exactly:
`system:serviceaccount:<namespace>:<service account>` (`librarian:librarian` with the guide's
names), and `serviceAccount.annotations` must carry the role ARN. Restart the pods after
fixing either.

**A config value seems ignored.**
Environment variables (`LIBRARIAN_...`, nested with `__`) override the config file. In YAML,
quote `"off"`, `"on"`, `"yes"` and `"no"`: unquoted they are true/false.
