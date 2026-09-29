"""A store and a tool context for pulse's own tests, built without touching the matrix extension.

The two extensions pin and activate independently (`extensions/test_distribution_pins.py`), and a
pulse test importing matrix's fakes would make that independence true of the distribution and false
of the suite. So this is pulse's own: the record's tables over an in-memory database, a context that
reaches them, and a carrier that can be present, absent, or broken — the three states the projection
has to tell apart.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from ufo_ext_pulse.record import (
    COVERAGE_TABLE,
    COVERED_TABLE,
    SERIES_TABLE,
    SIGHTING_TABLE,
)


async def store_engine() -> AsyncEngine:
    """The extension's tables over a fresh in-memory database, beside the one core column they
    reference. Built from the same `sa.Table` objects the handlers query, so a column this suite
    exercises is a column the handlers see; that the *migration* creates these same tables is
    asserted against the real DDL in `test_registry.py`, which is the half this cannot check."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    metadata = sa.MetaData()
    sa.Table("workspace", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    for table in (SIGHTING_TABLE, COVERED_TABLE, COVERAGE_TABLE, SERIES_TABLE):
        table.to_metadata(metadata)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    return engine


@dataclass
class Files:
    """A carrier that takes workspace files. `written` is what landed, last write per path."""

    written: dict[str, bytes] = field(default_factory=dict)
    breaks: bool = False
    breaks_for: frozenset[str] = field(default_factory=frozenset)
    on_write: Callable[[UUID, str, bytes], Awaitable[None]] | None = None
    """Runs before the bytes land — the seam a test uses to make something happen *during* a
    projection, which is the only way to exercise what the job reads versus what it stamps."""

    async def write(self, conversation_id: UUID, rel: str, content: bytes) -> str:
        if self.on_write is not None:
            await self.on_write(conversation_id, rel, content)
        if self.breaks or rel in self.breaks_for:
            raise RuntimeError("the conversation has no live sandbox")
        self.written[rel] = content
        return f"/workspace/{rel}"

    def text(self, rel: str) -> str:
        return self.written[rel].decode()


@dataclass
class Store:
    """An `ExtensionContext` as the record module, the handlers and the job use one.

    `files` defaults to **None**, because that is what core actually hands a tool handler: only the
    job runner and surface contexts are built with the sandbox seam wired. A test that wants the
    projection to be able to land says so, the way `job_store` does — the previous default was a
    capability no tool has ever held, and it let a projection be "tested" where it could never run.
    """

    engine: AsyncEngine
    workspace_id: UUID = field(default_factory=uuid4)
    files: Files | None = None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncConnection]:
        async with self.engine.begin() as connection:
            yield connection


def tool_context(
    store: Store,
    conversation_id: UUID | None = None,
    sandbox_conversation_id: UUID | None = None,
) -> SimpleNamespace:
    """What a handler reads off its `ToolContext`: the extension context and the two conversation
    ids core carries, because a turn can run in one conversation and share another's sandbox.
    Nothing else on a real one is touched, and a stand-in that grew a field a handler does not read
    would be a claim about core rather than about pulse."""
    return SimpleNamespace(
        ext=store,
        turn=SimpleNamespace(
            conversation_id=conversation_id or uuid4(),
            sandbox_conversation_id=sandbox_conversation_id,
        ),
    )


def on_store(test: Callable[..., Awaitable[None]]) -> Callable[..., None]:
    """Run an async test on its own loop with a fresh `Store`, so the suite needs no async pytest
    plugin and every engine lives and dies on the loop that made it."""
    params = [p for p in inspect.signature(test).parameters.values() if p.name != "store"]

    @functools.wraps(test)
    def run(*args: object, **kwargs: object) -> None:
        async def scenario() -> None:
            engine = await store_engine()
            try:
                await test(*args, store=Store(engine=engine), **kwargs)
            finally:
                await engine.dispose()

        asyncio.run(scenario())

    run.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]
    return run


def job_store(store: Store) -> Store:
    """The same workspace as a job sees it: `files` wired, the way `serve.py` builds the job
    runner's context and unlike anything a tool is given."""
    store.files = Files()
    return store
