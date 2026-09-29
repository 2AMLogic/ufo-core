"""Which room message carries each turn's question, in the table this extension's migration owns.

A turn that ends in a question sends it as a message of its own, apart from the words it answered
with, so marking the answer that landed rewrites the question and nothing the turn said around it.
`post` knows that message's event id and the stream that hears the answer does not, so `post`
records it here — one row per turn — and the answer reads back the one message it may rewrite. A
reply to any other message the bot sent still answers the question, and rewrites none of them.

A row is the turn's own and outlives the process, so an answer heard after a restart marks the
question exactly as it would have before."""

from dataclasses import dataclass
from uuid import UUID

import sqlalchemy as sa

from ufo_ext_matrix.since import Transactional

ASKING_TABLE = sa.Table(
    "matrix_ext_asking",
    sa.MetaData(),
    sa.Column(
        "workspace_id",
        sa.Uuid(),
        sa.ForeignKey("workspace.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("turn_id", sa.Uuid(), primary_key=True),
    sa.Column("room_id", sa.Text(), nullable=False),
    sa.Column("event_id", sa.Text(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)


@dataclass(frozen=True)
class Asking:
    """The message one turn's question was sent as: the room it was sent to and its event id."""

    room_id: str
    event_id: str


async def write_asking(ctx: Transactional, turn_id: UUID, asking: Asking) -> None:
    """Record which message carries this turn's question. A repeated delivery sends the question
    under the transaction id it was sent under before, and writes the same row."""
    values = {"room_id": asking.room_id, "event_id": asking.event_id}
    async with ctx.transaction() as connection:
        updated = await connection.execute(
            sa.update(ASKING_TABLE)
            .where(
                ASKING_TABLE.c.workspace_id == ctx.workspace_id,
                ASKING_TABLE.c.turn_id == turn_id,
            )
            .values(**values)
        )
        if updated.rowcount == 0:
            await connection.execute(
                sa.insert(ASKING_TABLE).values(
                    workspace_id=ctx.workspace_id,
                    turn_id=turn_id,
                    created_at=sa.func.now(),
                    **values,
                )
            )


async def read_asking(ctx: Transactional, turn_id: UUID) -> Asking | None:
    """The message carrying this turn's question, or None for a turn whose question this surface
    never sent as one message."""
    async with ctx.transaction() as connection:
        row = (
            await connection.execute(
                sa.select(ASKING_TABLE.c.room_id, ASKING_TABLE.c.event_id).where(
                    ASKING_TABLE.c.workspace_id == ctx.workspace_id,
                    ASKING_TABLE.c.turn_id == turn_id,
                )
            )
        ).one_or_none()
    if row is None:
        return None
    return Asking(room_id=row.room_id, event_id=row.event_id)
