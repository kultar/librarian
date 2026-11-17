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

**"caller ... is limited on API ...: ..."**
The agent's `limits` for that API (under `callers`, or the Substrate rule it matches) do not
cover the call:
- *key='OPS-9' is outside what it may use (PAY-\*)*: the argument's value matches none of the
  patterns. Matching is exact and case-sensitive.
- *the argument 'team' must be given*: a limited argument that the API treats as optional;
  without it the answer would not be limited.
- *the operation ... takes no such argument*: it takes none of the limited arguments, so it
  cannot be held to them. If the agent should have it, name it under `allow_without`.
- *it may not use the operation ...*: `operations` lists what it may use.

`capabilities` shows an agent only the operations it can reach, with its limits.

**"operation ... is not a read" / "is not enabled for writes"**
Reads only run operations classified as reads (GET, or reviewed). Writes need a verified entry,
`writable: true` on the operation, a `write_auth` credential, and the caller's `write_apis`.
When the entry is verified and only the enabling or the write credential is missing, the agent
gets `write_not_enabled` instead and the approvers are asked: the card (Slack) and the console's
waiting list lead to a page with Enable or Decline. "approvers declined to enable it" means
someone declined within the last week; the console's API page can still enable it.

**"invalid plan: ..."**
The plan itself is not well formed; the words after it say where (`steps.1.args.repo`) and
what. A step needs an `id` (lower case letters, digits, `-` and `_`, starting with a letter)
and an `operation`. A reference is written `{"$from": "<step id>.<field>"}`, as the whole
value, and can only name a step that comes before it. After the step id come field names
(letters, digits, `-` and `_`) and positions in a list, separated by dots: `pull.head.sha`,
`files.0.sha`.

**"step 3 (commit): ..."** (when asking for a plan)
That step would be refused as a write on its own, for the reason given: fix it and ask
again. Nobody was asked to approve anything. "the argument ... must be given in the plan
itself" means the agent's limits restrict that argument, so its value has to be written in
the plan instead of being taken from an earlier step. "is a DELETE: what it deletes has to
be written in the plan itself" means a DELETE step used `$from`: an approver has to see what
is removed, so write the value in, or ask for the delete on its own once the id is known.

**"plan stopped: step 3 of 5 (commit) ..."**
The plan was approved and ended at that step. What follows says why, and which steps were
made before it; nothing is undone, and the steps after it were not sent. `write_status`
gives each step's state and answer.
- *did not succeed: HTTP 422*: the API refused that step. Its answer is in the step's result.
- *could not be prepared: what step 'blob' answered has no 'sha'*: an earlier step did not
  answer what a reference expected. Look at that step's answer and correct the reference.
- *is no longer allowed: ...*: checked again just before it was sent, the step would be
  refused: a value filled in from an earlier answer is not a valid argument, or the
  operation was disabled or suspended after the plan was approved.
- *got no answer from ...: ... It was not sent*: nothing left librarian for that step.
- *... It may have reached ...*: the request went out and no answer came back; the step's
  state is `unknown`. Read the resource before asking for anything again.
- *interrupted: librarian stopped during this plan*: the replica running it was stopped. The
  note says how many steps had answered and which one may or may not have arrived.

To finish a stopped plan, ask for a new one with the steps that are left, writing in the
values the steps already made returned (they are in `write_status`).

**"a plan of 12 writes is more than ... may make here in a minute"**
A plan counts against the write rate limit as as many writes as it has steps, so one with
more steps than `writes.rate_limit_per_minute` (10 by default) can never be accepted. Split
it, or raise the limit. "write rate limit exceeded ... (a plan of 5 writes counts as 5)" is
the ordinary case: the agent has made other writes in the last minute; ask again shortly.

**"path argument ... is not a valid path segment"**
Path arguments are single segments: no `/`, `.` or `..`. A parameter that is a file path or a git
ref can carry `allow_slash: true` (inferred when the API is learned, or *Allow slashes* on the
console's API page, which lands in the entry's review); segments are still checked.

**A plain-language request comes back `partial`.**
The planner hit `fetch_timeout_seconds` (default 100 s) and returned the calls it had made. Narrow
the request, or raise the budget while keeping it below the caller's own timeout.

