"""memory's tables."""

import sqlalchemy as sa
from alembic import op

revision: str = "20260925191145"
down_revision: str | None = None
branch_labels: tuple[str, ...] | None = ("memory",)
depends_on: str | None = "20260927025054"

MEMORY_ITEM_PAGE_SOURCE = (
    "(created_from_page_uid is null and created_from_page_revision is null and source_uid is "
    "null) or (created_from_page_uid is not null and created_from_page_revision is not null and "
    "source_uid is not null)"
)
MEMORY_ITEM_SUBJECT = (
    "subject = 'shared' or subject like 'member:%' or subject like 'room:%:%' or subject like "
    "'foreign:%:%'"
)


def upgrade() -> None:
    op.create_table(
        "memory_item",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("body_digest", sa.Text(), nullable=True),
        sa.Column("item_class", sa.Text(), nullable=False),
        sa.Column("memory_kind", sa.Text(), server_default="fact", nullable=False),
        sa.Column("confidence", sa.Integer(), server_default="5", nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column("created_from_page_uid", sa.Uuid(), nullable=True),
        sa.Column("created_from_page_revision", sa.BigInteger(), nullable=True),
        sa.Column("source_uid", sa.Uuid(), nullable=True),
        sa.Column("created_from_conversation_id", sa.Uuid(), nullable=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=True),
        sa.Column("embedding_digest", sa.Text(), nullable=True),
        sa.Column("embedding_claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by", sa.Uuid(), nullable=True),
        sa.Column("overtaken_by", sa.Uuid(), nullable=True),
        sa.Column("deprecates", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("swept_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "item_class in ('fact', 'episodic', 'semantic', 'section', 'overview')",
            name="memory_item_class",
        ),
        sa.CheckConstraint(MEMORY_ITEM_SUBJECT, name="memory_item_subject"),
        sa.CheckConstraint(MEMORY_ITEM_PAGE_SOURCE, name="memory_item_page_source"),
        sa.CheckConstraint(
            "superseded_by is null or overtaken_by is null", name="memory_item_one_pointer"
        ),
        sa.ForeignKeyConstraint(
            ["overtaken_by"],
            ["memory_item.id"],
            name="memory_item_overtaken_by_fkey",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by"],
            ["memory_item.id"],
            name="memory_item_superseded_by_fkey",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="memory_item_workspace_id_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "memory_item_consolidate",
        "memory_item",
        ["workspace_id", "created_at"],
        unique=False,
        postgresql_where=sa.text("item_class = 'fact' and superseded_by is null"),
        sqlite_where=sa.text("item_class = 'fact' and superseded_by is null"),
    )
    op.create_index("memory_item_due", "memory_item", ["embedding_digest"], unique=False)
    op.create_index(
        "memory_item_inventory", "memory_item", ["workspace_id", "created_at"], unique=False
    )
    op.create_index(
        "memory_item_overtaken",
        "memory_item",
        ["overtaken_by"],
        unique=False,
        postgresql_where=sa.text("overtaken_by is not null"),
        sqlite_where=sa.text("overtaken_by is not null"),
    )
    op.create_index(
        "memory_item_page",
        "memory_item",
        ["workspace_id", "created_from_page_uid"],
        unique=False,
        postgresql_where=sa.text("created_from_page_uid is not null"),
        sqlite_where=sa.text("created_from_page_uid is not null"),
    )
    op.create_index(
        "memory_item_page_body",
        "memory_item",
        ["workspace_id", "subject", "item_class", "body_digest", "created_from_page_uid"],
        unique=True,
        postgresql_where=sa.text("created_from_page_uid is not null"),
        sqlite_where=sa.text("created_from_page_uid is not null"),
    )
    op.create_index(
        "memory_item_superseded",
        "memory_item",
        ["superseded_by"],
        unique=False,
        postgresql_where=sa.text("superseded_by is not null"),
        sqlite_where=sa.text("superseded_by is not null"),
    )
    op.create_index(
        "memory_item_unswept",
        "memory_item",
        ["workspace_id"],
        unique=False,
        postgresql_where=sa.text("deprecates is not null and swept_at is null"),
        sqlite_where=sa.text("deprecates is not null and swept_at is null"),
    )
    op.create_index(
        "memory_item_written_body",
        "memory_item",
        ["workspace_id", "subject", "item_class", "body_digest"],
        unique=True,
        postgresql_where=sa.text("created_from_page_uid is null"),
        sqlite_where=sa.text("created_from_page_uid is null"),
    )
    op.create_table(
        "memory_profile",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("focus", sa.Text(), nullable=False),
        sa.Column("written_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["member_id"], ["member.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id", "member_id"),
    )
    op.create_table(
        "workspace_export",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("active", sa.Integer(), nullable=True),
        sa.Column("blob_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status in ('queued', 'preparing', 'ready', 'failed', 'expired')",
            name="workspace_export_status",
        ),
        sa.CheckConstraint("active is null or active = 1", name="workspace_export_active_value"),
        sa.ForeignKeyConstraint(["member_id"], ["member.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "active", name="workspace_export_active"),
    )
    op.create_index(
        "workspace_export_created", "workspace_export", ["workspace_id", "created_at"], unique=False
    )
    op.create_index(
        "workspace_export_expiry", "workspace_export", ["status", "expires_at"], unique=False
    )
    op.create_table(
        "mem_page",
        sa.Column("page_uid", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "page_uid"],
            ["page.workspace_id", "page.uid"],
            name="mem_page_page_uid_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="mem_page_workspace_id_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("page_uid"),
    )


def downgrade() -> None:
    op.drop_table("mem_page")
    op.drop_index("workspace_export_expiry", table_name="workspace_export")
    op.drop_index("workspace_export_created", table_name="workspace_export")
    op.drop_table("workspace_export")
    op.drop_table("memory_profile")
    op.drop_index(
        "memory_item_written_body",
        table_name="memory_item",
        postgresql_where=sa.text("created_from_page_uid is null"),
        sqlite_where=sa.text("created_from_page_uid is null"),
    )
    op.drop_index(
        "memory_item_unswept",
        table_name="memory_item",
        postgresql_where=sa.text("deprecates is not null and swept_at is null"),
        sqlite_where=sa.text("deprecates is not null and swept_at is null"),
    )
    op.drop_index(
        "memory_item_superseded",
        table_name="memory_item",
        postgresql_where=sa.text("superseded_by is not null"),
        sqlite_where=sa.text("superseded_by is not null"),
    )
    op.drop_index(
        "memory_item_page_body",
        table_name="memory_item",
        postgresql_where=sa.text("created_from_page_uid is not null"),
        sqlite_where=sa.text("created_from_page_uid is not null"),
    )
    op.drop_index(
        "memory_item_page",
        table_name="memory_item",
        postgresql_where=sa.text("created_from_page_uid is not null"),
        sqlite_where=sa.text("created_from_page_uid is not null"),
    )
    op.drop_index(
        "memory_item_overtaken",
        table_name="memory_item",
        postgresql_where=sa.text("overtaken_by is not null"),
        sqlite_where=sa.text("overtaken_by is not null"),
    )
    op.drop_index("memory_item_inventory", table_name="memory_item")
    op.drop_index("memory_item_due", table_name="memory_item")
    op.drop_index(
        "memory_item_consolidate",
        table_name="memory_item",
        postgresql_where=sa.text("item_class = 'fact' and superseded_by is null"),
        sqlite_where=sa.text("item_class = 'fact' and superseded_by is null"),
    )
    op.drop_table("memory_item")
