"""What `capabilities` answers, in a size an agent can take.

`capabilities` is the first thing an agent asks, and whatever comes back goes into its
context whole. Most of an answer is its lists of operations, and they grow with every
API: a few large APIs made an answer that agent clients warn about, put aside in a
file, or refuse. So an answer has a size it stays under, and the lists are what is cut
to get there. Every API is still named, with how many operations it has (past a few
dozen APIs, the later ones by their id alone), and the answer says in `note` what it
left out and how to see it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pydantic_core

from librarian.registry.models import Body, BodyField, Operation

# How many operations a list holds: the best matches where the agent gave words, the
# first ones where it gave none.
MATCHES = 20
FIRST = 50
# What the lists are cut to, in turn, until the answer fits.
SHORTER = (30, 20, 10, 5, 3, 1, 0)

# How many fields of a body an operation names where it is listed; all of them are told
# when the agent asks about that operation.
BODY_NAMES = 10

Fits = Callable[[dict[str, Any]], bool]


def size(answer: dict[str, Any]) -> int:
    """An answer's size as JSON, in bytes."""
    return len(pydantic_core.to_json(answer, fallback=str))


@dataclass(frozen=True)
class Listed:
    """One API as `capabilities` lists it. Its operations are kept in the order they are
    listed, so that the lists can be cut without being worked out again."""

    id: str
    head: dict[str, Any]
    reads: list[dict[str, Any]]
    total_reads: int
    writes: list[dict[str, Any]] | None  # None: nothing this caller may ask to change
    total_writes: int
    tail: dict[str, Any]

    def told(self, most: int) -> dict[str, Any]:
        """The API with at most `most` operations in each list."""
        changes: dict[str, Any] = {}
        if self.writes is not None:
            changes = {
                "write_operations": self.writes[:most],
                "total_write_operations": self.total_writes,
            }
        return {
            **self.head,
            "read_operations": self.reads[:most],
            "total_read_operations": self.total_reads,
            **changes,
            **self.tail,
        }

    def longest(self) -> int:
        return max(len(self.reads), len(self.writes or []))


def shortened(apis: list[Listed], fits: Fits, *, searched: bool, one: bool) -> dict[str, Any]:
    """The answer: every API with its lists as they are where that fits, and otherwise
    the least shortened answer that does, with a `note`.

    What is cut, in this order. With words to match (`searched`), the lists of every API
    but the first: that one fits the words best and is most likely the one that was
    meant, so it keeps its matches. Then every list. Then, with no operations left to
    cut, the APIs past the first ones that fit are named by their id alone, in
    `more_apis`. `one`: the agent asked about one API.
    """
    whole = {"apis": [api.told(FIRST) for api in apis]}
    if not apis or fits(whole):
        return whole
    best = apis[0] if searched and len(apis) > 1 else None

    # A list is only cut to less than the longest one holds: anything else changes nothing.
    tries: list[tuple[Listed | None, int]] = []
    if best is not None:
        longest = max(api.longest() for api in apis[1:])
        tries += [(best, most) for most in SHORTER if most < longest]
    longest = max(api.longest() for api in apis)
    tries += [(None, most) for most in SHORTER if most < longest]
    for keep, most in tries:
        answer = {
            "apis": [api.told(FIRST if api is keep else most) for api in apis],
            "note": _note(most, keep, searched=searched, one=one),
        }
        if fits(answer):
            return answer

    def named(count: int) -> dict[str, Any]:
        return {
            "apis": [api.told(0) for api in apis[:count]],
            "more_apis": [api.id for api in apis[count:]],
            "note": (
                f"{SHORT}{count} of the {len(apis)} APIs are listed, without their "
                f"operations; more_apis has the ids of the others. {ONE_API}"
            ),
        }

    # Not even the APIs without their operations fit: as many of them as do (found by
    # halving), and the others by their id.
    low, high = 0, len(apis) - 1
    while low < high:
        middle = (low + high + 1) // 2
        if fits(named(middle)):
            low = middle
        else:
            high = middle - 1
    return named(low)


