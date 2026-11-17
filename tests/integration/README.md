# Integration tests

These run librarian against real services in Docker. They are skipped unless
`LIBRARIAN_INTEGRATION=1` is set.

| Stack | What it proves |
|---|---|
| `replicas/` | Two replicas on **Postgres** and **AWS Secrets Manager** (LocalStack) in front of go-httpbin: a credential requested on one replica is provided on the other (validated with a real call, stored in Secrets Manager, waiting task resumed); a write approved on the other replica using one key of a JSON secret; a plan of three writes asked for on one replica and approved on the other, each step taking what the one before it answered, and reading the same on both afterwards (A2A and MCP); approval links surviving a restart. |
| `litellm/` | librarian behind **LiteLLM v1.104.2** as an MCP server and an A2A agent: tools listed and called through the gateway (`librarian-*`), a write approved and finished via `write_status`, and A2A `SendMessage` + `GetTask` polling through the gateway (which forwards task calls in A2A 0.3 form). |
| `agentgateway/` | librarian behind **agentgateway v1.7.0** as an MCP server and an A2A agent, the two ways a gateway is used. With an agent's own key passed through: tools listed and called, a write and a plan approved and finished, the agent card rewritten to the gateway's address, a task followed through the gateway. With the gateway signing people in (**Keycloak**): an MCP client is told where to sign in, the gateway calls librarian with its own key and passes the person's token on, librarian verifies it itself and knows the person with proof; a rule in the gateway hides a tool; a made-up token is refused. And an answer too large for the gateway to pass on comes back as its outline. |
| `test_github.py` | The **GitHub publisher** against a real repo: an approved entry is written as a commit on `main` (through a pull request merged at once, where `main` only takes those), writing the same thing again commits nothing, a later decision is one more commit, the record reads back for a restore, and forgetting removes the file. Needs `LIBRARIAN_GITHUB_REPO=owner/repo` (a throwaway repo with `main`) and `GITHUB_TOKEN`. |
| `identity/` | **Keycloak** (real OIDC) + **oauth2-proxy** + librarian in one network namespace, like the Helm sidecar: verified principals from real tokens, tampered tokens refused, forged identity headers blocked, a real browser sign-in where only the requester can approve their write, and the credential page's approver-group check from real group claims. |
| `credentials/` | **Credentials made for each call**, against services that check them: **Keycloak** issues an OAuth client-credentials token and accepts it on its own userinfo endpoint (and a wrong client secret asks an approver for another); **MinIO** verifies AWS Signature Version 4 on every request and issues the short-lived credentials of an assumed role. Writes signed as the role, reads with librarian's own identity, object names and prefixes with spaces and commas, and a check that the server refuses a wrong signature. |
| `substrate/` | **Manual** (see its README): librarian on a kind cluster with **kagent 1.0.1 / Agent Substrate 1.0.0**. Agents with no key of their own call librarian's MCP tools through Substrate's egress gateway; librarian verifies the injected actor JWT, resolves the session to its kagent Agent and applies the rule for it. Covers learning and reading, a write approved by the rule's owner, a grant used from a second session of the same agent, and the refusals (no matching rule, wrong audience). |

```bash
# replicas
docker compose -p libint -f tests/integration/replicas/docker-compose.yaml up -d --build
LIBRARIAN_INTEGRATION=1 uv run pytest tests/integration/test_replicas.py -v
docker compose -p libint -f tests/integration/replicas/docker-compose.yaml down -v

# identity
docker compose -p libid -f tests/integration/identity/docker-compose.yaml up -d --build
LIBRARIAN_INTEGRATION=1 uv run pytest tests/integration/test_identity.py -v
docker compose -p libid -f tests/integration/identity/docker-compose.yaml down -v

# credentials
docker compose -p libcred -f tests/integration/credentials/docker-compose.yaml up -d --build
LIBRARIAN_INTEGRATION=1 uv run pytest tests/integration/test_credentials.py -v
docker compose -p libcred -f tests/integration/credentials/docker-compose.yaml down -v
```

```bash
# agentgateway
docker compose -p libagw -f tests/integration/agentgateway/docker-compose.yaml up -d --build
LIBRARIAN_INTEGRATION=1 uv run pytest tests/integration/test_agentgateway.py -v
docker compose -p libagw -f tests/integration/agentgateway/docker-compose.yaml down -v
```

```bash
# litellm
docker compose -p liblite -f tests/integration/litellm/docker-compose.yaml up -d --build
LIBRARIAN_INTEGRATION=1 uv run pytest tests/integration/test_litellm.py -v
docker compose -p liblite -f tests/integration/litellm/docker-compose.yaml down -v
```

Ports used on localhost: 14566, 18101, 18102 (replicas); 4180, 8180, 18201 (identity);
14000, 18301 (litellm); 8280, 18401, 19000 (credentials); 18600, 18601, 18602, 18680
(agentgateway).
