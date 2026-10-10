# Threat model

librarian sits between many agents and many APIs and holds the credentials. That makes it a
high-value target. This document lists what we defend against, how, and what is out of scope.

## Assets

1. **API credentials** in the secret store.
2. **Data** reachable through registered APIs.
3. **The registry**: a malicious entry can redirect calls or mislabel operations.
4. **The audit log**: the record of who read what.

## Actors

| Actor | Trust |
|---|---|
| Operator (deploys, configures, reviews registry PRs) | Trusted |
| Approver (adds credentials via the secure page) | Trusted for the approval action only |
| Calling agent | **Untrusted input**, authenticated by key |
| API responses | **Untrusted input**, may contain prompt injection |
| Downloaded OpenAPI specs / community registry entries | **Untrusted input** |
| The planner LLM | **Untrusted**: treated as a suggestion engine |

## Threats and mitigations

### T1. A caller gets the librarian to modify data
- The policy engine (`policy.py`, no LLM) only allows operations marked `access: read`.
- `read` by method is limited to GET/HEAD by model validation; a POST can only be a read after a
  human marks it `read_basis: reviewed` **and** the entry is `verified`.
- The planner's `describe_api` never even shows write operations to the model.
- **Defense in depth:** use read-only scoped credentials wherever the API supports them.

### T2. Prompt injection through API responses or requests
- The LLM can only *propose* calls; every call passes the same deterministic policy.
- Tool results are truncated and labelled as untrusted in the planner instructions.
- **Residual risk:** within read-only policy, injected text could steer the planner to read *other*
  allowed data and include it in its answer to the caller. The caller could already read that data
  through `execute`, so this is not an escalation for the caller, but restrict which APIs a caller
  may use (planned per-caller policies) if that matters.

### T3. Credential theft
- Secrets never enter A2A messages, Slack, logs, audit events, provenance or LLM context.
  Query-string keys are redacted in provenance URLs.
- Secrets are only accepted through the secure page and written straight to the store
  (file store: `0600` files, path traversal rejected).
- Credentials are only attached to requests for the entry's own origin; off-origin redirects are
  never followed, so a compromised API can't bounce the token elsewhere.
- Callers cannot supply credentials (prevents "use my key against my server" data poisoning).
- Internal secrets (caller keys, the link-signing key) live under the reserved `librarian/` prefix.
  Registry entries may not reference that prefix and approvers are never offered it, so no entry
  (community, learned or hand-written) can make the librarian send an internal secret to a host.

### T4. Server-side request forgery (SSRF)
- URLs are built from the registry entry; callers only supply argument values. Path arguments
  cannot contain `/` or `..`; header arguments can't set auth headers; CR/LF are rejected.
- `onboard` lets callers name a URL. By default spec fetching (each redirect hop included) and
  calls to **provisional** entries may only reach public addresses: loopback, private, link-local
  (cloud metadata) and similar ranges are refused (`allow_private_networks: false`).
- **Residual risk:** DNS rebinding between the check and the connection. Pin internal APIs as
  `verified` entries and keep `allow_private_networks` off; a connection-level resolver pin is planned.

### T5. Approval-link abuse
- Links are HMAC tokens bound to one request, valid only while it is pending and until expiry,
  and dead after use. Pages are `no-store`, `no-referrer`, framed-denied, CSP-restricted.
- `token` mode trusts link possession (local/dev). Use `trusted_header` mode behind an auth proxy
  (e.g. oauth2-proxy) with an approver group in shared deployments.

### T6. Malicious or broken registry entries
- Strict validation: unknown fields rejected, absolute/protocol-relative paths rejected, base URLs
  cannot embed credentials, file names must match ids, invalid files are skipped not fatal.
- Review registry changes by pull request; run `librarian registry validate` in CI.

### T7. Abuse / resource exhaustion
- Per-(caller, API) rate limits, request timeouts, response size caps, spec size caps, operation
  count caps, page limits, LLM request limits per `fetch`.

### T8. Caller impersonation
- Per-agent bearer keys, compared in constant time. The `principal` a caller asserts is recorded
  for audit but **not verified** in v0.1. Verified delegation (token exchange) is on the roadmap.

## Out of scope (v0.1)

- Writes of any kind.
- Multi-tenant isolation inside one librarian (run one per trust domain).
- Protection against a malicious operator or a compromised host.
- Waiting tasks resume after a restart only if the caller re-sends; pending credential requests
  and their approval links do survive restarts (durable state + persisted signing key).
