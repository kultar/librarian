# Integrations

## Any A2A client

librarian is a standard A2A 1.0 agent (JSON-RPC). Fetch the card at
`/.well-known/agent-card.json`, then send `SendMessage` with a data part naming the skill.
[`examples/client/ask_librarian.py`](../examples/client/ask_librarian.py) is a complete client
using the official A2A Python SDK, including polling tasks that wait for approval.

Things a client must handle:

- **`TASK_STATE_AUTH_REQUIRED`**: a credential or a write approval is pending. The blocking call
  returns at this point; poll `GetTask` (or subscribe) until the task completes, fails or is
  rejected. The status message carries `credential_request` / `write_approval` ids.
- **`TASK_STATE_REJECTED`**: refused by policy (not allowed, not a read, not writable, ...).
- **Numbers** arrive as floats (A2A data parts are protobuf `Value`s); integers above 2^53 arrive
  as strings so they keep their precision.
- **Principal**: pass the person the agent acts for as message metadata `{"principal": "..."}`.

## LiteLLM (agent gateway)

librarian works behind LiteLLM both as an **MCP server** and as an **A2A agent**, tested end to
end against LiteLLM v1.104.2 (`tests/integration/litellm`) and in a real deployment on v1.101.0
and v1.105.0.

```yaml
# LiteLLM config.yaml
mcp_servers:
  librarian:                          # tools appear as librarian-execute, librarian-write, ...
    server_id: librarian
    url: http://librarian.<ns>.svc:8080/mcp
    transport: http
    auth_type: bearer_token
    auth_value: os.environ/LIBRARIAN_GATEWAY_KEY   # librarian caller key for the gateway
    extra_headers: ["x-principal-token"]           # forward the user's identity token
    timeout: 120                                    # above librarian's slowest tool (fetch)

agents:
  - agent_name: librarian
    static_headers: {Authorization: os.environ/LIBRARIAN_AGENT_AUTH}   # "Bearer <caller key>"; LiteLLM resolves os.environ/ here too
    extra_headers: ["x-principal-token"]
    agent_card_params:
      name: librarian
      url: http://librarian.<ns>.svc:8080/
      protocolVersion: "1.0"                        # pin it
      # description, version, capabilities, input/output modes, skills: copy from
      # librarian's /.well-known/agent-card.json (LiteLLM serves this stored copy)
```

On the librarian side, give the gateway its own caller (`callers: [{agent: litellm-gateway,
key_ref: librarian/callers/litellm-gateway, apis: [...], write_apis: [...], owner: ...}]`).
Everything that reaches librarian through LiteLLM then arrives as that one agent, so scope it
accordingly; people are told apart by `X-Principal-Token` when principal verification is on.

Clients call LiteLLM with their LiteLLM key in `x-litellm-api-key` (so `Authorization` stays
free), at `/librarian/mcp` (MCP) or `/a2a/librarian` (A2A).

Things that matter behind LiteLLM:

- **A2A task calls arrive in 0.3 form.** LiteLLM forwards `GetTask`/`CancelTask` upstream with
  A2A 0.3 method names and params even for 1.0 agents; librarian accepts both (without that,
  polling a task through the gateway fails with "Method not found").
- **MCP is stateless with JSON responses**, so LiteLLM's per-call upstream sessions work and
  any replica can answer.
- **MCP tool calls never block on a human:** writes return `pending_approval` and are finished
  by librarian after approval (`write_status` reports the state and, once run, the result with
  `result_as_of`); missing credentials return `credential_pending`. An executed write carries
  `how` (approved, grant, auto or idempotent) and a plain `message` saying so.
