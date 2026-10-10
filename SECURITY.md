# Security policy

librarian holds API credentials and brokers access for other agents, so security reports are
very welcome.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's private vulnerability reporting
("Security" tab → "Report a vulnerability") on this repository. Include:

- what an attacker can do, and from which position (calling agent, API response, registry entry, ...)
- steps or a minimal proof of concept
- affected version/commit

You should get an acknowledgement within a week. Fixes are released as soon as practical, and
reporters are credited unless they prefer otherwise.

## Scope

See [docs/threat-model.md](docs/threat-model.md) for what the project defends against and what is
explicitly out of scope. Reports that break any mitigation listed there are in scope, especially:

- any way for a caller to perform a write, reach an unregistered host, or read a secret value
- secrets appearing in messages, logs, audit events, provenance or LLM context
- SSRF past the private-network guard
- approval-link forgery or reuse

## Supported versions

Only the latest release receives security fixes while the project is pre-1.0.
