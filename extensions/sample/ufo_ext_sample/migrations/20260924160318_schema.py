"""sample's tables."""

import sqlalchemy as sa
from alembic import op

revision: str = "20260924160318"
down_revision: str | None = None
branch_labels: tuple[str, ...] | None = ("sample_ext",)
depends_on: str | None = "20260927025054"


def upgrade() -> None:
    op.create_table(
        "sample_ext_allowance",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("remaining_micro_usd", sa.BigInteger(), nullable=False),
        sa.Column("on_empty", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id"),
    )
    op.create_table(
        "sample_ext_charge",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=True),
        sa.Column("dimension", sa.Text(), nullable=False),
        sa.Column("delta_micro_usd", sa.BigInteger(), nullable=False),
        sa.Column("platform_paid", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "sample_ext_charge_workspace", "sample_ext_charge", ["workspace_id"], unique=False
    )
    op.create_table(
        "sample_ext_note",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=True),
        sa.Column("noted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["member_id"], ["member.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id"),
    )


def downgrade() -> None:
    op.drop_table("sample_ext_note")
    op.drop_index("sample_ext_charge_workspace", table_name="sample_ext_charge")
    op.drop_table("sample_ext_charge")
    op.drop_table("sample_ext_allowance")
