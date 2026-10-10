"""Recipes: successful plans replayed without the LLM.

After `fetch` answers "Is PAY-1 open?" with getIssue(issueIdOrKey=PAY-1), the plan
is saved as a template: the request becomes the pattern "is {p0} open?" and the
call becomes getIssue(issueIdOrKey={p0}). "Is PAY-7 open?" then matches and runs
the same call with PAY-7, deterministically and without a model call. Every
replayed call still goes through the policy engine with the caller's permissions.

A plan is only saved when it is safe to template:
- every argument either appears verbatim in the request (a parameter) or is a
  constant that did not come from an earlier call's result (results-derived values,
  like an ID looked up in step 1, would be wrong on replay);
- parameters are single words (no whitespace), so matching stays unambiguous.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from librarian.calls import ArgValue, CallRequest, CallResult

MAX_STEPS = 5
_PUNCT = "?.!"


class RecipeStep(BaseModel):
    api: str
    operation: str
    args: dict[str, ArgValue] = Field(default_factory=dict[str, ArgValue])
    select: list[str] = Field(default_factory=list[str])
    max_pages: int = 1


class Recipe(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    template: str  # the request with parameters as {p0}, {p1}, ...
    pattern: str  # regex built from the template
    params: list[str]
    steps: list[RecipeStep]
    example: str  # the request it was learned from
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    uses: int = 0

    def match(self, intent: str) -> dict[str, str] | None:
        found = re.fullmatch(self.pattern, _normalize(intent), flags=re.IGNORECASE)
        return found.groupdict() if found else None

    def calls(self, values: dict[str, str]) -> list[CallRequest]:
        requests: list[CallRequest] = []
        for step in self.steps:
            args: dict[str, ArgValue] = {}
            for name, value in step.args.items():
                if isinstance(value, str):
                    for param, actual in values.items():
                        value = value.replace("{" + param + "}", actual)
                args[name] = value
            requests.append(
                CallRequest(
                    api=step.api,
                    operation=step.operation,
                    args=args,
                    select=step.select,
                    max_pages=step.max_pages,
                )
            )
        return requests


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().rstrip(_PUNCT).strip()


def learn(intent: str, results: list[CallResult], requests: list[CallRequest]) -> Recipe | None:
    """Turn a successful plan into a recipe, or None if it is not safe to template."""
    if not requests or len(requests) > MAX_STEPS or len(results) != len(requests):
        return None
    if not all(r.ok for r in results):
        return None
    text = _normalize(intent)
    params: dict[str, str] = {}  # value -> param name
    steps: list[RecipeStep] = []
    earlier = ""
    for request, result in zip(requests, results, strict=True):
        args: dict[str, ArgValue] = {}
        for name, value in request.args.items():
            if isinstance(value, list):
                return None  # list arguments are not templated
            literal = str(value)
            in_request = re.search(rf"(?<![\w-]){re.escape(literal)}(?![\w-])", text, re.I)
            if in_request:
                if re.search(r"\s", literal):
                    return None  # multi-word values make matching ambiguous
                param = params.setdefault(literal, f"p{len(params)}")
                args[name] = "{" + param + "}"
            elif literal and earlier and literal in earlier:
                return None  # derived from an earlier result: would be wrong on replay
            else:
                args[name] = value
        steps.append(
            RecipeStep(
                api=request.api,
                operation=request.operation,
                args=args,
                select=list(request.select),
                max_pages=request.max_pages,
            )
        )
        earlier += json.dumps(result.data, default=str)
    if not params:
        return None  # a fixed question: nothing to generalise
    template = text
    for value, param in sorted(params.items(), key=lambda item: -len(item[0])):
        template = re.sub(
            rf"(?<![\w-]){re.escape(value)}(?![\w-])",
            lambda _match, p=param: "{" + p + "}",
            template,
            flags=re.I,
        )
    pattern = _pattern(template)
    return Recipe(
        template=template,
        pattern=pattern,
        params=sorted(params.values()),
        steps=steps,
        example=text,
    )


def _pattern(template: str) -> str:
    """Regex for a template: literal text escaped, {pN} as named single-word groups."""
    parts: list[str] = []
    seen: set[str] = set()
    for chunk in re.split(r"(\{p\d+\})", template):
        slot = re.fullmatch(r"\{(p\d+)\}", chunk)
        if slot:
            name = slot.group(1)
            parts.append(f"(?P={name})" if name in seen else f"(?P<{name}>\\S+)")
            seen.add(name)
        else:
            parts.append(r"\s+".join(re.escape(word) for word in chunk.split(" ")))
    return "".join(parts)


def recipe_summary(recipe: Recipe, results: list[CallResult]) -> str:
    ok = sum(1 for r in results if r.ok)
    return (
        f"Answered with saved recipe {recipe.id} ('{recipe.template}'): {ok}/{len(results)} "
        "call(s) succeeded. See results for the data."
    )


def dump(recipe: Recipe) -> dict[str, Any]:
    return recipe.model_dump(mode="json")
