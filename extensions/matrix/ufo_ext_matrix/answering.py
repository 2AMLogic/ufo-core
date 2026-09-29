"""Which room message each turn answers, in the table this extension's migration owns.

A reply relates to the message that founded its turn, and a writeback names that turn's member
(`speaker_member_id`) and the room it belongs to (`queue_key`) but not the event the member sent.
The room's audience names the room by a digest, which recovers nothing, and the `queue_key` is the
room id a send is addressed to, which can carry nothing else. Admission is where the event id is
known, so admission records it here — one row per turn — and every later send for that turn reads
it back: `post` relates its reply to that message, `attach` hangs the turn's files under the reply,
and `speak` threads a mid-turn reply the same way.

A row is the turn's own and outlives the process, so a delivery recovered after a restart threads
exactly as the first attempt would have."""

from dataclasses import dataclass
from uuid import UUID

import sqlalchemy as sa

from ufo_ext_matrix.since import Transactional

ANSWERING_TABLE = sa.Table(
    "matrix_ext_answering",
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
    sa.Column("thread_root", sa.Text(), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)


@dataclass(frozen=True)
class Answering:
    """The room message one turn answers: the room it was sent in, its event id, and the thread it
    belongs to — None for a message sent to the room itself."""

    room_id: str
    event_id: str
    thread_root: str | None = None


async def write_answering(ctx: Transactional, turn_id: UUID, answering: Answering) -> None:
    """Record which message this turn answers. A replayed batch admits the same turn again and
    writes the same row, so the record is the message that founded the turn however often the
    stream reads it."""
    values = {
        "room_id": answering.room_id,
        "event_id": answering.event_id,
        "thread_root": answering.thread_root,
    }
    async with ctx.transaction() as connection:
        updated = await connection.execute(
            sa.update(ANSWERING_TABLE)
            .where(
                ANSWERING_TABLE.c.workspace_id == ctx.workspace_id,
                ANSWERING_TABLE.c.turn_id == turn_id,
            )
            .values(**values)
        )
        if updated.rowcount == 0:
            await connection.execute(
                sa.insert(ANSWERING_TABLE).values(
                    workspace_id=ctx.workspace_id,
                    turn_id=turn_id,
                    created_at=sa.func.now(),
                    **values,
                )
            )


async def read_answering(ctx: Transactional, turn_id: UUID) -> Answering | None:
    """The message this turn answers, or None for a turn no member's message founded — a scheduled
    run among them, whose reply relates to nothing and reads as the room's own line."""
    async with ctx.transaction() as connection:
        row = (
            await connection.execute(
                sa.select(
                    ANSWERING_TABLE.c.room_id,
                    ANSWERING_TABLE.c.event_id,
                    ANSWERING_TABLE.c.thread_root,
                ).where(
                    ANSWERING_TABLE.c.workspace_id == ctx.workspace_id,
                    ANSWERING_TABLE.c.turn_id == turn_id,
                )
            )
        ).one_or_none()
    if row is None:
        return None
    return Answering(room_id=row.room_id, event_id=row.event_id, thread_root=row.thread_root)
