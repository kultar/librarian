"""Shaping API responses: dotted paths and field selection.

Paths are dot-separated keys. Lists are traversed implicitly, so
`issues.fields.summary` picks the summary of every issue. A trailing `[]` on a
segment (`issues[].key`) is accepted for readability and means the same.
"""

from __future__ import annotations

from typing import Any, cast

MAX_SELECT_PATHS = 50

Tree = dict[str, "Tree | None"]


def split_path(path: str) -> list[str]:
    return [part.removesuffix("[]") for part in path.strip().split(".") if part.removesuffix("[]")]


def get_path(data: Any, path: str) -> Any:
    """Follow a dotted path through dicts (lists are not traversed here)."""
    current = data
    for key in split_path(path):
        if not isinstance(current, dict):
            return None
        current = cast(dict[str, Any], current).get(key)
    return current


def set_path(data: Any, path: str, value: Any) -> Any:
    """Return a copy of `data` with `path` replaced by `value` (dicts only)."""
    keys = split_path(path)
    if not keys:
        return value
    if not isinstance(data, dict):
        return data
    copy = dict(cast(dict[str, Any], data))
    copy[keys[0]] = set_path(copy.get(keys[0]), ".".join(keys[1:]), value) if keys[1:] else value
    return copy


def _tree(paths: list[str]) -> Tree:
    root: Tree = {}
    for path in paths:
        node = root
        keys = split_path(path)
        for i, key in enumerate(keys):
            last = i == len(keys) - 1
            if last:
                node[key] = None  # select everything below
            else:
                child = node.get(key, {})
                if child is None:  # an ancestor is already fully selected
                    break
                node[key] = child
                node = child
    return root


def _apply(data: Any, tree: Tree | None) -> Any:
    if tree is None:
        return data
    if isinstance(data, list):
        return [_apply(item, tree) for item in cast(list[Any], data)]
    if isinstance(data, dict):
        source = cast(dict[str, Any], data)
        return {key: _apply(source[key], sub) for key, sub in tree.items() if key in source}
    return data


def select(data: Any, paths: list[str]) -> Any:
    """Keep only the selected paths. An empty selection returns the data unchanged."""
    if not paths:
        return data
    return _apply(data, _tree(paths))
