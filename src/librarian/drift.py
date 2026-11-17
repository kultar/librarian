"""Noticing that an API changed: compare an entry with a fresh copy learned from the
same source, sort the differences by risk, and build what a reviewer should see.

- additive  new operations or optional parameters: nothing breaks
- breaking  removed operations, new required parameters, moved paths: calls will fail
- safety    an operation's HTTP method changed: what was a read may now change data

What a request body holds is told to agents, not enforced, so a change in it is listed
(as additive) and holds no operation back, whatever it is: the API refuses a body it no
longer takes, in its own words.

Approved entries are never changed here. The service suspends risky operations
(observations) and asks an approver to approve the update; see `Librarian.recheck`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel

from librarian.registry.models import ApiEntry, Body, Operation

Severity = Literal["additive", "breaking", "safety"]
# What is said of an operation whose body was not described before and now is. Many
# operations get it at once (an API learned before bodies were read), so it is told once.
DESCRIBED = "its request body is now described"
_RANK: dict[str, int] = {"additive": 0, "breaking": 1, "safety": 2}


class Change(BaseModel):
    kind: Literal["added", "removed", "changed"]
    operation: str  # id in the current entry (or the new one, for added)
    new_operation: str | None = None  # id in the fresh entry, when matched
    severity: Severity
    detail: str
    # It is about what the operation's request body holds, and about nothing else.
    body: bool = False


def diff(current: ApiEntry, fresh: ApiEntry) -> list[Change]:
    """Differences between the current entry and a freshly learned one."""
    changes: list[Change] = []
    fresh_by_id = {op.id: op for op in fresh.operations}
    fresh_by_route = {(op.method, op.path): op for op in fresh.operations}
    matched_fresh: set[str] = set()
    for old in current.operations:
        new = fresh_by_id.get(old.id)
        if new is None or new.id in matched_fresh:
            # Same route under a different id (ids are often generated): same operation.
            new = fresh_by_route.get((old.method, old.path))
        if new is None or new.id in matched_fresh:
            changes.append(
                Change(
                    kind="removed",
                    operation=old.id,
                    severity="breaking",
                    detail=f"{old.method} {old.path} is no longer in the source",
                )
            )
            continue
        matched_fresh.add(new.id)
        changes.extend(_compare(old, new))
    for new in fresh.operations:
        if new.id not in matched_fresh:
            changes.append(
                Change(
                    kind="added",
                    operation=new.id,
                    new_operation=new.id,
                    severity="additive",
                    detail=f"new operation {new.method} {new.path} ({new.access.value})",
                )
            )
    return changes


def _compare(old: Operation, new: Operation) -> list[Change]:
    def change(severity: Severity, detail: str) -> Change:
        return Change(
            kind="changed",
            operation=old.id,
            new_operation=new.id,
            severity=severity,
            detail=f"{old.id}: {detail}",
        )

    found: list[Change] = []
    if old.method != new.method:
        found.append(change("safety", f"method changed {old.method} -> {new.method} on {new.path}"))
    if old.path != new.path:
        found.append(change("breaking", f"path changed {old.path} -> {new.path}"))
    before = {(p.location, p.name): p for p in old.parameters}
    after = {(p.location, p.name): p for p in new.parameters}
    for key, param in after.items():
        previous = before.get(key)
        if previous is None:
            if param.required:
                found.append(
                    change("breaking", f"new required {key[0].value} parameter {key[1]!r}")
                )
            else:
                found.append(change("additive", f"new optional parameter {key[1]!r}"))
        elif param.required and not previous.required:
            found.append(change("breaking", f"parameter {key[1]!r} is now required"))
    for key in before.keys() - after.keys():
        found.append(change("breaking", f"{key[0].value} parameter {key[1]!r} was removed"))
    body = _body_change(old.body, new.body)
    if body:
        found.append(change("additive", body).model_copy(update={"body": True}))
    return found


def _body_change(old: Body | None, new: Body | None) -> str | None:
    """What changed in what a request body holds, in a sentence."""
    if old is None or new is None:
        if old is None and new is not None:
            return DESCRIBED
        return "its request body is no longer described" if old is not None else None
    before = {field.name: field for field in old.fields}
    after = {field.name: field for field in new.fields}
    parts = [
        (
            "now needs",
            [
                n
                for n, f in after.items()
                if f.required and not (n in before and before[n].required)
            ],
        ),
        ("now also takes", [n for n, f in after.items() if n not in before and not f.required]),
        ("no longer takes", [n for n in before if n not in after]),
    ]
    said = [f"{what} {_some(names)}" for what, names in parts if names]
    return f"its request body {'; '.join(said)}" if said else None


def _some(names: list[str], most: int = 5) -> str:
    listed = ", ".join(repr(name) for name in names[:most])
    return f"{listed} and {len(names) - most} more" if len(names) > most else listed


def told(changes: list[Change]) -> list[str]:
    """The changes as a reviewer reads them, the gravest first. The operations whose
    bodies are described for the first time are one line, however many there are."""
    described = [c for c in changes if c.body and c.detail.endswith(DESCRIBED)]
    others = [c for c in changes if c not in described]
    ranked = sorted(others, key=lambda c: -_RANK[c.severity])
    lines = [f"[{c.severity}] {c.detail}" for c in ranked]
    if len(described) > 3:
        lines.append(
            f"[additive] the request bodies of {len(described)} operations are now described"
        )
    else:
        lines += [f"[{c.severity}] {c.detail}" for c in described]
    return lines


def only_bodies(changes: list[Change]) -> bool:
    """Whether all that changed is what request bodies hold: nothing about how any
    operation is called, so nothing a person decided is in question."""
    return bool(changes) and all(change.body for change in changes)


def worst(changes: list[Change]) -> Severity | None:
    if not changes:
        return None
    return max((c.severity for c in changes), key=lambda s: _RANK[s])


def proposed_update(current: ApiEntry, fresh: ApiEntry, changes: list[Change]) -> ApiEntry:
    """What an approver is asked to approve for a verified entry.

    Decisions people made are kept wherever the operation is still the same request:
    write enablement, reviewed reads, pagination. Removed operations are kept but
    suspended, so references to them (discovery, credential tests) stay valid and the
    reviewer decides. New operations arrive disabled for writes.
    """
    fresh_ops = {op.id: op for op in fresh.operations}
    by_operation: dict[str, list[Change]] = {}
    for item in changes:
        by_operation.setdefault(item.operation, []).append(item)
    today = datetime.now(UTC).date().isoformat()
    operations: list[Operation] = []
    for old in current.operations:
        found = by_operation.get(old.id, [])
        if any(c.kind == "removed" for c in found):
            operations.append(
                old.model_copy(
                    update={
                        "suspended": f"no longer in the API's spec/docs as of {today}; "
                        "delete this operation if the API removed it",
                        "writable": False,
                    }
                )
            )
            continue
        target = next((c.new_operation for c in found if c.new_operation), None)
        if target is None or target not in fresh_ops:
            operations.append(old)
            continue
        new = fresh_ops[target]
        if only_bodies(found):
            # The operation is what it was: only what agents are told of its body is new.
            # Everything decided about it stays as it is (how it is approved, slashes
            # in its path, what was seen of it), which rebuilding it would put back.
            operations.append(
                old.model_copy(update={"body": new.body if old.accepts_body else None})
            )
            continue
        same_request = new.method == old.method
        keep: dict[str, object] = {"id": old.id, "pagination": old.pagination}
        if same_request:
            keep |= {
                "access": old.access,
                "read_basis": old.read_basis,
                "accepts_body": old.accepts_body,
                "writable": old.writable,
                "rememberable": old.rememberable,
            }
            if not old.accepts_body:
                keep["body"] = None  # no body is taken here, whatever the source describes
        operations.append(
            Operation.model_validate(
                {**new.model_dump(mode="json", by_alias=True), **_dumpable(keep)}
            )
        )
    for item in changes:
        if item.kind == "added" and item.new_operation in fresh_ops:
            operations.append(fresh_ops[item.new_operation].model_copy(update={"writable": False}))
    return current.model_copy(
        update={
            "operations": operations,
            "source": fresh.source,
            "updated_at": datetime.now(UTC),
        }
    )


def _dumpable(values: dict[str, object]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in values.items():
        out[key] = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return out
