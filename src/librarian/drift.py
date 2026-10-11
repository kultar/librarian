"""Noticing that an API changed: compare an entry with a fresh copy learned from the
same source, sort the differences by risk, and build what a reviewer should see.

- additive  new operations or optional parameters: nothing breaks
- breaking  removed operations, new required parameters, moved paths: calls will fail
- safety    an operation's HTTP method changed: what was a read may now change data

Verified entries are never changed here. The service suspends risky operations
(observations) and proposes the update for review; see `Librarian.recheck`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel

from librarian.registry.models import ApiEntry, Operation

Severity = Literal["additive", "breaking", "safety"]
_RANK: dict[str, int] = {"additive": 0, "breaking": 1, "safety": 2}


class Change(BaseModel):
    kind: Literal["added", "removed", "changed"]
    operation: str  # id in the current entry (or the new one, for added)
    new_operation: str | None = None  # id in the fresh entry, when matched
    severity: Severity
    detail: str


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
    return found


def worst(changes: list[Change]) -> Severity | None:
    if not changes:
        return None
    return max((c.severity for c in changes), key=lambda s: _RANK[s])


def proposed_update(current: ApiEntry, fresh: ApiEntry, changes: list[Change]) -> ApiEntry:
    """What a reviewer is asked to merge for a verified entry.

    Reviewed decisions are kept wherever the operation is still the same request:
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


def review_body(entry: ApiEntry, changes: list[Change], actions: list[str]) -> str:
    rows = sorted(changes, key=lambda c: -_RANK[c.severity])
    lines = [
        f"librarian noticed that **{entry.name}** (`{entry.id}`) changed at its source "
        f"({entry.source.url or entry.source.kind}).",
        "",
        "| Severity | Change |",
        "|---|---|",
        *[f"| {c.severity} | {c.detail} |" for c in rows[:100]],
    ]
    if len(rows) > 100:
        lines.append(f"| | ... and {len(rows) - 100} more |")
    if actions:
        lines += ["", "### Already done (until this is reviewed)", *[f"- {a}" for a in actions]]
    lines += [
        "",
        "### Review checklist",
        "- [ ] Operations whose method changed are still classified correctly",
        "- [ ] Write enablement still makes sense for changed operations",
        "- [ ] Delete operations marked `suspended` if the API really removed them",
        "",
        "Merging this clears the suspensions for the operations it changes.",
        "",
        "_Opened automatically by librarian._",
    ]
    return "\n".join(lines)
