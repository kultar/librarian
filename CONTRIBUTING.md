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

## Registry entries

New example/community entries go in `examples/registry/` (one `<id>.yaml` per API) and must pass
`LIBRARIAN_REGISTRY_DIR=examples/registry uv run librarian registry validate`. Mark non-GET reads `read_basis: reviewed` only with a note
explaining why the operation cannot change data.

## Commit style

Small, focused commits with a clear message. Sign-off is welcome but not required.
