# Deploying librarian for a team (Kubernetes)

This is the full setup, in order. Each step says what it is for, so you can skip what you don't
need. Try [getting started](getting-started.md) on a laptop first if you haven't.

**What you end up with.** An empty registry that fills itself: when an agent needs an API,
librarian learns it, opens a pull request so a person can review it, and asks on Slack for any key
it needs. Nothing is set up per API in advance.

```
agents / LiteLLM ──A2A or MCP──► librarian ──► the APIs your agents need
                                   │
       approvers (browser) ──SSO──►├─ approval pages (oauth2-proxy)
       Slack ◄─────────────────────┤  "a key is needed" / "approve this write"
       GitHub registry repo ◄──────┤  review PRs for learned APIs; git-sync pulls what's merged
       Postgres ◄──────────────────┤  requests, approvals, tasks (shared by replicas)
       Secrets Manager ◄───────────┘  the keys people add (agents never see them)
```

## 0. Before you start

You need:

| | Why |
|---|---|
| Kubernetes and Helm 3 | runs librarian (the chart is in this repo: `deploy/helm/librarian`) |
| An HTTPS hostname for the approval pages, and an ingress | people open approval links in a browser |
| An OIDC identity provider (Okta, Entra ID, Google, Keycloak, ...) | approvers sign in; approvals record who decided |
| A Slack workspace where you can install apps | where approvers are asked |
| A GitHub organization where you can create an App and a repo | the reviewed registry |
| PostgreSQL | shared state for all replicas (requests, approvals, learned APIs) |
| AWS Secrets Manager *(optional)* | where keys are stored; without it they go to files on the pod's volume (one replica only) |
| A model, directly or through LiteLLM *(optional)* | plain-language requests, and learning APIs that have no spec |

