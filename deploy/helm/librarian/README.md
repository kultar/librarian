# librarian Helm chart

**Setting up for a team? Follow [docs/deploy.md](../../../docs/deploy.md)**: it walks through
everything this chart needs (registry repo, Slack, sign-in, secrets, database), in order. This
page is the chart reference.

```bash
# from the published chart (same version as the image; every release pushes it)
helm install librarian oci://ghcr.io/kultar/charts/librarian --version 0.22.1 \
  -n librarian --create-namespace --set publicUrl=https://librarian.example.com
# or from a checkout
helm install librarian ./deploy/helm/librarian -n librarian --create-namespace \
  --set publicUrl=https://librarian.example.com
```

## What runs

```
                  ┌──────────────────────── pod ───────────────────────────┐
agents ──A2A────► │ librarian :8080 (A2A)                                  │
                  │           :8081 (pages, 127.0.0.1 when proxy enabled)  │
people ──HTTPS──► │ oauth2-proxy :4180 ──► 127.0.0.1:8081                  │  (optional)
                  │ git-sync ──► /git (hand-written entries to import)     │  (optional)
                  └── /data (PVC): SQLite, file secrets ───────────────────┘
```

- **Agents** call the A2A port through the Service (`http://<release>.<ns>.svc:8080`), which is
  also what the agent card advertises (`a2aUrl` to override). Behind
  [agentgateway](../../../docs/integrations.md#agentgateway), `agentgateway.enabled` attaches
  librarian to your Gateway and agents use the Gateway's address.
- **People** open approval links at `publicUrl`, served through **oauth2-proxy** when enabled.
  The pages listener then binds to `127.0.0.1`, so identity headers can only come from the proxy,
  and the A2A port does not serve the pages at all.
- **Registry**: lives in the database (SQLite on the PVC, or Postgres), and so does every
  approval: decisions take effect at once, on every replica. With `githubApp`, each approved
  decision is also committed to a repository you own (the record); librarian never reads it
  back. `registry.gitSync` is only for importing entries you write by hand and keep in git.

## Production checklist

| | Values |
|---|---|
| Approver sign-in | `oauth2Proxy.enabled=true`, `oauth2Proxy.existingSecret`, `oauth2Proxy.oidcIssuerUrl`, `oauth2Proxy.approverGroup`, `ingress.pages` |
| Hand-written entries from git (optional) | `registry.gitSync.enabled=true`, `registry.gitSync.repo`, `.path`: new files are imported into the database |
| The console at the pages host's root | on by default for signed-in approvers (`config.approvers.console`: `signed_in`, `open`, `off`) |
| The record on GitHub | `githubApp.enabled=true`, `githubApp.repo`, `githubApp.appId`, `githubApp.installationId`, `githubApp.secretName` (see below) |
| Probes | `probes.startup` (default: 10 s period, 60 failures, so a large registry can finish parsing before liveness applies), `probes.readiness`, `probes.liveness`; each takes Kubernetes probe fields |
| Automatic writes in the channel | `config.writes.announce_auto: true` posts each automatically approved write to Slack with a one-click way back; off by default, every one is in the audit log regardless |
| Grant uses in the channel | `config.writes.announce_grants: false` stops the post made each time a standing grant is used (a grant covering a multi-step flow posts one per step); on by default. Every use stays in the audit log and on the console's Activity page, and the Grants page can turn notices off per grant |
| Grant length | `config.writes.max_grant_days` (default `null`: no limit) caps how long an approver may make an "allow similar writes" grant last, e.g. `90`; with no limit approvers may choose until revoked. Grants are re-timed or revoked on the console's Grants page |
| Caller keys | `config.callers` + caller keys as `LIBRARIAN_SECRET_LIBRARIAN_CALLERS_<AGENT>` via `envFrom` |
| Behind agentgateway | `agentgateway.enabled=true`, `agentgateway.gateway.name` (and `.namespace`): an `HTTPRoute` and two `AgentgatewayBackend`s on your Gateway, MCP at `/mcp` and A2A on every other path. Sign-in, rules and limits are set on the gateway's side. See [integrations](../../../docs/integrations.md#agentgateway) |
| Agents on Agent Substrate / kagent | `substrate.enabled=true`, `substrate.rules`: agents are identified by the actor JWT the egress gateway injects (no key each). Mounts the cluster signer's trust bundle and creates a kagent `RemoteMCPServer`; `substrate.kagent.enabled=false` for plain Substrate. See [integrations](../../../docs/integrations.md#agent-substrate-and-kagent) |
| Secrets in AWS | `config.secrets_backend: aws`, `serviceAccount.annotations` (IRSA role) |
| Several replicas | `replicaCount>1`, `persistence.enabled=false`, Postgres (`LIBRARIAN_DATABASE_URL`), `config.secrets_backend: aws` |
| Slack | `config.notifier.type: slack`, `config.notifier.slack.channel`, token via `envFrom` |
| Model for `fetch` | `config.model`, optionally `config.model_base_url` (LiteLLM), key via `envFrom` |
| Telemetry | `otel.enabled=true`, `otel.endpoint` |

### GitHub App for the record

Approvals are made in librarian and stored in its database. One GitHub App, owned by your
organization, lets librarian commit each approved decision to a (private) repository as well,
so the registry also lives outside the database. Its tokens are minted per use, last under an
hour, and cover only that repo. Create it in one step:

```bash
# no Python environment needed: uvx runs the CLI from PyPI (or pip install librarian-a2a)
uvx --from librarian-a2a librarian github-app create --repo acme/api-registry \
  --key-file ~/.config/librarian/github-app.pem --init-repo
# opens GitHub: click "Create", then install it on acme/api-registry only. Saves the private
# key to --key-file (keep it outside any repo) and prints the values below. --init-repo gives an
# empty repo its first commit as the App (commits need a branch).

kubectl -n librarian create secret generic librarian-github-app \
  --from-file=private-key.pem=$HOME/.config/librarian/github-app.pem
```

```yaml
githubApp: {enabled: true, repo: acme/api-registry, appId: "123456", installationId: "7890123", secretName: librarian-github-app}
```

The chart mounts the key into librarian and points its record at that repo.
`librarian github-app check --repo ... --app-id ... --key-file ...` verifies the App can reach
the repo. To keep the key in AWS Secrets Manager instead, sync it into that Secret with
ExternalSecrets.

**Upgrading a release from before 0.20** (`registry.gitSync` + `githubApp`, where merging a
pull request was the approval): upgrade with the values unchanged, so the first start imports
the reviewed entries into the database; then set `githubApp.repo` and drop `registry.gitSync`.
With `githubApp.repo` unset the chart keeps the old layout (the record goes to the git-sync
repository, `path` and `ref`). See [docs/registry.md](../../../docs/registry.md#upgrading-from-019-or-earlier).

Example secret for the environment:

```bash
kubectl -n librarian create secret generic librarian-env \
  --from-literal=LIBRARIAN_NOTIFIER__SLACK__BOT_TOKEN=xoxb-... \
  --from-literal=LIBRARIAN_SECRET_LIBRARIAN_CALLERS_RELEASE_NOTES_AGENT="$(openssl rand -hex 32)"
# then: --set envFrom[0].secretRef.name=librarian-env
```

oauth2-proxy secret (`OAUTH2_PROXY_CLIENT_ID`, `OAUTH2_PROXY_CLIENT_SECRET`,
`OAUTH2_PROXY_COOKIE_SECRET`):

```bash
kubectl -n librarian create secret generic librarian-oauth2 \
  --from-literal=OAUTH2_PROXY_CLIENT_ID=... --from-literal=OAUTH2_PROXY_CLIENT_SECRET=... \
  --from-literal=OAUTH2_PROXY_COOKIE_SECRET="$(openssl rand -base64 32 | tr -- '+/' '-_')"
```

Your identity provider must put group membership in the token (`groups` claim by default; see
oauth2-proxy's `--oidc-groups-claim`) for `approverGroup` and group approval to work.

## Values

Every value of the chart. `config` is librarian's own configuration: its settings are in the
[README](../../../README.md#settings).

**The image and the pods**

| Value | Default | |
|---|---|---|
| `image.repository` | `ghcr.io/kultar/librarian` | The image |
| `image.tag` |  | Its tag. Default: the chart's `appVersion` |
| `image.pullPolicy` | `IfNotPresent` | |
| `imagePullSecrets` | `[]` | Secrets for a private registry |
| `replicaCount` | `1` | More than one needs Postgres and `config.secrets_backend: aws` (librarian refuses to start otherwise); the chart then adds a PodDisruptionBudget and spreads the pods across nodes |
| `resources` |  | Requests of 100m CPU and 256Mi memory, a memory limit of 512Mi |
| `probes.startup` |  | Every 10 s, up to 60 failures: a large registry is parsed before the server binds. `null` drops the probe |
| `probes.readiness` |  | Every 10 s |
| `probes.liveness` |  | Every 20 s, 3 failures |
| `podAnnotations` |  | Added to the pods |
| `podLabels` |  | Added to the pods |
| `podSecurityContext` |  | Not root (user and group 10001), `RuntimeDefault` seccomp |
| `securityContext` |  | No privilege escalation, a read-only root filesystem, every capability dropped |
| `topologySpreadConstraints` | `[]` | Used as given. With several replicas and none set, pods are spread across nodes |
| `nodeSelector` |  | |
| `tolerations` | `[]` | |
| `affinity` |  | |
| `serviceAccount.create` | `true` | librarian's own service account. It needs no Kubernetes permissions |
| `serviceAccount.name` |  | Its name. Default: the release's |
| `serviceAccount.annotations` |  | E.g. `eks.amazonaws.com/role-arn`, the IAM role for Secrets Manager |
| `serviceAccount.automountToken` | `false` | No Kubernetes API token in the pod: nothing uses it |

**Addresses**

| Value | Default | |
|---|---|---|
| `publicUrl` |  | Where approvers' browsers reach the pages; every link librarian sends starts with it. Usually the host of `ingress.pages` |
| `a2aUrl` |  | The A2A address the agent card names. Default: the Service inside the cluster |
| `service.type` | `ClusterIP` | |
| `service.a2aPort` | `8080` | Agents: A2A at `/`, MCP at `/mcp` |
| `service.pagesPort` | `8081` | People: the approval pages and the console |
| `ingress.a2a.enabled` | `false` | An Ingress for agents outside the cluster |
| `ingress.a2a.className` |  | |
| `ingress.a2a.annotations` |  | |
| `ingress.a2a.host` |  | |
| `ingress.a2a.tls` | `[]` | |
| `ingress.pages.enabled` | `false` | An Ingress for approvers' browsers (with `oauth2Proxy`, through the sign-in) |
| `ingress.pages.className` |  | |
| `ingress.pages.annotations` |  | |
| `ingress.pages.host` |  | |
| `ingress.pages.tls` | `[]` | |
| `agentgateway.enabled` | `false` | Attach librarian to a Gateway run by [agentgateway](https://agentgateway.dev): an `HTTPRoute` and two `AgentgatewayBackend`s (an MCP server at `/mcp`, an A2A agent on every other path). Needs the Gateway API and agentgateway's CRDs |
| `agentgateway.gateway.name` |  | The Gateway (required) |
| `agentgateway.gateway.namespace` |  | Its namespace. Default: the release's. In another one, the Gateway's listener has to allow routes from here |
| `agentgateway.gateway.sectionName` |  | One listener of it. Default: every listener that allows the route |
| `agentgateway.hostnames` | `[]` | Host names of the route. Default: the listener's |

**Configuration, secrets and storage**

| Value | Default | |
|---|---|---|
| `config` |  | librarian's settings. The chart manages the listeners, the paths and the approver header names; everything else is yours |
| `env` | `[]` | Extra environment variables |
| `envFrom` | `[]` | Secrets as environment: the Slack token, the model key, caller keys as `LIBRARIAN_SECRET_LIBRARIAN_CALLERS_<AGENT>` |
| `persistence.enabled` | `true` | A volume at `/data` for the SQLite database, file secrets and learned APIs. Not needed with Postgres and AWS secrets: turn it off for several replicas |
| `persistence.size` | `1Gi` | |
| `persistence.storageClass` |  | |
| `persistence.accessModes` | `[ReadWriteOnce]` | |
| `persistence.existingClaim` |  | Use this claim instead of creating one |

**Hand-written entries from git, and the record on GitHub**

| Value | Default | |
|---|---|---|
| `registry.gitSync.enabled` | `false` | Pull hand-written entries from a git repository; librarian imports new files into its database |
| `registry.gitSync.image` | `registry.k8s.io/git-sync/git-sync:v4.4.0` | |
| `registry.gitSync.repo` |  | E.g. `https://github.com/acme/api-registry.git` |
| `registry.gitSync.ref` | `main` | |
| `registry.gitSync.path` | `registry` | The folder in the repository that holds the `<id>.yaml` files |
| `registry.gitSync.period` | `60s` | How often it pulls |
| `registry.gitSync.credentialsSecret` |  | A Secret with git credentials, for a private repository |
| `registry.gitSync.resources` |  | |
| `githubApp.enabled` | `false` | Commit every approved decision to a GitHub repository as well (the record). Nothing is read back from it |
| `githubApp.repo` |  | `owner/name` of that repository. Default: `registry.gitSync.repo` |
| `githubApp.appId` |  | The App's id, in quotes |
| `githubApp.installationId` |  | Its installation's id, in quotes |
| `githubApp.secretName` |  | The Secret that holds the App's private key |
| `githubApp.secretKey` | `private-key.pem` | The key's name in that Secret |

**Agents on Agent Substrate and kagent**

| Value | Default | |
|---|---|---|
| `substrate.enabled` | `false` | Accept the actor JWT that the platform's egress gateway puts into each request, in place of a key per agent |
| `substrate.issuer` | `https://idp.ate-system.svc` | The actor JWT issuer |
| `substrate.audience` | `librarian` | What the tokens must be minted for |
| `substrate.clusterDomain` | `cluster.local` | The cluster's DNS domain: sandboxes resolve only full names, so librarian is registered under one |
| `substrate.trustBundle.enabled` | `true` | Mount the cluster signer's trust bundle, to fetch the issuer's keys. Off for an issuer with a publicly trusted certificate |
| `substrate.trustBundle.signerName` | `servicedns.podcert.ate.dev/identity` | |
| `substrate.trustBundle.labelSelector` |  | Which bundles of that signer: `podcert.ate.dev/canarying: live` |
| `substrate.kagent.enabled` | `true` | Ask kagent which Agent a session belongs to, so that rules, grants and the audit trail are about the Agent. Off: plain Substrate, where rules match the actor |
| `substrate.kagent.url` | `http://kagent-controller.kagent.svc:8083` | kagent's controller |
| `substrate.kagent.claimedUserHeader` |  | The header in which kagent's runtime names the signed-in user (`X-User-Id`, with kagent's token propagation on). Nothing signs it: librarian shows it to approvers and records it as what the agent's side says, and uses it for nothing else. Unset: ignored |
| `substrate.kagent.remoteMCPServer.create` | `true` | Register librarian as a `RemoteMCPServer`, for Agents to list under their tools |
| `substrate.kagent.remoteMCPServer.name` | `librarian` | |
| `substrate.kagent.remoteMCPServer.namespace` |  | Default: the release's (kagent must watch it) |
| `substrate.kagent.remoteMCPServer.allowedNamespaces` | `All` | Which namespaces' Agents may use it: `All`, or `Same` |
| `substrate.kagent.remoteMCPServer.timeout` | `120s` | How long kagent lets one tool call take |
| `substrate.kagent.remoteMCPServer.tokenSeconds` | `900` | How long each injected token lives (300 to 3600) |
| `substrate.rules` | `[]` | What each agent may do; the first match counts, and an agent no rule matches is refused |

**Sign-in for approvers**

| Value | Default | |
|---|---|---|
| `oauth2Proxy.enabled` | `false` | Run oauth2-proxy in front of the pages, and take approvers' identity from it |
| `oauth2Proxy.image` | `quay.io/oauth2-proxy/oauth2-proxy:v7.7.1` | |
| `oauth2Proxy.existingSecret` |  | A Secret with `OAUTH2_PROXY_CLIENT_ID`, `OAUTH2_PROXY_CLIENT_SECRET` and `OAUTH2_PROXY_COOKIE_SECRET` |
| `oauth2Proxy.provider` | `oidc` | |
| `oauth2Proxy.oidcIssuerUrl` |  | Your identity provider, e.g. `https://acme.okta.com` |
| `oauth2Proxy.emailDomains` | `["*"]` | Which email domains may sign in |
| `oauth2Proxy.approverGroup` |  | The group whose members may approve |
| `oauth2Proxy.extraArgs` | `[]` | More arguments for oauth2-proxy |
| `oauth2Proxy.resources` |  | |

**Telemetry**

| Value | Default | |
|---|---|---|
| `otel.enabled` | `false` | Send traces and metrics with OpenTelemetry |
| `otel.endpoint` |  | The collector, e.g. `http://otel-collector.observability:4318` |

## Tested

The chart is exercised on a local kind cluster with git-sync and oauth2-proxy enabled: helm test,
onboarding (surviving pod replacement), A2A port refusing the pages even with
forged identity headers, proxy redirecting unauthenticated browsers to sign-in, and the pages
listener unreachable from other pods. With `substrate.enabled`, on kind with kagent 1.0.1 and
Agent Substrate 1.0.0 (see [`tests/integration/substrate`](../../../tests/integration/substrate)).
With `agentgateway.enabled`, on kind with agentgateway v1.7.0 and Gateway API v1.6.3: MCP and
A2A calls through the Gateway.
