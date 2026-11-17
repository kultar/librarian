# librarian on kagent and Agent Substrate

A manual run, not part of CI: it needs a cluster with the feature gates Agent Substrate uses
and a model key for the test agents. Last run on kind v0.33 (Kubernetes 1.35.0) with kagent
1.0.1, which installs Agent Substrate 1.0.0.

What it shows:

- an agent calls librarian's MCP tools through Substrate's egress gateway with no key of its
  own; librarian verifies the injected actor JWT against the cluster's issuer (trusting the
  cluster signer's bundle the chart mounts), asks kagent which Agent the session belongs to,
  and applies the matching rule;
- the agent learns an API and reads from it;
- a write waits for the rule's owner; once approved with a grant, a **new session** of the same
  agent writes without asking (grants follow the Agent, not the session);
- a plan of two writes, the second using what the first returned, is approved once, and the
  agent has the outcome in the same turn by waiting in `write_status`;
- an agent bound to only some of the tools has only those, and kagent's own approval stops a
  write before librarian is asked;
- an agent no rule matches is refused (403), and a token minted for another audience is
  refused (401), each with the reason in librarian's log.

## Run it

```bash
cd tests/integration/substrate

# 1. A cluster with pod certificates and cluster trust bundles.
kind create cluster --name lib-substrate --config kind-config.yaml \
  --image kindest/node:v1.35.0 --kubeconfig ./kubeconfig --wait 180s
export KUBECONFIG=$PWD/kubeconfig

# 2. kagent, which brings Agent Substrate and PostgreSQL. The key is for the test agents' model.
export ANTHROPIC_API_KEY=...
kagent install --provider Anthropic          # the kagent CLI, v1.0.1

# 3. librarian from this checkout.
docker build -t librarian:substrate ../../..
kind load docker-image librarian:substrate --name lib-substrate
#   (a multi-arch image: docker save -o l.tar librarian:substrate && kind load image-archive l.tar ...)
helm install librarian ../../../deploy/helm/librarian -n librarian --create-namespace \
  -f librarian-values.yaml
kubectl -n librarian rollout status deploy/librarian
kubectl -n librarian get remotemcpservers      # librarian   ...   ACCEPTED True

# 4. A demo API with a write operation, its registry entry, and the test agents.
kubectl apply -f demo-api.yaml -f agents.yaml
kubectl -n librarian exec -i deploy/librarian -- sh -c \
  'mkdir -p /data/registry && cat > /data/registry/demo.yaml' \
  < ../../../examples/quickstart/registry/demo.yaml     # imported into the database within 15 s
kubectl -n kagent get agents                   # all four READY True

# 5. Talk to the agents, and open librarian's pages.
kubectl -n kagent port-forward svc/kagent-controller 18083:8083 &
kubectl -n librarian port-forward svc/librarian 18081:8081 &
K="kagent --api-url http://localhost:18083 --gateway-url http://localhost:18083"
session() { $K agent session create --agent "$1" -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["session"]["id"])'; }
```

### Reads

```bash
$K agent invoke --session "$(session lib-reader)" --task 'Use the librarian tools. First onboard
the API with id apisguru from the spec URL https://api.apis.guru/v2/openapi.yaml. Then execute
its getMetrics operation and tell me the value of numAPIs.'
```

The agent answers with the number. `kubectl -n librarian logs deploy/librarian` shows the
lookup, `substrate actor/kagent/session-... is calling as agent 'kagent/lib-reader'`, and audit
lines whose `caller` is `kagent/lib-reader` with the session under `extra.via`.

### A write, its owner, and the grant

```bash
S1=$(session lib-writer)
$K agent invoke --session "$S1" --task 'Call the librarian tool `write` with api "demo",
operation "addComment", args {"id": "42"}, body {"text": "hello"} and reason "test". Report the
status and the approval id.'
```

The agent reports `pending` and an approval id. librarian's log has the approval link
(`write approval needed: ... approve at http://localhost:18081/approvals/...`): open it. The page
says the agent is `kagent/lib-writer`, on behalf of nobody, requester `owner@example.test`. Choose
**Approve and allow similar writes**. librarian sends the write itself; the agent does not need to
be running.

