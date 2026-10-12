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
  by librarian after approval (`write_status`); missing credentials return `credential_pending`.
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

## kagent

kagent 1.0 (`api.kagent.dev/v1alpha3`, alpha at the time of writing) runs bring-your-own agents
as per-session sandboxed actors with an egress allowlist and kagent's own task store, which does
not fit a shared service like librarian. The supported way for kagent agents to use librarian
is as a tool: register librarian's `/mcp` endpoint as a `RemoteMCPServer` and bind it to the
agent (this also adds librarian to the agent's egress allowlist).

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
