# Examples

Each of these runs as it stands. The first three are walkthroughs with the expected output.

| | What it is | Takes |
|---|---|---|
| [`quickstart`](quickstart) | librarian and a demo API on your laptop: an API learned on demand, a key asked for on a page, a write held for approval. The walkthrough is [getting started](../docs/getting-started.md) | 10 minutes, Docker |
| [`agentgateway`](agentgateway) | librarian behind [agentgateway](https://agentgateway.dev): agents use the gateway's address for MCP and A2A, and people use librarian's pages | 5 minutes, Docker |
| [`kagent`](kagent) | librarian beside [kagent](https://kagent.dev) on Agent Substrate: an agent with no credentials reads from an API and asks for a change that you approve | 10 minutes, a kind cluster or your own |
| [`compose`](compose) | librarian planning plain-language requests through LiteLLM as its model gateway | Docker and a model key |
| [`client`](client/ask_librarian.py) | An agent's side in Python, with the official A2A SDK: the card, reads, a write, and a task that waits for approval | |
| [`librarian.yaml`](librarian.yaml) | Every setting, with comments | |
| [`registry/jira.yaml`](registry/jira.yaml) | An API's entry written by hand ([the format](../docs/registry.md)) | |

For the stack your agents already run on, what you add and how each is tested:
[In the stack you already run](../README.md#in-the-stack-you-already-run).
