"""Where each installation's `/sync` stream stands, in the table this extension's migration owns.

Core keeps one integer cursor per surface; a Matrix `next_batch` is an opaque string and one deploy
may run several bots, so the position lives here, one row per workspace, beside the installation it
belongs to. A row naming a different installation is a bot the workspace has since replaced, and
reads as no position at all — the new bot's stream starts at its own head."""

from contextlib import AbstractAsyncContextManager
from typing import Protocol
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

SINCE_TABLE = sa.Table(
    "matrix_ext_since",
    sa.MetaData(),
    sa.Column(
        "workspace_id",
        sa.Uuid(),
        sa.ForeignKey("workspace.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("installation_id", sa.Text(), nullable=False),
    sa.Column("since", sa.Text(), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)


class Transactional(Protocol):
    workspace_id: UUID

    def transaction(self) -> AbstractAsyncContextManager[AsyncConnection]: ...


async def read_since(ctx: Transactional, installation_id: str) -> str | None:
    async with ctx.transaction() as connection:
        row = (
            await connection.execute(
                sa.select(SINCE_TABLE.c.installation_id, SINCE_TABLE.c.since).where(
                    SINCE_TABLE.c.workspace_id == ctx.workspace_id
                )
            )
        ).one_or_none()
    if row is None or row.installation_id != installation_id:
        return None
    return row.since


async def write_since(ctx: Transactional, installation_id: str, since: str) -> None:
    async with ctx.transaction() as connection:
        await _write(connection, ctx.workspace_id, installation_id, since)


async def _write(
    connection: AsyncConnection, workspace_id: UUID, installation_id: str, since: str
) -> None:
    values = {"installation_id": installation_id, "since": since, "updated_at": sa.func.now()}
    updated = await connection.execute(
        sa.update(SINCE_TABLE).where(SINCE_TABLE.c.workspace_id == workspace_id).values(**values)
    )
    if updated.rowcount == 0:
        await connection.execute(sa.insert(SINCE_TABLE).values(workspace_id=workspace_id, **values))
