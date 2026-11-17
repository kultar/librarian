"""Plans: several writes on one API, approved as one and run in order.

Some changes are one decision made of several requests. A commit on GitHub is a blob, a
tree, a commit and a branch, and then a pull request. Asked for one by one, each needs
its own approval (or a standing grant wide enough to cover them all), and the approver
sees each without the others. A plan puts them in front of the approver together: every
request, exactly as it will be sent, in the order it will be sent.

Later steps need what earlier ones returned (the tree needs the blob's id). In a step's
arguments or body, `{"$from": "blob.sha"}` stands for "the value at `sha` in what step
`blob` answered". The approver sees the reference; librarian fills it in when the plan
runs, and checks the step again, with the real value, before sending it.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Annotated, Any, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    ValidationError,
    field_validator,
    model_validator,
)

from librarian.calls import ArgValue, Caller, WriteRequest
from librarian.errors import LibrarianError

REF_KEY = "$from"
STEP_ID = r"^[a-z][a-z0-9_-]{0,31}$"
_SOURCE = re.compile(r"^([a-z][a-z0-9_-]{0,31})((?:\.[A-Za-z0-9_-]{1,64}){1,8})$")
MAX_STEPS = 20


class PlanError(Exception):
    """A step could not be prepared from what the steps before it answered."""


class Ref(BaseModel):
    """ "The value an earlier step returned": `<step>.<path>`, e.g. `blob.sha` or
    `commit.tree.sha`. A number in the path picks from a list (`files.0.sha`)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    source: str = Field(alias=REF_KEY, max_length=600)

    @field_validator("source")
    @classmethod
    def _well_formed(cls, source: str) -> str:
        if not _SOURCE.match(source):
            raise ValueError(f"{source!r} is not a reference: write <step>.<field>, e.g. blob.sha")
        return source

    @property
    def step(self) -> str:
        return self.source.split(".", 1)[0]

    @property
    def path(self) -> list[str]:
        return self.source.split(".")[1:]


def _kind(value: Any) -> str:
    return "ref" if isinstance(value, dict | Ref) else "value"


# An argument of a step: a value, or a reference to what an earlier step answered. Told
# apart by shape, so that what is wrong with one is said about that one.
PlanArg = Annotated[
    Annotated[ArgValue, Tag("value")] | Annotated[Ref, Tag("ref")], Discriminator(_kind)
]


def _refs_in(body: Any) -> list[Ref]:
    """The references in a request body: every object of the form {"$from": "..."}."""
    found: list[Ref] = []
    todo: list[Any] = [body]
    while todo:
        node = todo.pop()
        if isinstance(node, dict):
            items = cast(dict[str, Any], node)
            if set(items) == {REF_KEY}:
                found.append(Ref.model_validate(items))
            else:
                todo += list(items.values())
        elif isinstance(node, list):
            todo += cast(list[Any], node)
    return found


