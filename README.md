# librarian

**An A2A agent that learns APIs on demand and gives other agents safe, audited access to them: reads freely within policy, writes only with human approval.**

Agents shouldn't each carry their own Jira token, GitHub token and API client. With librarian,
agents ask one specialist, over [A2A](https://a2a-protocol.org), for the data they need. The
librarian learns the API the first time it is needed (from its OpenAPI spec, or from its
documentation when there is no spec), asks a human for a
credential (once, out of band), and from then on serves every agent, without ever handing the
credential out.

```
 your agents ──A2A──► librarian ──(policy engine)──► Jira, GitHub, internal APIs, ...
                         │
                         ├── registry (git): what each API is and which operations are reads
                         ├── secret store: credentials (agents only ever see references)
                         └── planner (Pydantic AI, optional) ──► any model / LiteLLM
```

> Status: **alpha (v0.10)**. Expect breaking changes.

## Why

- **One choke point.** Every call from every agent goes through one deterministic policy engine and
  lands in one audit log.
- **Credentials never travel through agents.** Secrets enter via a one-time secure page, straight
  into the secret store. Agents, messages, logs and the LLM only see references like `jira/readonly`.
- **Learn once, reuse everywhere.** The first agent that needs an API triggers onboarding; every
  agent after that benefits.
- **The LLM proposes, code decides.** The optional planner can suggest any call, but only the rule
  engine (no LLM) can approve it. No prompt can talk its way into a write.

## Get started

| I want to... | Go to |
|---|---|
| Try it on my laptop (10 minutes, Docker) | [docs/getting-started.md](docs/getting-started.md) |
| Run it for my team (Kubernetes, step by step) | [docs/deploy.md](docs/deploy.md) |
| Connect agents (LiteLLM, MCP clients, A2A) | [docs/integrations.md](docs/integrations.md) |
| Fix something | [docs/troubleshooting.md](docs/troubleshooting.md) |

```bash
pip install librarian-a2a                  # the CLI and import are `librarian`
docker pull ghcr.io/kultar/librarian:0.10.1
```

### What happens on a request

1. An agent asks for data from an API librarian doesn't know yet.
2. librarian learns it: from its OpenAPI spec, else its documentation (found by itself if
   needed), else the model's knowledge. The API is usable for reads at once, and a pull request
   is opened on your registry repo for a person to review.
3. If the API needs a key, the agent's task waits and approvers get a Slack card. Someone follows
   the link, signs in and adds the key (it goes straight to your secret store), and the task
   carries on by itself.
4. Writes wait for a person's approval every time, unless a narrow, expiring grant covers them.

Nothing is set up per API in advance: the registry fills itself as agents need things.

## Skills (A2A)

| Skill | What it does | LLM? |
|---|---|---|
| `execute` | Run one read operation: `{"skill":"execute","api":"jira","operation":"getIssue","args":{"issueIdOrKey":"ABC-1"}}`. Optional `select` (dotted paths, e.g. `["fields.summary"]`) and `max_pages` | No |
| `capabilities` | List APIs and read operations: `{"skill":"capabilities","query":"issues"}` | No |
| `onboard` | Learn an API: `{"skill":"onboard","api":"jira","spec_url":"https://..."}`, `"base_url"` to auto-discover, or `"docs_url"` + `"base_url"` when there is no spec | No |
| `write` | Change data, with human approval or a grant: `{"skill":"write","api":"jira","operation":"addComment","args":{...},"body":{...},"reason":"why"}` | No |
| `fetch` | Plain language: *"open bugs assigned to the payments team"* (only when a model is configured) | Yes |

The same skills are available as **MCP tools** at `/mcp` (Streamable HTTP, same caller keys):
`capabilities`, `execute`, `fetch`, `write`, `write_status`, `onboard`. MCP calls never wait on a
human: writes return `pending_approval` and librarian finishes them once approved. librarian
works behind the **LiteLLM** gateway as both an MCP server and an A2A agent
([docs/integrations.md](docs/integrations.md#litellm-agent-gateway)).

Results are A2A artifacts with the data plus **provenance**: which API and operation, the URL
(secrets redacted), when, for which calling agent and principal, and which credential reference.

When a credential is missing, the task enters A2A's `TASK_STATE_AUTH_REQUIRED`, approvers are
notified (Slack by default), and the task **resumes on its own** once someone adds the key. The
caller polls or subscribes to the task as usual.

## Configuration

Settings come from a YAML file (`--config`) and/or `LIBRARIAN_*` environment variables (env wins;
nested keys use `__`, e.g. `LIBRARIAN_NOTIFIER__SLACK__BOT_TOKEN`). See
[`examples/librarian.yaml`](examples/librarian.yaml) for every option.

**Models / LiteLLM.** `fetch` needs a model. Use any Pydantic AI model string
(`model: anthropic:claude-sonnet-5-5`, with the provider's usual API-key env var), or point at any
OpenAI-compatible gateway such as LiteLLM:

```yaml
model: claude-sonnet-5-5            # the model name your gateway serves
model_base_url: http://litellm:4000/v1
model_api_key: sk-litellm-...       # or LIBRARIAN_MODEL_API_KEY
```

**Secrets.** Choose a backend with `secrets_backend`:

- `file` (default): one `0600` file per secret, good locally or on a mounted volume.
- `aws`: AWS Secrets Manager (`uv sync --extra aws`). `jira/readonly` maps to the secret
  `<prefix>jira/readonly`; append `#key` to read one key of a JSON secret
  (`service/jira#api_token`). Values are cached briefly (`aws_secrets.cache_seconds`).

Environment variables are always checked last (`jira/readonly` → `LIBRARIAN_SECRET_JIRA_READONLY`).
Internal secrets (caller keys, the link-signing key) live under the reserved `librarian/` prefix.

**State.** Credential requests, approvals, A2A tasks and APIs learned on demand are stored in
SQLite by default (`.librarian/librarian.db`), so they survive restarts. For several replicas use
Postgres (`database_url: postgresql+asyncpg://...`, the `postgres` extra; in the image) and
`secrets_backend: aws`: then every replica shares everything, an API learned through one is usable
through the others at once, and pods need no volume. Set `replicas` (the Helm chart does) and
librarian refuses to start with per-replica state. Rate limits are per replica.

**Caller authentication.** List agents and the secret holding each one's key:

```yaml
callers:
  - agent: release-notes-agent
    key_ref: librarian/callers/release-notes-agent   # internal secrets live under librarian/
    apis: ["jira", "github-*"]                       # glob patterns; default ["*"]
    can_onboard: false                               # may this agent teach new APIs?
```

An agent only sees and uses the APIs its patterns allow, in `capabilities`, `execute` and `fetch`.

With no callers configured the server is open: fine on a laptop, never in a shared deployment.

## Observability

Set `otel_enabled: true` (install the `otel` extra; the Docker image has it) and the standard
`OTEL_EXPORTER_OTLP_ENDPOINT`. You get:

- **Traces**: `librarian.skill` and `librarian.call` spans (plus the A2A SDK's and Pydantic AI's
  model-call spans). A calling agent's `traceparent` is honoured, so its trace continues through
  the librarian. Trace headers are **never** forwarded to third-party APIs.
- **Metrics**: `librarian.api.calls` (by API, operation, caller, status class),
  `librarian.api.call.duration`, `librarian.policy.denials`, `librarian.credential.requests`.

The JSONL audit log is separate and always on.

## Big APIs

- **Operation search.** Requests are matched to operations with BM25 ranking over operation ids
  (split into words: `getIssueWatchers` -> issue, watch), tags, paths and summaries. On Jira's
  620-operation API, "who is watching an issue" finds `getIssueWatchers` first.
- **Glossary.** Registry entries can declare `discovery` reads that teach instance-specific
  names, e.g. Jira's `/field` maps `customfield_10020` to "Sprint". They run after a credential
  is approved (or `librarian discover <api>`), and the planner sees the relevant entries.
- **Recipes.** A successful plain-language request is saved as a template: "Is PAY-1 open?"
  becomes `Is {p0} open`, and "Is PAY-7 open?" later replays the same calls **without the LLM**
  (still policy-checked, with the caller's permissions). Plans whose later calls use values from
  earlier results are never saved. `librarian recipes list|delete`; `recipes_enabled: false`
  turns it off.

## APIs without a spec, and APIs that change

**Learning without a spec** (needs `model`). librarian learns from, in order of preference:

1. **An OpenAPI spec**: given, found at a well-known path, or linked from the docs.
2. **Documentation pages**: the model reads the page and related pages on the same site and
   lists the operations, each with a verbatim quote. librarian checks every quote against the
   fetched page and drops operations it can't find there. With only a `base_url`, librarian
   **finds the docs itself**: the usual places first (`/llms.txt`, `/docs`, `docs.<domain>`,
   links on the homepage, one click further), then a **web search** if configured.
3. **The model's own knowledge** (nothing found): reads only, for well-known APIs.

Search results are untrusted (anyone can publish "Acme API docs"), so sources are ranked:
the API's own domain first, then well-known docs hosts (GitHub, ReadMe, GitBook, ...);
other sites are skipped unless `learning.search.allow_third_party: true`. The review PR says
how the docs were found and how far they are trusted.

```yaml
learning:
  search:
    provider: model        # off | model | searxng | brave
    # url: https://searx.internal          (searxng)
    # api_key: via LIBRARIAN_LEARNING__SEARCH__API_KEY   (brave)
```

`provider: model` uses the model's own web search. Through a gateway such as LiteLLM that
needs the Anthropic API format (`model_api: anthropic`); with the OpenAI formats the search is
refused or silently dropped by the gateway, and librarian reports that instead of guessing.

Documentation is untrusted, so the model never decides anything that matters: it has no tools;
the API host is always the `base_url` you gave (never one from the docs); read vs write is decided
by HTTP method; the auth *type* may come from the docs but where the key is stored never does.
Operations from docs or model knowledge are **unconfirmed** until a real call succeeds (callers
see a warning). One that keeps answering 404 is **suspended** and its owners are told.

```bash
uv run librarian learn dogs --docs-url https://dog.ceo/dog-api/documentation/ \
    --base-url https://dog.ceo/api --name "Dog CEO"
```

**Noticing changes.** Every `drift.recheck_hours` (default 24), one replica re-reads each entry's
spec or docs. A new model read only happens when the docs' text actually changed.

| Change | Provisional entry | Verified entry |
|---|---|---|
| New operations, optional parameters | updated, re-proposed | proposed for review |
| Removed operation, new required parameter, moved path | updated, re-proposed | callers warned; writes **suspended**; proposed |
| HTTP method changed (a read may now change data) | updated, re-proposed | **suspended**; proposed |

Verified entries are never changed automatically. The proposal PR keeps reviewed decisions
(writes enabled, reviewed reads, pagination); merging it lifts the suspensions it addresses.

Calls also watch for change: `Deprecation`/`Sunset` headers, repeated 404/405/410 responses, and
responses that lose most of the fields seen before are reported to the API's owners (once), and
callers see a warning. `librarian registry recheck [api]` runs a check now;
`librarian registry clear <api> <operation>` lifts a suspension after checking by hand.

## Verified principals

By default the person an agent acts for (`principal`) is whatever the agent claims. Turn on
verification to require proof: the agent sends a token from your identity provider in the
`X-Principal-Token` header, and librarian checks its signature (issuer's published keys), issuer,
audience and expiry.

```yaml
principals:
  mode: required          # off | optional | required
  issuers:
    - issuer: https://acme.okta.com/oauth2/default
      audience: librarian  # tokens must be minted for librarian
      claim: email
```

- `optional`: verified when a token is present; a claimed principal without one is recorded
  but does **not** count as the requester for write approvals.
- `required`: a claimed principal without a valid token is refused. Requests with no person
  behind them (autonomous agents) still work.

Verified identities appear in provenance (`principal_verified: true`) and the audit log. How an
agent gets a token for librarian on a user's behalf is covered in
[docs/integrations.md](docs/integrations.md#verified-principals).

## Slack notifications

1. Create a Slack app from [`deploy/slack-app-manifest.yaml`](deploy/slack-app-manifest.yaml) and
   install it to your workspace.
2. Set `notifier.type: slack`, the bot token (`LIBRARIAN_NOTIFIER__SLACK__BOT_TOKEN`), a channel,
   and optionally an approver user-group ID (rendered as a real @mention).

Approvers get one card per API (deduplicated, with a live "waiting" count) and a **Review request**
button that opens the secure page: *add a key*, *use an existing secret*, or *deny*. The card
updates in place and the outcome is posted in the thread. The key itself never goes through Slack.

No Slack? `notifier.type: webhook` posts JSON events you can route anywhere.

## Writes

Reads are the default; writes are opt-in at every level:

1. A reviewer marks the operation `writable: true` on a **verified** entry and sets a separate
   write credential (`write_auth`). The approver adding a key can also tick *"allow approved
   writes with this key"*.
2. The agent is allowed to request writes on that API (`write_apis` in its caller config).
3. Each write is **approved by a human** for that exact request, or matches a **grant**.

The approval card (Slack) shows the operation, arguments and full body. On the approval page the
approver can **approve once**, **approve and allow similar writes** (a grant for that agent and
operation, with the argument values they choose kept fixed, an expiry and an optional use cap), or
**deny**. DELETE operations can only ever be approved once. The task waits in `AUTH_REQUIRED` and
completes on its own after the decision.

Who may approve (`writes.approvers`): the **requester** (the person the agent acts for, or the
agent's `owner`), the **approver group**, or either (default). Approvers sign in through your
SSO proxy (`approvers.mode: trusted_header`); agents can never approve.

**Autonomous agents** (no person behind them) get an `owner`, who stands in as requester. For
unattended work, configure bounded grants:

```yaml
callers:
  - agent: triage-bot
    key_ref: librarian/callers/triage-bot
    owner: ops@example.com
    write_apis: ["jira"]
writes:
  approvers: requester_or_group
  grants:
    - agent: triage-bot
      api: jira
      operation: addLabel
      pinned_args: {}                  # e.g. {issueIdOrKey: PAY-1}; {} allows any issue
      max_uses_per_day: 50
      notify_owner: true               # FYI to the channel on each use
```

An agent with no owner and no person can only be approved by the approver group; if no one could
approve, the write is refused immediately. `librarian grants list|revoke` and `librarian approvals
list` manage it all; revoking is the kill switch.

## The registry

One YAML file per API, normally in a git repo you review by pull request. Onboarded entries start
as **provisional**:

- GET/HEAD operations are reads and usable immediately.
- POST operations that look like searches are marked `unknown` and blocked until a human sets
  `access: read` + `read_basis: reviewed` **and** the entry is `verified`.
- Everything else is a write: refused unless a reviewer enables it (see [Writes](#writes)).

**Review loop.** With `publish.type: github`, every learned API is also opened as a pull request
against your registry repo, with a review checklist and the operations that need a decision. Run a
[git-sync](https://github.com/kubernetes/git-sync) sidecar (or any process that updates the
registry directory) and merged changes such as `status: verified` are picked up automatically:
the registry hot-reloads when files change.

```yaml
publish:
  type: github
  repo: acme/api-registry
  directory: registry
  github_app:                      # recommended: owned by the org, short-lived tokens
    app_id: "123456"
    private_key_file: .librarian/github-app.pem
  # or token: via LIBRARIAN_PUBLISH__TOKEN (contents:write + pull_requests:write)
```

`librarian github-app create --repo acme/api-registry` creates that GitHub App in one step:
one click on GitHub, install it on the repo, and it saves the key and prints the settings.
The same App lets git-sync read a private registry repo (see the Helm chart README).

When learning an API, the librarian also looks for a cheap "who am I" read (`/me`, `/myself`,
`/user`, ...) and uses it to test every new credential.

Validate in CI with `uv run librarian registry validate`. Format reference:
[docs/registry.md](docs/registry.md). Example: [`examples/registry/jira.yaml`](examples/registry/jira.yaml).

## Security

Read [docs/threat-model.md](docs/threat-model.md). Highlights: deterministic policy engine;
host-locked URL building (callers can't choose hosts); same-origin-only redirects; response size
caps; SSRF protection for onboarding and provisional entries; one-time, expiring approval links;
secrets never logged or returned. Report vulnerabilities via [SECURITY.md](SECURITY.md).

## Development

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pyright          # strict mode
uv run pytest
```

## Roadmap

- **v0.2** ✅: durable state (SQLite/Postgres) and stable approval links; per-agent API
  allowlists; 401 vs 403 handling; DNS-rebinding-proof SSRF guard; GitHub review PRs + hot reload;
  AWS Secrets Manager; field selection and pagination styles; OpenTelemetry
- **v0.3** ✅: writes behind human approval; grants (approve-and-allow-similar, config grants for
  autonomous agents); agent owners; planner write proposals; grants/approvals CLI
- **v0.4** ✅: Helm chart (oauth2-proxy sign-in, git-sync registry, hardened pod), separate
  pages listener, registry overlay, release workflow (GHCR, PyPI), example client, LiteLLM compose
- **v0.5** ✅: verified principals (signed tokens from your identity provider; optional and
  required modes); claimed principals no longer count as requesters once verification is on
- **v0.6** ✅: big APIs: ranked operation search, discovery-built glossary (e.g. Jira custom
  fields), recipes replayed without the LLM
- **v0.7** ✅: MCP endpoint; LiteLLM agent gateway (MCP + A2A); exactly-once writes across
  replicas; integration suite
- **v0.8** ✅: APIs without a spec (documentation, model knowledge) with quote-checked
  extraction and confirm-on-use; change detection (spec/docs re-checks, deprecation and
  removal signals, response shape) with suspensions and review proposals
- **v0.9** ✅: finding documentation from just a base URL (usual places, then web search via
  the model or SearXNG/Brave), trust-ranked sources; Anthropic API format for gateways
- **v0.10** ✅: GitHub App for the registry repo (review PRs and git-sync), created in one step
  with `librarian github-app create`; several replicas with no volumes (learned APIs in Postgres,
  PodDisruptionBudget, spread across nodes); step-by-step docs
- **Later**: Vault/GCP/Azure secret stores; per-user OAuth; per-API approver routing in Slack;
  community registry with signed entries

## License

[Apache-2.0](LICENSE)
