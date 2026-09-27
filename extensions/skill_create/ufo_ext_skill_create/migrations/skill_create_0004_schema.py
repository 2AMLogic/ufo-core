"""skill_create's tables."""

import sqlalchemy as sa
from alembic import op

revision: str = "skill_create_0004"
down_revision: str | None = None
branch_labels: tuple[str, ...] | None = ("skill_create",)
depends_on: str | None = "20260927025054"


def upgrade() -> None:
    op.create_table(
        "user_skill",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("generation", sa.Uuid(), nullable=False),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("depends", sa.Text(), server_default="[]", nullable=False),
        sa.Column("agents", sa.Text(), nullable=False),
        sa.Column("pinned", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("indexed_digest", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id", "name"),
    )


def downgrade() -> None:
    op.drop_table("user_skill")
