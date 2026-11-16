# Contributing

Thanks for helping. A few ground rules keep librarian safe to run in front of real credentials.

## Setup

```bash
uv sync
uv run pytest
```

Before opening a pull request:

```bash
uv run ruff check . && uv run ruff format .
uv run pyright        # strict mode must stay at 0 errors
uv run pytest
```

## Design rules (please don't break these)

1. **The policy engine stays deterministic.** No LLM call may decide whether a request runs.
2. **Secrets never leave the secret store** except as an outbound request header/query value.
   Never put them in messages, logs, audit events, exceptions or LLM context. Add a test.
3. **Untrusted input is validated with Pydantic at the boundary**: A2A messages, registry files,
   downloaded specs, config.
4. **Read-only by default.** Anything that could widen what counts as a "read" needs a human step.
5. **Every security behaviour has a test.**

## When an API surprises librarian

`tests/hostile.py` is a fake API that misbehaves in every way we know of: it repeats the key it
was sent, answers with broken or enormous JSON, redirects in circles, does not answer.
`tests/test_hostile_api.py` runs each behaviour through every way an agent reaches librarian
(the service, A2A, MCP), for a read and for an approved write, and checks the same three things
each time: the agent is told something it can act on, no credential leaves, and a write ends.

Found a new way an API can misbehave? Add a handler to `tests/hostile.py`; it then runs
everywhere. What only a real connection can do (trickling bytes, hanging up mid-answer) goes in
`tests/test_real_transport.py`. Set `LIBRARIAN_TEST_POSTGRES_URL` to an empty test database to
also check that such answers can be stored in Postgres (CI does).

## Registry entries

New example/community entries go in `examples/registry/` (one `<id>.yaml` per API) and must pass
`LIBRARIAN_REGISTRY_DIR=examples/registry uv run librarian registry validate`. Mark non-GET reads `read_basis: reviewed` only with a note
explaining why the operation cannot change data.

## Commit style

Small, focused commits with a clear message. Sign-off is welcome but not required.
