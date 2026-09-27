"""Workspace provisioning: the one write that founds a workspace.

`ufoctl init` and `DeployContext.provision` both call `Provisioning.seat`, so a workspace's
first admin and main agent are created one way. It is core rather than an extension because the
`workspace_founded` handlers run inside the transaction that founds the workspace, a point only
core controls."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from ufo.harness.models.interface import AUTO_MODEL
from ufo.runtime.seats import create_member
from ufo.schema import tables
from ufo.schema.records import DEFAULT_AGENT_NAME, MAIN_AGENT_ICON, ReasoningEffort

DEFAULT_AGENT_PROMPT = "You are a helpful assistant."
DEFAULT_AGENT_MODEL = AUTO_MODEL
DEFAULT_AGENT_REASONING: ReasoningEffort = "medium"


@dataclass(frozen=True)
class Founded:
    """The workspace a seat just created and the member it seated first."""

    workspace_id: UUID
    member_id: UUID
    email: str


@dataclass(frozen=True)
class WorkspaceFoundedSpec:
    """A handler core runs on the founding transaction's connection once the first admin and the
    main agent are inserted; the agent then carries the default model and reasoning, which
    `ufoctl init` replaces with its choice after the handlers. It gets no `ExtensionContext`; an
    exception aborts the founding."""

    handler: Callable[[AsyncConnection, Founded], Awaitable[None]]


@dataclass(frozen=True)
class Seating:
    """What a seat read under the workspace lock, for its caller's `verify` to refuse: whether this
    call created the workspace row, and the address of the workspace's first member."""

    founded: bool
    first_email: str | None


@dataclass(frozen=True)
class Provisioned:
    """A seat's result. `founded`: this call created the workspace. `admin`: the member administers
    it — a new member only when no member existed before, an existing one as they stand."""

    workspace_id: UUID
    member_id: UUID
    founded: bool
    admin: bool


@dataclass(frozen=True)
class Provisioning:
    """The seat `ufoctl init` and `DeployContext.provision` run, with the deploy's founded
    handlers."""

    founded: tuple[WorkspaceFoundedSpec, ...]

    async def seat(
        self,
        connection: AsyncConnection,
        workspace_id: UUID,
        email: str,
        *,
        verify: Callable[[Seating], None] | None = None,
    ) -> Provisioned:
        """Found `workspace_id` when absent and seat `email` in it, on the caller's transaction.
        Idempotent: a repeat returns the member it seated, and founded handlers run only on the
        call that created the workspace row. `verify` may raise to refuse the seat, which aborts
        the caller's transaction."""
        insert = pg_insert if connection.dialect.name == "postgresql" else sqlite_insert
        founded = (
            await connection.execute(
                insert(tables.workspace)
                .values(id=workspace_id, created_at=sa.func.now(), updated_at=sa.func.now())
                .on_conflict_do_nothing(index_elements=[tables.workspace.c.id])
                .returning(tables.workspace.c.id)
            )
        ).one_or_none() is not None
        await connection.execute(
            sa.select(tables.workspace.c.id)
            .where(tables.workspace.c.id == workspace_id)
            .with_for_update()
        )
        first_email = (
            await connection.execute(
                sa.select(tables.member.c.email)
                .where(tables.member.c.workspace_id == workspace_id)
                .order_by(tables.member.c.created_at, tables.member.c.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if verify is not None:
            verify(Seating(founded=founded, first_email=first_email))
        address = email.lower()
        existing = (
            await connection.execute(
                sa.select(tables.member.c.id, tables.member.c.is_admin).where(
                    tables.member.c.workspace_id == workspace_id,
                    tables.member.c.email == address,
                )
            )
        ).one_or_none()
        if existing is not None:
            return Provisioned(
                workspace_id=workspace_id,
                member_id=existing.id,
                founded=founded,
                admin=existing.is_admin,
            )
        member_id = await create_member(
            connection, workspace_id, email, is_admin=first_email is None
        )
        await connection.execute(
            insert(tables.agent)
            .values(
                id=uuid4(),
                workspace_id=workspace_id,
                name=DEFAULT_AGENT_NAME,
                icon=MAIN_AGENT_ICON,
                prompt=DEFAULT_AGENT_PROMPT,
                model=DEFAULT_AGENT_MODEL,
                reasoning=DEFAULT_AGENT_REASONING,
                is_main=True,
                # The column defaults to `private`, which hides the main agent from every
                # non-admin member.
                visibility="workspace",
                created_at=sa.func.now(),
                updated_at=sa.func.now(),
            )
            .on_conflict_do_nothing()
        )
        if founded:
            for spec in self.founded:
                await spec.handler(
                    connection,
                    Founded(workspace_id=workspace_id, member_id=member_id, email=address),
                )
        return Provisioned(
            workspace_id=workspace_id,
            member_id=member_id,
            founded=founded,
            admin=first_email is None,
        )
