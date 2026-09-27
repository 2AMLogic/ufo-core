"""scheduled_tasks's tables."""

import sqlalchemy as sa
from alembic import op

revision: str = "scheduled_tasks_0004"
down_revision: str | None = None
branch_labels: tuple[str, ...] | None = ("scheduled_tasks",)
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
PAUSE_CLAIM_GUC = "app.scope_preserving_pause_claim"
PAUSE_GENERATION_SCOPE_RESET = f"""
CREATE FUNCTION pause_generation_scope_reset()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id THEN
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
    IF NEW.claimed_by IS NOT NULL
       AND NEW.claimed_by IS DISTINCT FROM OLD.claimed_by
       AND current_setting('{PAUSE_CLAIM_GUC}', true) IS DISTINCT FROM 'true'
    THEN
        RAISE EXCEPTION 'pause claim requires current scope support'
            USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END;
$$
"""
PAUSE_GENERATION_SCOPE_TRIGGER = """
CREATE TRIGGER pause_generation_scope
BEFORE UPDATE OF claimed_by ON pause
FOR EACH ROW EXECUTE FUNCTION pause_generation_scope_reset()
"""


def upgrade() -> None:
    op.create_table(
        "pause",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_member_id", sa.Uuid(), nullable=True),
        sa.Column("resume_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("origin_seq", sa.Integer(), nullable=False),
        sa.Column("origin_arrival_seq", sa.Integer(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("user_description", sa.Text(), nullable=False),
        sa.Column("connections", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("internet_access", sa.Boolean(), nullable=False),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agent.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversation.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_member_id"], ["member.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "conversation_id", name="pause_conversation"),
    )
    op.create_index("pause_due", "pause", ["resume_at"], unique=False)
    op.create_table(
        "scheduled_task",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("created_by_member_id", sa.Uuid(), nullable=True),
        sa.Column("schedule", sa.Text(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_turn_id", sa.Uuid(), nullable=True),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("connections", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("internet_access", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agent.id"],
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversation.id"],
        ),
        sa.ForeignKeyConstraint(
            ["created_by_member_id"],
            ["member.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "agent_id", "name", name="scheduled_task_name"),
    )
    op.create_index("scheduled_task_due", "scheduled_task", ["next_run_at"], unique=False)
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(INTERNET_SCOPE_FUNCTION.format(name="pause_internet_scope"))
    op.execute(INTERNET_SCOPE_TRIGGER.format(name="pause_internet_scope", table="pause"))
    op.execute(PAUSE_GENERATION_SCOPE_RESET)
    op.execute(PAUSE_GENERATION_SCOPE_TRIGGER)


def downgrade() -> None:
    op.drop_index("scheduled_task_due", table_name="scheduled_task")
    op.drop_table("scheduled_task")
    op.drop_index("pause_due", table_name="pause")
    op.drop_table("pause")
