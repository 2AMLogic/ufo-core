"""Where a member's shared file landed, keyed on the event that carried it.

`deliver` runs before the `/sync` position is written, so a crash between the two replays the batch
with the same event ids. Core admits the replayed message once, because `admit` carries the event id
as its idempotency key; the file half carried no such key, so the same bytes were fetched again,
stored under a fresh artifact key, and delivered to a path numbered beside the first — leaving the
member two copies of one file and the turn two artifact rows for one message.

This table is that key. A row names the path the file landed under and the artifact it points at, so
a replayed event answers from the row rather than doing the work again. Returning the *original*
artifact key is what settles the second copy too: core's own attachment insert conflicts on
`(turn_id, blob_key)` and does nothing, which a freshly minted key could never hit.

One row per event per workspace. A room's event id is unique within its homeserver, and a workspace
holds one bot on one homeserver, so the pair is the identity."""

from contextlib import AbstractAsyncContextManager
from typing import Protocol
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

DELIVERED_TABLE = sa.Table(
    "matrix_ext_delivered",
    sa.MetaData(),
    sa.Column(
        "workspace_id",
        sa.Uuid(),
        sa.ForeignKey("workspace.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("event_id", sa.Text(), primary_key=True),
    sa.Column("rel", sa.Text(), nullable=False),
    sa.Column("blob_key", sa.Text(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)


class Transactional(Protocol):
    workspace_id: UUID

    def transaction(self) -> AbstractAsyncContextManager[AsyncConnection]: ...


async def read_delivered(ctx: Transactional, event_id: str) -> tuple[str, str] | None:
    """The path and artifact key this event's file already landed under, or nothing for an event
    whose file has not been delivered."""
    async with ctx.transaction() as connection:
        row = (
            await connection.execute(
                sa.select(DELIVERED_TABLE.c.rel, DELIVERED_TABLE.c.blob_key).where(
                    DELIVERED_TABLE.c.workspace_id == ctx.workspace_id,
                    DELIVERED_TABLE.c.event_id == event_id,
                )
            )
        ).one_or_none()
    return None if row is None else (row.rel, row.blob_key)


async def write_delivered(ctx: Transactional, event_id: str, rel: str, blob_key: str) -> None:
    """Record where this event's file landed. Written after the delivery core accepted, so a row
    stands for a file that is on disk rather than one that was about to be."""
    async with ctx.transaction() as connection:
        await connection.execute(
            sa.insert(DELIVERED_TABLE).values(
                workspace_id=ctx.workspace_id,
                event_id=event_id,
                rel=rel,
                blob_key=blob_key,
                created_at=sa.func.now(),
            )
        )
