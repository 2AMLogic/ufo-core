"""The workspace change log: one row per conversation whose drawn state moved, appended in the
same transaction as the write that moved it. A feed reads the rows after a position and re-reads
the conversations they name; the log carries no payload, so nothing in it can drift from the row a
reader draws.

A position is the workspace's counter row, raised by the append and locked until the transaction
commits, so positions follow commit order and a reader that took the newest one has seen every row
below it. That lock is taken last: the append is the last statement of every transaction that
makes one, so a transaction holding the counter waits on nothing and no lock cycle can include
it."""

from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from ufo.schema import tables


async def conversation_changed(
    connection: AsyncConnection, workspace_id: UUID, conversation_id: UUID
) -> None:
    """The append for a write that already holds the conversation's ids."""
    insert = postgres_insert if connection.dialect.name == "postgresql" else sqlite_insert
    position = (
        await connection.execute(
            insert(tables.conversation_change_cursor)
            .values(workspace_id=workspace_id, position=1)
            .on_conflict_do_update(
                index_elements=("workspace_id",),
                set_={"position": tables.conversation_change_cursor.c.position + 1},
            )
            .returning(tables.conversation_change_cursor.c.position)
        )
    ).scalar_one()
    await connection.execute(
        sa.insert(tables.conversation_change_log).values(
            workspace_id=workspace_id,
            position=position,
            conversation_id=conversation_id,
            created_at=sa.func.now(),
        )
    )


async def turn_conversation_changed(connection: AsyncConnection, turn_id: UUID) -> None:
    """The append for a write that holds only the turn's id."""
    turn = (
        await connection.execute(
            sa.select(tables.turn.c.workspace_id, tables.turn.c.conversation_id).where(
                tables.turn.c.id == turn_id
            )
        )
    ).one()
    await conversation_changed(connection, turn.workspace_id, turn.conversation_id)
