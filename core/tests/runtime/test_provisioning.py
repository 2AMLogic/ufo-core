from collections.abc import Callable, Sequence
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
import ufo_ext_sample.manifest as sample
from sqlalchemy.ext.asyncio import AsyncConnection
from ufo_ext_sample.provisioning import FOUNDING_NOTE, REFUSED_FOUNDER, FoundingRefused
from ufo_ext_sample.tools import NOTE_TABLE

from ufo.db import workspace_tx
from ufo.host.ext import loader
from ufo.host.ext.loader import load_manifests
from ufo.runtime.ext.manifest import Manifest
from ufo.runtime.provisioning import (
    DEFAULT_AGENT_MODEL,
    DEFAULT_AGENT_PROMPT,
    DEFAULT_AGENT_REASONING,
    Founded,
    Provisioned,
    Provisioning,
    Seating,
    WorkspaceFoundedSpec,
)
from ufo.runtime.workspace import ws
from ufo.schema import tables
from ufo.schema.records import DEFAULT_AGENT_NAME, MAIN_AGENT_ICON

FOUNDER = "founder@acme.test"
TEAMMATE = "teammate@acme.test"


class Refused(RuntimeError):
    pass


def _sample_provisioning() -> Provisioning:
    manifest = next(m for m in load_manifests() if m.name == sample.NAME)
    return Provisioning(founded=manifest.workspace_founded)


async def _seat(
    provisioning: Provisioning,
    workspace_id: UUID,
    email: str,
    verify: Callable[[Seating], None] | None = None,
) -> Provisioned:
    with ws(workspace_id):
        async with workspace_tx() as connection:
            return await provisioning.seat(connection, workspace_id, email, verify=verify)


async def _rows(workspace_id: UUID) -> SimpleNamespace:
    with ws(workspace_id):
        async with workspace_tx() as connection:
            return SimpleNamespace(
                workspaces=(
                    await connection.execute(
                        sa.select(tables.workspace.c.id).where(
                            tables.workspace.c.id == workspace_id
                        )
                    )
                ).all(),
                members=(
                    await connection.execute(
                        sa.select(
                            tables.member.c.id, tables.member.c.email, tables.member.c.is_admin
                        )
                        .where(tables.member.c.workspace_id == workspace_id)
                        .order_by(tables.member.c.created_at, tables.member.c.id)
                    )
                ).all(),
                agents=(
                    await connection.execute(
                        sa.select(tables.agent).where(tables.agent.c.workspace_id == workspace_id)
                    )
                ).all(),
                notes=(
                    await connection.execute(
                        sa.select(NOTE_TABLE.c.note).where(
                            NOTE_TABLE.c.workspace_id == workspace_id
                        )
                    )
                )
                .scalars()
                .all(),
            )


async def test_a_seat_founds_the_workspace_its_admin_and_its_main_agent(db: None) -> None:
    workspace_id = uuid4()
    provisioned = await _seat(Provisioning(founded=()), workspace_id, "Founder@Acme.test")
    rows = await _rows(workspace_id)
    assert rows.workspaces == [(workspace_id,)]
    assert [(email, admin) for _, email, admin in rows.members] == [(FOUNDER, True)]
    assert provisioned == Provisioned(
        workspace_id=workspace_id, member_id=rows.members[0].id, founded=True, admin=True
    )
    (agent,) = rows.agents
    assert (
        agent.name,
        agent.icon,
        agent.prompt,
        agent.model,
        agent.reasoning,
        agent.is_main,
        agent.visibility,
    ) == (
        DEFAULT_AGENT_NAME,
        MAIN_AGENT_ICON,
        DEFAULT_AGENT_PROMPT,
        DEFAULT_AGENT_MODEL,
        DEFAULT_AGENT_REASONING,
        True,
        "workspace",
    )


async def test_a_repeat_seat_returns_the_member_it_seated(db: None) -> None:
    workspace_id = uuid4()
    provisioning = Provisioning(founded=())
    first = await _seat(provisioning, workspace_id, FOUNDER)
    again = await _seat(provisioning, workspace_id, FOUNDER)
    rows = await _rows(workspace_id)
    assert again == Provisioned(
        workspace_id=workspace_id, member_id=first.member_id, founded=False, admin=True
    )
    assert len(rows.members) == 1
    assert len(rows.agents) == 1