```bash
$K agent invoke --session "$S1" --task 'Call the librarian tool `write_status` with that approval id.'
$K agent invoke --session "$(session lib-writer)" --task 'Call the librarian tool `write` with api
"demo", operation "addComment", args {"id": "77"}, body {"text": "second session"} and reason "test".'
```

The first reports the write as executed (HTTP 200). The second, a different session and so a
different actor, runs at once: "Ran at once under a standing grant ...".

### A plan, and waiting for it in the same turn

```bash
$K agent invoke --session "$(session lib-writer)" --task 'Use the librarian tools, exactly as
told. First call `write_plan` with api "demo", reason "test", and steps [{"id":"one","operation":
"addComment","args":{"id":"42"},"body":{"text":"first"}},{"id":"two","operation":"addComment",
"args":{"id":"43"},"body":{"text":"second","seen":{"$from":"one.json.text"}}}]. Then call
`write_status` once with the approval id from the answer and wait_seconds 45. Report the final
state and the state of each step.'
```

While the agent waits, open the approval link from librarian's log: the page shows both
requests, the second with `seen` marked as taken from the first. Choose **Approve this plan**.
The agent's one `write_status` call answers within seconds: `executed`, both steps `made`, the
second sent with `"seen": "first"`. (A plan is never made under a grant, so this one asks even
after the grant above.) Nothing can wake a kagent session when a decision arrives later; without
`wait_seconds` the agent reports `pending` and has to be asked again.

### Some of the tools, and kagent's own approval

`lib-careful` is bound to four of librarian's tools freely and to `write` and `write_plan` with
`requireApproval: true`.

```bash
$K agent invoke --session "$(session lib-careful)" --task 'Which librarian tools do you have?'
$K agent invoke --session "$(session lib-careful)" --task 'Call the librarian tool `write` with
api "demo", operation "addComment", args {"id": "42"}, body {"text": "hello"} and reason "test".'
```

Of librarian's tools the first lists the six of its two bindings (no `onboard`), beside the
harness's own. The second stops before the call is made: kagent holds it for the person in the
conversation ("Please approve or reject the tool call write() ... Input required to continue
this Session"), and librarian's log shows no request. That question is answered in kagent's UI;
the `kagent` CLI cannot. librarian's own approval comes after it.

### Another harness

The agents above run on kagent's default harness. With `harnessRef: {name: claude-default}`
in an Agent (kagent's Claude-based harness) the same was run: a read, and a plan approved while
the agent waited in `write_status`. `codex-default` was not tried: it needs an OpenAI key.

### Refusals

```bash
$K agent invoke --session "$(session lib-outsider)" --task 'Which librarian tools do you have?'
kubectl -n librarian logs deploy/librarian | grep 'refused a Substrate caller'
```

The agent has none. The log says
`refused a Substrate caller (HTTP 403): agent 'kagent/lib-outsider' is not allowed to use
librarian: no rule under substrate.rules matches it`. To see a wrong audience refused, register a
second `RemoteMCPServer` for the same URL whose `actorJwt.audiences` omit `librarian`, bind an
agent to it, and look for `HTTP 401 ... was not issued for librarian`.

## Clean up

```bash
kind delete cluster --name lib-substrate
```

## Things this run found

- A sandbox's DNS resolves only fully qualified names: `librarian.librarian.svc` fails,
  `librarian.librarian.svc.cluster.local` works. The chart registers the full name.
- kagent reports a refused tool discovery only as "Forbidden"; the reason is in librarian's log.
- kagent sends an MCP server the agent's identity or the signed-in user's token, never both
  (it refuses a server configured for both). With token propagation switched on
  (`config.runtimeDefaults.tokenPropagation.enabled=true` when installing kagent) the runtime
  adds an `X-User-Id` header beside the actor's token; nothing signs it and it is set inside the
  sandbox, so librarian does not take it for the person. With
  `substrate.kagent.claimedUserHeader: X-User-Id` it shows the name on the approval, as a claim.
- With an actor token, kagent's controller does not list the server's tools (the
  `RemoteMCPServer` is accepted with discovery disabled): agents discover them at run time, and
  kagent's tool picker stays empty for librarian.
