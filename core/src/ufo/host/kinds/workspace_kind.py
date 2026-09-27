"""The `workspace` object kind: who this workspace answers, and whether its members add teammates.

One `workspace` row is one instance, named by its id. The one authored field is `members_can_add`,
which a speaking admin on the main agent applies. Everything else it reports is derived: the member
count and the seated count come from the member rows, and nothing bounds either — the workspace
pays one flat fee and its members are unlimited. Seating one member, or unseating them to remove
their access, is the `member` kind's admin-gated apply.

Status names who holds a seat, under the roster's own rule: a member asking the main agent in an
internal conversation reads every colleague, and a child agent answers the speaker's own row
alone, exactly as the `member` kind does.

The loader binds it with no extension context, so the handlers read the ambient workspace
directly."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field

from ufo.db import workspace_tx
from ufo.runtime.ext.json_value import JsonValue
from ufo.runtime.objects import (
    AdminRequired,
    ObjectDetail,
    ObjectKind,
    ObjectListQuery,
    ObjectPage,
    ObjectRow,
    UnknownObject,
    VerbNotSupported,
    object_page,
)
from ufo.runtime.seats import SeatEntry, Seats, member_is_admin
from ufo.runtime.tools.context import ToolContext
from ufo.runtime.turns.audience import FOREIGN_AUDIENCE_PREFIX
from ufo.runtime.workspace import ws_current
from ufo.schema import tables

WORKSPACE_KIND = "workspace"
WORKSPACE_IS_PERMANENT = "a workspace is created at first run and is never deleted"
WORKSPACE_ADMIN_GATE = (
    "a workspace's settings are changed by a workspace admin using the main agent"
)
WORKSPACE_ROOM = (
    "a workspace's settings are changed in an internal conversation, never in a channel shared "
    "with another organization"
)


class WorkspaceSpec(BaseModel):
    """The workspace's one authored setting. Its counts are derived, so they are status."""

    model_config = ConfigDict(extra="forbid")
    members_can_add: bool = Field(
        description="Whether a member who is not an admin may add a teammate. An admin always may, "
        "and only an admin may add another admin."
    )