async def test_a_later_member_joins_the_workspace_without_administering_it(db: None) -> None:
    workspace_id = uuid4()
    provisioning = Provisioning(founded=())
    await _seat(provisioning, workspace_id, FOUNDER)
    joined = await _seat(provisioning, workspace_id, TEAMMATE)
    rows = await _rows(workspace_id)
    assert (joined.founded, joined.admin) == (False, False)
    assert {email: admin for _, email, admin in rows.members} == {FOUNDER: True, TEAMMATE: False}
    assert len(rows.agents) == 1


async def test_a_refused_seating_founds_nothing(db: None) -> None:
    workspace_id = uuid4()
    provisioning = Provisioning(founded=())
    seen: list[Seating] = []

    def refuse(seating: Seating) -> None:
        seen.append(seating)
        raise Refused(FOUNDER)

    with pytest.raises(Refused):
        await _seat(provisioning, workspace_id, FOUNDER, refuse)
    rows = await _rows(workspace_id)
    assert seen == [Seating(founded=True, first_email=None)]
    assert (rows.workspaces, rows.members, rows.agents) == ([], [], [])


async def test_verify_reads_the_first_member_of_a_workspace_that_stands(db: None) -> None:
    workspace_id = uuid4()
    provisioning = Provisioning(founded=())
    await _seat(provisioning, workspace_id, FOUNDER)
    seen: list[Seating] = []
    await _seat(provisioning, workspace_id, TEAMMATE, seen.append)
    assert seen == [Seating(founded=False, first_email=FOUNDER)]


async def test_the_sample_founded_handler_runs_once_inside_the_founding(db: None) -> None:
    workspace_id = uuid4()
    provisioning = _sample_provisioning()
    founded = await _seat(provisioning, workspace_id, FOUNDER)
    await _seat(provisioning, workspace_id, FOUNDER)
    await _seat(provisioning, workspace_id, TEAMMATE)
    rows = await _rows(workspace_id)
    assert rows.notes == [FOUNDING_NOTE.format(member_id=founded.member_id, email=FOUNDER)]


async def test_the_founded_handlers_see_the_admin_and_the_main_agent(db: None) -> None:
    workspace_id = uuid4()
    seen: list[tuple[Sequence[sa.Row[tuple[UUID, bool]]], Sequence[bool]]] = []

    async def observe(connection: AsyncConnection, founded: Founded) -> None:
        members = await connection.execute(
            sa.select(tables.member.c.id, tables.member.c.is_admin).where(
                tables.member.c.workspace_id == founded.workspace_id
            )
        )
        agents = await connection.execute(
            sa.select(tables.agent.c.is_main).where(
                tables.agent.c.workspace_id == founded.workspace_id
            )
        )
        seen.append((members.all(), agents.scalars().all()))

    provisioned = await _seat(
        Provisioning(founded=(WorkspaceFoundedSpec(handler=observe),)), workspace_id, FOUNDER
    )
    assert seen == [([(provisioned.member_id, True)], [True])]


async def test_a_founded_handler_that_raises_aborts_the_founding(db: None) -> None:
    workspace_id = uuid4()
    with pytest.raises(FoundingRefused):
        await _seat(_sample_provisioning(), workspace_id, REFUSED_FOUNDER)
    rows = await _rows(workspace_id)
    assert (rows.workspaces, rows.members, rows.agents, rows.notes) == ([], [], [], [])


def test_a_third_party_extension_cannot_declare_a_founded_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _founded(connection: AsyncConnection, founded: Founded) -> None:
        return None

    entry = SimpleNamespace(
        load=lambda: (
            lambda: Manifest(
                name="outside",
                version="1",
                workspace_founded=(WorkspaceFoundedSpec(handler=_founded),),
            )
        ),
        dist=SimpleNamespace(name="outside-package"),
    )
    monkeypatch.setattr(loader, "entry_points", lambda group: (entry,))
    with pytest.raises(ValueError, match="third-party extension 'outside'"):
        loader.discovered()
