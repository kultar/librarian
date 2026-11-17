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
| Operator (deploys, configures, owns the database and the registry directory) | Trusted |
| Approver (approves learned APIs, enables writes, adds credentials, on the signed-in pages) | Trusted for those decisions |
| The record repository on GitHub | **Not trusted as input**: written to, never read back |
| Calling agent | **Untrusted input**, authenticated by key, or by an Agent Substrate actor token |
| API responses | **Untrusted input**, may contain prompt injection |
| Downloaded OpenAPI specs / community registry entries | **Untrusted input** |
| The planner LLM | **Untrusted**: treated as a suggestion engine |

## Threats and mitigations

### T1. Unauthorized or unintended writes
- Reads and writes are separate paths. `execute` and `fetch` can never write; only the `write`
  skill can, and only for operations an approver enabled (`writable: true`) on a **verified** entry.
  Auto-learned (provisional) entries can never be written to.
- Writes use a **separate write credential** (`write_auth`); the read path never holds it.
- **Automatic approval** is per operation, chosen by an approver (recorded in the
  entry), capped per day, never for DELETE, still subject to every policy check and the write
  rate limit, audited on each use (and announced, unless `writes.announce_grants` or the
  grant's notices are off), and revocable with one click.
- Every write needs a human approval for that exact request (bound by a fingerprint of agent,
  principal, operation, arguments and body) or a matching **grant**. Agents cannot approve
  anything; approvals come only from the approval page.
- Grants are bounded: one agent, one operation, pinned argument values, use caps, and an expiry
  the approver chooses (until revoked is allowed unless `writes.max_grant_days` caps it); each is
  revocable or re-timed at once from the console (`librarian grants revoke` too). DELETE
  approvals can never become grants.
- Writes are sent exactly once and never follow redirects (a 307/308 would replay the write).
  `Idempotency-Key` is forwarded and idempotent retries return the first result, on any replica
  and after a restart (the key is indexed with the write in the database).
- **Plans** (several writes approved as one): a plan is on one API, so what a step answers
  only ever goes back to the API that said it. Every step passes the same policy check as a
  write before anyone is asked, and the approval is bound to exactly these steps (a
  fingerprint of agent, principal and every step); a plan never runs under a grant or
  automatic approval and cannot become a grant. A value taken from an earlier step
  (`{"$from": ...}`) is shown to the approver as a reference, filled in when the plan runs,
  and the step is then checked again as a write with the real value, against the registry
  as it is at that moment. An argument an agent's limits restrict cannot be a reference, and
  a DELETE cannot take anything from an earlier step: what it removes is in the plan. A
  credential an API echoes is removed from a step's answer before the next step can take
  anything from it. The first step that fails or cannot be checked ends the plan; each step
  is recorded before the next is sent, so a stopped replica leaves a record of how far it
  got, and a plan closed as interrupted is never carried on. **Residual risk:** nothing is
  undone, so a plan that stops part of the way leaves what it had made; and the approver
  decides on a reference without seeing the value the API will put there (it comes from the
  same API the write goes to, and only into the place shown).
- The planner can only *propose* writes; proposals go back to the calling agent.
- Per-agent `write_apis` allowlists and a separate, lower write rate limit.
- **Limits inside an API** (`limits` on a caller or a Substrate rule): the operations an agent
  may use and the values its arguments may take, decided by the policy engine before anything
  else about the operation. A call outside them is refused for reads and writes alike, is
  never turned into a request for an approver, and is not covered by any grant. An operation
  that takes none of the limited arguments is refused unless named, since it could not be
  held to anything; a limited argument cannot be left out. **Residual risk:** only arguments
  are looked at, exactly and case-sensitively. What a request body names (another repository
  as a pull request's head, a query in a search body) is not, and an API that treats two
  spellings as the same resource is not known to. A credential that cannot do more is the
  stronger control where there is one.
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
  and limits inside an API bound this to what the caller could already read through `execute`.

- **People are steered too, by what they are shown.** An approver reads what the agent wrote:
  its reason, its arguments, the name of an API it onboarded. On the pages all of it is
  escaped. In Slack, `<...>` is markup: a mention that pings a whole channel, a link with any
  words over it ("approved by security: open here"). Every text librarian sends to Slack is
  therefore escaped where it leaves; only its own mentions and links are markup. (Before
  0.22 an agent's text went into the card as it was.)

- **Documentation is input too.** When learning from docs, the model has no tools and only fills
  a fixed structure. Each operation must quote the page (checked against the fetched text, outside
  `<script>`); the host is the onboarding caller's `base_url`, never the docs'; read/write comes
  from the HTTP method; the secret location never comes from the docs. Model-knowledge entries
  are reads only. Both start unconfirmed, and repeated failures suspend an operation.
- **Search results are untrusted too.** Docs found by search are ranked by trust (the API's own
  domain, then well-known docs hosts); third-party sites are not even fetched unless allowed,
  and a redirect off the official site makes a page third-party. The source and its trust
  level are recorded on the entry and shown on the review page. The model's own search must
  visibly run (a search call in the reply), so an answer from memory is never mistaken for one.
- **Residual risk:** a docs page can describe a real GET operation misleadingly (e.g. one with
  side effects). This is the same trust level as an agent-supplied OpenAPI spec: provisional
  entries only allow GET/HEAD to the given host, and review before `verified` is the control.

### T3. Credential theft
- **Record repository access.** Prefer the GitHub App: its installation tokens last under an hour and
  are requested for the record repository only, with contents and pull requests permissions. Its
  private key is the long-lived secret: keep it in a Kubernetes Secret (or Secrets Manager via
  ExternalSecrets), mounted read-only. `github-app create` writes it to an owner-only file and
  never prints it; its one-time callback listens on 127.0.0.1 and checks a random `state`.
- **Model gateway keys.** With `model_base_url` set, librarian sends only `model_api_key` to the
  gateway. (Before 0.9.0, with no key configured, the model libraries fell back to
  `OPENAI_API_KEY`/`ANTHROPIC_API_KEY` from the environment and sent that provider key to the
  gateway URL.)
- Secrets never enter A2A messages, Slack, logs, audit events, provenance or LLM context.
  Query-string keys are redacted in provenance URLs.
- **Credentials made for each call.** Where an API allows it, what is sent is not a stored
  key: an OAuth client-credentials token (the client secret goes to the token endpoint only),
  a GitHub App installation token (an hour, the one repository the call names, and for a read
  every permission turned down to `read`, so GitHub itself refuses a write made with it), or
  an AWS signature that fits one request (made with librarian's own identity, or with
  fifteen-minute credentials of a role assumed for that API). A token is kept in memory until
  shortly before it expires, never stored, and removed from answers like any credential.
  These kinds say where a secret is sent and whose identity is used, so only an approved
  entry may use them, a token endpoint must be https, and through the console a secret can
  only be referred to under the API's own name (one API cannot be pointed at another's key).
  What a token endpoint or GitHub answers is never quoted beyond a status and a short error
  code. **Residual risk:** an approver decides these settings; whoever can approve can name a
  token endpoint or a role.
- **An API that sends the credential back.** Some APIs answer with the request they received:
  an echo or debugging endpoint, links that repeat the query string with its key, an error page
  that quotes the URL, a message that names the rejected key. librarian removes the credential
  it sent from every answer before the answer goes anywhere (the agent, a model, the audit
  trail, the stored result of a write). It looks for the value as sent, for Basic
  authentication also the encoded pair and its password, and each of those as a URL, a JSON
  string or an HTML page would write it; an answer cut off at the size limit inside the
  credential loses that fragment too. **Limits:** only the credential of that call is removed
  (another key that an API returns as data is data); a value shorter than six characters is
  left alone, because replacing it everywhere would mangle ordinary data; and a key the API
  transformed (hashed, or encoded some other way) is not recognised. Results stored before
  0.20 are not rewritten: `librarian doctor` finds any credential that got into the database
  or the audit file (and `--redact` replaces it there). If an API you write to sends its key
  back, replace that key after upgrading.
- **A stored key that cannot be sent.** A key stored with a line break after it (the usual
  accident when it is created from a file) makes an HTTP client refuse the request, with an
  error that quotes the header it refused: the key. librarian therefore sends a credential
  without the whitespace around it (or a byte-order mark before it), refuses one with a line
  break or a non-ASCII character inside it before anything is sent, with a reason that does
  not name it, and never passes the HTTP client's own message on. (Before 0.20 that message
  went to the log, and for a write to the agent and into the approval's record.)
- Secrets are only accepted through the secure page and written straight to the store
  (file store: `0600` files, path traversal rejected).
- Credentials are only attached to requests for the entry's own origin; off-origin redirects are
  never followed, so a compromised API can't bounce the token elsewhere.
- Callers cannot supply credentials (prevents "use my key against my server" data poisoning).
- Internal secrets (caller keys, the link-signing key) live under the reserved `librarian/` prefix.
  Registry entries may not reference that prefix and approvers are never offered it, so no entry
  (community, learned or hand-written) can make the librarian send an internal secret to a host.

### T3b. Acting as the person (per-person OAuth)
- Each person's tokens live in the secret store under the API and a hash of the principal,
  never in messages, logs or the registry. The executor picks the token by the request's
  principal; nothing is cached across people, and a test proves B's request never carries A's
  token.
- Only the person themselves can use their sign-in link (the proxy identity must match the
  principal; the link is one-time and expires in 30 minutes). The OAuth `state` carries the
  link token, and PKCE binds the code to librarian's request.
- Per-person auth is only honoured on verified entries, so the token and authorization URLs
  (where the client secret and codes go) were approved by a person. With `principals.mode`
  other than `off`, the person must be verified by the identity provider.

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
  and list only the domains you mean in `internal_hosts`. The review page and the audit log say
  when an entry was learned under such a pattern.

The console (the pages listener's root) lists every API, pending request and grant, with
credential *references* only, and offers a few actions (revoke, block, forget, invalidate,
re-learn, re-check, clear). It needs a signed-in approver (or `approvers.console: open`, meant
for a laptop) and never appears on the A2A listener alone. Each action form carries a token
bound to the signed-in person (an HMAC with the link-signing key), so a page on another site
cannot submit it for them, and each action is written to the audit log with who did it.
Re-learning goes through the normal onboarding path (provisional entry, a review for the
approvers); a verified entry changes only by an approver's decision in the console or on an
approval page.

The review of a learned API works the same way: a one-time link to a page where a signed-in
approver approves or denies. Approving sets `status: verified` in the database; operations whose
access is unknown stay unknown, hence refused, until a person decides them on that page or in
the console.

**Where authority sits (since 0.20).** What agents may do is decided by the entries in
librarian's database and nowhere else. The people who can change them are the approvers
(`approvers.group`, signed in through the proxy), each decision bound to their identity and
audited; and the operator, through the database, the config and the registry directory.
- The record repository is an output. librarian commits each approved decision there and does
  not read it back, so write access to that repository (a merged pull request, a force-push, a
  compromised token) cannot enable a write, verify an API or change a host. Before 0.20 a merge
  there *was* the approval; that path is gone.
- This also removes a control some deployments added on top: a required human review on the
  registry repository. If you relied on that as a second approval, the approver group is now
  the only gate: keep it small, and keep `approvers.mode: trusted_header` (not `token`, where
  holding a link is enough) anywhere real credentials are at stake.
- `librarian registry restore` is the one path from the record back into the database. It is a
  command the operator runs on purpose, and it trusts the repository's content as it is at
  that moment: check the repository's history before restoring from it.
- Registry files (`registry_dir`) are operator configuration: a file's `status` is taken as
  written when it is imported. A file cannot overrule a decision made in librarian (the
  database's entry wins and the console flags the file), but a new file can add a verified
  entry, so guard that directory, and any repository synced into it, like the config.

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
- Hand-written entries: review them like code and run `librarian registry validate` in CI.

- **APIs change under an approved entry.** Re-checks compare the source with the entry; an
  operation whose HTTP method changed, or a writable one whose request changed, is suspended
  until an approver approves the update (or clears the suspension). Suspensions live in the
  state database, tied to the operation's definition.

### T7. Abuse / resource exhaustion
- Per-(caller, API) rate limits, request timeouts, response size caps, spec size caps, operation
  count caps, page limits, LLM request limits per `fetch`.
- **An API that misbehaves** (by accident, or on purpose: a provisional entry is an API an
  agent chose). Besides the size cap, one request has a time limit as a whole (twice
  `request_timeout_seconds`), so an API that sends a byte now and then cannot hold a call
  open; a compressed body is cut at the size cap while it is unpacked; redirects, pages and
  rate-limit headers are bounded or ignored when they make no sense. An answer is passed on as
  data only if everything downstream can handle it: JSON nested deeper than 100 levels is
  passed on as text, `NaN` and `Infinity` as `null`, half a Unicode surrogate pair as the
  replacement character, and nothing a terminal would act on reaches the log.
- **What goes back to an agent is bounded too.** Over MCP a tool result of more than
  `mcp_max_result_bytes` is not sent: the agent gets the answer's outline and asks for less.
  What `capabilities` answers stays under `capabilities_max_bytes` however many APIs and
  operations the registry holds, so learning one more large API cannot push every agent's
  first answer out of its context. What it leaves out it says, and it names only what that
  caller may use: the hint for an API or operation written another way is taken from the
  same list.
  An agent that waits for a decision (`write_status` with `wait_seconds`) waits at most
  `mcp_max_wait_seconds`, and while it does librarian looks at the database every two seconds,
  which is less than the agent asking again and again would cost.
- **A write always ends.** Once a replica has taken an approved write to send it, its approval
  reaches a final state whatever happens: the API's answer, "not sent", "may have reached the
  API", or, if the replica stopped in the middle, "interrupted" (closed after fifteen minutes,
  or longer where requests are allowed to take long; never sent again by librarian). A plan
  is measured from the last step it recorded, and a replica that comes back after its plan
  was closed sends nothing more of it. A repeat with the same idempotency key is refused
  while the first is still open. An agent is told which of these it was, never "internal
  error".
- These are tested as a whole: `tests/hostile.py` is a fake API with every misbehaviour we
  know of, and each one runs through every way an agent reaches librarian (the service, A2A,
  MCP), for a read and for an approved write, with the same three claims checked each time:
  the agent is told something it can act on, no credential leaves, and a write ends.

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
- **Behind a gateway that signs people in** (agentgateway, LiteLLM) the gateway's own key says
  only that a request came through the gateway. Who the person is, librarian takes from the
  person's token, which the gateway passes on and librarian verifies itself: a gateway cannot
  make librarian believe in a person it holds no valid token for. It does hold the tokens
  that pass through it, so the residual risk above applies to the gateway as well.
- **Agents on Agent Substrate / kagent** are identified by an actor JWT that the platform's
  egress gateway puts into the request; the agent never holds it. librarian checks it like a
  principal token (signature against the cluster issuer's keys, issuer, expiry) and also that:
  - its audience is librarian's, so a token an actor was given for another service, or one a
    service received from an actor, cannot be replayed here;
  - it names an actor: the same keys sign tokens for workers acting for an actor, which are
    refused;
  - with kagent, kagent still vouches for it: librarian exchanges the token for the Agent
    behind the actor and repeats that every minute, so a deleted agent or an ended session
    stops working within that minute although the token itself cannot be revoked.
  What the agent may then do comes only from `substrate.rules` in librarian's config, never
  from the token or from metadata on the Agent: whoever can edit Agents in a namespace gets
  what the rule for that namespace allows, no more. A rule's field names are checked strictly,
  because a misspelt `match` would otherwise match every agent. A person is never taken from
  what the agent's side sends unsigned: kagent can add the signed-in user's name in an
  `X-User-Id` header, set inside the sandbox where the agent itself runs commands. librarian
  ignores it by default. With `substrate.claimed_user_header` it is shown to the approver and
  recorded, always worded as a claim, with control characters removed and its length
  bounded; it is never the requester of a write, and never the person an API acts as.
- **Residual risks there:** the token is a bearer credential valid for up to an hour (Substrate
  has no revocation), so anything between the gateway and librarian that can read it can use
  it: that hop is plain HTTP in-cluster unless a mesh encrypts it, so limit who can reach
  librarian's Service. librarian never logs, stores or echoes the token. Trusting the issuer
  means trusting the cluster's Substrate control plane: whoever can make it mint tokens, or
  edit an actor's egress policy to request librarian's audience, can call librarian as that
  actor (and gets only what the rules give that agent).

## Out of scope

- Multi-tenant isolation inside one librarian (run one per trust domain).
- Protection against a malicious operator or a compromised host.
- Waiting tasks resume after a restart only if the caller re-sends; pending credential requests
  and their approval links do survive restarts (durable state + persisted signing key).
