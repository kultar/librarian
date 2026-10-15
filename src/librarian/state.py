"""Durable state: credential requests survive restarts and are shared by replicas.

Uses SQLAlchemy async so the same code runs on SQLite (single instance) and
Postgres (several replicas). A2A tasks are stored by the SDK's own
`DatabaseTaskStore` on the same engine.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    delete,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from librarian.audit import AuditEvent
from librarian.credentials import CredentialRequest, RequestState

metadata = MetaData()

credential_requests = Table(
    "librarian_credential_requests",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("api", String(64), nullable=False, index=True),
    Column("state", String(16), nullable=False, index=True),
    Column("data", Text, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


rate_limits = Table(
    "rate_limits",
    metadata,
    Column("key", String(200), primary_key=True),
    Column("window", Integer, nullable=False),
    Column("count", Integer, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

audit_events = Table(
    "audit_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime(timezone=True), nullable=False, index=True),
    Column("event", String(32), nullable=False, index=True),
    Column("caller", String(128)),
    Column("principal", String(200)),
    Column("api", String(64), index=True),
    Column("operation", String(128)),
    Column("status_code", Integer),
    Column("bytes", Integer),
    Column("duration_ms", Integer),
    Column("detail", Text),
    Column("extra", Text),
)

documents = Table(
    "librarian_documents",
    metadata,
    Column("kind", String(32), primary_key=True),
    Column("id", String(64), primary_key=True),
    Column("state", String(16), nullable=False, index=True),
    Column("data", Text, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


def normalize_database_url(url: str) -> str:
    """asyncpg takes `ssl=require`, not psql's `sslmode=require`; accept both."""
    if "+asyncpg" in url.split("://", 1)[0] and "sslmode=" in url:
        return url.replace("?sslmode=", "?ssl=").replace("&sslmode=", "&ssl=")
    return url


def make_engine(url: str) -> AsyncEngine:
    """Create an async engine; for SQLite, make sure the directory exists."""
    url = normalize_database_url(url)
    if url.startswith("sqlite"):
        from pathlib import Path

        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        return create_async_engine(url)
    # Test each pooled connection before use and replace old ones, so a database
    # failover (or an idle timeout) costs a reconnect, not a failed request.
    return create_async_engine(url, pool_pre_ping=True, pool_recycle=1800)


async def retry_already_exists(setup: Callable[[], Awaitable[None]]) -> None:
    """Run a schema setup step, tolerating another replica doing it at the same moment.

    `create_all` checks before creating, so two replicas starting together can both
    decide to create a table; the loser gets "already exists". Retrying succeeds
    because the check then sees the table.
    """
    for attempt in range(5):
        try:
            await setup()
            return
        except DBAPIError as exc:
            if "already exists" not in str(exc).lower() or attempt == 4:
                raise
            await asyncio.sleep(0.2 * (attempt + 1))


class StateStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def initialize(self) -> None:
        async def create() -> None:
            async with self.engine.begin() as conn:
                await conn.run_sync(metadata.create_all)

        await retry_already_exists(create)

    async def save_request(self, request: CredentialRequest) -> None:
        row = {
            "id": request.id,
            "api": request.api,
            "state": request.state.value,
            "data": request.model_dump_json(),
            "updated_at": datetime.now(UTC),
        }
        async with self.engine.begin() as conn:
            await conn.execute(
                delete(credential_requests).where(credential_requests.c.id == request.id)
            )
            await conn.execute(insert(credential_requests).values(**row))

    async def update_request_if_pending(self, request: CredentialRequest) -> bool:
        """Save a request only if it is still pending in the store (no lost resolutions)."""
        async with self.engine.begin() as conn:
            result = await conn.execute(
                update(credential_requests)
                .where(credential_requests.c.id == request.id)
                .where(credential_requests.c.state == RequestState.PENDING.value)
                .values(data=request.model_dump_json(), updated_at=datetime.now(UTC))
            )
            return result.rowcount == 1

    async def get_request(self, request_id: str) -> CredentialRequest | None:
        async with self.engine.connect() as conn:
            result = await conn.execute(
                select(credential_requests.c.data).where(credential_requests.c.id == request_id)
            )
            data = result.scalar_one_or_none()
        return None if data is None else CredentialRequest.model_validate_json(data)

    async def pending_for(self, api: str, purpose: str = "read") -> CredentialRequest | None:
        async with self.engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(credential_requests.c.data)
                    .where(credential_requests.c.api == api)
                    .where(credential_requests.c.state == RequestState.PENDING.value)
                )
            ).scalars()
            requests = [CredentialRequest.model_validate_json(row) for row in rows]
        return next((r for r in requests if r.purpose == purpose), None)

    async def list_requests(self, state: RequestState | None = None) -> list[CredentialRequest]:
        query = select(credential_requests.c.data)
        if state is not None:
            query = query.where(credential_requests.c.state == state.value)
        async with self.engine.connect() as conn:
            rows = (await conn.execute(query)).scalars().all()
        return [CredentialRequest.model_validate_json(row) for row in rows]

    # -- generic documents (write approvals, grants) ---------------------------------

    # -- audit --------------------------------------------------------------------

    async def record_audit(self, event: AuditEvent) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                insert(audit_events).values(
                    ts=event.ts,
                    event=event.event[:32],
                    caller=(event.caller or "")[:128] or None,
                    principal=(event.principal or "")[:200] or None,
                    api=(event.api or "")[:64] or None,
                    operation=(event.operation or "")[:128] or None,
                    status_code=event.status_code,
                    bytes=event.bytes,
                    duration_ms=event.duration_ms,
                    detail=event.detail,
                    extra=json.dumps(event.extra, default=str) if event.extra else None,
                )
            )

    async def recent_audit(self, api: str | None = None, limit: int = 50) -> list[AuditEvent]:
        query = (
            select(audit_events)
            .order_by(audit_events.c.ts.desc(), audit_events.c.id.desc())
            .limit(limit)
        )
        if api is not None:
            query = query.where(audit_events.c.api == api)
        async with self.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        events: list[AuditEvent] = []
        for row in rows:
            data = dict(row)
            data.pop("id", None)
            raw = data.pop("extra", None)
            data["extra"] = json.loads(raw) if raw else {}
            ts = data["ts"]
            if ts.tzinfo is None:  # SQLite hands naive datetimes back
                data["ts"] = ts.replace(tzinfo=UTC)
            events.append(AuditEvent.model_validate(data))
        return events

    async def audit_summary(self, since: datetime) -> dict[str, Any]:
        """Counts for the console: calls, denials, bytes, and plain-language requests
        answered by the planner versus replayed from a recipe."""
        async with self.engine.connect() as conn:
            counts = (
                await conn.execute(
                    select(audit_events.c.event, func.count(), func.sum(audit_events.c.bytes))
                    .where(audit_events.c.ts >= since)
                    .group_by(audit_events.c.event)
                )
            ).all()
            fetches = (
                await conn.execute(
                    select(audit_events.c.detail, func.count())
                    .where(audit_events.c.ts >= since)
                    .where(audit_events.c.event == "fetch")
                    .group_by(audit_events.c.detail)
                )
            ).all()
            per_api = (
                await conn.execute(
                    select(audit_events.c.api, func.count())
                    .where(audit_events.c.ts >= since)
                    .where(audit_events.c.event == "call")
                    .group_by(audit_events.c.api)
                    .order_by(func.count().desc())
                    .limit(10)
                )
            ).all()
        by_event = {str(event): int(count) for event, count, _ in counts}
        total_bytes = sum(int(total or 0) for event, _, total in counts if event == "call")
        return {
            "calls": by_event.get("call", 0),
            "denied": by_event.get("denied", 0) + by_event.get("write_denied", 0),
            "writes": by_event.get("write", 0),
            "fetch_planner": sum(int(n) for how, n in fetches if how == "planner"),
            "fetch_recipe": sum(int(n) for how, n in fetches if how == "recipe"),
            "bytes": total_bytes,
            "by_api": [(str(api), int(n)) for api, n in per_api if api],
        }

    async def prune_audit(self, before: datetime) -> int:
        async with self.engine.begin() as conn:
            result = await conn.execute(delete(audit_events).where(audit_events.c.ts < before))
        return int(result.rowcount or 0)

    async def count_hit(self, key: str, window: int) -> int:
        """Count one hit for `key` in `window` and return the window's total, atomically
        across replicas. A new window replaces the previous one for that key."""
        now = datetime.now(UTC)
        for _ in range(3):
            try:
                async with self.engine.begin() as conn:
                    bumped = await conn.execute(
                        update(rate_limits)
                        .where(rate_limits.c.key == key)
                        .where(rate_limits.c.window == window)
                        .values(count=rate_limits.c.count + 1, updated_at=now)
                        .returning(rate_limits.c.count)
                    )
                    row = bumped.first()
                    if row is not None:
                        return int(row[0])
                    await conn.execute(delete(rate_limits).where(rate_limits.c.key == key))
                    await conn.execute(
                        insert(rate_limits).values(key=key, window=window, count=1, updated_at=now)
                    )
                    return 1
            except IntegrityError:  # another replica opened this window first: count on it
                continue
        return 1_000_000  # could not count: fail closed

    async def prune_rate_limits(self, before_window: int) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(delete(rate_limits).where(rate_limits.c.window < before_window))

    async def save_doc(self, kind: str, doc_id: str, state: str, data: str) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                delete(documents).where(documents.c.kind == kind).where(documents.c.id == doc_id)
            )
            await conn.execute(
                insert(documents).values(
                    kind=kind, id=doc_id, state=state, data=data, updated_at=datetime.now(UTC)
                )
            )

    async def create_doc_if_absent(self, kind: str, doc_id: str, state: str, data: str) -> bool:
        """Insert a document unless it exists. True if this call created it."""
        try:
            async with self.engine.begin() as conn:
                await conn.execute(
                    insert(documents).values(
                        kind=kind, id=doc_id, state=state, data=data, updated_at=datetime.now(UTC)
                    )
                )
        except IntegrityError:
            return False
        return True

    async def get_doc(self, kind: str, doc_id: str) -> str | None:
        async with self.engine.connect() as conn:
            result = await conn.execute(
                select(documents.c.data)
                .where(documents.c.kind == kind)
                .where(documents.c.id == doc_id)
            )
            return result.scalar_one_or_none()

    async def docs_version(self, kind: str) -> str:
        """Changes whenever a document of this kind is added or updated."""
        async with self.engine.connect() as conn:
            row = (
                await conn.execute(
                    select(func.count(), func.max(documents.c.updated_at)).where(
                        documents.c.kind == kind
                    )
                )
            ).one()
        return f"{row[0]}:{row[1]}"

    async def list_docs(self, kind: str, state: str | None = None) -> list[str]:
        query = select(documents.c.data).where(documents.c.kind == kind)
        if state is not None:
            query = query.where(documents.c.state == state)
        async with self.engine.connect() as conn:
            return list((await conn.execute(query)).scalars().all())

    async def transition_doc(
        self, kind: str, doc_id: str, from_state: str, to_state: str, data: str
    ) -> bool:
        """Atomically move a document from one state to another. False if it was not
        in `from_state` (e.g. another replica got there first)."""
        async with self.engine.begin() as conn:
            result = await conn.execute(
                update(documents)
                .where(documents.c.kind == kind)
                .where(documents.c.id == doc_id)
                .where(documents.c.state == from_state)
                .values(state=to_state, data=data, updated_at=datetime.now(UTC))
            )
            return result.rowcount == 1
