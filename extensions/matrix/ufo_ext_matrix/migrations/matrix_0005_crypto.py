"""matrix's crypto store."""

import sqlalchemy as sa
from alembic import op

revision: str = "matrix_0005"
down_revision: str | None = "matrix_0004"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "matrix_ext_crypto",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("device_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("value", sa.LargeBinary(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id", "user_id", "device_id", "kind", "name"),
    )


def downgrade() -> None:
    op.drop_table("matrix_ext_crypto")
