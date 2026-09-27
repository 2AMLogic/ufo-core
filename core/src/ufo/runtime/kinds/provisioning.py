"""Agents an extension ships, applied to a workspace.

An active extension declares `AgentProvision`s; this turns each into an ordinary `agent` row and
then stops owning it. The workspace's copy is the live configuration from that moment.

A shipped row is created once and never written again, with one exception: the two fields that are
the extension's own statement rather than the member's — the setup it declares, and the purpose
where the row has none — are carried forward on every pass, or a workspace that already holds the
row would never meet either again. Such a write moves the recorded version with it, so the version
always names the declaration the row carries. Everything else a later version of the extension
changes reaches new workspaces only, and the member's own edits are never overwritten.

A shipped agent is identified by the extension that ships it and the name that extension declared,
never by the row's own name. A name already in use — by a member's agent or by a second extension's
— sends the shipped agent to a free variant. Nothing is overwritten and nothing is stuck.

A main provision adopts the workspace's main agent without replacing its configuration or edges.
"""

from dataclasses import dataclass
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from ufo.db import workspace_tx
from ufo.runtime.ext.manifest import AgentProvision, Manifest
from ufo.runtime.kinds.agents import ARCHIVED_AGENT_NAME_PREFIX
from ufo.runtime.workspace import ws
from ufo.schema import tables
from ufo.schema.records import auto_agent_icon

CREATED = "created"
ADOPTED = "adopted"
PRESENT = "present"
FREE_NAME_LIMIT = 50


@dataclass(frozen=True)
class ProvisionOutcome:
    extension: str
    name: str
    result: str


