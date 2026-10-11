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
URL. `allow_private_networks: true` allows private ones (all of them).

**Learned APIs disappear after a restart.**
They live in the registry overlay on the pod's volume (`/data/registry` in the chart) until their
pull request is merged. Keep `persistence.enabled: true`.

## Slack

**Nothing is posted.** Check the log for `slack chat.postMessage error: <code>`:
- `invalid_auth` / `not_authed`: the bot token is wrong (`LIBRARIAN_NOTIFIER__SLACK__BOT_TOKEN`).
- `channel_not_found` / `not_in_channel`: a private channel; `/invite @librarian` there.

**Cards stay "pending" after a decision.**
Fixed in 0.8.1: upgrade.

**Nobody is @mentioned.**
`notifier.slack.approver_group` (or an entry's `owners.approver_group`) must be the user group's
**ID** (`S...`), not its handle.

## Sign-in and approvals

**"Only ... can approve this write." / "Only members of ... can approve credential requests."**
Approvals follow `writes.approvers`; adding keys needs the approver group. The group comes from
your identity provider's `groups` claim (oauth2-proxy `--oidc-groups-claim` if it is named
differently).

**Approval pages show "executed by link holder".**
`approvers.mode: token` (local mode): whoever has the link decides, so no name is known. In a
deployment enable oauth2-proxy (`oauth2Proxy.enabled`), which records the signed-in person.

## The registry repo (GitHub App)

**`github-app create` says the account is not found, or the name is taken.**
Check `--repo owner/name`; pick another name with `--name`.

**"the GitHub App is not installed on owner/repo".**
Install it (github.com/apps/<app>/installations/new) on that repo. `librarian github-app check
--repo ... --app-id ... --key-file ...` confirms access.

**git-sync can't clone.** `kubectl logs deploy/librarian -c git-sync`. The repo must exist with at
least one commit, and the App must be installed on it.

## Kubernetes

**The second replica is stuck in `Pending`.**
The default volume is ReadWriteOnce. Use one replica, or a ReadWriteMany volume
([deploy.md](deploy.md#more-than-one-replica)).

**The pod can't write keys to Secrets Manager: `AccessDeniedException`.**
The pod's role needs the policy in [deploy.md, step 4](deploy.md#4-where-keys-are-stored),
scoped to your `aws_secrets.prefix`.

**A config value seems ignored.**
Environment variables (`LIBRARIAN_...`, nested with `__`) override the config file. In YAML,
quote `"off"`, `"on"`, `"yes"` and `"no"`: unquoted they are true/false.
