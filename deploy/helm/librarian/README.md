# librarian Helm chart

**Setting up for a team? Follow [docs/deploy.md](../../../docs/deploy.md)**: it walks through
everything this chart needs (registry repo, Slack, sign-in, secrets, database), in order. This
page is the chart reference.

```bash
# from the published chart (same version as the image; every release pushes it)
helm install librarian oci://ghcr.io/kultar/charts/librarian --version 0.14.0 \
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
                  │ git-sync ──► /git (reviewed registry)                  │  (optional)
                  └── /data (PVC): SQLite, file secrets, registry overlay ─┘
```

- **Agents** call the A2A port through the Service (`http://<release>.<ns>.svc:8080`), which is
  also what the agent card advertises (`a2aUrl` to override).
- **People** open approval links at `publicUrl`, served through **oauth2-proxy** when enabled.
  The pages listener then binds to `127.0.0.1`, so identity headers can only come from the proxy,
  and the A2A port does not serve the pages at all.
- **Registry**: with `registry.gitSync`, your reviewed registry repo is pulled continuously and
  hot-reloaded; the librarian's own changes (learned APIs, blocks) go to an overlay on the PVC.

## Production checklist

| | Values |
|---|---|
| Approver sign-in | `oauth2Proxy.enabled=true`, `oauth2Proxy.existingSecret`, `oauth2Proxy.oidcIssuerUrl`, `oauth2Proxy.approverGroup`, `ingress.pages` |
| Reviewed registry | `registry.gitSync.enabled=true`, `registry.gitSync.repo`, `.path` |
| The console at the pages host's root | on by default for signed-in approvers (`config.approvers.console`: `signed_in`, `open`, `off`) |
| Registry repo access (GitHub App) | `githubApp.enabled=true`, `githubApp.appId`, `githubApp.installationId`, `githubApp.secretName` (see below) |
| Caller keys | `config.callers` + caller keys as `LIBRARIAN_SECRET_LIBRARIAN_CALLERS_<AGENT>` via `envFrom` |
| Secrets in AWS | `config.secrets_backend: aws`, `serviceAccount.annotations` (IRSA role) |
| Several replicas | `replicaCount>1`, `persistence.enabled=false`, Postgres (`LIBRARIAN_DATABASE_URL`), `config.secrets_backend: aws` |
| Slack | `config.notifier.type: slack`, `config.notifier.slack.channel`, token via `envFrom` |
| Model for `fetch` | `config.model`, optionally `config.model_base_url` (LiteLLM), key via `envFrom` |
| Telemetry | `otel.enabled=true`, `otel.endpoint` |

### GitHub App for the registry repo

One GitHub App, owned by your organization, lets git-sync read the (private) registry repo and
lets librarian open review pull requests on it. Its tokens are minted per use, last under an
hour, and cover only that repo. Create it in one step:

```bash
# no Python environment needed: uvx runs the CLI from PyPI (or pip install librarian-a2a)
uvx --from librarian-a2a librarian github-app create --repo acme/api-registry \
  --key-file ~/.config/librarian/github-app.pem --init-repo
# opens GitHub: click "Create", then install it on acme/api-registry only. Saves the private
# key to --key-file (keep it outside any repo) and prints the values below. --init-repo gives an
# empty repo its first commit as the App (git-sync and pull requests need a branch).

kubectl -n librarian create secret generic librarian-github-app \
  --from-file=private-key.pem=$HOME/.config/librarian/github-app.pem
```

```yaml
registry:
  gitSync: {enabled: true, repo: https://github.com/acme/api-registry.git}
githubApp: {enabled: true, appId: "123456", installationId: "7890123", secretName: librarian-github-app}
```

The chart mounts the key into git-sync and librarian, and points librarian's review pull
requests at the same repo. `librarian github-app check --repo ... --app-id ... --key-file ...`
verifies the App can reach the repo. To keep the key in AWS Secrets Manager instead, sync it
into that Secret with ExternalSecrets.

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

## Tested

The chart is exercised on a local kind cluster with git-sync and oauth2-proxy enabled: helm test,
onboarding into the overlay (surviving pod replacement), A2A port refusing the pages even with
forged identity headers, proxy redirecting unauthenticated browsers to sign-in, and the pages
listener unreachable from other pods.