@dataclass(frozen=True)
class AgentProvisioning:
    """The provisions the active manifests declare, applied to one workspace."""

    manifests: tuple[Manifest, ...]

    async def apply(self, workspace_id: UUID) -> tuple[ProvisionOutcome, ...]:
        with ws(workspace_id):
            return tuple(
                [
                    await self._one(workspace_id, manifest, provision)
                    for manifest in self.manifests
                    for provision in manifest.agents
                ]
            )

    async def _one(
        self, workspace_id: UUID, manifest: Manifest, provision: AgentProvision
    ) -> ProvisionOutcome:
        extension = manifest.name
        member_name = sa.func.coalesce(tables.agent.c.archived_name, tables.agent.c.name)
        async with workspace_tx() as connection:
            shipped = (
                await connection.execute(
                    sa.select(
                        tables.agent.c.id,
                        member_name.label("name"),
                        tables.agent.c.purpose,
                        tables.agent.c.setup,
                        tables.agent.c.is_main,
                        tables.agent.c.archived_at,
                    ).where(
                        tables.agent.c.workspace_id == workspace_id,
                        tables.agent.c.provisioned_by == extension,
                        tables.agent.c.provisioned_name == provision.name,
                    )
                )
            ).one_or_none()
            if shipped is not None and provision.main and not shipped.is_main:
                values: dict[str, object] = {
                    "provisioned_by": None,
                    "provisioned_name": None,
                    "provisioned_version": None,
                    "updated_at": sa.func.now(),
                }
                if shipped.archived_at is None:
                    values.update(
                        archived_name=shipped.name,
                        name=f"{ARCHIVED_AGENT_NAME_PREFIX}{shipped.id}",
                        archived_at=sa.func.now(),
                    )
                await connection.execute(
                    sa.update(tables.agent).where(tables.agent.c.id == shipped.id).values(**values)
                )
                shipped = None
            if shipped is not None:
                await self._fill(connection, shipped, manifest, provision)
                return ProvisionOutcome(extension, shipped.name, PRESENT)

            standing = (
                await connection.execute(
                    sa.select(
                        tables.agent.c.id,
                        tables.agent.c.name,
                        tables.agent.c.prompt,
                        tables.agent.c.model,
                        tables.agent.c.reasoning,
                        tables.agent.c.internet_access_allowed,
                        tables.agent.c.sandbox_size,
                        tables.agent.c.visibility,
                        tables.agent.c.tools,
                        tables.agent.c.purpose,
                    ).where(
                        tables.agent.c.workspace_id == workspace_id,
                        tables.agent.c.is_main.is_(True)
                        if provision.main
                        else tables.agent.c.name == provision.name,
                        tables.agent.c.provisioned_by.is_(None),
                        tables.agent.c.archived_at.is_(None),
                    )
                )
            ).one_or_none()
            if standing is not None and (provision.main or self._identical(standing, provision)):
                await connection.execute(
                    sa.update(tables.agent)
                    .values(
                        provisioned_by=extension,
                        provisioned_name=provision.name,
                        provisioned_version=manifest.version,
                        setup=provision.setup.model_dump(mode="json"),
                        purpose=standing.purpose or provision.spec.purpose,
                        updated_at=sa.func.now(),
                    )
                    .where(tables.agent.c.id == standing.id)
                )
                return ProvisionOutcome(extension, standing.name, ADOPTED)

            name = await self._free_name(connection, workspace_id, extension, provision.name)
            await self._create(connection, workspace_id, manifest, provision, name)
        return ProvisionOutcome(extension, name, CREATED)

    async def _fill(
        self,
        connection: AsyncConnection,
        shipped: sa.Row,
        manifest: Manifest,
        provision: AgentProvision,
    ) -> None:
        """A no-change pass writes nothing: this runs on a process's first turn for a workspace, and
        an update would lock rows that turn reads."""
        declared = provision.setup.model_dump(mode="json")
        values: dict[str, object] = {} if shipped.setup == declared else {"setup": declared}
        if shipped.purpose is None:
            values["purpose"] = provision.spec.purpose
        if not values:
            return
        await connection.execute(
            sa.update(tables.agent)
            .values(**values, provisioned_version=manifest.version, updated_at=sa.func.now())
            .where(tables.agent.c.id == shipped.id)
        )

    async def _free_name(
        self, connection: AsyncConnection, workspace_id: UUID, extension: str, declared: str
    ) -> str:
        """The object grammar has no underscore, so the suffix spells the extension without one."""
        taken = {
            row.name
            for row in await connection.execute(
                sa.select(tables.agent.c.name).where(
                    tables.agent.c.workspace_id == workspace_id,
                    tables.agent.c.archived_at.is_(None),
                )
            )
        }
        suffix = extension.replace("_", "-")
        for candidate in (
            declared,
            f"{declared}-{suffix}",
            *(f"{declared}-{suffix}-{count}" for count in range(2, FREE_NAME_LIMIT)),
        ):
            if candidate not in taken:
                return candidate
        raise ValueError(f"no free name for the {extension!r} agent {declared!r}")

    async def _create(
        self,
        connection: AsyncConnection,
        workspace_id: UUID,
        manifest: Manifest,
        provision: AgentProvision,
        name: str,
    ) -> None:
        """Two first turns of one workspace, in one process or two replicas, can both reach here;
        the unique provision identity settles which one writes."""
        spec = provision.spec
        icon = provision.icon
        if icon is None:
            taken = (
                (
                    await connection.execute(
                        sa.select(tables.agent.c.icon).where(
                            tables.agent.c.workspace_id == workspace_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            icon = auto_agent_icon(name, taken)
        insert = pg_insert if connection.dialect.name == "postgresql" else sqlite_insert
        await connection.execute(
            insert(tables.agent)
            .values(
                id=uuid4(),
                workspace_id=workspace_id,
                name=name,
                icon=icon,
                prompt=spec.prompt,
                purpose=spec.purpose,
                model=spec.model,
                reasoning=spec.reasoning,
                is_main=provision.main,
                internet_access_allowed=spec.internet_access_allowed,
                sandbox_size=spec.sandbox_size,
                visibility=spec.visibility,
                tools=list(provision.tools) if provision.tools is not None else None,
                provisioned_by=manifest.name,
                provisioned_name=provision.name,
                provisioned_version=manifest.version,
                setup=provision.setup.model_dump(mode="json"),
                created_at=sa.func.now(),
                updated_at=sa.func.now(),
            )
            .on_conflict_do_nothing()
        )

    def _identical(self, row: sa.Row, provision: AgentProvision) -> bool:
        """Whether a row the workspace already holds is the agent this provision would create. An
        identical row is adopted rather than duplicated under a second name."""
        spec = provision.spec
        stored = None if row.tools is None else tuple(row.tools)
        return (
            row.prompt == spec.prompt
            and row.model == spec.model
            and row.reasoning == spec.reasoning
            and row.internet_access_allowed == spec.internet_access_allowed
            and row.sandbox_size == spec.sandbox_size
            and row.visibility == spec.visibility
            and stored == provision.tools
        )
