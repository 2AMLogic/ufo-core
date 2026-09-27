import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from ufo.sdk.workspaces import Founded
from ufo_ext_sample.tools import NOTE_TABLE

FOUNDING_NOTE = "founded {member_id} {email}"
REFUSED_FOUNDER = "refused@sample.test"


class FoundingRefused(RuntimeError):
    pass


async def record_founding(connection: AsyncConnection, founded: Founded) -> None:
    await connection.execute(
        sa.insert(NOTE_TABLE).values(
            workspace_id=founded.workspace_id,
            note=FOUNDING_NOTE.format(member_id=founded.member_id, email=founded.email),
        )
    )
    if founded.email == REFUSED_FOUNDER:
        raise FoundingRefused(f"the sample refuses the workspace {founded.email} founds")
