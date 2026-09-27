"""monitors's tables."""

import sqlalchemy as sa
from alembic import op

revision: str = "monitors_0004"
down_revision: str | None = None
branch_labels: tuple[str, ...] | None = ("monitors",)
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
        "monitor",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("audience", sa.Text(), nullable=True),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("interval_minutes", sa.Integer(), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("next_steps", sa.Text(), nullable=False),
        sa.Column("metadata", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("user_description", sa.Text(), nullable=False),
        sa.Column("created_by_member_id", sa.Uuid(), nullable=True),
        sa.Column("requesting_message_ref", sa.Uuid(), nullable=True),
        sa.Column("connections", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("internet_access", sa.Boolean(), nullable=False),
        sa.Column("baseline", sa.Text(), nullable=False),
        sa.Column("probes_run", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("quiet_streak", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("failure_streak", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("skipped", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_probe_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_probe_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("interval_minutes >= 1", name="monitor_interval"),
        sa.ForeignKeyConstraint(["agent_id"], ["agent.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversation.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_member_id"], ["member.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "name", name="monitor_name"),
    )
    op.create_index("monitor_due", "monitor", ["next_probe_at", "deadline_at"], unique=False)
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(INTERNET_SCOPE_FUNCTION.format(name="monitor_internet_scope"))
    op.execute(INTERNET_SCOPE_TRIGGER.format(name="monitor_internet_scope", table="monitor"))


def downgrade() -> None:
    op.drop_index("monitor_due", table_name="monitor")
    op.drop_table("monitor")