Every value below goes into one Helm values file (`values.yaml`) and one Secret
(`librarian-env`, for the sensitive ones). The complete example is in [step 9](#9-the-values-file).

librarian runs in its own namespace with its own service account (the chart creates it,
named `librarian`). It needs no Kubernetes permissions at all, and the pod gets no Kubernetes
API token; the service account only carries the AWS role in step 4.

```bash
kubectl create namespace librarian
```

## Network: what must reach what

librarian does not need to be reachable from the internet. Everything it talks to, it calls
out to; the only thing that connects *in* is approvers' browsers.

**Inbound**

| From | To | Notes |
|---|---|---|
| Agents, LiteLLM | `librarian.librarian.svc:8080` (A2A at `/`, MCP at `/mcp`) | Inside the cluster. For agents elsewhere, `ingress.a2a` on an internal address. |
| Approvers' browsers | the approval pages (`ingress.pages`, at `publicUrl`) | Only approvers need to reach them: an **internal** address on your VPN or office network is enough. |

Nothing else connects in. Slack only shows a link (no webhooks or interactive buttons), the
GitHub App has no webhook, your identity provider sends the *browser* back to the callback
URL (so an internal `publicUrl` works for sign-in too), and `librarian github-app create`
uses a callback on `127.0.0.1` of the machine you run it on.

If approvers should be able to approve from outside your network (say, from a phone), give
only the **pages** a public address, behind your edge (e.g. a CDN or identity-aware proxy) as
well as librarian's own sign-in. Never expose the A2A/MCP port publicly.

An internal-only pages ingress, e.g. with the AWS Load Balancer Controller:

```yaml
ingress:
  pages:
    enabled: true
    className: alb
    host: librarian.internal.example.com
    annotations:
      alb.ingress.kubernetes.io/scheme: internal
      alb.ingress.kubernetes.io/target-type: ip
      alb.ingress.kubernetes.io/listen-ports: '[{"HTTPS":443}]'
publicUrl: https://librarian.internal.example.com
```

**Outbound** (allow these if your cluster's egress is restricted)

| To | Why |
|---|---|
| `slack.com` | posting and updating cards |
| `api.github.com`, `github.com` | review pull requests (librarian), pulling the registry (git-sync) |
| AWS Secrets Manager and STS in your region | keys, and assuming the pod's role |
| your identity provider (OIDC issuer) | sign-in for the approval pages (oauth2-proxy) |
| your model or LiteLLM | `fetch`, learning APIs without a spec |
| your search provider (if `learning.search` is on) | finding API documentation |
| the APIs your agents use | the calls themselves, plus their specs and docs when learning them |

The last row is open-ended by design (agents bring APIs on demand). Private addresses are
refused unless the host matches a pattern in `config.internal_hosts` (see step 9), or
`allow_private_networks` is set; restrict egress further with your cluster's network policies
or egress proxy if you need to.

## 1. The registry repo and its GitHub App

The registry is a git repo with one YAML file per API. librarian proposes new entries there as
pull requests; people review and merge; git-sync pulls the merged result into the pod.

1. Create a **private** repo, e.g. `acme/api-registry`. Keep it private: it is a map of your
   APIs. It can start empty; step 2 gives it its first commit.
2. Create its GitHub App in one step. No Python environment is needed: with
   [`uv`](https://docs.astral.sh/uv/) installed, `uvx` runs the CLI straight from PyPI (or
   `pip install librarian-a2a` and run `librarian`):

   ```bash
   uvx --from librarian-a2a librarian github-app create --repo acme/api-registry \
     --key-file ~/.config/librarian/github-app.pem --init-repo
   ```

   Your browser opens GitHub: click **Create GitHub App**, then install it on **only**
   `acme/api-registry`. The command saves the App's private key where `--key-file` says
   (readable only by you; choose a path outside any git repo, the default `.librarian/` lands in
   whatever repo you run it from) and prints the App id and installation id.

   `--init-repo` creates `main` with a README **as the App** if the repo has no commits yet
   (an empty repo has no branch, so git-sync and review pull requests would fail). It works as
   long as no ruleset restricts creating the default branch; afterwards branch rules apply as
   usual and librarian works through pull requests. If it fails with a rule violation, make the
   first commit as a person, or exclude the App from the rule for a moment. For an App created
   earlier: `librarian github-app init-repo --repo ... --app-id ... --key-file ...`.
3. Store the key in the cluster:

   ```bash
   kubectl -n librarian create secret generic librarian-github-app \
     --from-file=private-key.pem=$HOME/.config/librarian/github-app.pem
   ```

   Then delete the local copy, or keep it in your password manager. (Using ExternalSecrets? Put
   the key in your secret store and sync it into a Secret with this name and key.)

**Branch protection** on `main` ("pull requests only, one approving review, code owners") is
fine: librarian only ever pushes `librarian/*` branches and opens pull requests, so leave those
branches unprotected. A code-owner rule does nothing until the repo has a `CODEOWNERS` file.

## 2. Slack

1. [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From a manifest** →
   paste [`deploy/slack-app-manifest.yaml`](../deploy/slack-app-manifest.yaml) → **Install**.
   It asks only to post messages and to look up people by email (to @mention requesters).
2. Copy the **Bot User OAuth Token** (`xoxb-...`) for the `librarian-env` Secret (step 8).
3. Pick a channel, e.g. `#api-access`. A public channel works without inviting the bot; for a
   private one, `/invite @librarian` there.
4. Optionally, create a Slack **user group** for approvers (e.g. `@api-approvers`) and note its
   **ID** (it starts with `S`). Cards @mention this group; without one they still post, just
   @mentioning nobody. Who may *decide* is settled by sign-in (step 3), not by Slack. If you
   can't see group IDs (no Slack admin): mention the group in a message in Slack's web version,
   right-click it and choose Inspect; the id is in a `data-...-id` attribute.

## 3. Sign-in for the approval pages

Approval links open librarian's pages through oauth2-proxy, so every decision records a real
person, and only members of your approver group can add keys.

1. In your identity provider, create an OIDC web application:
   - redirect URI: `https://librarian.example.com/oauth2/callback` (your pages hostname)
   - include group membership in the ID token, as a `groups` claim
2. Create its Secret:

   ```bash
   kubectl -n librarian create secret generic librarian-oauth2 \
     --from-literal=OAUTH2_PROXY_CLIENT_ID=<client id> \
     --from-literal=OAUTH2_PROXY_CLIENT_SECRET=<client secret> \
     --from-literal=OAUTH2_PROXY_COOKIE_SECRET="$(openssl rand -base64 32 | tr -- '+/' '-_')"
   ```

3. Decide the approver group *as your identity provider names it* (e.g. `api-approvers`). It is
   separate from the Slack group: Slack is who gets pinged, this is who may decide.

Once signed in, the pages host's root (`https://librarian.example.com/`) is the **console**:
every API librarian knows, what is waiting on a person, the standing grants, and the levers
(revoke a grant, block or forget an API, invalidate a credential, re-learn from a documentation
URL, re-check, clear a suspension). Only for members of the approver group; every action is
audited with who did it.

### Entra ID (Azure AD) recipe

The same three steps with the `az` CLI. Entra puts group **object ids** (not names) in the
`groups` claim, so the approver group is its object id.

```bash
az login --scope https://graph.microsoft.com//.default   # the Graph calls below may need this scope
APP=$(az ad app create --display-name librarian-approvals --sign-in-audience AzureADMyOrg \
  --web-redirect-uris https://librarian.example.com/oauth2/callback --query id -o tsv)
APP_ID=$(az ad app show --id "$APP" --query appId -o tsv)
# group membership and the email claims in the ID token
az rest --method PATCH --url "https://graph.microsoft.com/v1.0/applications/$APP" --body \
  '{"groupMembershipClaims":"SecurityGroup","optionalClaims":{"idToken":[{"name":"email"},{"name":"preferred_username"}]}}'
SP=$(az ad sp create --id "$APP_ID" --query id -o tsv)
# only members of the approver group may sign in at all
az ad sp update --id "$SP" --set appRoleAssignmentRequired=true
az rest --method POST --url "https://graph.microsoft.com/v1.0/servicePrincipals/$SP/appRoleAssignedTo" \
  --body "{\"principalId\":\"<approver group object id>\",\"resourceId\":\"$SP\",\"appRoleId\":\"00000000-0000-0000-0000-000000000000\"}"
# the client secret goes straight into the Kubernetes Secret, never through the terminal
az ad app credential reset --id "$APP_ID" --append --display-name oauth2-proxy --years 1 \
    --query password -o tsv \
  | kubectl -n librarian create secret generic librarian-oauth2 \
      --from-literal=OAUTH2_PROXY_CLIENT_ID="$APP_ID" \
      --from-file=OAUTH2_PROXY_CLIENT_SECRET=/dev/stdin \
      --from-literal=OAUTH2_PROXY_COOKIE_SECRET="$(openssl rand -base64 32 | tr -- '+/' '-_')"
```

`--append` matters: without it `credential reset` replaces the app's existing secrets
(`az ad app credential add` does not exist). Then, in the values file (step 9):

```yaml
oauth2Proxy:
  enabled: true
  existingSecret: librarian-oauth2
  oidcIssuerUrl: https://login.microsoftonline.com/<tenant id>/v2.0
  approverGroup: <approver group object id>
  extraArgs:                                 # Entra's ID tokens carry the address in
    - --oidc-email-claim=preferred_username  # preferred_username, unverified
    - --insecure-oidc-allow-unverified-email=true
```

Verified end to end with oauth2-proxy v7.7.1: the sign-in arrives as `preferred_username`
(the person's UPN), which becomes the approver identity librarian records.

## 4. Where keys are stored

**AWS Secrets Manager** (recommended): keys people add are written there, under a prefix.

1. An IAM role for the pod (IRSA or EKS Pod Identity) with this policy, scoped to the prefix:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Action": [
           "secretsmanager:GetSecretValue",
           "secretsmanager:PutSecretValue",
           "secretsmanager:CreateSecret",
           "secretsmanager:TagResource"
         ],
         "Resource": "arn:aws:secretsmanager:<region>:<account>:secret:librarian/*"
       },
       {
         "Effect": "Allow",
         "Action": "secretsmanager:ListSecrets",
         "Resource": "*"
       }
     ]
   }
   ```

   `ListSecrets` lets an approver pick an *existing* secret instead of pasting a key; drop it if
   you don't want that. Add `kms:Decrypt` / `kms:GenerateDataKey` if you encrypt with your own
   KMS key.

   The role's trust policy must name librarian's service account exactly, namespace included
   (IRSA shown; with EKS Pod Identity, associate the role with `librarian/librarian` instead):

   ```json
   {
     "Effect": "Allow",
     "Principal": {"Federated": "arn:aws:iam::<account>:oidc-provider/<cluster OIDC issuer>"},
     "Action": "sts:AssumeRoleWithWebIdentity",
     "Condition": {"StringEquals": {
       "<cluster OIDC issuer>:sub": "system:serviceaccount:librarian:librarian",
       "<cluster OIDC issuer>:aud": "sts.amazonaws.com"
     }}
   }
   ```

   If you install under another release name or namespace, the service account is
   `<namespace>:<release name>` (or set `serviceAccount.name`).
2. In the values: `serviceAccount.annotations` with the role, and
   `config.secrets_backend: aws`, `config.aws_secrets.prefix: librarian/`.

Without AWS: keys are files on the pod's volume. Fine for one replica; back up the volume.

## 5. The database

Create a PostgreSQL database and user for librarian. The URL goes in the `librarian-env` Secret:
`LIBRARIAN_DATABASE_URL=postgresql+asyncpg://librarian:<password>@<host>:5432/librarian?ssl=require`.
librarian creates its tables on start. (Without it, SQLite on the volume: one replica only.)

