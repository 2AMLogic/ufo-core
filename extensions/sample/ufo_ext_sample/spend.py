import time
from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from ufo.sdk.spend import (
    ALLOW,
    PARK,
    REJECT,
    Charge,
    GateDeploy,
    SpendAsk,
    SpendDecision,
    SustainDecision,
)

SPEND_GATE = "sample_allowance"
EXEMPT_ACTION = "sample_exempt"
RAISE = "raise"
HOLD = "hold"
ABSENT_TTL_SECONDS = 5.0

OnEmpty = Literal["park", "reject", "hold", "raise"]

ALLOWANCE_TABLE = sa.Table(
    "sample_ext_allowance",
    sa.MetaData(),
    sa.Column(
        "workspace_id",
        sa.Uuid(),
        sa.ForeignKey("workspace.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("remaining_micro_usd", sa.BigInteger(), nullable=False),
    sa.Column("on_empty", sa.Text(), nullable=False),
)
CHARGE_TABLE = sa.Table(
    "sample_ext_charge",
    sa.MetaData(),
    sa.Column("id", sa.Uuid(), primary_key=True),
    sa.Column(
        "workspace_id",
        sa.Uuid(),
        sa.ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("ledger_id", sa.Uuid(), nullable=False),
    sa.Column("turn_id", sa.Uuid()),
    sa.Column("dimension", sa.Text(), nullable=False),
    sa.Column("delta_micro_usd", sa.BigInteger(), nullable=False),
    sa.Column("platform_paid", sa.Boolean(), nullable=False),
    sa.Index("sample_ext_charge_workspace", "workspace_id"),
)

_unmetered: dict[UUID, float] = {}


class AllowanceRaised(RuntimeError):
    pass


async def allow(
    connection: AsyncConnection, workspace_id: UUID, remaining_micro_usd: int, on_empty: OnEmpty
) -> None:
    """Set what `workspace_id` may still spend and what the gate does once it is spent."""
    await connection.execute(sa.delete(ALLOWANCE_TABLE).where(_of(workspace_id)))
    await connection.execute(
        sa.insert(ALLOWANCE_TABLE).values(
            workspace_id=workspace_id,
            remaining_micro_usd=remaining_micro_usd,
            on_empty=on_empty,
        )
    )
    _unmetered.pop(workspace_id, None)


def _of(workspace_id: UUID) -> sa.ColumnElement[bool]:
    return ALLOWANCE_TABLE.c.workspace_id == workspace_id


@dataclass(frozen=True)
class SampleGate:
    """A workspace with no allowance row spends freely; one with a row spends until it is gone,
    then its `on_empty` decides: `hold` parks a member's own message and rejects anything else.
    Every charge the ledger books is logged and, platform-paid, taken off the row, so a test reads
    back exactly what core told the gate."""

    deploy: GateDeploy

    async def admit(self, connection: AsyncConnection, ask: SpendAsk) -> SpendDecision:
        row = await self._allowance(connection, ask.workspace_id)
        if row is None or ask.self_funded or row.remaining_micro_usd > 0:
            return SpendDecision(outcome=ALLOW, message="")
        if ask.intent is not None and ask.intent.action == EXEMPT_ACTION:
            return SpendDecision(outcome=ALLOW, message="")
        parks = row.on_empty == PARK or (row.on_empty == HOLD and ask.member_admission)
        return SpendDecision(outcome=PARK if parks else REJECT, message=self._spent(ask.moment))

    async def sustain(
        self,
        connection: AsyncConnection,
        workspace_id: UUID,
        turn_id: UUID,
        pending_micro_usd: int,
    ) -> SustainDecision:
        row = await self._allowance(connection, workspace_id)
        if row is None or row.remaining_micro_usd - pending_micro_usd > 0:
            return SustainDecision(outcome=ALLOW, message="")
        return SustainDecision(outcome=PARK, message=self._spent("round"))

    def admits_sql(
        self, workspace_id: sa.ColumnElement[UUID], self_funded: sa.ColumnElement[bool]
    ) -> sa.ColumnElement[bool]:
        spent = sa.select(sa.literal(1)).where(
            ALLOWANCE_TABLE.c.workspace_id == workspace_id,
            ALLOWANCE_TABLE.c.remaining_micro_usd <= 0,
            ~self_funded,
        )
        return ~sa.exists(spent)

    async def charged(self, connection: AsyncConnection, charge: Charge) -> None:
        row = await self._allowance(connection, charge.workspace_id)
        await connection.execute(
            sa.insert(CHARGE_TABLE).values(
                id=uuid4(),
                workspace_id=charge.workspace_id,
                ledger_id=charge.ledger_id,
                turn_id=charge.turn_id,
                dimension=charge.dimension,
                delta_micro_usd=charge.delta_micro_usd,
                platform_paid=charge.platform_paid,
            )
        )
        if row is None or not charge.platform_paid:
            return
        await connection.execute(
            sa.update(ALLOWANCE_TABLE)
            .where(_of(charge.workspace_id))
            .values(
                remaining_micro_usd=ALLOWANCE_TABLE.c.remaining_micro_usd - charge.delta_micro_usd
            )
        )

    def absent(self, workspace_id: UUID) -> bool:
        expiry = _unmetered.get(workspace_id)
        return expiry is not None and expiry > time.monotonic()

    async def _allowance(
        self, connection: AsyncConnection, workspace_id: UUID
    ) -> sa.Row[tuple[int, str]] | None:
        row = (
            await connection.execute(
                sa.select(ALLOWANCE_TABLE.c.remaining_micro_usd, ALLOWANCE_TABLE.c.on_empty).where(
                    _of(workspace_id)
                )
            )
        ).one_or_none()
        if row is None:
            _unmetered[workspace_id] = time.monotonic() + ABSENT_TTL_SECONDS
            return None
        _unmetered.pop(workspace_id, None)
        if row.on_empty == RAISE:
            raise AllowanceRaised(f"the sample gate refuses to decide for {workspace_id}")
        return row

    def _spent(self, moment: str) -> str:
        spent = f"The sample allowance is spent at {moment}."
        if self.deploy.public_base_url is None or self.deploy.home_surface is None:
            return spent
        return (
            f"{spent} Add allowance at "
            f"{self.deploy.public_base_url.rstrip('/')}/surface/{self.deploy.home_surface}"
        )
