"""pulse's tables: the historical record a brief series builds, one row at a time.

**Why a table.** The record used to be a JSON Lines file at a workspace-relative path, which
resolves against the working directory the turn's carrier happens to start in. A conversation whose
`sandbox_handle` is `client:<cwd>` runs on the member's own machine in that directory; any other
conversation gets `workspace_root/<conversation_id>`. So one series accumulated one ledger per tree,
and none of them knew about the others. Measured on the demo deploy: the
`agent-runtimes` series had two `covered.jsonl` files, 15 rows each, and for edition 2026-09-28 they
shared **no story at all** — six rows in each, zero overlap. Both were plausible, and the no-repeat
rule was enforced against whichever half the running carrier could see.

A row keyed by `(workspace_id, series, ...)` is reachable identically from every turn in the
workspace, whatever carrier ran it and whatever directory it started in. That is the property the
file lacked and the whole reason these exist.

The sighting and covered keys are natural rather than surrogate: the key is what a second write of
the same fact would carry, so a retried fire, a crash replay, or a gather that surfaces one lead
twice lands one row rather than a duplicate. That also makes importing an old file safe to repeat,
which is how a deploy carrying divergent ledgers merges them.

`pulse_ext_series` is the third table and a different kind: not record but bookkeeping. It names the
conversation a series lives in and how far the workspace-file projection has caught up, so the
projection job opens a sandbox only for a series whose record has actually advanced.


**What each key means.** One lead seen once on one day at one address is one sighting; the url is in
the key because a story that moves address between sightings is a different row to reconcile, which is
the question `reconcile` exists to ask and this must not answer for it. A covered row is one row per
story per edition, exactly as the ledger has always meant it: a slug carried again under the
material-new-development exception is a row in each edition that carried it, and one edition cannot
carry a story twice.

**The series stamp is a counter, not a clock.** Dueness is `projected_revision < revision`, and a
wall-clock watermark makes that comparison only as monotonic as the clock behind it — a write stamped
earlier than an already-recorded projection reads as older than the file, so its rows land in the
record and the projection never comes back for them. `revision = revision + 1` cannot go backwards
under skew, a replayed job, or two writers disagreeing about now.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "pulse_0001"
down_revision: str | None = None
branch_labels: tuple[str, ...] | None = ("pulse",)
depends_on: str | None = "20260927025054"


def upgrade() -> None:
    op.create_table(
        "pulse_ext_sighting",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("series", sa.Text(), nullable=False),
        sa.Column("seen", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id", "series", "seen", "slug", "url"),
    )
    op.create_table(
        "pulse_ext_covered",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("series", sa.Text(), nullable=False),
        sa.Column("edition", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id", "series", "edition", "slug"),
    )
    op.create_table(
        "pulse_ext_series",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("series", sa.Text(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        # Null until the first projection lands; otherwise the revision the file was rendered from.
        sa.Column("projected_revision", sa.BigInteger(), nullable=True),
        # Informational only — for a human reading the table, never for dueness.
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id", "series"),
    )


def downgrade() -> None:
    op.drop_table("pulse_ext_series")
    op.drop_table("pulse_ext_covered")
    op.drop_table("pulse_ext_sighting")
