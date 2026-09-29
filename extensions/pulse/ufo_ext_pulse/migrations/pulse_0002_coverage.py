"""pulse's third store: what each source returned on each gather.

**Why a table, for the reason `pulse_0001` gives for the other two.** A coverage row lived at
`pulse/<series>.coverage.jsonl`, a workspace-relative path, which resolves against the working
directory the turn's carrier started in. A conversation bound to a terminal runs on the member's own
machine in that directory; any other conversation gets `workspace_root/<conversation_id>`. So one
series accumulated one set of source states per tree, and an edition assembled from a different
conversation wrote its footer from whichever set the running carrier could see — the same split
`pulse_0001` measured as two `covered.jsonl` files of 15 rows each with no story in common. A footer
is the one thing in a brief that claims what the series could and could not read, so a footer built
from half the states is the claim `coverage-honesty` exists to prevent.

**The key is `(workspace_id, series, gathered, source)`**, natural like the two beside it: it is
what a second write of the same fact carries, so a retried fire or a crash replay lands one row
rather than a duplicate.

**A retry inside one gather is what decides that key.** A source that rate-limited the first attempt
and answered the second was read that day, and the file records both rows and resolves them on read
— the later row is that gather's answer. The table resolves the same pair on write instead: the
second attempt replaces the first, and `coverage.py`'s window aggregates to the identical states
either way, because both surfaces take the later row. What is lost is the fact that a first attempt
failed, which no footer has ever stated and which a state per gather per source cannot express
anyway.

The row's content is `coverage-honesty`'s three states: `read` with an item count, `read-empty`,
and `not-read` naming which of the four failures kept the source out. `items` and `reason` are the
halves of those two distinctions, and both columns are always present because a row that omitted one
would read as a state nobody set.


One source's answer on one gather is one row. The source is keyed by its slug rather than by the words
a footer used, which is what lets three days of one source aggregate as one source rather than three.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "pulse_0002"
down_revision: str | None = "pulse_0001"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "pulse_ext_coverage",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("series", sa.Text(), nullable=False),
        sa.Column("gathered", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("items", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id", "series", "gathered", "source"),
    )


def downgrade() -> None:
    op.drop_table("pulse_ext_coverage")
