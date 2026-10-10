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

## LiteLLM (model gateway)

The `fetch` skill plans with any OpenAI-compatible endpoint:

```yaml
model: claude-sonnet-5-5                 # the model name LiteLLM serves
model_base_url: http://litellm:4000/v1
# model_api_key: via LIBRARIAN_MODEL_API_KEY
```

Through a gateway, the planner asks for its answer as JSON in the reply (`planner_output:
prompted`, the default with `model_base_url`), because some providers reject the forced tool use
that structured output otherwise needs. [`examples/compose`](../examples/compose) runs librarian
and LiteLLM together.

## kagent

kagent agents talk A2A, so they can delegate API work to librarian as a remote A2A agent. How a
kagent `Agent` references a remote A2A agent depends on your kagent version; check
`kubectl explain agent.spec` on your cluster for the remote-agent / A2A tool fields rather than
copying a manifest from elsewhere. Point it at the librarian Service
(`http://librarian.<namespace>.svc:8080`), give the kagent agent its own caller key, and scope it
with `apis` / `write_apis` in librarian's `callers` config.

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
