"""sources's tables."""

import sqlalchemy as sa
from alembic import op

revision: str = "sources_0011"
down_revision: str | None = None
branch_labels: tuple[str, ...] | None = ("sources",)
depends_on: str | None = "20260927025054"

INTERNET_SCOPE_FUNCTION = """
CREATE FUNCTION {name}()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.internet_access IS NULL THEN
        SELECT COALESCE(
            bool_and(
                COALESCE((runtime_config ->> 'internet_access')::boolean, true)
            ),
            false
        )
        INTO NEW.internet_access
        FROM turn
        WHERE workspace_id = NEW.workspace_id
          AND conversation_id = NEW.conversation_id
          AND status = 'running';
    END IF;
    RETURN NEW;
END;
$$
"""
INTERNET_SCOPE_TRIGGER = """
CREATE TRIGGER {name}
BEFORE INSERT ON {table}
FOR EACH ROW EXECUTE FUNCTION {name}()
"""


def upgrade() -> None:
    op.create_table(
        "source_trigger",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("when", sa.Text(), nullable=False),
        sa.Column("fault", sa.Text(), nullable=True),
        sa.Column("delivery", sa.Text(), server_default="current", nullable=False),
        sa.Column("resource", sa.Text(), server_default="", nullable=False),
        sa.Column("streams", sa.Text(), server_default="", nullable=False),
        sa.Column("paused", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_by_member_id", sa.Uuid(), nullable=True),
        sa.Column("requesting_message_ref", sa.Uuid(), nullable=True),
        sa.Column("internet_access", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agent.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["connection_id"], ["connection.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversation.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_member_id"], ["member.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "name", name="source_trigger_name"),
    )
    op.create_index(
        "source_trigger_connection",
        "source_trigger",
        ["workspace_id", "connection_id"],
        unique=False,
    )
    op.create_table(
        "source_trigger_match",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("trigger_id", sa.Uuid(), nullable=False),
        sa.Column("page_uid", sa.Uuid(), nullable=False),
        sa.Column("clause", sa.SmallInteger(), nullable=False),
        sa.Column("met", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["trigger_id"], ["source_trigger.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint(
            "trigger_id", "page_uid", "clause", name="source_trigger_match_pkey"
        ),
    )
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(INTERNET_SCOPE_FUNCTION.format(name="source_trigger_internet_scope"))
    op.execute(
        INTERNET_SCOPE_TRIGGER.format(name="source_trigger_internet_scope", table="source_trigger")
    )


def downgrade() -> None:
    op.drop_table("source_trigger_match")
    op.drop_index("source_trigger_connection", table_name="source_trigger")
    op.drop_table("source_trigger")