- **A change that takes several writes goes in one `write_plan`** (a blob, a tree, a commit, a
  branch, a pull request): the approver sees all of it and approves once, and `write_status`
  lists every step with its state and answer. See
  [Several writes as one plan](../README.md#several-writes-as-one-plan).
- **Host header:** librarian accepts any `Host` on `/mcp` by default (the MCP SDK's default
  allows only localhost and answers 421 through a gateway). Pin hosts with `mcp_allowed_hosts`.

### LiteLLM as librarian's model gateway

The `fetch` skill plans with any OpenAI-compatible endpoint:

```yaml
model: claude-sonnet-5-5                 # the model name LiteLLM serves
model_base_url: http://litellm:4000/v1
# model_api_key: via LIBRARIAN_MODEL_API_KEY
```

To let librarian search the web for API documentation through the gateway, talk to it in
the Anthropic format (LiteLLM serves `/v1/messages`); verified with LiteLLM v1.104.2 and an
Anthropic model:

```yaml
model_api: anthropic                     # default: openai (Chat Completions)
learning: {search: {provider: model}}
```

With the OpenAI Chat Completions format web search is unsupported, and with the Responses
format LiteLLM dropped the search tool silently in testing; librarian detects that (no search
call in the reply) and refuses rather than use an answer from memory.

Through a gateway, the planner asks for its answer as JSON in the reply (`planner_output:
prompted`, the default with `model_base_url`), because some providers reject the forced tool use
that structured output otherwise needs. [`examples/compose`](../examples/compose) runs librarian
and LiteLLM together.

## agentgateway

[agentgateway](https://agentgateway.dev) is a proxy for agent traffic (MCP, A2A, model APIs),
run on its own or on Kubernetes with the Gateway API. librarian works behind it as an **MCP
server** and as an **A2A agent**, with nothing changed in librarian. Both ways of putting it
in front are tested against agentgateway v1.7.0
([`tests/integration/agentgateway`](../tests/integration/agentgateway), in the weekly
integration run). (Agent Substrate's egress is agentgateway too: for agents that run in
kagent, see [the next section](#agent-substrate-and-kagent).)

### librarian authenticates: each agent's key passes through

The gateway routes, observes and limits. Each agent keeps its own key, so librarian knows which
agent is calling and applies that agent's rules.

```yaml
# agentgateway config.yaml
binds:
- port: 3000
  listeners:
  - routes:
    - name: librarian-mcp
      matches:
      - path: {exact: /mcp}
      backends:
      - mcp:
          statefulMode: stateless        # librarian keeps no MCP sessions
          targets:
          - name: librarian
            mcp: {host: http://librarian:8080/mcp}
    - name: librarian-a2a
      matches:
      - path: {pathPrefix: /}
      policies:
        a2a: {}                          # rewrites the agent card to the gateway's address
      backends:
      - host: librarian:8080
```

Agents use the gateway's address with `Authorization: Bearer <their key>`, at `/mcp` (MCP) or
`/` (A2A). With one MCP target the tools keep their names (`execute`, `write`, ...). What
librarian refuses (a wrong key, a policy decision) reaches the agent as librarian said it.

### The gateway signs people in

For people with an MCP client (an IDE, a chat app). The gateway does the OAuth sign-in with
your identity provider, calls librarian with one key of its own, and passes the person's token
on. librarian checks that token itself, so it knows the person with proof and not on the
gateway's word: their writes are theirs to approve, and APIs that act as the person use their
own sign-in.

```yaml
# agentgateway config.yaml: the route for people
    - name: librarian-mcp-people
      matches:
      - path: {exact: /mcp}
      - path: {exact: /.well-known/oauth-protected-resource/mcp}
      - path: {exact: /.well-known/oauth-authorization-server/mcp}
      policies:
        mcpAuthentication:               # an MCP client without a token is told where to sign in
          mode: strict
          issuer: https://idp.example.com/realms/acme
          audiences: [librarian]
          jwks: {url: https://idp.example.com/realms/acme/protocol/openid-connect/certs}
          provider: {keycloak: {}}
          resourceMetadata:
            resource: https://agents.example.com/mcp
            scopesSupported: [openid, email]
        mcpAuthorization:
          rules:                         # which tools people get: here, all but one
          - 'mcp.tool.name != "onboard"'
        transformations:
          request:
            set:                         # the person's token, for librarian to check itself
              x-principal-token: jwt.rawToken.unredacted()
        backendAuth:
          key: {file: /secrets/librarian-key}   # the gateway's own caller key
      backends:
      - mcp:
          statefulMode: stateless
          targets:
          - name: librarian
            mcp: {host: http://librarian:8080/mcp}
```

```yaml
# librarian config
callers:
  - agent: agentgateway                  # every call through the gateway arrives as this caller
    key_ref: librarian/callers/agentgateway
    write_apis: [jira]
principals:
  mode: optional                         # or required: see Verified principals below
  issuers:
    - issuer: https://idp.example.com/realms/acme
      audience: librarian
      claim: email
```

The A2A route takes the same `transformations` and `backendAuth`, with `jwtAuth` in place of
`mcpAuthentication` (the tested configuration has both routes).

### On Kubernetes

The Helm chart attaches librarian to a Gateway that agentgateway's controller runs:

```yaml
# librarian values
agentgateway:
  enabled: true
  gateway: {name: agents, namespace: agentgateway-system}
```

It creates an `HTTPRoute` and two `AgentgatewayBackend`s: an MCP server at `/mcp` and an A2A
agent on every other path. Agents then use the Gateway's address. Tested on kind with
agentgateway v1.7.0 and Gateway API v1.6.3: tools listed and called over MCP, a read, A2A
messages, the agent card naming the gateway, and librarian's refusal of a missing key passed
on. In another namespace than the Gateway's, its listener has to allow routes from librarian's
(`allowedRoutes`). What the gateway adds (sign-in, rules, rate limits) is attached on its side,
with an `AgentgatewayPolicy` that targets the `HTTPRoute` (named like the release); this
project has tested those policies in the standalone configuration above, not on Kubernetes.

### Things that matter behind agentgateway

- **Large answers over MCP.** The gateway holds an MCP server's whole answer (2 MiB by default)
  and hands it to the client as one event, which clients limit too (the MCP Python SDK: 1 MiB).
  Past either, the agent only learns that the message could not be sent. librarian therefore
  never sends a tool result of more than `mcp_max_result_bytes` (1 MB): the agent gets the
  answer's outline and asks for less with `select`. To allow more, raise that setting and the
  gateway's `frontendPolicies.http.maxBufferSize` together. A2A answers are not held this way
  (3 MB passed).
- **Timeouts.** agentgateway sets no request timeout unless you do. If you set one
  (`policies.timeout`), keep it above `fetch_timeout_seconds` (100) for plain-language
  requests and above `mcp_max_wait_seconds` (50) for agents that wait for a decision.
- **The agent card** names the address the request came in on (`policies: {a2a: {}}` rewrites
  it), so agents that read the card keep talking to the gateway.
- **Answers arrive as events.** The gateway answers MCP clients with `text/event-stream`, also
  where librarian answered JSON. MCP clients read both.
- **Who the caller is.** With keys passed through, the audit names each agent. With the
  gateway signing people in, every call is by the caller `agentgateway`, for a named and
  verified person: give that caller the APIs people may use.

## Agent Substrate and kagent

[Agent Substrate](https://github.com/agent-substrate/substrate) runs each agent session as an
*actor* in a sandbox that holds no credentials and can reach nothing by default. Everything an
actor sends out passes Substrate's egress gateway, which decides per destination and can put a
credential into the request on the way. [kagent](https://kagent.dev) 1.0 is the distribution
that runs it on any Kubernetes.

librarian is the destination an actor reaches when it needs somebody else's API: one egress
rule instead of one per API, and behind it the APIs learned on demand, the keys, the
per-operation policy and the human approval. No agent holds a key for librarian either:

```
agent (an actor: sandboxed, no credentials)
   │  MCP call to librarian, Authorization: a placeholder
   ▼
egress gateway        swaps the placeholder for an actor JWT, signed by the cluster's issuer
   ▼
librarian             1. verifies the token: issuer, signature, audience "librarian", an actor
                      2. asks kagent which Agent this actor (one session) belongs to
                      3. applies the first matching rule in substrate.rules
   ▼
the API, with the key only librarian holds
```

### With kagent (Helm)

```yaml
# librarian values
substrate:
  enabled: true
  rules:                                  # first match wins; no match: refused
    - match: {namespace: payments}        # the kagent Agent's namespace, name (globs), labels
      write_apis: [github]                # may ask for writes there; each still needs approval
      owner: payments-leads@example.com   # approves them
    - match: {}                           # every other agent: reads and learning, no writes
```

That sets `config.substrate` (issuer `https://idp.ate-system.svc`, audience `librarian`, kagent's
controller at `http://kagent-controller.kagent.svc:8083`), mounts the cluster signer's trust
bundle so librarian can fetch the issuer's keys, and creates a `RemoteMCPServer` named
`librarian` that any namespace's Agents may use. kagent's controller must watch the namespace
it is in (`substrate.kagent.remoteMCPServer.namespace`, by default librarian's).

Tested end to end on kind (Kubernetes 1.35) with kagent 1.0.1 and Agent Substrate 1.0.0: an
agent learning an API and reading from it, a write approved by the rule's owner, a second
session of the same agent writing under the grant that approval created, a plan of two writes
approved once and waited for in one tool call, an agent given only some of the tools, kagent's
own approval in front of librarian's, and the refusals below; on kagent's default harness, and
the read and the plan on its Claude-based one (`claude-default`) as well. The steps and
manifests are in [`tests/integration/substrate`](../tests/integration/substrate).

Give an agent the tools by naming the server in its template:

```yaml
apiVersion: api.kagent.dev/v1alpha3
kind: AgentTemplate
metadata: {name: release-notes, namespace: payments}
spec:
  systemPrompt: Write release notes from merged pull requests and Jira issues.
  tools:
    - mcp:
        server: {kind: RemoteMCPServer, name: librarian, namespace: librarian}
```

Nothing else is needed: kagent adds librarian to that agent's egress policy and has the gateway
inject the token. The agent discovers the tools itself, at run time, so they do not show in
kagent's tool picker.

**Some of the tools, and kagent's own approval.** A binding can name the tools an agent gets,
and kagent can hold a tool call for the person in the conversation before it is made:

```yaml
  tools:
    - mcp:                                # reads, and asking how a write stands
        server: {kind: RemoteMCPServer, name: librarian, namespace: librarian}
        tools: [capabilities, execute, write_status, enable_status]
    - mcp:                                # writes: kagent asks the person first
        server: {kind: RemoteMCPServer, name: librarian, namespace: librarian}
        tools: [write, write_plan]
        requireApproval: true
```

That is a second gate, in front of librarian's, and the two know different things. kagent
asks whoever is in the conversation whether the agent may make that tool call (its UI shows
the question; the `kagent` CLI cannot answer one). librarian then asks the approver its policy
names, shows them the request as it will be sent to the API, and can turn the answer into a
grant. Use kagent's where the person talking to the agent should see each write before anyone
else is asked.

**Who the caller is.** An actor is one session, so librarian records the *Agent*:
`payments/release-notes`. Grants, rate limits, the console and the audit trail are about that
name, and stay valid across the agent's sessions. Each audited call also carries the session
(`via: actor/kagent/<actor> uid=... session=...`).

**Who approves.** kagent can send an MCP server the agent's identity or the signed-in user's
token, not both, so librarian knows the agent but not the person behind it. A rule's `owner`
stands in: they (or the approver group) approve the agent's writes. Two things therefore do not
work through kagent yet: APIs that use each person's own sign-in, and "only the requester may
approve".

**What kagent says about the person.** With kagent's token propagation switched on
(`config.runtimeDefaults.tokenPropagation.enabled=true` when installing it), the agent's
runtime sends the signed-in user's name in an `X-User-Id` header beside the actor's token. It
is set inside the sandbox, where the agent itself can run commands, and nothing signs it: an
agent could send any name. librarian therefore ignores it unless you ask for it to be shown:

```yaml
# librarian values
substrate:
  kagent:
    claimedUserHeader: X-User-Id        # config: substrate.claimed_user_header
```

The approval page, the Slack card and the audit trail then say what it is: "On behalf of:
nobody librarian has proof of. The agent's platform names kim@example.com: nothing signs that,
and it does not make them the requester." It helps the rule's owner see whose conversation a
write came from. It never makes that person the requester, and never reaches an API that acts
as the person.

**Waiting on a person.** A write that needs approval answers `pending` at once; the agent can
end its turn (the actor is suspended, at no cost) and ask `write_status` later. The approval
does not need the agent to be running. The same holds for a plan (`write_plan`): librarian
makes its writes once it is approved, whether or not the agent is there. Nothing can wake a
kagent session when the decision arrives, so an agent that should finish the job in one turn
asks `write_status` with `wait_seconds` (say 45): the call answers as soon as the write has
ended, or after that time with where it stands. librarian waits at most
`mcp_max_wait_seconds` (50), which fits inside the 120 seconds the chart gives a tool call.

### Plain Agent Substrate

Without kagent there is no Agent to look up: the caller is the actor, and rules match on its
atespace and name. An actor is one session, so give a group of them one caller name:

```yaml
# librarian config
substrate:
  issuer: https://idp.ate-system.svc
  ca_file: /var/run/librarian/substrate/ca.crt   # the servicedns.podcert.ate.dev/identity bundle
  rules:
    - match: {atespace: team-a}
      agent: team-a                       # recorded instead of team-a/<actor name>
      write_apis: [github]
      owner: team-a-leads@example.com
```

(Helm: `substrate.enabled: true`, `substrate.kagent.enabled: false`.) Then let the actor reach
librarian and have the gateway sign for it, in the actor's `EgressPolicy`:

```yaml
rules:
- http:
    hostnames: ["librarian.librarian.svc.cluster.local"]
    ports: {all: {}}
    effects:
      replaceHeaders:
      - header: Authorization
        prefix: "Bearer "
        actorJwt: {audiences: ["librarian"], expirationSeconds: 900}
```

The agent sends `Authorization: Bearer anything` to
`http://librarian.librarian.svc.cluster.local:8080/mcp` (or the A2A endpoint at `/`). Use the
full name: a sandbox's DNS answers only for the exact hostnames in the actor's policy and has
no search domains, so `librarian.librarian.svc` does not resolve there.

### Settings

| `substrate.` | |
|---|---|
| `issuer` | The actor JWT issuer; setting it turns this on. Substrate's default is `https://idp.<its namespace>.svc` |
| `audience` | What tokens must be minted for (`librarian`). A token for any other service is refused, so a service that receives an actor's token cannot replay it here |
| `ca_file` | CA bundle for fetching the issuer's keys. The in-cluster issuer's certificate comes from the cluster signer `servicedns.podcert.ate.dev/identity`; the chart projects its ClusterTrustBundle |
| `kagent_url` | kagent's controller. Set: callers are Agents (`namespace/name`), and tokens must also carry the audience `kagent-agent-info`. Unset: callers are actors |
| `claimed_user_header` | A header in which the platform names the person at the agent without proof (kagent: `X-User-Id`). Shown to approvers and recorded as what the agent's side says; used for nothing else. Unset: ignored |
| `rules[].match` | `namespace`, `name` (globs) and `labels` of the Agent; `atespace` and `actor` (globs) of the actor. Every field given must match; `{}` matches everyone |
| `rules[]` | `apis` (default all), `write_apis` (default none), `can_onboard`, `owner`, `agent` (a caller name to record instead), `approval_wait_seconds`, `limits` (what the agents may do inside an API: [README](../README.md#limits-inside-an-api)): the same meaning as under `callers` |

Agents with their own key (`callers`) keep working beside this.

### Things to know

- **Tokens are bearer tokens that cannot be revoked**; Substrate lets them live 5 to 60
  minutes (the chart asks for 15). librarian never logs or stores one, and asks kagent again
  about an actor every minute, so a deleted agent or an ended session stops working within
  that minute rather than at the token's expiry.
- **In-cluster traffic is plain HTTP** between the gateway and librarian unless a service mesh
  encrypts it: kagent cannot yet trust a private CA for an MCP server. Restrict who can reach
  librarian's Service with a NetworkPolicy if other workloads share the cluster network.
- **Timeouts.** kagent's default for an MCP server is 30 seconds; the chart sets 120, because a
  plain-language request may run for `fetch_timeout_seconds` (100) and an agent may wait
  `mcp_max_wait_seconds` (50) for a decision.
- **Large answers.** A tool result is at most `mcp_max_result_bytes` (1 MB as sent): past
  that the agent gets the answer's outline and asks for the fields it needs with `select`.
- **Why an agent was refused** is in librarian's log (`refused a Substrate caller (HTTP 403):
  ...`, each reason at most once a minute). kagent itself only reports that discovering the
  tools failed with "Forbidden" or "Unauthorized", and leaves the toolset out of that run.
- **The platform needs a recent cluster.** Agent Substrate 1.0 uses pod certificates and cluster
  trust bundles (Kubernetes 1.35 with the `PodCertificateRequest`, `ClusterTrustBundle` and
  `ClusterTrustBundleProjection` feature gates, as kagent's own kind setup enables them).
  librarian's chart relies on the same trust-bundle projection to verify the issuer.
- **Only HTTP leaves a sandbox** (no WebSocket, no raw TCP). librarian's MCP endpoint is
  stateless Streamable HTTP with JSON responses, so nothing else is needed.

## Verified principals

With `principals.mode: optional` or `required`, an agent proves who it acts for by sending a
JWT from your identity provider in `X-Principal-Token` (configurable). The token's audience must
be librarian (`principals.issuers[].audience`); tokens minted for other applications are
refused, so an agent cannot simply forward a user's token for some other API.

Ways for an agent to get such a token on a user's behalf:

- **OAuth 2.0 Token Exchange (RFC 8693)**: the agent exchanges the user's token for one with
  audience librarian, where your identity provider supports it.
- **On-behalf-of flows** offered by your provider (for example Microsoft Entra's OBO flow).
- **Your own token service** that mints short-lived tokens for librarian after checking the user.

Keep tokens short-lived. librarian accepts only asymmetric signatures (RS*, PS*, ES*, EdDSA),
fetches the issuer's keys from its OIDC discovery document (or `jwks_url`), caches them for an
hour, and refreshes on an unknown key id at most every 30 seconds.