`?ssl=require` makes the connection use TLS even when the server doesn't insist on it (Aurora
has `force_ssl` off by default). The driver is asyncpg, so the parameter is `ssl`, not
`sslmode` as in `psql` URLs; librarian accepts `sslmode=` and translates it.

Point it at the **writer** (on Aurora, the cluster endpoint), never a read replica or reader
endpoint: replicas read each other's writes straight away (approvals, newly learned APIs), and
"run exactly once" relies on conditional updates. The load is small. A writer failover or
restart costs a reconnect, not failed requests.

## 6. A model (optional)

Needed for plain-language requests (`fetch`) and for learning APIs that have no OpenAPI spec.
Through LiteLLM:

```yaml
config:
  model: claude-sonnet-5-5               # a model name your LiteLLM serves
  model_base_url: http://litellm.litellm.svc:4000/v1
  model_api: anthropic                   # needed for web search through LiteLLM
  learning:
    model: claude-opus-5-5               # optional: a stronger model for learning only
    search: {provider: model}            # let librarian search the web for API docs
```

`model` answers every plain-language request, so its cost scales with agent traffic;
`learning.model` runs once per API (reading its documentation, searching for it), so a stronger
model there is cheap. Both go through the same gateway settings. Unset, learning uses `model`.
`fallback_model` is tried once when either fails or declines a request (content-filter false
positives happen on wording like "private repositories").

