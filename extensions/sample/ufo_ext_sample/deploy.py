from uuid import UUID

import sqlalchemy as sa
from pydantic import BaseModel

from ufo.sdk.deploy import MEMBER_MODEL_PROVIDERS, CommandRefused, DeployContext
from ufo.sdk.http import JSONResponse, Request, Response
from ufo.sdk.workspaces import Seating
from ufo_ext_sample.tools import NOTE_TABLE

SEAT_PATH = "seat"
FLEET_PATH = "fleet"
SEAT_KEY = "deploy:seat"
REFUSED_SEAT = "refused-seat@sample.test"
MEMBERSHIPS_COMMAND = "memberships"
FLAGS_COMMAND = "flags"
MODEL_PROVIDER = MEMBER_MODEL_PROVIDERS[0]


class SeatAsk(BaseModel):
    workspace_id: UUID
    email: str
    name: str | None = None
    model_key: str | None = None


class MembershipsParams(BaseModel):
    email: str


class FlagsParams(BaseModel):
    pass


class SeatRefused(Exception):
    pass


async def seat(ctx: DeployContext, request: Request) -> Response:
    """Seat an address, refusing `REFUSED_SEAT` inside the seat, then record what the seat wrote
    through the bound workspace: the founded handler's note read on its transaction, a name, a
    model key."""
    ask = SeatAsk.model_validate_json(await request.body())

    def held(seating: Seating) -> None:
        if ask.email == REFUSED_SEAT:
            raise SeatRefused(ask.email)

    try:
        provisioned = await ctx.provision(ask.workspace_id, ask.email, verify=held)
    except SeatRefused as refused:
        return JSONResponse({"detail": f"{refused} is refused"}, status_code=409)
    async with ctx.bound(ask.workspace_id) as bound:
        async with bound.transaction() as connection:
            note = (
                await connection.execute(
                    sa.select(NOTE_TABLE.c.note).where(
                        NOTE_TABLE.c.workspace_id == ask.workspace_id
                    )
                )
            ).scalar_one_or_none()
        if ask.name is not None:
            await bound.profiles.set_name(provisioned.member_id, ask.name, "signin")
        if ask.model_key is not None:
            await bound.put_member_model_key(provisioned.member_id, MODEL_PROVIDER, ask.model_key)
        await bound.store.put(
            SEAT_KEY,
            {"member_id": str(provisioned.member_id), "founded": provisioned.founded, "note": note},
        )
    return JSONResponse({"member_id": str(provisioned.member_id), "admin": provisioned.admin})


async def fleet(ctx: DeployContext, request: Request) -> Response:
    async with ctx.owner_transaction(audit=False) as connection:
        notes = (
            await connection.execute(sa.select(sa.func.count()).select_from(NOTE_TABLE))
        ).scalar_one()
    return JSONResponse({"workspaces": await ctx.workspace_count(audit=True), "notes": notes})


async def memberships(ctx: DeployContext, params: MembershipsParams) -> str:
    found = await ctx.memberships(params.email, audit=True)
    if not found:
        raise CommandRefused(f"{params.email} is a member of no workspace.")
    return " ".join(str(membership.workspace_id) for membership in found)


async def deploy_flags(ctx: DeployContext, _params: FlagsParams) -> str:
    return " ".join((str(ctx.flag_backend), *sorted(ctx.flag_keys)))
