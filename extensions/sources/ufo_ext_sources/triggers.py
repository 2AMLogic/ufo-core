"""The source-trigger table and the scoped store that owns it.

A trigger is one conversation's standing interest in one connection's feed. Each batch returns to
that conversation. The row carries the connection it watches, the owning conversation, the agent
it invokes, and the member who asked for it.

The row carries the `name` the apply gave it and the one-line `description` a member reads it by,
as a scheduled task does. A name is how every surface asks for one trigger, so it is the table's one
unique key and an apply under a taken one is refused in those words.

What wakes it is `when`: a list of JMESPath clauses over the connection's changed pages, stored as
JSON, so a thread watches the two pull requests it is talking about and the whole feed beside them
as rows of one table. The clauses are free text and carry no index — a key over them would make a
btree page's limit the limit on what a member may write — so what keeps a conversation from holding
one watch twice is a read of its own rows before the insert. A trigger wakes when a page *starts* to
meet a clause, which `source_trigger_match` is the memory of — one bit per trigger, page and clause,
dying with the trigger through its foreign key. A clause that raises while being evaluated pauses
its own trigger and writes the error to `fault`, where the member reads it; resuming clears it.

The connection is a foreign key that cascades, so disconnecting an account takes its triggers with
its source rows and its pages: nothing here sweeps them, and no trigger outlives the feed it
watches.

Every statement filters `workspace_id` itself — `ExtensionContext.transaction` yields an unscoped
connection. The alert sweep runs workspace-wide, because a connection belongs to the workspace and
the conversations waking on it belong to whichever agents subscribed; a member-facing listing also
filters the selected object namespace, which defaults to the turn's agent."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ufo.sdk.audience import Audience
from ufo.sdk.context import ExtensionContext
from ufo.sdk.objects import object_agent_id

_metadata = sa.MetaData()
source_trigger = sa.Table(
    "source_trigger",
    _metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column(
        "workspace_id",
        sa.Uuid,
        sa.ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "conversation_id",
        sa.Uuid,
        sa.ForeignKey("conversation.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("agent_id", sa.Uuid, sa.ForeignKey("agent.id", ondelete="CASCADE"), nullable=False),
    sa.Column(
        "connection_id",
        sa.Uuid,
        sa.ForeignKey("connection.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("description", sa.Text, nullable=False, server_default=""),
    sa.Column("when", sa.Text, nullable=False),
    sa.Column("fault", sa.Text, nullable=True),
    sa.Column("delivery", sa.Text, nullable=False, server_default="current"),
    sa.Column("resource", sa.Text, nullable=False, server_default=""),
    sa.Column("streams", sa.Text, nullable=False, server_default=""),
    sa.Column("paused", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column(
        "created_by_member_id",
        sa.Uuid,
        sa.ForeignKey("member.id", ondelete="SET NULL"),
        nullable=True,
    ),
    sa.Column("requesting_message_ref", sa.Uuid, nullable=True),
    sa.Column("internet_access", sa.Boolean, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("workspace_id", "name", name="source_trigger_name"),
    sa.Index("source_trigger_connection", "workspace_id", "connection_id"),
)

source_trigger_match = sa.Table(
    "source_trigger_match",
    _metadata,
    sa.Column(
        "workspace_id",
        sa.Uuid,
        sa.ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "trigger_id",
        sa.Uuid,
        sa.ForeignKey("source_trigger.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("page_uid", sa.Uuid, nullable=False),
    sa.Column("clause", sa.SmallInteger, nullable=False),
    sa.Column("met", sa.Boolean, nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint("trigger_id", "page_uid", "clause", name="source_trigger_match_pkey"),
)

_COLUMNS = (
    source_trigger.c.id,
    source_trigger.c.conversation_id,
    source_trigger.c.agent_id,
    source_trigger.c.connection_id,
    source_trigger.c.name,
    source_trigger.c.description,
    source_trigger.c.when,
    source_trigger.c.fault,
    source_trigger.c.paused,
    source_trigger.c.created_by_member_id,
    source_trigger.c.requesting_message_ref,
    source_trigger.c.internet_access,
    source_trigger.c.created_at,
    source_trigger.c.updated_at,
)


SourceTriggerDelivery = Literal["current"]


@dataclass(frozen=True)
class SourceTrigger:
    """One trigger as a handler reads it. A value object — never leaves the process, so a live
    capability hands it out and a wire type never mirrors it."""

    id: UUID
    conversation_id: UUID
    agent_id: UUID
    connection_id: UUID
    name: str
    description: str
    when: tuple[str, ...]
    fault: str | None
    delivery: SourceTriggerDelivery
    paused: bool
    created_by_member_id: UUID | None
    internet_access: Literal[False] | None
    created_at: datetime
    updated_at: datetime
    requesting_message_ref: UUID | None = None


@dataclass(frozen=True)
class ListedTrigger:
    """One trigger beside the disclosure audience of the conversation that owns it — the pair every
    member-facing read decides visibility from, since who may see a trigger is a fact of where it
    fires and not of the trigger row. An alert reads `SourceTrigger` alone, so only a read that
    must answer for a member pays for the lookup."""

    trigger: SourceTrigger
    audience: Audience
    surface_label: str | None


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _trigger(row: sa.RowMapping) -> SourceTrigger:
    """One row as a handler reads it. The clause list is stored as JSON, and its order is part of
    what the trigger is: `source_trigger_match` remembers a clause by its index."""
    return SourceTrigger(
        id=row["id"],
        conversation_id=row["conversation_id"],
        agent_id=row["agent_id"],
        connection_id=row["connection_id"],
        name=row["name"],
        description=row["description"],
        when=tuple(json.loads(row["when"])),
        fault=row["fault"],
        delivery="current",
        paused=bool(row["paused"]),
        created_by_member_id=row["created_by_member_id"],
        requesting_message_ref=row["requesting_message_ref"],
        internet_access=None if row["internet_access"] else False,
        created_at=_utc(row["created_at"]),
        updated_at=_utc(row["updated_at"]),
    )


@dataclass(frozen=True)
class SourceTriggerStore:
    """Source-trigger rows behind the ambient workspace boundary."""

    ctx: ExtensionContext

    @property
    def workspace_id(self) -> UUID:
        return self.ctx.workspace_id

    async def create(
        self,
        conversation_id: UUID,
        connection_id: UUID,
        delivery: SourceTriggerDelivery,
        created_by_member_id: UUID,
        name: str,
        when: tuple[str, ...],
        description: str = "",
        internet_access: Literal[False] | None = None,
        requesting_message_ref: UUID | None = None,
    ) -> SourceTrigger:
        """Create one wake-up on one connection's feed, woken by the changes its `when` clauses
        admit. The owning conversation is checked against the object namespace, so a trigger can
        never invoke as an agent other than its owner. A taken name and an identity this
        conversation already watches each refuse in this vocabulary rather than as a constraint
        violation, so the caller is told which of the two it met."""
        agent_id = object_agent_id()
        if await self.ctx.conversation_agent(conversation_id) != agent_id:
            raise ValueError(
                "a source trigger must wake a conversation bound to its executing agent"
            )
        clauses = json.dumps(list(when))
        values = {
            "id": uuid4(),
            "workspace_id": self.workspace_id,
            "conversation_id": conversation_id,
            "agent_id": agent_id,
            "connection_id": connection_id,
            "name": name,
            "description": description,
            "when": clauses,
            "fault": None,
            "delivery": delivery,
            "paused": False,
            "created_by_member_id": created_by_member_id,
            "requesting_message_ref": requesting_message_ref,
            "internet_access": internet_access is not False,
            "created_at": sa.func.now(),
            "updated_at": sa.func.now(),
        }
        async with self.ctx.transaction() as connection:
            held = await connection.scalar(
                sa.select(sa.func.count())
                .select_from(source_trigger)
                .where(
                    source_trigger.c.workspace_id == self.workspace_id,
                    source_trigger.c.conversation_id == conversation_id,
                    source_trigger.c.connection_id == connection_id,
                    source_trigger.c.when == clauses,
                )
            )
            if held:
                raise ValueError(f"this conversation already watches {', '.join(when)}")
            insert = pg_insert if connection.dialect.name == "postgresql" else sqlite_insert
            statement = (
                insert(source_trigger)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=(source_trigger.c.workspace_id, source_trigger.c.name)
                )
                .returning(*_COLUMNS)
            )
            row = (await connection.execute(statement)).mappings().one_or_none()
            if row is None:
                raise ValueError(f"a source trigger is already named {name!r}")
        return _trigger(row)

    async def amend(
        self, expected: SourceTrigger, *, paused: bool, description: str
    ) -> SourceTrigger:
        """Stop this trigger waking its conversation or start it again, and set the line a listing
        reads it by. Those are the whole of what a standing trigger can be changed to: the
        connection, the clauses and the conversation it names are its identity, and those are
        applied as another trigger or not at all. Resuming clears the fault, because a member who
        resumes a trigger a clause paused is saying to try it again — and the next batch writes the
        fault back if the clause still raises."""
        agent_id = object_agent_id()
        if expected.agent_id != agent_id:
            raise ValueError("source trigger executor changed while editing")
        async with self.ctx.transaction() as connection:
            row = (
                (
                    await connection.execute(
                        sa.update(source_trigger)
                        .where(
                            source_trigger.c.workspace_id == self.workspace_id,
                            source_trigger.c.id == expected.id,
                            source_trigger.c.agent_id == expected.agent_id,
                        )
                        .values(
                            paused=paused,
                            description=description,
                            fault=None if not paused else source_trigger.c.fault,
                            updated_at=sa.func.now(),
                        )
                        .returning(*_COLUMNS)
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise ValueError(f"source trigger {expected.id} changed while editing")
        return _trigger(row)

    async def fault(self, expected: SourceTrigger, reason: str) -> None:
        """Pause this trigger and record why. A clause that raises is the member's to fix, and the
        row is where they read it: pausing stops the wake without losing the trigger, and leaves
        every other trigger in the workspace running — a handler that raised instead would leave the
        `page_change` cursor unadvanced and replay the batch for all of them on the next tick."""
        async with self.ctx.transaction() as connection:
            await connection.execute(
                sa.update(source_trigger)
                .where(
                    source_trigger.c.workspace_id == self.workspace_id,
                    source_trigger.c.id == expected.id,
                )
                .values(paused=True, fault=reason, updated_at=sa.func.now())
            )

    async def matched(
        self, trigger_id: UUID, page_uids: frozenset[UUID]
    ) -> dict[tuple[UUID, int], bool]:
        """Which clauses these pages met the last time this trigger was evaluated against them,
        keyed by page and clause index. A pair with no row is absent, which the caller reads as not
        met: a trigger applied over a feed already synced starts from nothing met, so the first
        change to a page that meets a clause wakes it."""
        if not page_uids:
            return {}
        async with self.ctx.transaction() as connection:
            rows = (
                await connection.execute(
                    sa.select(
                        source_trigger_match.c.page_uid,
                        source_trigger_match.c.clause,
                        source_trigger_match.c.met,
                    ).where(
                        source_trigger_match.c.workspace_id == self.workspace_id,
                        source_trigger_match.c.trigger_id == trigger_id,
                        source_trigger_match.c.page_uid.in_(page_uids),
                    )
                )
            ).all()
        return {(row.page_uid, row.clause): bool(row.met) for row in rows}

    async def record_matches(
        self,
        trigger_id: UUID,
        met: dict[tuple[UUID, int], bool],
        tombstoned: frozenset[UUID],
    ) -> None:
        """Store what each page met this pass, and drop the rows of the pages this batch removed —
        a page GitHub tombstoned and later restores is a page starting to meet its clauses again.
        Written after the batch fires, so a failure here replays the batch and the turn's own
        idempotency key drops the duplicate."""
        async with self.ctx.transaction() as connection:
            if tombstoned:
                await connection.execute(
                    sa.delete(source_trigger_match).where(
                        source_trigger_match.c.workspace_id == self.workspace_id,
                        source_trigger_match.c.trigger_id == trigger_id,
                        source_trigger_match.c.page_uid.in_(tombstoned),
                    )
                )
            if not met:
                return
            insert = pg_insert if connection.dialect.name == "postgresql" else sqlite_insert
            statement = insert(source_trigger_match).values(
                [
                    {
                        "workspace_id": self.workspace_id,
                        "trigger_id": trigger_id,
                        "page_uid": page_uid,
                        "clause": clause,
                        "met": value,
                        "updated_at": sa.func.now(),
                    }
                    for (page_uid, clause), value in sorted(
                        met.items(), key=lambda entry: (entry[0][0].hex, entry[0][1])
                    )
                ]
            )
            await connection.execute(
                statement.on_conflict_do_update(
                    index_elements=(
                        source_trigger_match.c.trigger_id,
                        source_trigger_match.c.page_uid,
                        source_trigger_match.c.clause,
                    ),
                    set_={"met": statement.excluded.met, "updated_at": sa.func.now()},
                )
            )

    async def remove(self, expected: SourceTrigger) -> None:
        agent_id = object_agent_id()
        if expected.agent_id != agent_id:
            raise ValueError("source trigger executor changed while removing")
        async with self.ctx.transaction() as connection:
            deleted = await connection.execute(
                sa.delete(source_trigger).where(
                    source_trigger.c.workspace_id == self.workspace_id,
                    source_trigger.c.id == expected.id,
                    source_trigger.c.agent_id == expected.agent_id,
                    source_trigger.c.conversation_id == expected.conversation_id,
                    source_trigger.c.connection_id == expected.connection_id,
                )
            )
        if deleted.rowcount == 0:
            raise ValueError(f"source trigger {expected.id} changed while removing")

    async def retire_unattributed(self, expected: SourceTrigger) -> None:
        """Delete a trigger created without a member owner."""
        if expected.created_by_member_id is not None:
            raise ValueError("only an unattributed source trigger may be retired here")
        async with self.ctx.transaction() as connection:
            await connection.execute(
                sa.delete(source_trigger).where(
                    source_trigger.c.workspace_id == self.workspace_id,
                    source_trigger.c.id == expected.id,
                    source_trigger.c.created_by_member_id.is_(None),
                )
            )

    async def watched(self, conversation_id: UUID) -> dict[UUID, tuple[str, ...]]:
        """The clauses one conversation already holds, per connection — what an offer to watch a
        link shows under itself, so the agent reads what is covered and decides. A model judging
        structured clauses is the read here, where a string comparison against a resource would be
        a heuristic over text the member never wrote."""
        async with self.ctx.transaction() as connection:
            rows = (
                await connection.execute(
                    sa.select(source_trigger.c.connection_id, source_trigger.c.when).where(
                        source_trigger.c.workspace_id == self.workspace_id,
                        source_trigger.c.conversation_id == conversation_id,
                    )
                )
            ).all()
        held: dict[UUID, list[str]] = {}
        for row in rows:
            held.setdefault(row.connection_id, []).extend(json.loads(row.when))
        return {connection_id: tuple(clauses) for connection_id, clauses in held.items()}

    async def waking(self, connection_id: UUID) -> tuple[SourceTrigger, ...]:
        """Every running trigger for this connection, workspace-wide — the alert sweep's read, the
        whole-feed triggers and the narrowed ones in one pass, oldest first with the id breaking a
        tie, so one batch wakes conversations in the order they subscribed. It spans agents on
        purpose: a connection belongs to the workspace, and each row names its agent. A paused
        trigger is absent: pausing is what stops the wake, and the row stays for the member who
        resumes it."""
        query = sa.select(*_COLUMNS).where(
            source_trigger.c.workspace_id == self.workspace_id,
            source_trigger.c.connection_id == connection_id,
            source_trigger.c.paused == sa.false(),
        )
        async with self.ctx.transaction() as connection:
            rows = (await connection.execute(query)).mappings().all()
        woken = [_trigger(row) for row in rows]
        return tuple(sorted(woken, key=lambda trigger: (trigger.created_at, trigger.id)))

    async def list_reported(
        self, *, conversation_id: UUID | None = None
    ) -> tuple[ListedTrigger, ...]:
        """This agent's triggers, each beside the audience and surface label of its owning
        conversation — the read a member-facing surface answers visibility from, ordered by
        connection and then by the clauses, so one connection's watches read in one order however
        they were applied. The facts are read live rather than snapshotted onto the row: an audience
        never changes, but a channel's label does when it is renamed, and a listing showing a
        channel's old name is one that lies. A trigger whose conversation is gone is absent from
        this page."""
        agent_id = object_agent_id()
        query = sa.select(*_COLUMNS).where(
            source_trigger.c.workspace_id == self.workspace_id,
            source_trigger.c.agent_id == agent_id,
        )
        if conversation_id is not None:
            query = query.where(source_trigger.c.conversation_id == conversation_id)
        async with self.ctx.transaction() as connection:
            rows = (await connection.execute(query)).mappings().all()
        listed = [_trigger(row) for row in rows]
        triggers = tuple(
            sorted(
                listed,
                key=lambda trigger: (trigger.connection_id, trigger.when, trigger.id),
            )
        )
        if not triggers:
            return ()
        facts = await self.ctx.conversation_facts(
            tuple({trigger.conversation_id for trigger in triggers})
        )
        return tuple(
            ListedTrigger(
                trigger=trigger,
                audience=facts[trigger.conversation_id].audience,
                surface_label=facts[trigger.conversation_id].surface_label,
            )
            for trigger in triggers
            if trigger.conversation_id in facts
        )
