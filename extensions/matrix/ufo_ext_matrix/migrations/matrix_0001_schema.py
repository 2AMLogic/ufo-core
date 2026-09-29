"""matrix's tables."""

import sqlalchemy as sa
from alembic import op

revision: str = "matrix_0001"
down_revision: str | None = None
branch_labels: tuple[str, ...] | None = ("matrix",)
depends_on: str | None = "20260927025054"


def upgrade() -> None:
    op.create_table(
        "matrix_ext_since",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("installation_id", sa.Text(), nullable=False),
        sa.Column("since", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id"),
    )


def downgrade() -> None:
    op.drop_table("matrix_ext_since")
