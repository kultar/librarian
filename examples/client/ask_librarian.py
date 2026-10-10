"""A minimal agent-side client for librarian, using the official A2A SDK.

    uv run python examples/client/ask_librarian.py card
    uv run python examples/client/ask_librarian.py capabilities
    uv run python examples/client/ask_librarian.py execute apisguru getMetrics
    uv run python examples/client/ask_librarian.py execute jira getIssue issueIdOrKey=PAY-1 \\
        --select key --select fields.summary
    uv run python examples/client/ask_librarian.py write jira addComment issueIdOrKey=PAY-1 \\
        --body '{"body": "Fixed in 2.3"}' --reason "close the loop"
    uv run python examples/client/ask_librarian.py ask "How many APIs does APIs.guru track?"

Environment: LIBRARIAN_URL (default http://127.0.0.1:8080), LIBRARIAN_KEY (this agent's bearer
key, if the server requires one), LIBRARIAN_PRINCIPAL (who the agent acts for, optional).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from typing import Any

import httpx
from a2a.client import ClientConfig, create_client
from a2a.helpers.proto_helpers import get_data_parts, get_message_text, new_data_part
from a2a.types import AgentCard, GetTaskRequest, Message, Role, SendMessageRequest, Task, TaskState
from google.protobuf.json_format import ParseDict
from google.protobuf.struct_pb2 import Struct

WAITING = {TaskState.TASK_STATE_AUTH_REQUIRED, TaskState.TASK_STATE_WORKING,
           TaskState.TASK_STATE_SUBMITTED}


def parse_args(pairs: list[str]) -> dict[str, str]:
    return dict(pair.split("=", 1) for pair in pairs)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["card", "capabilities", "execute", "write", "ask"])
    parser.add_argument("rest", nargs="*", help="api operation [name=value ...] or a question")
    parser.add_argument("--select", action="append", default=[])
    parser.add_argument("--body", help="JSON request body for writes")
    parser.add_argument("--reason", default="")
    parser.add_argument("--wait", type=float, default=300, help="seconds to wait for approval")
    opts = parser.parse_args()

    url = os.environ.get("LIBRARIAN_URL", "http://127.0.0.1:8080")
    headers = {"A2A-Version": "1.0"}
    if key := os.environ.get("LIBRARIAN_KEY"):
        headers["Authorization"] = f"Bearer {key}"

    async with httpx.AsyncClient(headers=headers, timeout=60) as http:
        if opts.command == "card":
            card = (await http.get(f"{url}/.well-known/agent-card.json")).json()
            print(json.dumps({"name": card["name"], "skills": [s["id"] for s in card["skills"]]}))
            return

        command: dict[str, Any]
        if opts.command == "capabilities":
            command = {"skill": "capabilities"}
        elif opts.command == "ask":
            command = {"skill": "fetch", "intent": " ".join(opts.rest)}
        else:
            api, operation, *pairs = opts.rest
            command = {"skill": opts.command, "api": api, "operation": operation,
                       "args": parse_args(pairs)}
            if opts.select:
                command["select"] = opts.select
            if opts.body:
                command["body"] = json.loads(opts.body)
            if opts.reason:
                command["reason"] = opts.reason

        # Use the card's skills and settings, but connect to the URL we were given: the
        # card advertises the in-cluster address, which a port-forward can't reach.
        card = ParseDict(
            (await http.get(f"{url}/.well-known/agent-card.json")).json(),
            AgentCard(),
            ignore_unknown_fields=True,
        )
        for interface in card.supported_interfaces:
            interface.url = url.rstrip("/") + "/"
        client = await create_client(
            card, client_config=ClientConfig(httpx_client=http, streaming=False)
        )
        request = SendMessageRequest(
            message=Message(role=Role.ROLE_USER, message_id=uuid.uuid4().hex,
                            parts=[new_data_part(command)])
        )
        if principal := os.environ.get("LIBRARIAN_PRINCIPAL"):
            request.metadata.CopyFrom(ParseDict({"principal": principal}, Struct()))

        task: Task | None = None
        async for response in client.send_message(request):
            if response.HasField("task"):
                task = response.task
        if task is None:
            raise SystemExit("no task returned")

        # Writes (and missing credentials) park the task until a human acts: poll for it.
        waited = 0.0
        while task.status.state in WAITING and waited < opts.wait:
            if task.status.HasField("message"):
                print("…", get_message_text(task.status.message))
            await asyncio.sleep(3)
            waited += 3
            task = await client.get_task(GetTaskRequest(id=task.id))

        state = TaskState.Name(task.status.state)
        results = [d for a in task.artifacts for d in get_data_parts(a.parts)]
        if results:
            print(json.dumps(results[-1], indent=2))
        else:
            message = get_message_text(task.status.message) if task.status.HasField(
                "message") else ""
            print(f"{state}: {message}")


if __name__ == "__main__":
    asyncio.run(main())
