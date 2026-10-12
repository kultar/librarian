# Getting started

Try librarian on your laptop in about ten minutes. You will see the three things it does:

1. **Learn an API the moment an agent needs it**, and serve it straight away.
2. **Ask a person for a missing key**, out of band, then carry on by itself.
3. **Hold a write until a person approves it.**

You need [Docker](https://docs.docker.com/get-docker/) with Compose, `git` and `curl`.
[`jq`](https://jqlang.org/) is optional (it makes the output readable).

## 1. Start it

```bash
git clone https://github.com/kultar/librarian
cd librarian/examples/quickstart
docker compose up -d
curl -s localhost:8080/healthz
# {"status":"ok","apis":1,"invalid_registry_files":0}
```

This runs librarian and a **demo API** (an echo service standing in for an internal API that
needs a key). The setup is in [`examples/quickstart`](../examples/quickstart):

- `librarian.yaml`: one agent, `demo-agent`, with the key `demo-agent-key`.
- `registry/demo.yaml`: the demo API, already reviewed. Normally librarian learns entries like
  this on demand (step 2); this one is prewritten so steps 3 and 4 have something that needs a key.
- `./ask`: sends requests to librarian as `demo-agent` (it wraps the A2A JSON-RPC call).

```bash
./ask '{"skill":"capabilities"}'          # what can this agent use? (the demo API)
```

Open <http://localhost:8080/> in a browser: the console shows what librarian knows and what is
waiting on you, and lets you block or forget an API, invalidate a key, or re-learn an API from
a documentation URL. (The quick start serves it without sign-in; a shared deployment puts it behind
your identity provider.)

## 2. An agent needs an API librarian has never seen

The agent asks librarian to learn [APIs.guru](https://apis.guru) (a public directory of APIs)
from its OpenAPI spec, then reads from it:

```bash
./ask '{"skill":"onboard","api":"apisguru","spec_url":"https://api.apis.guru/v2/openapi.yaml"}'
# state TASK_STATE_COMPLETED: ..."learned_from": "openapi", "read_operations": 7, "auth": "none"

./ask '{"skill":"execute","api":"apisguru","operation":"getMetrics","select":["numAPIs"]}'
# ..."data": {"numAPIs": 2529.0}
```

It was usable at once: learned APIs start as **provisional**, which allows reads (GET) only. In a
real deployment librarian also opens a pull request so a person can review the entry
([docs/registry.md](registry.md)).

No spec? Give it the documentation page instead (`"docs_url"` plus `"base_url"`), or just the
`"base_url"`, and librarian finds the docs itself. That needs a model: see
[Using a model](#using-a-model).

## 3. The API needs a key nobody has added yet

```bash
./ask '{"skill":"execute","api":"demo","operation":"getRecord","args":{"id":"42"}}'
# "id": "37afee23-...", "state": "TASK_STATE_AUTH_REQUIRED",
# "Demo API needs a credential. An approver has been notified ...; this task resumes
#  automatically once access is granted."
```

The agent's task is now waiting. Approvers are notified: in production on Slack, here in the log.
Find the link:

```bash
docker compose logs librarian | grep -o 'http://localhost:8080/credentials/[^ ]*' | tail -1
```

Open it in your browser, paste any value as the key (the demo API accepts anything) and click
**Save and test**. librarian stores it, tests it, and the waiting task finishes on its own:

```bash
./ask task 37afee23-...                   # use your task id
# "state": "TASK_STATE_COMPLETED", ... "url": "http://demo-api:8080/anything/records/42"
```

The key went from your browser straight into librarian's secret store: never through the agent,
the chat, or a model. Agents only ever see a reference such as `demo/read`.

## 4. A write waits for a person

```bash
./ask '{"skill":"write","api":"demo","operation":"addComment","args":{"id":"42"},"body":{"text":"Looks good"},"reason":"Trying out write approvals"}'
# "state": "TASK_STATE_AUTH_REQUIRED"

docker compose logs librarian | grep -o 'http://localhost:8080/approvals/[^ ]*' | tail -1
```

The approval page shows exactly what will be sent and why. Click **Approve this write** (or
**Approve and allow similar**, which also saves a narrow, expiring grant). Then:

```bash
./ask task <the write's task id>
# "state": "TASK_STATE_COMPLETED", the demo API received {"text": "Looks good"}
```

Writes only ever run on reviewed entries, for operations a reviewer enabled, with a separate
write key, after approval.

## 5. Connect a real agent

librarian speaks **A2A** (at `/`) and **MCP** (at `/mcp`), with the same keys and rules. For
example, to give Claude Code the librarian's tools:

```bash
claude mcp add --transport http librarian http://localhost:8080/mcp \
  --header "Authorization: Bearer demo-agent-key"
```

Then ask Claude Code something like *"use librarian to find how many APIs APIs.guru tracks"*.
Over MCP nothing blocks on a person: a write returns `pending_approval`, and librarian finishes
it after approval (`write_status` reports the result).

For agents behind the **LiteLLM** gateway, see [docs/integrations.md](integrations.md).

## Using a model

The steps above use no model. Add one for plain-language requests (the `fetch` skill) and for
learning APIs that have no spec. In `librarian.yaml`:

```yaml
model: anthropic:claude-sonnet-5-5
```

and pass your key to the container: uncomment the `ANTHROPIC_API_KEY` line in
`docker-compose.yaml` and `export ANTHROPIC_API_KEY=...` before starting.

or, through a gateway such as LiteLLM:

```yaml
model: claude-sonnet-5-5
model_base_url: http://litellm:4000/v1
model_api: anthropic                 # needed for web search through the gateway
# key: LIBRARIAN_MODEL_API_KEY
```

Then restart (`docker compose up -d`) and try:

```bash
./ask '{"skill":"fetch","intent":"How many APIs does APIs.guru track?"}'
./ask '{"skill":"onboard","api":"dogs","base_url":"https://dog.ceo/api","name":"Dog CEO"}'
```

## Clean up

```bash
docker compose down -v        # -v also deletes the stored keys and learned APIs
```

## Next

- Run it for a team: [docs/deploy.md](deploy.md) (Kubernetes, step by step).
- Something not working: [docs/troubleshooting.md](troubleshooting.md).
- How it stays safe: [docs/threat-model.md](threat-model.md).
