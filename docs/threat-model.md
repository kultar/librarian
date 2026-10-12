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

### T1. Unauthorized or unintended writes
- Reads and writes are separate paths. `execute` and `fetch` can never write; only the `write`
  skill can, and only for operations a reviewer marked `writable: true` on a **verified** entry.
  Auto-learned (provisional) entries can never be written to.
- Writes use a **separate write credential** (`write_auth`); the read path never holds it.
- Every write needs a human approval for that exact request (bound by a fingerprint of agent,
  principal, operation, arguments and body) or a matching **grant**. Agents cannot approve
  anything; approvals come only from the approval page.
- Grants are bounded: one agent, one operation, pinned argument values, expiry, use caps, and
  revocable at once (`librarian grants revoke`). DELETE approvals can never become grants.
- Writes are sent exactly once and never follow redirects (a 307/308 would replay the write).
  `Idempotency-Key` is forwarded and idempotent retries return the first result.
- The planner can only *propose* writes; proposals go back to the calling agent.
- Per-agent `write_apis` allowlists and a separate, lower write rate limit.
- With `principals.mode: optional` or `required`, only a **verified** principal (or the agent's
  configured owner) counts as the requester. With `principals.mode: off`, the requester is
  whatever the agent claims: a compromised agent colluding with an insider could name that
  insider. Turn verification on, or use `group` approval for sensitive APIs.
- **Residual risk:** a grant allows any request body for its operation. Pin the arguments that
  matter and prefer short expiries.
- **Defense in depth:** scope credentials (read key read-only; write key only what is needed).

### T2. Prompt injection through API responses or requests
- The LLM can only *propose* calls; every call passes the same deterministic policy.
- Tool results are truncated and labelled as untrusted in the planner instructions.
- **Residual risk:** injected text could steer the planner to read *other*
  data the same caller is allowed to read and include it in the answer. Per-agent API allowlists
  bound this to what the caller could already read through `execute`.

- **Documentation is input too.** When learning from docs, the model has no tools and only fills
  a fixed structure. Each operation must quote the page (checked against the fetched text, outside
  `<script>`); the host is the onboarding caller's `base_url`, never the docs'; read/write comes
  from the HTTP method; the secret location never comes from the docs. Model-knowledge entries
  are reads only. Both start unconfirmed, and repeated failures suspend an operation.
- **Search results are untrusted too.** Docs found by search are ranked by trust (the API's own
  domain, then well-known docs hosts); third-party sites are not even fetched unless allowed,
  and a redirect off the official site makes a page third-party. The source and its trust
  level are recorded on the entry and shown in the review PR. The model's own search must
  visibly run (a search call in the reply), so an answer from memory is never mistaken for one.
- **Residual risk:** a docs page can describe a real GET operation misleadingly (e.g. one with
  side effects). This is the same trust level as an agent-supplied OpenAPI spec: provisional
  entries only allow GET/HEAD to the given host, and review before `verified` is the control.

### T3. Credential theft
- **Registry repo access.** Prefer the GitHub App: its installation tokens last under an hour and
  are requested for the registry repo only, with contents and pull requests permissions. Its
  private key is the long-lived secret: keep it in a Kubernetes Secret (or Secrets Manager via
  ExternalSecrets), mounted read-only. `github-app create` writes it to an owner-only file and
  never prints it; its one-time callback listens on 127.0.0.1 and checks a random `state`.
- **Model gateway keys.** With `model_base_url` set, librarian sends only `model_api_key` to the
  gateway. (Before 0.9.0, with no key configured, the model libraries fell back to
  `OPENAI_API_KEY`/`ANTHROPIC_API_KEY` from the environment and sent that provider key to the
  gateway URL.)
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
- DNS rebinding is closed by pinning: the host is resolved once, every address must be public,
  and the connection goes to that vetted IP. TLS still verifies the certificate against the real
  hostname (SNI), and pinned connections are never reused for another hostname.
- `internal_hosts` narrows that exception to hosts matching the listed patterns (e.g.
  `*.internal.example.com`): they may resolve to private addresses, but never to loopback or
  link-local, and DNS pinning still applies. A pattern can't be a bare `*`.
- Internal APIs belong in human-reviewed `verified` entries; keep `allow_private_networks` off
  and list only the domains you mean in `internal_hosts`. The review PR and the audit log say
  when an entry was learned under such a pattern.

The console (the pages listener's root) lists every API, pending request and grant, with
credential *references* only, and offers a few actions (revoke, block, forget, invalidate,
re-learn, re-check, clear). It needs a signed-in approver (or `approvers.console: open`, meant
for a laptop) and never appears on the A2A listener alone. Each action form carries a token
bound to the signed-in person (an HMAC with the link-signing key), so a page on another site
cannot submit it for them, and each action is written to the audit log with who did it.
Re-learning goes through the normal onboarding path (provisional entry, review pull request);
verified entries only change through the registry repo.

### T5. Approval-link abuse
- Links are HMAC tokens bound to one request, valid only while it is pending and until expiry,
  and dead after use. Pages are `no-store`, `no-referrer`, framed-denied, CSP-restricted.
- `token` mode trusts link possession (local/dev). Use `trusted_header` mode behind an auth proxy
  (e.g. oauth2-proxy) with an approver group in shared deployments.
- In `trusted_header` mode the identity headers must only ever come from the proxy. Set
  `pages_port`: the pages then get their own listener (bound to `127.0.0.1` by default, reachable
  only by a proxy sidecar) and the A2A listener, which agents reach directly, does not serve them
  at all. Without this, any client that can reach the A2A port could forge identity headers;
  librarian logs a warning in that configuration. The Helm chart wires this up for you.

### T6. Malicious or broken registry entries
- Strict validation: unknown fields rejected, absolute/protocol-relative paths rejected, base URLs
  cannot embed credentials, file names must match ids, invalid files are skipped not fatal.
- Review registry changes by pull request; run `librarian registry validate` in CI.

- **APIs change under a reviewed entry.** Re-checks compare the source with the entry; an
  operation whose HTTP method changed, or a writable one whose request changed, is suspended
  until a reviewer merges the proposed update. Suspensions live in the state database (tied to
  the operation's definition), so they cannot be undone by a registry file being re-synced.

### T7. Abuse / resource exhaustion
- Per-(caller, API) rate limits, request timeouts, response size caps, spec size caps, operation
  count caps, page limits, LLM request limits per `fetch`.

### T8. Caller impersonation
- Per-agent bearer keys, compared in constant time.
- Per-agent API allowlists (glob patterns) are enforced by the policy engine and also hide
  disallowed APIs from discovery (`capabilities`, planner tools). Onboarding and writes are
  separate per-agent permissions.
- Principals can be **verified**: a JWT from a configured issuer, checked for signature (JWKS,
  asymmetric algorithms only, no `none`, no HMAC so no key-confusion), issuer, audience
  (librarian) and expiry. The token travels in a header, not in message metadata, so it is not
  stored with tasks. In `required` mode a claimed principal without a token is refused.
- **Residual risk:** a token is a bearer credential; an agent holding a user's librarian-audience
  token can act as that user until it expires. Keep lifetimes short.

## Out of scope

- Multi-tenant isolation inside one librarian (run one per trust domain).
- Protection against a malicious operator or a compromised host.
- Waiting tasks resume after a restart only if the caller re-sends; pending credential requests
  and their approval links do survive restarts (durable state + persisted signing key).