class PlanStep(BaseModel):
    """One write of a plan. `id` is the name later steps refer to its answer by."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=STEP_ID)
    operation: str = Field(min_length=1, max_length=128)
    args: dict[str, PlanArg] = Field(default_factory=dict[str, PlanArg])
    body: dict[str, Any] | list[Any] | None = None

    def references(self) -> list[Ref]:
        in_args = [value for value in self.args.values() if isinstance(value, Ref)]
        return in_args + _refs_in(self.body)

    @property
    def unresolved(self) -> frozenset[str]:
        """The arguments whose value is only known once the plan runs."""
        return frozenset(name for name, value in self.args.items() if isinstance(value, Ref))

    @property
    def given(self) -> dict[str, ArgValue]:
        """The arguments whose value is in the plan itself."""
        return {name: value for name, value in self.args.items() if not isinstance(value, Ref)}

    def shown(self) -> dict[str, Any]:
        """The step as the agent wrote it, for a person (or the agent) to read."""
        return self.model_dump(
            mode="json", by_alias=True, include={"id", "operation", "args", "body"}
        )


class PlannedStep(PlanStep):
    """A step as it is kept with the approval: with what the operation is, so the
    approver sees the request without looking anything up."""

    method: str = ""
    path: str = ""
    summary: str = ""


class PlanRequest(BaseModel):
    """Several writes on one API, to be approved as one and run in order."""

    model_config = ConfigDict(extra="forbid")

    api: str = Field(min_length=1, max_length=64)
    steps: list[PlanStep] = Field(min_length=1, max_length=MAX_STEPS)
    reason: str = Field(default="", max_length=2000)  # shown to the approver
    # A second plan with the same key returns the first one's outcome instead of running.
    idempotency_key: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _steps_follow(self) -> PlanRequest:
        before: set[str] = set()
        for step in self.steps:
            if step.id in before:
                raise ValueError(f"two steps are called {step.id!r}")
            for ref in step.references():
                if ref.step not in before:
                    raise ValueError(
                        f"step {step.id!r} refers to {ref.source!r}, and no step before it "
                        f"is called {ref.step!r}"
                    )
            before.add(step.id)
        return self

    def fingerprint(self, caller: Caller) -> str:
        """Identity of exactly this plan by this caller (binds the approval to it)."""
        payload = self.model_dump_json(by_alias=True, exclude={"reason", "idempotency_key"})
        return hashlib.sha256(
            f"plan|{caller.agent}|{caller.principal}|{payload}".encode()
        ).hexdigest()


def parse(data: Mapping[str, Any]) -> PlanRequest:
    """A plan as an agent sent it. What is wrong with it is said in one line, with where."""
    try:
        return PlanRequest.model_validate(data)
    except ValidationError as exc:
        first = exc.errors(include_url=False, include_context=False, include_input=False)[0]
        # Which of the two shapes an argument has is ours to know, not the agent's.
        where = ".".join(str(part) for part in first["loc"] if part not in ("value", "ref"))
        said = first["msg"].removeprefix("Value error, ")
        raise LibrarianError(f"invalid plan: {where + ': ' if where else ''}{said}") from None


def step_key(plan_key: str | None, step_id: str) -> str | None:
    """The idempotency key a step is sent with: the plan's own, and which step. Never the
    same for two steps of a plan, however long the plan's key is: an API that honours
    the key would answer the second with what it answered the first."""
    if not plan_key:
        return None
    key = f"{plan_key}:{step_id}"
    if len(key) <= 128:
        return key
    return hashlib.sha256(f"{plan_key}\0{step_id}".encode()).hexdigest()


def _lookup(ref: Ref, answers: Mapping[str, Any]) -> Any:
    if ref.step not in answers:
        raise PlanError(f"step {ref.step!r} gave no answer to take {ref.source!r} from")
    value: Any = answers[ref.step]
    for key in ref.path:
        if isinstance(value, dict) and key in value:
            value = cast(dict[str, Any], value)[key]
        elif isinstance(value, list) and key.isdigit() and int(key) < len(cast(list[Any], value)):
            value = cast(list[Any], value)[int(key)]
        else:
            raise PlanError(
                f"what step {ref.step!r} answered has no {'.'.join(ref.path)!r} "
                f"(needed for {ref.source!r})"
            )
    return value


def _fill(body: Any, answers: Mapping[str, Any]) -> Any:
    if isinstance(body, dict):
        items = cast(dict[str, Any], body)
        if set(items) == {REF_KEY}:
            return _lookup(Ref.model_validate(items), answers)
        return {key: _fill(value, answers) for key, value in items.items()}
    if isinstance(body, list):
        return [_fill(value, answers) for value in cast(list[Any], body)]
    return body


def _as_arg(found: Any) -> ArgValue | None:
    """What an earlier step answered, as an argument; None if it cannot be one."""
    if isinstance(found, bool | int | float | str):
        return found
    if isinstance(found, list):
        items = cast(list[Any], found)
        if all(isinstance(item, str) for item in items):
            return cast(list[str], items)
        if all(isinstance(item, int) and not isinstance(item, bool) for item in items):
            return cast(list[int], items)
    return None


def _called(found: Any) -> str:
    if found is None:
        return "nothing"
    for kind, name in ((dict, "an object"), (list, "a list"), (str, "a string")):
        if isinstance(found, kind):
            return name
    return "true or false" if isinstance(found, bool) else "a number"


def resolve(
    step: PlanStep, api: str, answers: Mapping[str, Any], idempotency_key: str | None = None
) -> WriteRequest:
    """The write this step is, now that the steps before it have answered."""
    args: dict[str, ArgValue] = {}
    for name, value in step.args.items():
        if not isinstance(value, Ref):
            args[name] = value
            continue
        found = _lookup(value, answers)
        arg = _as_arg(found)
        if arg is None:
            raise PlanError(
                f"the argument {name!r} needs a value, and {value.source!r} is {_called(found)}"
            )
        args[name] = arg
    body = _fill(step.body, answers)
    if body is not None and not isinstance(body, dict | list):
        # The whole body was a reference, and what it points at is one value.
        raise PlanError(f"the request body has to be an object or a list, not {_called(body)}")
    return WriteRequest(
        api=api,
        operation=step.operation,
        args=args,
        body=cast("dict[str, Any] | list[Any] | None", body),
        idempotency_key=idempotency_key,
    )
