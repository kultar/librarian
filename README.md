# librarian

**An A2A agent that learns APIs on demand and gives other agents safe, read-only, audited access to them.**

Agents shouldn't each carry their own Jira token, GitHub token and API client. With librarian,
agents ask one specialist, over [A2A](https://a2a-protocol.org), for the data they need. The
librarian learns the API from its OpenAPI spec the first time it is needed, asks a human for a
credential (once, out of band), and from then on serves every agent, without ever handing the
credential out.

```
 your agents ──A2A──► librarian ──(policy engine)──► Jira, GitHub, internal APIs, ...
                         │
                         ├── registry (git): what each API is and which operations are reads
                         ├── secret store: credentials (agents only ever see references)
                         └── planner (Pydantic AI, optional) ──► any model / LiteLLM
```

> Status: **alpha (v0.1)**. Read-only by design. Expect breaking changes.

## Why

- **One choke point.** Every call from every agent goes through one deterministic policy engine and
  lands in one audit log.
- **Credentials never travel through agents.** Secrets enter via a one-time secure page, straight
  into the secret store. Agents, messages, logs and the LLM only see references like `jira/readonly`.
- **Learn once, reuse everywhere.** The first agent that needs an API triggers onboarding; every
  agent after that benefits.
- **The LLM proposes, code decides.** The optional planner can suggest any call, but only the rule
  engine (no LLM) can approve it. No prompt can talk its way into a write.

## Skills (A2A)

| Skill | What it does | LLM? |
|---|---|---|
| `execute` | Run one read operation: `{"skill":"execute","api":"jira","operation":"getIssue","args":{"issueIdOrKey":"ABC-1"}}` | No |
| `capabilities` | List APIs and read operations: `{"skill":"capabilities","query":"issues"}` | No |
| `onboard` | Learn an API: `{"skill":"onboard","api":"jira","spec_url":"https://..."}` or `"base_url"` to auto-discover | No |
| `fetch` | Plain language: *"open bugs assigned to the payments team"* (only when a model is configured) | Yes |

Results are A2A artifacts with the data plus **provenance**: which API and operation, the URL
(secrets redacted), when, for which calling agent and principal, and which credential reference.

When a credential is missing, the task enters A2A's `TASK_STATE_AUTH_REQUIRED`, approvers are
notified (Slack by default), and the task **resumes on its own** once someone adds the key. The
caller polls or subscribes to the task as usual.

## Quick start

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/kultar/librarian && cd librarian
uv sync

# Learn a real public API from its OpenAPI spec (no credential needed)
uv run librarian learn apisguru --spec-url https://api.apis.guru/v2/openapi.yaml

# Call it locally, through the same policy engine the server uses
uv run librarian call apisguru getMetrics

# Or ask in plain language (needs a model, e.g. ANTHROPIC_API_KEY)
uv run librarian ask "How many APIs does APIs.guru track?" --model anthropic:claude-sonnet-5-5

# Run the A2A server
uv run librarian serve --config examples/librarian.yaml
curl -s localhost:8080/.well-known/agent-card.json | jq '.skills[].id'
```

Send an A2A message (JSON-RPC):

```bash
curl -s localhost:8080/ -H 'content-type: application/json' -H 'A2A-Version: 1.0' \
  -H 'Authorization: Bearer <agent key>' -d '{
  "jsonrpc": "2.0", "id": 1, "method": "SendMessage",
  "params": {"message": {"messageId": "1", "role": "ROLE_USER",
    "parts": [{"data": {"skill": "execute", "api": "apisguru", "operation": "getMetrics"}}]},
    "metadata": {"principal": "kim@example.com"}}}'
```

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

**Secrets.** v0.1 ships a file store (one `0600` file per secret, good for local use or a mounted
Kubernetes volume) and a read-only environment store (`jira/readonly` →
`LIBRARIAN_SECRET_JIRA_READONLY`). Cloud secret managers are planned plugins.

**Caller authentication.** List agents and the secret holding each one's key:

```yaml
callers:
  - agent: release-notes-agent
    key_ref: callers/release-notes-agent
```

With no callers configured the server is open: fine on a laptop, never in a shared deployment.

## Slack notifications

1. Create a Slack app from [`deploy/slack-app-manifest.yaml`](deploy/slack-app-manifest.yaml) and
   install it to your workspace.
2. Set `notifier.type: slack`, the bot token (`LIBRARIAN_NOTIFIER__SLACK__BOT_TOKEN`), a channel,
   and optionally an approver user-group ID (rendered as a real @mention).

Approvers get one card per API (deduplicated, with a live "waiting" count) and a **Review request**
button that opens the secure page: *add a key*, *use an existing secret*, or *deny*. The card
updates in place and the outcome is posted in the thread. The key itself never goes through Slack.

No Slack? `notifier.type: webhook` posts JSON events you can route anywhere.

## The registry

One YAML file per API, normally in a git repo you review by pull request. Onboarded entries start
as **provisional**:

- GET/HEAD operations are reads and usable immediately.
- POST operations that look like searches are marked `unknown` and blocked until a human sets
  `access: read` + `read_basis: reviewed` **and** the entry is `verified`.
- Everything else is a write and is always refused in v0.1.

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

- **v0.2**: cloud secret stores (AWS Secrets Manager, Vault, GCP, Azure); OpenTelemetry audit
  sink; Helm chart; persistent task/request store
- **v0.3**: verified caller identity and delegated principals (OAuth token exchange); OIDC for
  approvers; per-API approver routing in Slack
- **v0.4**: saved **recipes** (successful plans replayed without the LLM); learning instance
  details (e.g. Jira custom fields); drift detection
- **v1.0**: community registry with signed entries; per-user OAuth; writes behind human approval

## License

[Apache-2.0](LICENSE)
