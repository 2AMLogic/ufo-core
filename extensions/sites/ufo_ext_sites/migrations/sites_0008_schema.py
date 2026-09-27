"""sites's tables."""

import sqlalchemy as sa
from alembic import op

revision: str = "sites_0008"
down_revision: str | None = None
branch_labels: tuple[str, ...] | None = ("sites",)
depends_on: str | None = "20260927025054"


def upgrade() -> None:
    op.create_table(
        "hosted_site",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("visibility", sa.Text(), nullable=False),
        sa.Column("creator_member_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Uuid(), nullable=False),
        sa.Column("deploy_generation", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("homepage_agent_id", sa.Uuid(), nullable=True),
        sa.Column("preview_blob_key", sa.Text(), nullable=True),
        sa.Column("preview_size_bytes", sa.Integer(), nullable=True),
        sa.Column("share_card_blob_key", sa.Text(), nullable=True),
        sa.Column("share_card_hash", sa.Text(), nullable=True),
        sa.Column("source_manifest", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "visibility in ('private', 'workspace', 'public')", name="hosted_site_visibility"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id", "conversation_id", "name"),
    )
    op.create_index(
        "hosted_site_homepage_agent",
        "hosted_site",
        ["workspace_id", "homepage_agent_id"],
        unique=True,
        postgresql_where=sa.text("homepage_agent_id is not null"),
        sqlite_where=sa.text("homepage_agent_id is not null"),
    )
    op.create_index(
        "hosted_site_origin",
        "hosted_site",
        ["workspace_id", "conversation_id", "port"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("hosted_site_origin", table_name="hosted_site")
    op.drop_index(
        "hosted_site_homepage_agent",
        table_name="hosted_site",
        postgresql_where=sa.text("homepage_agent_id is not null"),
        sqlite_where=sa.text("homepage_agent_id is not null"),
    )
    op.drop_table("hosted_site")