@dataclass(frozen=True)
class WorkspaceShape:
    """The one read every verb of this kind answers from: the setting, the member count, how many
    of them hold a seat, the roster naming which, and the row's own timestamps."""

    members_can_add: bool
    members: int
    seated: int
    roster: tuple[SeatEntry, ...]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class WorkspaceObjects:
    """Handlers over the bound workspace's own row: one instance named by its id, listed and read
    with its member counts, its setting applied by an admin, and create and delete refused."""

    async def list(self, ctx: ToolContext, query: ObjectListQuery) -> ObjectPage:
        if ctx.audience.startswith(FOREIGN_AUDIENCE_PREFIX):
            return object_page((), query)
        shape = await self._shape()
        return object_page(
            (
                ObjectRow(
                    name=str(ws_current().workspace_id),
                    summary=f"{shape.members} members, {shape.seated} seated",
                    fields={"members": shape.members, "seated": shape.seated},
                ),
            ),
            query,
        )

    async def get(self, ctx: ToolContext, name: str) -> ObjectDetail[WorkspaceSpec] | None:
        if ctx.audience.startswith(FOREIGN_AUDIENCE_PREFIX):
            return None
        if name != str(ws_current().workspace_id):
            return None
        shape = await self._shape()
        return ObjectDetail(
            spec=WorkspaceSpec(members_can_add=shape.members_can_add),
            created_at=shape.created_at,
            updated_at=shape.updated_at,
        )

    async def status(
        self,
        ctx: ToolContext,
        name: str,
        *,
        expected_generation: UUID | None,
    ) -> dict[str, JsonValue] | None:
        if ctx.audience.startswith(FOREIGN_AUDIENCE_PREFIX):
            return None
        if name != str(ws_current().workspace_id):
            return None
        shape = await self._shape()
        whole = ctx.speaker_member_id is not None and await ctx.agent_is_main()
        return {
            "members": shape.members,
            "seated": shape.seated,
            "roster": [
                {"email": entry.email, "seated": entry.seated, "admin": entry.admin}
                for entry in shape.roster
                if whole or entry.id == ctx.speaker_member_id
            ],
        }

    async def apply(
        self,
        ctx: ToolContext,
        name: str,
        spec: WorkspaceSpec,
        old: WorkspaceSpec | None,
        *,
        expected_generation: UUID | None,
    ) -> None:
        speaker = ctx.require_speaker(WORKSPACE_ADMIN_GATE)
        if not await ctx.agent_is_main():
            raise AdminRequired(WORKSPACE_ADMIN_GATE)
        if ctx.audience.startswith(FOREIGN_AUDIENCE_PREFIX):
            raise AdminRequired(WORKSPACE_ROOM)
        if old is None:
            raise VerbNotSupported(WORKSPACE_IS_PERMANENT)
        workspace_id = ws_current().workspace_id
        if name != str(workspace_id):
            raise UnknownObject(f"no workspace object named {name!r}")
        async with workspace_tx() as connection:
            await connection.execute(
                sa.select(tables.workspace.c.id)
                .where(tables.workspace.c.id == workspace_id)
                .with_for_update()
            )
            if not await member_is_admin(connection, workspace_id, speaker):
                raise AdminRequired(WORKSPACE_ADMIN_GATE)
            await connection.execute(
                sa.update(tables.workspace)
                .where(tables.workspace.c.id == workspace_id)
                .values(members_can_add=spec.members_can_add, updated_at=sa.func.now())
            )

    async def delete(
        self,
        ctx: ToolContext,
        name: str,
        *,
        expected_generation: UUID | None,
    ) -> None:
        raise VerbNotSupported(WORKSPACE_IS_PERMANENT)

    async def _shape(self) -> WorkspaceShape:
        workspace_id = ws_current().workspace_id
        async with workspace_tx() as connection:
            row = (
                await connection.execute(
                    sa.select(
                        tables.workspace.c.members_can_add,
                        tables.workspace.c.created_at,
                        tables.workspace.c.updated_at,
                    ).where(tables.workspace.c.id == workspace_id)
                )
            ).one()
            snapshot = await Seats(workspace_id).snapshot(connection)
        return WorkspaceShape(
            members_can_add=row.members_can_add,
            members=len(snapshot.members),
            seated=snapshot.seated,
            roster=snapshot.members,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


WORKSPACE_OBJECT = ObjectKind(
    name=WORKSPACE_KIND,
    description=(
        "Show how many members this workspace has, who holds a seat, and whether members who are "
        "not admins may add teammates. A member's seat changes on the member object."
    ),
    guidance=(
        "One object per workspace, named by the workspace id, so a listing returns exactly one "
        "row. Listings filter and order on `members` and `seated`; status carries both plus "
        "`roster`, which names every member with whether they hold a seat and whether they "
        "administer the workspace. A member asking the main agent in an internal conversation "
        "reads the whole roster; a child agent answers the speaker's own row alone. Members are "
        "unlimited and nothing is billed per head, so the counts are figures to report and never "
        "a limit to check before adding someone. A member holds a seat from the moment they are "
        "created and the agent answers them; an admin unseats one to remove that person's "
        "access, which is the only way to remove it. The spec's one field, `members_can_add`, is "
        "whether a member who is not an admin may add a teammate; only a speaking admin using the "
        "main agent applies it. Create and delete are refused. Seat or unseat one member by "
        "applying {seated: true} or {seated: false} on their member object. Reads answer an "
        "internal conversation only — an externally shared channel lists nothing."
    ),
    spec_model=WorkspaceSpec,
    store=WorkspaceObjects(),
    list_fields=frozenset({"members", "seated"}),
)
