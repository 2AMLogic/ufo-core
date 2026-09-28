"""Which room message each turn answers."""

import sqlalchemy as sa
from alembic import op

revision: str = "matrix_0003"
down_revision: str | None = "matrix_0002"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "matrix_ext_answering",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("room_id", sa.Text(), nullable=False),
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("thread_root", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id", "turn_id"),
    )


def downgrade() -> None:
    op.drop_table("matrix_ext_answering")
