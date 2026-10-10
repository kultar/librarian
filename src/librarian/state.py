"""Durable state: credential requests survive restarts and are shared by replicas.

Uses SQLAlchemy async so the same code runs on SQLite (single instance) and
Postgres (several replicas). A2A tasks are stored by the SDK's own
`DatabaseTaskStore` on the same engine.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, MetaData, String, Table, Text, delete, insert, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

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


documents = Table(
    "librarian_documents",
    metadata,
    Column("kind", String(32), primary_key=True),
    Column("id", String(64), primary_key=True),
    Column("state", String(16), nullable=False, index=True),
    Column("data", Text, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


def make_engine(url: str) -> AsyncEngine:
    """Create an async engine; for SQLite, make sure the directory exists."""
    if url.startswith("sqlite"):
        from pathlib import Path

        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    return create_async_engine(url)


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

    async def get_request(self, request_id: str) -> CredentialRequest | None:
        async with self.engine.connect() as conn:
            result = await conn.execute(
                select(credential_requests.c.data).where(credential_requests.c.id == request_id)
            )
            data = result.scalar_one_or_none()
        return None if data is None else CredentialRequest.model_validate_json(data)

    async def pending_for(self, api: str) -> CredentialRequest | None:
        async with self.engine.connect() as conn:
            result = await conn.execute(
                select(credential_requests.c.data)
                .where(credential_requests.c.api == api)
                .where(credential_requests.c.state == RequestState.PENDING.value)
                .limit(1)
            )
            data = result.scalar_one_or_none()
        return None if data is None else CredentialRequest.model_validate_json(data)

    async def list_requests(self, state: RequestState | None = None) -> list[CredentialRequest]:
        query = select(credential_requests.c.data)
        if state is not None:
            query = query.where(credential_requests.c.state == state.value)
        async with self.engine.connect() as conn:
            rows = (await conn.execute(query)).scalars().all()
        return [CredentialRequest.model_validate_json(row) for row in rows]

    # -- generic documents (write approvals, grants) ---------------------------------

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

    async def get_doc(self, kind: str, doc_id: str) -> str | None:
        async with self.engine.connect() as conn:
            result = await conn.execute(
                select(documents.c.data)
                .where(documents.c.kind == kind)
                .where(documents.c.id == doc_id)
            )
            return result.scalar_one_or_none()

    async def list_docs(self, kind: str, state: str | None = None) -> list[str]:
        query = select(documents.c.data).where(documents.c.kind == kind)
        if state is not None:
            query = query.where(documents.c.state == state)
        async with self.engine.connect() as conn:
            return list((await conn.execute(query)).scalars().all())
