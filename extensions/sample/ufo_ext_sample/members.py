import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from ufo.sdk.seats import MemberAdded, MemberAddedSpec
from ufo_ext_sample.tools import NOTE_TABLE

ADDED_NOTE = "added {member_id} {email} by {added_by} notify={notify}"
REFUSED_INVITEE = "refused@sample.test"
NOTIFY_DESCRIPTION = "Whether the sample extension records the add as one to tell them about."
ADD_DESCRIPTION = "The sample extension records each add."
RECORDED_NOTICE = "The sample recorded the add."
RECORDED_TO_TELL_NOTICE = "The sample recorded the add as one to tell them about."


class AddRefused(RuntimeError):
    pass


async def record_added(connection: AsyncConnection, added: MemberAdded) -> None:
    """Write the add into the sample's own table on `add_member`'s connection, then refuse
    `REFUSED_INVITEE`, so a test proves a refusal takes that write and the member with it."""
    note = ADDED_NOTE.format(
        member_id=added.member_id,
        email=added.email,
        added_by=added.added_by,
        notify=added.notify,
    )
    updated = await connection.execute(
        sa.update(NOTE_TABLE)
        .values(note=note)
        .where(NOTE_TABLE.c.workspace_id == added.workspace_id)
    )
    if updated.rowcount == 0:
        await connection.execute(
            sa.insert(NOTE_TABLE).values(workspace_id=added.workspace_id, note=note)
        )
    if added.email == REFUSED_INVITEE:
        raise AddRefused(f"the sample refuses the add of {added.email}")


MEMBER_ADDED = MemberAddedSpec(
    handler=record_added,
    notify_description=NOTIFY_DESCRIPTION,
    description=ADD_DESCRIPTION,
    notice=lambda notify: RECORDED_TO_TELL_NOTICE if notify else RECORDED_NOTICE,
)