**"operation ... is suspended until reviewed"**
librarian held it back: it was learned from docs or model knowledge and kept failing, or the API
changed under an approved entry. Approve the update librarian found (the console lists it
under *Waiting*), or, after checking by hand, clear the suspension on the API's page or with
`librarian registry clear <api> <operation>`.

**"... gave no answer: ..."**
The API could not be reached, or did not finish answering; the words after the colon say which:
it could not be reached (with the reason when there is one: the name did not resolve, the
connection was refused, its TLS certificate was not accepted), it did not answer in time, the
connection broke, or librarian has too many calls to it in progress. Nothing is wrong with the
request. For a read, try again later. For a write the message ends one of two ways:
- *The write was not sent*: nothing left librarian. It is safe to ask again.
- *The write may have reached it*: the request went out and no answer came back. Read the
  resource first to see whether the change was made, and use an `idempotency_key` so a repeat
  cannot be sent twice.

One request to an API (connecting, sending and reading the whole answer) may take at most twice
`request_timeout_seconds` (so a minute by default), however slowly the answer trickles in.
librarian's log has the HTTP client's own words for the failure, under `got no answer`.

**A write is `failed` with "interrupted".**
The replica that was sending it stopped before it had an answer (a rollout, a crash), so nobody
knows whether the write was made. librarian closes such a write after fifteen minutes (ten
times `request_timeout_seconds` when that is longer), says so, and never sends it again by
itself. Check the resource, then ask again if it is needed.

**JSON from an API arrives as text, or a number arrives as `null`.**
An answer is passed on as data only when everything after librarian can handle it. JSON nested
more than 100 levels deep, JSON cut off at `max_response_bytes`, and anything that does not
parse are passed on as text. `NaN` and `Infinity`, which are not JSON, become `null`.

**An answer contains `[redacted]`.**
The API sent back the credential librarian called it with (an echo endpoint, a link that repeats
the query string, an error that quotes the key), and librarian removed it: agents never get a
key. Nothing else in the answer is changed. If ordinary data is affected, the stored credential
is a word that also occurs in the data; values shorter than six characters are never replaced.

**"... contains a line break or another character that cannot be sent in an HTTP header"**
The stored key has a line break (or a character outside ASCII) in the middle of it, typically
two lines pasted as one value. Store it again: on the credential page, or in the secret store
librarian reads. Whitespace around a key, such as the line break at the end of a file, is
ignored and needs no fixing.

**A task stays in `TASK_STATE_AUTH_REQUIRED`.**
It is waiting for a person: a key to add, or a write to approve. The link is in Slack (or in the
log with the default `log` notifier). The task resumes by itself; agents poll with `GetTask`.

**Numbers come back as `2529.0` over A2A.**
A2A carries JSON numbers as floating point. Over MCP they arrive as written (`2529`).

**MCP calls get HTTP 421 (older versions).**
Fixed in 0.7.0. If you restrict hosts with `mcp_allowed_hosts`, include the name clients use
(e.g. `librarian.librarian.svc:8080`).

**"... did not issue a token" / "... is not installed on ..." / "... may not assume ..."**
The API's authentication makes a credential for each call (`oauth2_client`, `github_app`,
`aws_sigv4`) and making one failed in a way a new key would not fix, so no approver is asked
for one. The message says what to change, in the console under the API's *Authentication*:
- *the token endpoint did not issue a token (HTTP 400, invalid_scope)*: the scopes or the
  audience are not what the provider expects for this client.
- *the GitHub App is not installed on owner/repo*: install the App there, or the agent has no
  business in that repository.
- *this operation names no repository or organisation*: operations such as `/user` or search
  cannot tell librarian whose installation to use; set `installation_id`.
- *librarian may not assume arn:... (AccessDenied)*: the role's trust policy must name
  librarian's own role, and librarian's role must be allowed `sts:AssumeRole` on it.
- *librarian has no AWS identity to sign with*: its pod has no role (IRSA or Pod Identity).
- *refused ... (HTTP 401) although the credential was made for it a moment ago*: the token
  was issued but the API does not accept it: a missing scope, the wrong audience, a permission
  the App does not have.

