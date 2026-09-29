"""matrix's claims on a member's MXID, and the links proved or undone by them."""

import sqlalchemy as sa
from alembic import op

revision: str = "matrix_0002"
down_revision: str | None = "matrix_0001"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "matrix_ext_claim",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("mxid", sa.Text(), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=False),
        sa.Column("code_hash", sa.LargeBinary(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id", "mxid"),
    )
    op.create_table(
        "matrix_ext_link",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("mxid", sa.Text(), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=True),
        sa.Column("proved_by", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id", "mxid"),
    )


def downgrade() -> None:
    op.drop_table("matrix_ext_link")
    op.drop_table("matrix_ext_claim")
