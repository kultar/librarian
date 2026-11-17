"""A request body sent as a form (`application/x-www-form-urlencoded`).

An agent gives a body as an object, whatever the API takes. Where the API's description
says it takes a form (Stripe's whole API, Twilio's), librarian writes the object as one:
a field is `name=value`, a field inside another goes in brackets (`metadata[order]=6735`),
and a list is written the way that API's description says: its values one after the
other under the same name (`tag=a&tag=b`), numbered in brackets (`items[0][price]=p1`),
or joined by commas (`tag=a,b`).
"""

from __future__ import annotations

from typing import Any, cast
from urllib.parse import urlencode

CONTENT_TYPE = "application/x-www-form-urlencoded"


def _value(value: Any) -> str:
    """A value as a form has it. Nothing is the empty value, which is how an API that
    takes forms is told to clear a field."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def pairs(body: dict[str, Any], lists: str = "repeated") -> list[tuple[str, str]]:
    """The fields of a form, in the order of the object: `[(name, value), ...]`."""
    found: list[tuple[str, str]] = []

    def put(name: str, value: Any) -> None:
        if isinstance(value, dict):
            inside = cast(dict[str, Any], value)
            if not inside:
                found.append((name, ""))
            for key, inner in inside.items():
                put(f"{name}[{key}]", inner)
        elif isinstance(value, list):
            items = cast(list[Any], value)
            plain = not any(isinstance(item, dict | list) for item in items)
            if not items:
                found.append((name, ""))
            elif plain and lists == "commas":
                found.append((name, ",".join(_value(item) for item in items)))
            elif plain and lists == "repeated":
                found.extend((name, _value(item)) for item in items)
            else:  # numbered: the only way to say which fields belong to which item
                for index, item in enumerate(items):
                    put(f"{name}[{index}]", item)
        else:
            found.append((name, _value(value)))

    for key, value in body.items():
        put(str(key), value)
    return found


def encoded(body: dict[str, Any], lists: str = "repeated") -> bytes:
    """The body as it goes over the wire."""
    return urlencode(pairs(body, lists)).encode("ascii")
