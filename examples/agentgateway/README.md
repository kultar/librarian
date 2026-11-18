# librarian behind agentgateway, in five minutes

[agentgateway](https://agentgateway.dev) in front of librarian, the simple way: each agent
keeps its own key, the gateway routes and observes, and librarian decides. Agents get one
address, the gateway's, for MCP and for A2A. librarian is not changed for it: this example
runs the [quick start](../../docs/getting-started.md)'s settings as they are.

## You need

[Docker](https://docs.docker.com/get-docker/) with Compose, `curl`, and this repository:
`git clone https://github.com/kultar/librarian && cd librarian/examples/agentgateway`.
[`jq`](https://jqlang.org/) is optional (it makes the output readable).

## 1. Start it

```bash
docker compose up -d --wait
curl -s localhost:3000/.well-known/agent-card.json | grep -o '"url": *"[^"]*"' | head -1
# "url":"http://localhost:3000/"
```

Port 3000 is the gateway. The agent card it serves is librarian's, with the gateway's address
in it, so an agent that reads the card keeps talking to the gateway.

## 2. An agent's calls go through the gateway

```bash
export LIBRARIAN_URL=http://localhost:3000        # what the agent is given

../quickstart/ask '{"skill":"execute","api":"demo","operation":"getRecord","args":{"id":"42"}}'
# "state": "TASK_STATE_COMPLETED" ... "url": "http://demo-api:8080/anything/records/42"
```

That was A2A. MCP is at the same address, under `/mcp`:

```bash
curl -s localhost:3000/mcp -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' -H 'Authorization: Bearer demo-agent-key' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | head -c 120
# event: message
# data: {"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"capabilities", ...
```

For an agent with an MCP client, for example Claude Code: `claude mcp add --transport http
librarian http://localhost:3000/mcp --header "Authorization: Bearer demo-agent-key"`.

## 3. A write waits for a person

```bash
../quickstart/ask '{"skill":"write","api":"demo","operation":"addComment","args":{"id":"42"},"body":{"text":"Looks good"},"reason":"Trying it through the gateway"}'
# "id": "67923fa6-...", "state": "TASK_STATE_AUTH_REQUIRED"

docker compose logs librarian | grep -o 'http://localhost:8080/approvals/[^ ]*' | tail -1
```

The link is to librarian's own page, on port 8080: people who approve do not go through the
gateway. It shows the request as it will be sent. Choose **Approve this write**, then:

```bash
../quickstart/ask task <the write's task id>
# "state": "TASK_STATE_COMPLETED" ... "how": "approved" ... "json": {"text": "Looks good"}
```

## 4. Who decided what

librarian decides, and the gateway passes its answer on as it is:

```bash
LIBRARIAN_KEY=wrong ../quickstart/ask '{"skill":"capabilities"}'
# {"error": "unauthorized"}
```

The gateway saw every call, and for MCP it knows which method it was:

```bash
docker compose logs agentgateway | grep "protocol=mcp" | tail -1
# ... route=default/librarian-mcp ... http.status=200 protocol=mcp mcp.method.name=tools/list duration=21ms
```

## The configuration

[`agentgateway.yaml`](agentgateway.yaml) is all of it: one route for `/mcp` to librarian as an
MCP backend, and one for everything else, with the policy that rewrites the agent card.
[`docker-compose.yaml`](docker-compose.yaml) adds librarian and a demo API that needs a key.

## Clean up

```bash
docker compose down -v
```

## Next

- **The gateway signs people in.** For people with an MCP client, the gateway does the OAuth
  sign-in, and librarian checks the same token itself:
  [the gateway signs people in](../../docs/integrations.md#the-gateway-signs-people-in).
- **On Kubernetes.** librarian's chart attaches it to a Gateway you already run
  (`agentgateway.enabled`): [on Kubernetes](../../docs/integrations.md#on-kubernetes).
- **What to know behind a gateway.** Message sizes, answers that arrive as events, who the
  caller is: [things that matter behind agentgateway](../../docs/integrations.md#things-that-matter-behind-agentgateway).
- **Agents with no key at all.** On kagent and Agent Substrate the platform signs each call:
  [`examples/kagent`](../kagent).

## How this is tested

This configuration, and the one in which the gateway signs people in, run every week against
agentgateway v1.7.0 with librarian built from `main`
([`tests/integration/agentgateway`](../../tests/integration/agentgateway)): tools listed and
called, a write and a plan approved and finished, the agent card, a task followed through the
gateway. This page was run by hand on 2026-11-17 with librarian 0.23.1 as published.
