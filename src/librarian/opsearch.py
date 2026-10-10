"""Ranking an API's operations for a request (BM25), so big APIs stay usable.

Jira's platform API has ~600 operations; the planner and `capabilities` need the
handful that match "add a comment to an issue", not the first 40 in file order.
Operation ids are split into words (getIssueComments -> get issue comments), and
ids and tags are weighted above summaries.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from librarian.registry.models import ApiEntry, Operation

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    [
        "a",
        "an",
        "the",
        "of",
        "to",
        "for",
        "in",
        "on",
        "by",
        "with",
        "and",
        "or",
        "is",
        "are",
        "be",
        "my",
        "me",
        "i",
        "get",
        "show",
        "find",
        "list",
        "all",
        "please",
        "what",
        "which",
        "how",
        "do",
        "does",
        "from",
        "at",
        "this",
        "that",
        "it",
    ]
)
K1, B = 1.4, 0.75


def tokens(text: str) -> list[str]:
    words = _WORD.findall(_CAMEL.sub(" ", text).lower())
    return [_stem(w) for w in words if w not in _STOP and not w.isdigit()]


def _stem(word: str) -> str:
    """A deliberately small stemmer: watching/watchers/watcher -> watch, issues -> issue."""
    if word.endswith("ss") or len(word) <= 4:
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"
    for suffix in ("ings", "ing", "ers", "er", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def _document(op: Operation) -> list[str]:
    path = re.sub(r"\{[^}]*\}", " ", op.path)  # drop {params}
    weighted = [op.id] * 3 + list(op.tags) * 2 + [path, op.summary]
    return tokens(" ".join(weighted))


@dataclass(frozen=True)
class _Index:
    docs: tuple[tuple[str, Counter[str], int], ...]  # (operation id, term counts, length)
    df: dict[str, int]
    avg_len: float


_CACHE: dict[tuple[str, str], _Index] = {}


def _index(entry: ApiEntry) -> _Index:
    key = (entry.id, entry.updated_at.isoformat())
    cached = _CACHE.get(key)
    if cached is None:
        if len(_CACHE) >= 64:
            _CACHE.clear()
        cached = _CACHE[key] = _build(entry.operations)
    return cached


def _build(operations: list[Operation]) -> _Index:
    docs: list[tuple[str, Counter[str], int]] = []
    df: Counter[str] = Counter()
    for op in operations:
        terms = _document(op)
        counts = Counter(terms)
        docs.append((op.id, counts, len(terms)))
        df.update(counts.keys())
    avg = sum(length for _, _, length in docs) / len(docs) if docs else 1.0
    return _Index(tuple(docs), dict(df), avg or 1.0)


def rank(
    entry: ApiEntry, query: str, operations: list[Operation], limit: int = 15
) -> list[Operation]:
    """The `operations` (a subset of the entry's) best matching `query`, best first."""
    terms = tokens(query)
    if not terms or not operations:
        return operations[:limit]
    index = _index(entry)
    allowed = {op.id: op for op in operations}
    n = len(index.docs)
    scored: list[tuple[float, str]] = []
    for op_id, counts, length in index.docs:
        if op_id not in allowed:
            continue
        score = 0.0
        for term in terms:
            tf = counts.get(term, 0)
            if not tf:
                continue
            df = index.df.get(term, 0)
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            score += idf * tf * (K1 + 1) / (tf + K1 * (1 - B + B * length / index.avg_len))
        if score > 0:
            scored.append((score, op_id))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [allowed[op_id] for _, op_id in scored[:limit]]
