# Deploying librarian for a team (Kubernetes)

This is the full setup, in order. Each step says what it is for, so you can skip what you don't
need. Try [getting started](getting-started.md) on a laptop first if you haven't.

**What you end up with.** An empty registry that fills itself: when an agent needs an API,
librarian learns it, asks your approvers to review it, and asks on Slack for any key it needs.
Approvals are made on librarian's own signed-in pages and live in its database; a GitHub
repository receives a copy of every decision. Nothing is set up per API in advance.

```
agents / LiteLLM ──A2A or MCP──► librarian ──► the APIs your agents need
                                   │
       approvers (browser) ──SSO──►├─ approval pages (oauth2-proxy)
       Slack ◄─────────────────────┤  "a key is needed" / "approve this write"
       Postgres ◄──────────────────┤  the registry, decisions, requests, tasks (the source of truth)
       GitHub record repo ◄────────┤  a commit for every approved decision (written, never read)
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
| PostgreSQL | the source of truth, shared by all replicas: the registry, every decision, requests |
| A GitHub organization where you can create an App and a repo *(optional, recommended)* | the record: a copy of every approved decision outside the database |
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
| `api.github.com` | writing the record (and `github.com` for git-sync, if you import hand-written entries) |
| AWS Secrets Manager and STS in your region | keys, and assuming the pod's role |
| your identity provider (OIDC issuer) | sign-in for the approval pages (oauth2-proxy) |
| your model or LiteLLM | `fetch`, learning APIs without a spec |
| your search provider (if `learning.search` is on) | finding API documentation |
| the APIs your agents use | the calls themselves, plus their specs and docs when learning them |

The last row is open-ended by design (agents bring APIs on demand). Private addresses are
refused unless the host matches a pattern in `config.internal_hosts` (see step 9), or
`allow_private_networks` is set; restrict egress further with your cluster's network policies
or egress proxy if you need to.

## 1. The record repository and its GitHub App

The registry itself lives in Postgres, and that is where approvals are made. This step gives it
a second home: a git repo to which librarian commits one YAML file per approved API and one
commit per decision, saying who decided what. You get a readable history and a copy to restore
from; librarian never reads it back, so nothing merged there changes what agents may do. Skip
this step and everything still works, with the database as the only copy.

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
   (an empty repo has no branch to commit to). It works as
   long as no ruleset restricts creating the default branch. If it fails with a rule violation, make the
   first commit as a person, or exclude the App from the rule for a moment. For an App created
   earlier: `librarian github-app init-repo --repo ... --app-id ... --key-file ...`.
3. Store the key in the cluster:

   ```bash
   kubectl -n librarian create secret generic librarian-github-app \
     --from-file=private-key.pem=$HOME/.config/librarian/github-app.pem
   ```

   Then delete the local copy, or keep it in your password manager. (Using ExternalSecrets? Put
   the key in your secret store and sync it into a Secret with this name and key.)

**Branch rules.** librarian commits to `main` directly. If a rule there only lets changes in
through pull requests, librarian opens one from a `librarian/record-...` branch and merges it as
the App in the same step: leave `librarian/*` branches unprotected, and put the App on the
rule's bypass list if merging needs an approving review or a check (nobody is meant to review
these: the decision was already made, by a signed-in approver, in librarian). If GitHub refuses
anyway, decisions are unaffected; the console shows which APIs are not written yet and why.

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
person, and only members of your approver group can add keys, approve writes, or approve a
learned API. These pages are where approval happens, so the approver group is the control on
what agents may do: keep it to the people who should decide that.

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

**No stored key at all** is possible for some APIs: a GitHub App's tokens, OAuth client
credentials and AWS request signing are made for each call
([README](../README.md#credentials-made-for-each-call)). For AWS APIs that means IAM instead
of secrets: give each API a role with only what it needs, let those roles trust librarian's
pod role, and allow the pod role to assume exactly them:

```json
{"Effect": "Allow", "Action": "sts:AssumeRole",
 "Resource": ["arn:aws:iam::<account>:role/orders-reader", "arn:aws:iam::<account>:role/orders-writer"]}
```

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

This database is the source of truth for what agents may do: the registry, and every approval
and decision. Back it up like any database you care about. The record repository (step 1) is
the copy outside it: after losing the database, `librarian registry restore` brings the
approved entries back (pending requests and grants are not in the record).

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
  tag: "0.20.0"

replicaCount: 2                          # needs steps 4 and 5; see "Replicas" below
persistence:
  enabled: false                         # everything lives in Postgres and Secrets Manager
publicUrl: https://librarian.example.com # the approval pages' address

envFrom:
  - secretRef: {name: librarian-env}

serviceAccount:
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::<account>:role/librarian

githubApp:                               # the record (step 1); leave out to keep no copy
  enabled: true
  repo: acme/api-registry
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
    # max_grant_days: 90        # cap on "allow similar writes" grants; default: no cap
    # announce_grants: false    # no channel post on each use of a grant (audited regardless)
    # announce_auto: true       # post each automatically approved write (off by default)
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
helm install librarian oci://ghcr.io/kultar/charts/librarian --version 0.20.0 \
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

- the reply says approvers were asked to approve it, and a review card appears in Slack
- `./examples/quickstart/ask '{"skill":"execute","api":"apisguru","operation":"getMetrics"}'`
  returns data (reads work before approval)
- open the card's **Review** link (or the console at `publicUrl`, under *Waiting*), sign in and
  approve: the API is verified at once, and a commit for it appears in your record repository
  a moment later

The first API that needs a key will post a card in your Slack channel; follow its link, sign in,
and add the key. That is the whole loop.

## 11. Connect your agents

- **Through LiteLLM** (MCP and/or A2A): [integrations](integrations.md#litellm-agent-gateway).
- **On kagent or Agent Substrate**: set `substrate.enabled: true` and `substrate.rules` in the
  values file. Agents then need no key for librarian: the platform's gateway signs each call,
  and a rule says what each agent may do:
  [integrations](integrations.md#agent-substrate-and-kagent).
- **Directly**: A2A at `http://librarian.librarian.svc:8080/`, MCP at `.../mcp`, with
  `Authorization: Bearer <caller key>`.

## Replicas

Run two or more for production. Everything a replica needs is shared: requests, approvals, tasks,
the registry, every decision and what librarian has observed live in **Postgres**; keys in
**Secrets Manager**. An API learned or approved through one replica is in effect on the others
immediately. So replicas need no volume:
`persistence.enabled: false`.

With `replicaCount` above 1 the chart passes the count to librarian, which refuses to start
without Postgres and `secrets_backend: aws` (rather than let replicas drift apart), adds a
PodDisruptionBudget (`maxUnavailable: 1`), and spreads pods across nodes. It refuses a
ReadWriteOnce volume with several replicas.

Rate limits are shared: with Postgres, `rate_limit_per_minute` is one limit across all replicas.

## Running it

- **Upgrades**: bump `image.tag`, then `helm upgrade librarian oci://ghcr.io/kultar/charts/librarian
  --version <same> -n librarian -f values.yaml`.
  State is in Postgres and Secrets Manager.
- **Upgrading from 0.19 or earlier**: approvals moved from the GitHub repository into the
  database. Upgrade with your values unchanged (git-sync still on): the first start imports
  every reviewed entry into the database and closes the review pull requests librarian had
  left open (what they asked for is in the console). Check the console lists your APIs, then
  set `githubApp.repo` and remove `registry.gitSync`: the repository keeps receiving every
  decision, as commits. Details and what to do if the files were already gone:
  [registry](registry.md#upgrading-from-019-or-earlier).
- **After upgrading from before 0.20**, run `kubectl -n librarian exec deploy/librarian --
  librarian doctor` once: it looks for credentials those versions could have stored in the
  database, and says what to do about each
  ([troubleshooting](troubleshooting.md#agents-and-calls)).
- **Turning things on later**: writes per agent (`write_apis`; the operations themselves are
  enabled from the Slack card or the console when an agent first asks), standing grants
  (`config.writes.grants`; approvers also create them from the approval page, capped by
  `config.writes.max_grant_days`, quieted with `config.writes.announce_grants`), verified
  sign-in for agents' users (`config.principals`), internal
  APIs within the domains you list (`config.internal_hosts`). See [`examples/librarian.yaml`](../examples/librarian.yaml)
  for every option.
- **Audit**: one JSON line per call, decision and approval on stdout (`"audit"` events), plus
  OpenTelemetry traces and metrics when enabled. With a database, the same events are also
  kept there for `audit_retention_days` (30) and shown on the console's Activity pages; the
  stream remains the complete record. Ship it with your log pipeline.
- **Problems**: [troubleshooting](troubleshooting.md).
