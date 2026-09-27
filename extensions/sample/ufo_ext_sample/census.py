from datetime import datetime

import sqlalchemy as sa

from ufo.sdk.manifest import CensusRefs
from ufo_ext_sample.tools import NOTE_TABLE

NOTED_STAGE = "sample_noted"
NOTED_STEP = "sample_note_written"
NOTER_CHATTED_STEP = "sample_noter_chatted"


def first_noted(refs: CensusRefs) -> sa.ColumnElement[datetime | None]:
    return (
        sa.select(sa.func.min(NOTE_TABLE.c.noted_at))
        .where(NOTE_TABLE.c.workspace_id == refs.workspace_id)
        .scalar_subquery()
    )


def first_noter_turn(refs: CensusRefs) -> sa.ColumnElement[datetime | None]:
    return (
        sa.select(sa.func.min(refs.member_first_turn(NOTE_TABLE.c.member_id)))
        .where(NOTE_TABLE.c.workspace_id == refs.workspace_id)
        .scalar_subquery()
    )
