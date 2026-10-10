# Integration tests

These run librarian against real services in Docker. They are skipped unless
`LIBRARIAN_INTEGRATION=1` is set.

| Stack | What it proves |
|---|---|
| `replicas/` | Two replicas on **Postgres** and **AWS Secrets Manager** (LocalStack) in front of go-httpbin: a credential requested on one replica is provided on the other (validated with a real call, stored in Secrets Manager, waiting task resumed); a write approved on the other replica using one key of a JSON secret; approval links surviving a restart. |
| `test_github.py` | The **GitHub publisher** against a real repo: a learned API becomes a pull request with the review checklist and a valid provisional entry; the PR and branch are cleaned up afterwards. Needs `LIBRARIAN_GITHUB_REPO=owner/repo` (a throwaway repo with `main`) and `GITHUB_TOKEN`. |
| `identity/` | **Keycloak** (real OIDC) + **oauth2-proxy** + librarian in one network namespace, like the Helm sidecar: verified principals from real tokens, tampered tokens refused, forged identity headers blocked, a real browser sign-in where only the requester can approve their write, and the credential page's approver-group check from real group claims. |

```bash
# replicas
docker compose -p libint -f tests/integration/replicas/docker-compose.yaml up -d --build
LIBRARIAN_INTEGRATION=1 uv run pytest tests/integration/test_replicas.py -v
docker compose -p libint -f tests/integration/replicas/docker-compose.yaml down -v

# identity
docker compose -p libid -f tests/integration/identity/docker-compose.yaml up -d --build
LIBRARIAN_INTEGRATION=1 uv run pytest tests/integration/test_identity.py -v
docker compose -p libid -f tests/integration/identity/docker-compose.yaml down -v
```

Ports used on localhost: 14566, 18101, 18102 (replicas); 4180, 8180, 18201 (identity).