and `LIBRARIAN_MODEL_API_KEY=<a LiteLLM virtual key for librarian>` in `librarian-env`.

Make that key in LiteLLM with its value **pre-generated**, so it only ever lives in your secret
store and the `librarian-env` Secret (LiteLLM accepts a `key` of your choosing):

```bash
curl -s "$LITELLM_URL/key/generate" -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'content-type: application/json' \
  -d '{"key": "<sk-... from your secret store>", "key_alias": "librarian",
       "models": ["claude-sonnet-5-5"]}'
```

`models` is an allowlist: any other model answers 403. Change it later with `/key/update`
(same body, with `key`).

## 7. Which agents may call librarian

Each caller gets a key and a scope. For agents that reach librarian through LiteLLM, the gateway
is one caller (people are told apart by their identity token; see
[integrations](integrations.md#litellm-agent-gateway)).

```yaml
config:
  callers:
    - agent: litellm-gateway
      key_ref: librarian/callers/litellm-gateway
      apis: ["*"]               # every API, as they are learned
      write_apis: []            # no writes until you decide otherwise
      owner: platform-team@example.com
```

Generate the key: `openssl rand -hex 32`. It goes in `librarian-env` as
`LIBRARIAN_SECRET_LIBRARIAN_CALLERS_LITELLM_GATEWAY` (the `key_ref`, upper-cased, `/` and `-`
become `_`), and the same value is configured on the caller's side (LiteLLM).

## 8. The environment Secret

```bash
kubectl -n librarian create secret generic librarian-env \
  --from-literal=LIBRARIAN_NOTIFIER__SLACK__BOT_TOKEN=xoxb-... \
  --from-literal=LIBRARIAN_DATABASE_URL='postgresql+asyncpg://librarian:...@db:5432/librarian' \
  --from-literal=LIBRARIAN_MODEL_API_KEY=sk-... \
  --from-literal=LIBRARIAN_SECRET_LIBRARIAN_CALLERS_LITELLM_GATEWAY="$(openssl rand -hex 32)"
```

Using ExternalSecrets instead? Create the same keys in your secret store and sync them into a
Secret called `librarian-env`.

## 9. The values file

```yaml
image:
  tag: "0.12.1"

replicaCount: 2                          # needs steps 4 and 5; see "Replicas" below
persistence:
  enabled: false                         # everything lives in Postgres, Secrets Manager, git
publicUrl: https://librarian.example.com # the approval pages' address

envFrom:
  - secretRef: {name: librarian-env}

serviceAccount:
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::<account>:role/librarian

registry:
  gitSync:
    enabled: true
    repo: https://github.com/acme/api-registry.git
    path: registry                       # folder in the repo for the YAML files
githubApp:
  enabled: true
  appId: "<printed by github-app create>"
  installationId: "<printed by github-app create>"
  secretName: librarian-github-app

oauth2Proxy:
  enabled: true
  existingSecret: librarian-oauth2
  oidcIssuerUrl: https://login.example.com
  approverGroup: api-approvers           # as your identity provider names it

ingress:
  pages:
    enabled: true
    className: <your ingress class>
    host: librarian.example.com
    tls: [{secretName: librarian-tls, hosts: [librarian.example.com]}]

config:
  secrets_backend: aws
  aws_secrets: {prefix: librarian/}
  internal_hosts: ["*.internal.example.com"]   # internal APIs agents may learn on demand
  notifier:
    type: slack
    slack: {channel: "#api-access", approver_group: S0123ABCD}
  writes:
    approvers: requester_or_group
  callers:
    - agent: litellm-gateway
      key_ref: librarian/callers/litellm-gateway
      apis: ["*"]
      write_apis: []
      owner: platform-team@example.com
  # model, model_base_url, model_api, learning: see step 6

otel:                                    # optional: traces and metrics
  enabled: false
  endpoint: http://otel-collector.observability:4318
```

Agents inside the cluster reach librarian at `http://librarian.librarian.svc:8080` (A2A) and
`.../mcp` (MCP). Only the approval pages need an ingress.

## 10. Install and check

The chart is published with every release as an OCI artifact, with the same version as the
image (`image.tag` above):

```bash
helm install librarian oci://ghcr.io/kultar/charts/librarian --version 0.12.1 \
  -n librarian -f values.yaml
```

(or from a checkout of this repo: `helm install librarian ./deploy/helm/librarian ...`). Then:

```bash
kubectl -n librarian rollout status deploy/librarian
helm test librarian -n librarian
```

Then, from a pod in the cluster (or `kubectl -n librarian port-forward svc/librarian 8080`):

```bash
curl -s localhost:8080/healthz          # {"status":"ok","apis":0,...}: empty, as expected
```

End to end, with the quick start's `ask` helper (`LIBRARIAN_KEY` = the caller key from step 8):

```bash
export LIBRARIAN_URL=http://localhost:8080 LIBRARIAN_KEY=<caller key>
./examples/quickstart/ask '{"skill":"onboard","api":"apisguru","spec_url":"https://api.apis.guru/v2/openapi.yaml"}'
```

- the reply includes a `review_url`: a pull request on your registry repo, opened by your App
- `./examples/quickstart/ask '{"skill":"execute","api":"apisguru","operation":"getMetrics"}'`
  returns data
- after merging the pull request, `curl localhost:8080/healthz` shows `"apis": 1` within a minute
  (git-sync)

The first API that needs a key will post a card in your Slack channel; follow its link, sign in,
and add the key. That is the whole loop.

## 11. Connect your agents

- **Through LiteLLM** (MCP and/or A2A): [integrations](integrations.md#litellm-agent-gateway).
- **Directly**: A2A at `http://librarian.librarian.svc:8080/`, MCP at `.../mcp`, with
  `Authorization: Bearer <caller key>`.

## Replicas

Run two or more for production. Everything a replica needs is shared: requests, approvals, tasks,
APIs learned on demand and what librarian has observed live in **Postgres**; keys in **Secrets
Manager**; the reviewed registry in **git** (each pod pulls it). An API learned through one
replica is usable through the others immediately. So replicas need no volume:
`persistence.enabled: false`.

With `replicaCount` above 1 the chart passes the count to librarian, which refuses to start
without Postgres and `secrets_backend: aws` (rather than let replicas drift apart), adds a
PodDisruptionBudget (`maxUnavailable: 1`), and spreads pods across nodes. It refuses a
ReadWriteOnce volume with several replicas.

Per-replica on purpose: rate limits (`rate_limit_per_minute` applies per replica).

## Running it

- **Upgrades**: bump `image.tag`, then `helm upgrade librarian oci://ghcr.io/kultar/charts/librarian
  --version <same> -n librarian -f values.yaml`.
  State is in Postgres and Secrets Manager; the registry is in git.
- **Turning things on later**: writes per agent (`write_apis`), standing grants
  (`config.writes.grants`), verified sign-in for agents' users (`config.principals`), internal
  APIs within the domains you list (`config.internal_hosts`). See [`examples/librarian.yaml`](../examples/librarian.yaml)
  for every option.
- **Audit**: one JSON line per call, decision and approval on stdout (`"audit"` events), plus
  OpenTelemetry traces and metrics when enabled.
- **Problems**: [troubleshooting](troubleshooting.md).