SHORT = "Shortened to keep this answer small: "
TOTALS = "total_read_operations and total_write_operations say how many there are"
ONE_API = (
    "Call capabilities with api (an id from this answer) to see one API's operations, and "
    "add a query, a few words for what you need, to get the ones that fit."
)


def _note(most: int, keep: Listed | None, *, searched: bool, one: bool) -> str:
    """What was left out, and how to see it."""
    if one:
        left = (
            f"each list of operations holds at most {most}"
            if most
            else "the operations are not listed"
        )
        how = (
            "Ask in other words to get others."
            if searched
            else "Give a query, a few words for what you need, to get the ones that fit."
        )
        return f"{SHORT}{left} ({TOTALS}). {how}"
    if keep is not None:
        others = (
            f"every other list of operations holds at most {most}"
            if most
            else "the other APIs are named without their operations"
        )
        return (
            f"{SHORT}the operations of {keep.id!r}, which fits your words best, are listed as "
            f"usual; {others} ({TOTALS}). {ONE_API}"
        )
    left = (
        f"each list of operations holds at most {most}"
        if most
        else "the APIs are named without their operations"
    )
    return f"{SHORT}{left} ({TOTALS}). {ONE_API}"


def _key(field: BodyField) -> str:
    """A field as an agent writes it: the keys that lead to it, with a dot between.
    Where one of them is a list, the field is in each of its items (as in `select`).
    The brackets an entry marks a list with are not part of any key, and an agent that
    is shown `expand[]` writes just that as the key."""
    return field.name.replace("[]", "")


def _kind(field: BodyField) -> str:
    return f"list of {field.type}" if field.name.endswith("[]") else field.type


def body_names(body: Body) -> dict[str, Any]:
    """A body in a list of operations: its first fields by name (`title`, `draft?` for
    one that may be left out, `author.name` for a field inside another), and how many
    more there are."""
    names = [_key(field) if field.required else f"{_key(field)}?" for field in body.fields]
    more = len(names) - BODY_NAMES + body.more
    return {"body": names[:BODY_NAMES], **({"body_more": more} if more > 0 else {})}


def described(
    api: str, op: Operation, *, enabled: bool | None, descriptions: int = 160
) -> dict[str, Any]:
    """One operation in full, for an agent about to call it: every argument with what it
    is, and what its body holds. `enabled`: for one that writes, whether anybody has
    allowed writes through it. `descriptions`: how much of each is kept."""

    def words(text: str) -> dict[str, str]:
        return {"description": text[:descriptions]} if text and descriptions else {}

    told: dict[str, Any] = {
        "api": api,
        "operation": op.id,
        "summary": op.summary[:500],
        "method": op.method,
        "path": op.path,
        "kind": "read" if enabled is None else "write",
        **({} if enabled is None else {"enabled": enabled}),
        "args": [
            {
                "name": p.name,
                "in": p.location.value,
                "type": p.type,
                "required": p.required,
                **words(p.description),
                **({"may_contain_slashes": True} if p.allow_slash else {}),
            }
            for p in op.parameters
        ],
    }
    if op.accepts_body:
        told["body"] = True  # it takes one; what it holds is not known
    if op.body is not None:
        told["body"] = {
            "fields": [
                {
                    "name": _key(field),
                    "type": _kind(field),
                    "required": field.required,
                    **({"values": field.values} if field.values else {}),
                    **words(field.description),
                }
                for field in op.body.fields
            ],
            **({"more": op.body.more} if op.body.more else {}),
            **_how_sent(op.body),
        }
    if op.warning:
        told["warning"] = op.warning
    return told


def _how_sent(body: Body) -> dict[str, str]:
    """How the body goes to the API, where that is not as JSON. The agent gives an
    object either way; this is so that it knows what the API will see."""
    if body.encoding == "form":
        return {"sent_as": "form"}
    if body.encoding == "multipart":
        return {
            "note": "The API's description says it takes this body as multipart form data "
            "(a file upload). librarian sends JSON, which the API may refuse."
        }
    return {}
