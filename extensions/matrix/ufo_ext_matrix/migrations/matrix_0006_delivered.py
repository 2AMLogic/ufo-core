"""Where a member's shared file landed, keyed on the event that carried it."""

import sqlalchemy as sa
from alembic import op

revision: str = "matrix_0006"
down_revision: str | None = "matrix_0005"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "matrix_ext_delivered",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("rel", sa.Text(), nullable=False),
        sa.Column("blob_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id", "event_id"),
    )


def downgrade() -> None:
    op.drop_table("matrix_ext_delivered")