A refused client secret or App private key is different: that is asked for like any key
("the token endpoint refused the client id and secret", "the GitHub App's private key is not
usable"). A private key pasted on one line is fine; librarian restores its line breaks.

**Was a key ever stored where it should not be?**
`librarian doctor` compares every credential in the secret store with every row of the database
and every line of the audit file, in each form a credential can take there, and names what it
finds by the secret's reference; it never prints a credential. Versions before 0.20 could
store one (an API that sent its key back in the answer to a write; an error that quoted a key
stored with a line break), so run it once after upgrading:

```bash
kubectl -n librarian exec deploy/librarian -- librarian doctor
# demo/write is stored in librarian_documents approval/5027a47b... (data)
kubectl -n librarian exec deploy/librarian -- librarian doctor --redact   # replaces the copies
```

A credential that was found was readable by whoever can read that table: replace it at the
API. `--redact` then removes the copies from the database (backups and write-ahead logs made
before keep theirs). The audit file is only reported, since librarian appends to it while it
runs. The command also lists stored credentials that cannot be used (a line break inside), and
exits 1 while anything is left to do.

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

**The channel fills with "ran ... under a standing grant" cards.**
A grant covering a multi-step flow posts one card per step. `writes.announce_grants: false`
(chart: `config.writes.announce_grants`) stops the post for every grant; the console's Grants
page turns notices off per grant. Every use stays in the audit log and on the Activity page.

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

**GitHub answers 403 "Resource protected by organization SAML enforcement".**
The token is fine but not authorized for an organization that enforces SAML single sign-on.
On GitHub open the token's settings and use *Configure SSO* for that organization, then retry.
Since 0.19.1 the write's error and the credential page say so.

**"the credential is valid but lacks permission" near the top of every hour (GitHub).**
That was a rate limit, not a permission problem: a token shared with another consumer runs
out of its hourly core limit, and GitHub answers 403 with `X-RateLimit-Remaining: 0`. Since
0.19.1 librarian reports it as rate limiting, with the reset time, in the error and in the audit
record (`rate limited by the API: ...`); the audit record's `upstream` field keeps the API's
own message. Give librarian its own token, or raise the limit (a GitHub App gets its own).

**A retried write ran twice / opened a second approval.**
Before 0.19.1 an `idempotency_key` was matched in one pod's memory only; with two replicas a
retry on the other pod sent the write again, and grant or automatic writes were never matched.
Upgrade; the key is now kept in the database with the write.

**"no one can approve: ... (approvers.group)"** when an agent asks to write.
The agent has no `owner` (or acts for no one) and no approver group is configured, so nobody
could ever approve: set `approvers.group`, or give the agent an owner who signs in.

**Approval pages show "executed by link holder".**
`approvers.mode: token` (local mode): whoever has the link decides, so no name is known. In a
deployment enable oauth2-proxy (`oauth2Proxy.enabled`), which records the signed-in person.

## The record repository (GitHub App)

**A decision was made, but the repository has no commit for it.**
The decision is in effect regardless: the database decides, the repository is only written to.
The console's overview lists APIs whose record is behind, and the API's page says why and
offers *Write it now*; librarian also retries by itself. The usual reasons:

- *"takes no direct commits to main ... refused to merge"*: a rule requires pull requests and
  also something the App cannot give (an approving review, a status check). Add the App to the
  rule's bypass list, or merge the pull request librarian opened by hand.
- *"Resource not accessible by integration"*: the App lacks **Contents: write** (and **Pull
  requests: write** for the fallback), or is not installed on the repository.
- GitHub was unreachable: nothing to do, the next retry writes it.

**I edited a file in the repository (or merged a pull request there) and nothing changed.**
By design since 0.20: librarian writes the record and never reads it back. Make the change in
the console.

**"<id>.yaml changed on disk, but decisions were made about this API in librarian".**
A registry file differs from the database's entry and people have decided things about that API
in librarian, so the database wins. If the file is what you want, forget the API in the console:
the file is imported afresh. If the directory is a git-sync checkout of the record repository,
switch `registry.gitSync` off: librarian's own commits arrive there a moment late.

**After upgrading to 0.20 some APIs are missing.**
The registry directory was not there when 0.20 first started (git-sync switched off in the same
upgrade), so nothing was imported. The entries are still in the repository:
`kubectl exec deploy/librarian -c librarian -- librarian registry restore --config /etc/librarian/config.yaml`.

**`github-app create` says the account is not found, or the name is taken.**
Check `--repo owner/name`; pick another name with `--name`.

**"the GitHub App is not installed on owner/repo".**
Install it (github.com/apps/<app>/installations/new) on that repo. `librarian github-app check
--repo ... --app-id ... --key-file ...` confirms access.

**git-sync can't clone, or prints its usage; writing the record fails with "not found".**
`kubectl logs deploy/librarian -c git-sync` (when `registry.gitSync` is on). The repo must exist **with at least one commit**
(an empty repo has no branch) and the App must be installed on it. Give it its first commit as
the App: `librarian github-app init-repo --repo acme/api-registry --app-id ... --key-file ...`
(or `github-app create --init-repo` from the start). That creates `main`, so it works unless a
ruleset restricts creating the default branch; if it reports a rule violation, make the first
commit as a person or exclude the App from the rule for a moment.

## Agent Substrate and kagent

An agent's call is refused before it reaches any API. kagent only shows that discovering the
tools failed ("Forbidden", "Unauthorized") and runs the agent without them, so look in
librarian's log, which says why: `refused a Substrate caller (HTTP 403): ...`. The reasons:

**401 "actor token was not issued for librarian (audience 'librarian')".**
The gateway minted the token for another audience. The `RemoteMCPServer` (or the actor's
`EgressPolicy`) must list librarian's `substrate.audience` under `actorJwt.audiences`.

**401 "actor token cannot be used to look up its agent: add 'kagent-agent-info' to the audiences".**
With `substrate.kagent_url` set, the same token is shown to kagent to learn which Agent the actor
is, and kagent only accepts tokens that carry that audience. The chart's `RemoteMCPServer`
includes it; add it to one you wrote yourself.

**401 "actor token is from an untrusted issuer".**
`substrate.issuer` must equal the token's `iss` exactly: Substrate's `actorJWT.issuer`, by default
`https://idp.<namespace Substrate is installed in>.svc`.

**401 "kagent does not recognise this actor as a current agent session".**
The session is obsolete or its Agent was deleted or recreated. A new session works.

**403 "agent 'payments/release-notes' is not allowed to use librarian: no rule under
substrate.rules matches it".**
Add a rule for it, or a catch-all `- match: {}` as the last rule. The name in the message is what
to match on (`namespace` / `name`).

**503 "cannot verify actor tokens right now".**
librarian could not fetch the issuer's signing keys. From librarian's pod,
`https://idp.ate-system.svc/.well-known/openid-configuration` must answer, and its certificate
must verify against `substrate.ca_file`. With the chart, check the pod has the `substrate-ca`
volume and that a ClusterTrustBundle for the signer exists:
`kubectl get clustertrustbundles -l podcert.ate.dev/canarying=live`.

**503 "cannot look up the agent behind this actor right now".**
librarian could not reach kagent's controller at `substrate.kagent_url` (default
`http://kagent-controller.kagent.svc:8083`), or a NetworkPolicy drops the connection.

**The agent has no librarian tools, and librarian's log shows no request from it at all.**
The call never left the sandbox. The agent runtime's log (the worker pod, e.g.
`kubectl -n kagent logs deploy/kagent-default`) says `failed to list MCP tools`:
- `lookup librarian.librarian.svc ... server misbehaving`: the `RemoteMCPServer`'s URL uses a
  short service name. A sandbox's DNS answers only for the exact hostnames in the actor's egress
  policy, without search domains: use `<service>.<namespace>.svc.cluster.local` (the chart does,
  since 0.20; set `substrate.clusterDomain` if your cluster's domain differs).
- a timeout: raise the `RemoteMCPServer`'s `timeout` (kagent's default is 30 seconds).
Also check the Agent's conditions (`kubectl describe agent <name>`): the `RemoteMCPServer` must be
in a namespace kagent watches, and its `allowedNamespaces` must admit the agent's namespace. The
agent discovers the tools at run time, so kagent's tool picker never lists them.

**Calls work but everything is recorded as one caller per session, or grants never apply.**
`substrate.kagent_url` is not set, so the caller is the actor (one session) instead of the Agent.

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
