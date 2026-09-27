"""ufo's schema.

`source` and `page` are hash-partitioned on `workspace_id` into sixteen each, so their primary keys
lead with the partition column and a unique index over them has to carry it too. SQLite has no
partitioning and takes the same tables whole.

`page.revision` and `workspace.egress_rules_generation` are stamped by triggers rather than by the
writer, so a row that changes out from under a reader still moves the counter it is watched by.
Postgres drives both from one `plpgsql` function per counter; SQLite has no
`for each row ... execute function`, so the same rules are spelled as one trigger per table and
operation.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "20260927025054"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None

AGENT_ARCHIVED_NAME_STATE = (
    "(archived_at is null and archived_name is null) or (archived_at is not null and "
    "archived_name is not null)"
)
AGENT_PROVENANCE = (
    "(provisioned_by is null) = (provisioned_name is null) and (provisioned_by is null) = "
    "(provisioned_version is null)"
)
CONVERSATION_AUDIENCE = (
    "audience = 'shared' or audience like 'member:%' or audience like 'room:%:%' or audience "
    "like 'foreign:%:%'"
)
CONVERSATION_AUDIENCE_MEMBER = (
    "(member_id is null and audience not like 'member:%') or (member_id is not null and "
    "audience like 'member:%')"
)
LEDGER_DIMENSION = "dimension in ('tokens', 'egress', 'sandbox_tokens', 'images', 'videos')"
LEDGER_PROMPT_CLASSES_TOTAL = (
    "dimension not in ('tokens', 'sandbox_tokens') or not token_classes_complete or "
    "prompt_tokens = input_tokens + cache_read_tokens + cache_write_5m_tokens + "
    "cache_write_30m_tokens + cache_write_1h_tokens"
)
LEDGER_TOKEN_CLASSES_NONNEGATIVE = (
    "input_tokens >= 0 and output_tokens >= 0 and cache_read_tokens >= 0 and "
    "cache_write_5m_tokens >= 0 and cache_write_30m_tokens >= 0 and cache_write_1h_tokens >= 0"
)
LEDGER_TOKEN_CLASSES_TOTAL = (
    "dimension not in ('tokens', 'sandbox_tokens') or not token_classes_complete or amount = "
    "input_tokens + output_tokens + cache_read_tokens + cache_write_5m_tokens + "
    "cache_write_30m_tokens + cache_write_1h_tokens"
)
MEMBER_AUTHORIZATION_BASIS = (
    "basis is null or basis in ('selected_message', 'pending_answer', 'standing')"
)
MEMBER_AUTHORIZATION_DECIDED = (
    "(decision is null) = (decided_by is null) and (decision is null) = (decision_key is null) "
    "and (decision is null) = (basis is null) and (decision is null) = (evidence is null)"
)
MEMBER_AUTHORIZATION_DECISION = (
    "decision is null or decision in ('allow', 'always', 'deny', 'revoke', 'superseded')"
)
MEMBER_DISPLAY_NAME_SOURCE = (
    "display_name_source is null or display_name_source in ('member', 'signin', 'slack', "
    "'gravatar')"
)
MEMBER_PHOTO_SOURCE = (
    "photo_source is null or photo_source in ('member', 'signin', 'slack', 'gravatar')"
)
SHARED_ARTIFACT_PREVIEW = (
    "(preview_blob_key IS NULL) = (preview_media_type IS NULL) AND (preview_blob_key IS NULL) = "
    "(preview_size_bytes IS NULL) AND (preview_size_bytes IS NULL OR preview_size_bytes >= 0)"
)
TURN_STATUS = "status in ('queued', 'running', 'parked', 'done', 'failed', 'cancelled')"

PARTITIONS = 16
PARTITIONED = ("source", "page")
EGRESS_TABLES = ("connection", "connector_grant", "credential")
PAGE_REVISION_COLUMNS = ("digest", "body_ref", "subject", "tombstone", "indexed")
ASSIGN_PAGE_REVISION = """
create function assign_page_revision() returns trigger as $$
begin
    if tg_op = 'INSERT'
       or row({new}) is distinct from row({old})
    then
        update workspace
        set page_revision = page_revision + 1
        where id = new.workspace_id
        returning page_revision into new.revision;
    end if;
    return new;
end;
$$ language plpgsql
"""
BUMP_EGRESS_RULES_GENERATION = """
create function bump_egress_rules_generation() returns trigger as $$
begin
    update workspace
    set egress_rules_generation = egress_rules_generation + 1
    where id = coalesce(new.workspace_id, old.workspace_id);
    return coalesce(new, old);
end;
$$ language plpgsql
"""
SQLITE_PAGE_REVISION_INSERT = """
create trigger page_assign_revision_insert
after insert on page
begin
    update workspace
    set page_revision = page_revision + 1
    where id = new.workspace_id;
    update page
    set revision = (
        select page_revision from workspace where id = new.workspace_id
    )
    where workspace_id = new.workspace_id and uid = new.uid;
end
"""
SQLITE_PAGE_REVISION_UPDATE = """
create trigger page_assign_revision_update
after update of {columns} on page
when {changed}
begin
    update workspace
    set page_revision = page_revision + 1
    where id = new.workspace_id;
    update page
    set revision = (
        select page_revision from workspace where id = new.workspace_id
    )
    where workspace_id = new.workspace_id and uid = new.uid;
end
"""
SQLITE_EGRESS = """
create trigger {table}_bump_egress_rules_{event}
after {event} on "{table}"
begin
    update workspace
    set egress_rules_generation = egress_rules_generation + 1
    where id = {row}.workspace_id;
end
"""


def upgrade() -> None:
    _tables()
    if op.get_bind().dialect.name == "postgresql":
        _partitions()
        _postgres_counters()
        return
    _sqlite_counters()


def _tables() -> None:
    op.create_table(
        "ledger_job_day",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("dimension", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("price_digest", sa.Text(), nullable=True),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("priced_micro_usd", sa.BigInteger(), nullable=False),
        sa.Column("first_used_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount > 0", name="ledger_job_day_amount"),
        sa.CheckConstraint("priced_micro_usd >= 0", name="ledger_job_day_priced"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ledger_job_day_workspace", "ledger_job_day", ["workspace_id", "day"], unique=False
    )
    op.create_table(
        "workspace",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("page_revision", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("egress_rules_generation", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("members_can_add", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "agent",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("icon", sa.Text(), server_default=sa.text("'propylon'"), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("reasoning", sa.Text(), server_default=sa.text("'auto'"), nullable=False),
        sa.Column("is_main", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("visibility", sa.Text(), server_default=sa.text("'private'"), nullable=False),
        sa.Column(
            "internet_access_allowed", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
        sa.Column("use_workspace_skills", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("sandbox_size", sa.Text(), server_default=sa.text("'small'"), nullable=False),
        sa.Column("tools", sa.JSON(), nullable=True),
        sa.Column("input_schema", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("output_schema", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("owner_member_id", sa.Uuid(), nullable=True),
        sa.Column("provisioned_by", sa.Text(), nullable=True),
        sa.Column("provisioned_name", sa.Text(), nullable=True),
        sa.Column("provisioned_version", sa.Text(), nullable=True),
        sa.Column("setup", sa.JSON(), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_name", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "reasoning in ('auto', 'off', 'low', 'medium', 'high')", name="agent_reasoning"
        ),
        sa.CheckConstraint(
            "sandbox_size in ('small', 'medium', 'large')", name="agent_sandbox_size"
        ),
        sa.CheckConstraint("visibility in ('private', 'workspace')", name="agent_visibility"),
        sa.CheckConstraint(AGENT_ARCHIVED_NAME_STATE, name="agent_archived_name_state"),
        sa.CheckConstraint(AGENT_PROVENANCE, name="agent_provenance"),
        sa.CheckConstraint("archived_at is null or not is_main", name="agent_archive_scope"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "id", name="agent_workspace_identity"),
        sa.UniqueConstraint("workspace_id", "name"),
        sa.UniqueConstraint(
            "workspace_id", "provisioned_by", "provisioned_name", name="agent_provision_identity"
        ),
    )
    op.create_index(
        "agent_workspace_main",
        "agent",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("is_main"),
        sqlite_where=sa.text("is_main"),
    )
    op.create_table(
        "conversation_change_cursor",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id"),
    )
    op.create_table(
        "credential",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("slot", sa.Text(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
        ),
        sa.PrimaryKeyConstraint("workspace_id", "slot"),
    )
    op.create_table(
        "ext_store",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("extension", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
        ),
        sa.PrimaryKeyConstraint("workspace_id", "extension", "key"),
    )
    op.create_index("ext_store_key", "ext_store", ["extension", "key"], unique=False)
    op.create_table(
        "member",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("is_admin", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "seated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
        sa.Column("timezone", sa.Text(), nullable=True),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("given_name", sa.Text(), nullable=True),
        sa.Column("display_name_source", sa.Text(), nullable=True),
        sa.Column("photo_digest", sa.Text(), nullable=True),
        sa.Column("photo_source", sa.Text(), nullable=True),
        sa.Column("signin_photo_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(MEMBER_DISPLAY_NAME_SOURCE, name="member_display_name_source"),
        sa.CheckConstraint(MEMBER_PHOTO_SOURCE, name="member_photo_source"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "email"),
        sa.UniqueConstraint("workspace_id", "id", name="member_workspace_identity"),
    )
    op.create_index("member_email", "member", ["email"], unique=False)
    op.create_table(
        "object_change",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("verb", sa.Text(), nullable=False),
        sa.Column("caller", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("spec_before", sa.Text(), nullable=True),
        sa.Column("spec_after", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("verb in ('create', 'update', 'delete')", name="object_change_verb"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "object_change_workspace", "object_change", ["workspace_id", "created_at"], unique=False
    )
    op.create_table(
        "runtime_instance",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "runtime_instance_live", "runtime_instance", ["workspace_id", "heartbeat_at"], unique=False
    )
    op.create_table(
        "spend_cap",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=True),
        sa.Column("window_seconds", sa.Integer(), nullable=False),
        sa.Column("limit_micro_usd", sa.BigInteger(), nullable=False),
        sa.Column("on_breach", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(scope = 'workspace') = (subject_id is null)", name="spend_cap_subject"
        ),
        sa.CheckConstraint("on_breach in ('park', 'reject')", name="spend_cap_on_breach"),
        sa.CheckConstraint("scope in ('workspace', 'member', 'agent')", name="spend_cap_scope"),
        sa.CheckConstraint("limit_micro_usd > 0", name="spend_cap_limit"),
        sa.CheckConstraint("window_seconds > 0", name="spend_cap_window"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "scope", "subject_id", "window_seconds", name="spend_cap_identity"
        ),
    )
    op.create_index("spend_cap_workspace", "spend_cap", ["workspace_id"], unique=False)
    op.create_table(
        "surface_stream_cursor",
        sa.Column("surface", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("installation_id", sa.Text(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("installation_id <> ''", name="surface_stream_cursor_id_nonempty"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
        ),
        sa.PrimaryKeyConstraint("surface"),
    )
    op.create_table(
        "connection",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("identity", sa.Text(), nullable=True),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=True),
        sa.Column("backfill_days", sa.Integer(), nullable=True),
        sa.Column("owner_member_id", sa.Uuid(), nullable=True),
        sa.Column("shared", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("account_label", sa.Text(), nullable=True),
        sa.Column("commit_name", sa.Text(), nullable=True),
        sa.Column("commit_email", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "backfill_days is null or backfill_days between 1 and 36500",
            name="connection_backfill_days",
        ),
        sa.CheckConstraint("owner_member_id is not null or shared", name="connection_shared"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "owner_member_id"],
            ["member.workspace_id", "member.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "id", name="connection_workspace_identity"),
        sa.UniqueConstraint("workspace_id", "provider", "account_id", name="connection_identity"),
    )
    op.create_index(
        "connection_provider_subject",
        "connection",
        ["workspace_id", "provider", "identity"],
        unique=True,
        postgresql_where=sa.text("identity is not null"),
        sqlite_where=sa.text("identity is not null"),
    )
    op.create_table(
        "conversation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("surface", sa.Text(), nullable=False),
        sa.Column("queue_key", sa.Text(), nullable=False),
        sa.Column("surface_label", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("title_summarized", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=True),
        sa.Column("audience", sa.Text(), server_default="shared", nullable=False),
        sa.Column("sandbox_conversation_id", sa.Uuid(), nullable=True),
        sa.Column("sandbox_handle", sa.Text(), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(CONVERSATION_AUDIENCE_MEMBER, name="conversation_audience_member"),
        sa.CheckConstraint(CONVERSATION_AUDIENCE, name="conversation_audience"),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agent.id"],
        ),
        sa.ForeignKeyConstraint(
            ["member_id"],
            ["member.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "id", name="conversation_workspace_identity"),
        sa.UniqueConstraint(
            "workspace_id",
            "surface",
            "queue_key",
            name="conversation_workspace_surface_queue_key_key",
        ),
    )
    op.create_index(
        "conversation_awaiting_title",
        "conversation",
        ["workspace_id"],
        unique=False,
        postgresql_where=sa.text("not title_summarized"),
        sqlite_where=sa.text("not title_summarized"),
    )
    op.create_index(
        "conversation_sandbox",
        "conversation",
        ["workspace_id"],
        unique=False,
        postgresql_where=sa.text("sandbox_handle is not null"),
        sqlite_where=sa.text("sandbox_handle is not null"),
    )
    op.create_index("conversation_workspace", "conversation", ["workspace_id"], unique=False)
    op.create_table(
        "credential_fulfillment",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("slot", sa.Text(), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=True),
        sa.Column("fulfilled_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "member_id"],
            ["member.workspace_id", "member.id"],
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("workspace_id", "request_id", "slot"),
    )
    op.create_table(
        "member_permission",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("call", sa.Text(), nullable=False),
        sa.Column("effect_digest", sa.Text(), nullable=False),
        sa.Column("effect", sa.JSON(), nullable=False),
        sa.Column("scope_digest", sa.Text(), nullable=True),
        sa.Column("scope", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("granted_by", sa.Uuid(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("call <> ''", name="member_permission_call_nonempty"),
        sa.CheckConstraint("effect_digest <> ''", name="member_permission_digest_nonempty"),
        sa.CheckConstraint(
            "(scope_digest is null) = (scope is null)", name="member_permission_scope_pair"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "agent_id"],
            ["agent.workspace_id", "agent.id"],
            name="member_permission_agent_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "member_id"],
            ["member.workspace_id", "member.id"],
            name="member_permission_member_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "member_id",
            "agent_id",
            "call",
            "effect_digest",
            name="member_permission_identity",
        ),
    )
    op.create_index(
        "member_permission_member", "member_permission", ["workspace_id", "member_id"], unique=False
    )
    op.create_index(
        "member_permission_scope_identity",
        "member_permission",
        ["workspace_id", "member_id", "agent_id", "call", "scope_digest"],
        unique=True,
    )
    op.create_table(
        "proposal",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("extension", sa.Text(), nullable=False),
        sa.Column("from_digest", sa.Text(), nullable=False),
        sa.Column("to_digest", sa.Text(), nullable=False),
        sa.Column("body", sa.JSON(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status in ('pending', 'approved', 'rejected')", name="proposal_status"),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agent.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "surface_address",
        sa.Column("surface", sa.Text(), nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=False),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("proved_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("address <> ''", name="surface_address_address_nonempty"),
        sa.CheckConstraint(
            "claim_expires_at is null or proved_by is null", name="surface_address_claim_or_proof"
        ),
        sa.ForeignKeyConstraint(
            ["member_id"],
            ["member.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
        ),
        sa.PrimaryKeyConstraint("surface", "address"),
    )
    op.create_index(
        op.f("ix_surface_address_workspace_id"), "surface_address", ["workspace_id"], unique=False
    )
    op.create_table(
        "surface_identity",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=False),
        sa.Column("surface", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["member_id"],
            ["member.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
        ),
        sa.PrimaryKeyConstraint("workspace_id", "surface", "external_id"),
    )
    op.create_table(
        "surface_installation",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("surface", sa.Text(), nullable=False),
        sa.Column("installation_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("routes_ingress", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("installation_id <> ''", name="surface_installation_id_nonempty"),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agent.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
        ),
        sa.PrimaryKeyConstraint("workspace_id", "surface"),
    )
    op.create_index(
        "surface_installation_surface_installation_id_key",
        "surface_installation",
        ["surface", "installation_id"],
        unique=True,
        postgresql_where=sa.text("routes_ingress"),
        sqlite_where=sa.text("routes_ingress"),
    )
    op.create_table(
        "surface_listener_claim",
        sa.Column("surface", sa.Text(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("owner_token", sa.Uuid(), nullable=False),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("surface <> ''", name="surface_listener_claim_surface_nonempty"),
        sa.ForeignKeyConstraint(["owner_id"], ["runtime_instance.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
        ),
        sa.PrimaryKeyConstraint("surface"),
    )
    op.create_table(
        "connector_grant",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "agent_id"],
            ["agent.workspace_id", "agent.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "connection_id"],
            ["connection.workspace_id", "connection.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "agent_id", "connection_id", name="connector_grant_identity"
        ),
    )
    op.create_table(
        "conversation_change",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("scan", sa.JSON(none_as_null=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "conversation_id"],
            ["conversation.workspace_id", "conversation.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
        ),
        sa.PrimaryKeyConstraint("workspace_id", "conversation_id"),
    )
    op.create_table(
        "conversation_change_log",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "conversation_id"],
            ["conversation.workspace_id", "conversation.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
        ),
        sa.PrimaryKeyConstraint("workspace_id", "position"),
    )
    op.create_index(
        "conversation_change_log_age", "conversation_change_log", ["created_at"], unique=False
    )
    op.create_table(
        "conversation_pin",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=False),
        sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "conversation_id"],
            ["conversation.workspace_id", "conversation.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "member_id"], ["member.workspace_id", "member.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
        ),
        sa.PrimaryKeyConstraint("workspace_id", "conversation_id", "member_id"),
    )
    op.create_table(
        "conversation_read",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "conversation_id"],
            ["conversation.workspace_id", "conversation.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "member_id"], ["member.workspace_id", "member.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
        ),
        sa.PrimaryKeyConstraint("workspace_id", "conversation_id", "member_id"),
    )
    op.create_table(
        "member_authorization",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("call", sa.Text(), nullable=False),
        sa.Column("effect_digest", sa.Text(), nullable=False),
        sa.Column("effect", sa.JSON(), nullable=False),
        sa.Column("scope_digest", sa.Text(), nullable=True),
        sa.Column("scope", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("binding_digest", sa.Text(), nullable=True),
        sa.Column("request_summary", sa.Text(), nullable=True),
        sa.Column("scope_summary", sa.Text(), nullable=True),
        sa.Column("request_key", sa.Text(), nullable=False),
        sa.Column("decision_key", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.Uuid(), nullable=False),
        sa.Column("decided_by", sa.Uuid(), nullable=True),
        sa.Column("decision", sa.Text(), nullable=True),
        sa.Column("basis", sa.Text(), nullable=True),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(MEMBER_AUTHORIZATION_BASIS, name="member_authorization_basis"),
        sa.CheckConstraint("call <> ''", name="member_authorization_call_nonempty"),
        sa.CheckConstraint(MEMBER_AUTHORIZATION_DECISION, name="member_authorization_decision"),
        sa.CheckConstraint("effect_digest <> ''", name="member_authorization_digest_nonempty"),
        sa.CheckConstraint("request_key <> ''", name="member_authorization_request_key_nonempty"),
        sa.CheckConstraint(
            "(binding_digest is null) = (scope_digest is null)",
            name="member_authorization_binding_scope_pair",
        ),
        sa.CheckConstraint(MEMBER_AUTHORIZATION_DECIDED, name="member_authorization_decided"),
        sa.CheckConstraint(
            "(scope_digest is null) = (scope is null)", name="member_authorization_scope_pair"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "agent_id"],
            ["agent.workspace_id", "agent.id"],
            name="member_authorization_agent_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "conversation_id"],
            ["conversation.workspace_id", "conversation.id"],
            name="member_authorization_conversation_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "member_id"],
            ["member.workspace_id", "member.id"],
            name="member_authorization_member_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "decision_key", name="member_authorization_decision_key"
        ),
        sa.UniqueConstraint("workspace_id", "request_key", name="member_authorization_request_key"),
    )
    op.create_index(
        "member_authorization_pending",
        "member_authorization",
        ["workspace_id", "conversation_id", "member_id"],
        unique=True,
        postgresql_where=sa.text("decision is null"),
        sqlite_where=sa.text("decision is null"),
    )
    op.create_table(
        "source",
        sa.Column("uid", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("backend", sa.Text(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("feed_handle", sa.Text(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column("partition_cursor", sa.Text(), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_sync_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consecutive_errors", sa.Integer(), server_default="0", nullable=False),
        sa.Column("consecutive_refusals", sa.Integer(), server_default="0", nullable=False),
        sa.Column("consecutive_empty", sa.Integer(), server_default="0", nullable=False),
        sa.Column("parked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("parked_reason", sa.Text(), nullable=True),
        sa.Column("parked_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("parked_awaits_grant", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "connection_id"],
            ["connection.workspace_id", "connection.id"],
            name="source_authority_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
        ),
        sa.PrimaryKeyConstraint("workspace_id", "uid", name="source_pkey"),
        postgresql_partition_by="HASH (workspace_id)",
    )
    op.create_index("source_authority", "source", ["workspace_id", "connection_id"], unique=False)
    op.create_index("source_due", "source", ["next_sync_at"], unique=False)
    op.create_index(
        "source_feed_handle",
        "source",
        ["workspace_id", "connection_id", "backend", "feed_handle"],
        unique=True,
    )
    op.create_table(
        "transcript_access",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("reader_member_id", sa.Uuid(), nullable=False),
        sa.Column("subject_member_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "conversation_id"],
            ["conversation.workspace_id", "conversation.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "reader_member_id"],
            ["member.workspace_id", "member.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "subject_member_id"],
            ["member.workspace_id", "member.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "transcript_access_conversation",
        "transcript_access",
        ["workspace_id", "conversation_id"],
        unique=False,
    )
    op.create_table(
        "turn",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("inbound", sa.Text(), nullable=False),
        sa.Column("admission_source", sa.Text(), server_default="internal", nullable=False),
        sa.Column("speaker_member_id", sa.Uuid(), nullable=True),
        sa.Column("member_id", sa.Uuid(), nullable=True),
        sa.Column("fired_by_kind", sa.Text(), nullable=True),
        sa.Column("fired_by_name", sa.Text(), nullable=True),
        sa.Column("fired_by_title", sa.Text(), nullable=True),
        sa.Column("fired_by_provider", sa.Text(), nullable=True),
        sa.Column("connect_authorization_url", sa.Text(), nullable=True),
        sa.Column("connect_authorized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("connect_landed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("context", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("terminal", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("created_refs", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("parent_turn_id", sa.Uuid(), nullable=True),
        sa.Column("subagent_profile", sa.Text(), nullable=True),
        sa.Column("subagent_name", sa.Text(), nullable=True),
        sa.Column("byok", sa.Boolean(), nullable=True),
        sa.Column("byok_attempt", sa.Text(), nullable=True),
        sa.Column("billing_identity", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("result_delivery", sa.Text(), nullable=True),
        sa.Column("spawn_delivers_result", sa.Boolean(), nullable=True),
        sa.Column("spawn_request_fingerprint", sa.Text(), nullable=True),
        sa.Column("traceparent", sa.Text(), nullable=True),
        sa.Column("runtime_config", sa.JSON(none_as_null=True), nullable=True),
        sa.Column(
            "model_accounts",
            sa.JSON().with_variant(JSONB(), "postgresql"),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("running_attempt", sa.Text(), nullable=True),
        sa.Column("dispatch_enqueued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("external_retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("detached_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(status in ('queued', 'running', 'parked')) = (terminal is null)", name="turn_terminal"
        ),
        sa.CheckConstraint(
            "admission_source in ('member', 'internal', 'scheduled', 'intent')",
            name="turn_admission_source",
        ),
        sa.CheckConstraint(
            "result_delivery in ('pending', 'delivered')", name="turn_result_delivery"
        ),
        sa.CheckConstraint(TURN_STATUS, name="turn_status"),
        sa.CheckConstraint(
            "(connect_authorization_url is null) = (connect_authorized_at is null)",
            name="turn_connect_authorization",
        ),
        sa.CheckConstraint("external_retry_count >= 0", name="turn_external_retry_count"),
        sa.CheckConstraint("seq >= 1", name="turn_seq"),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agent.id"],
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversation.id"],
        ),
        sa.ForeignKeyConstraint(
            ["member_id"],
            ["member.id"],
        ),
        sa.ForeignKeyConstraint(
            ["speaker_member_id"],
            ["member.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", "seq"),
    )
    op.create_index("turn_agent_activity", "turn", ["agent_id", "updated_at", "id"], unique=False)
    op.create_index(
        "turn_agent_live",
        "turn",
        ["agent_id", "status"],
        unique=False,
        postgresql_where=sa.text("terminal is null"),
        sqlite_where=sa.text("terminal is null"),
    )
    op.create_index(
        "turn_conversation_activity", "turn", ["conversation_id", "updated_at"], unique=False
    )
    op.create_index(
        "turn_detached",
        "turn",
        ["workspace_id"],
        unique=False,
        postgresql_where=sa.text("detached_until is not null"),
        sqlite_where=sa.text("detached_until is not null"),
    )
    op.create_index(
        "turn_fired",
        "turn",
        ["workspace_id", "agent_id", "created_at"],
        unique=False,
        postgresql_where=sa.text("fired_by_kind is not null"),
        sqlite_where=sa.text("fired_by_kind is not null"),
    )
    op.create_index(
        "turn_fired_by",
        "turn",
        ["workspace_id", "fired_by_kind", "fired_by_name", "created_at"],
        unique=False,
        postgresql_where=sa.text("fired_by_kind is not null"),
        sqlite_where=sa.text("fired_by_kind is not null"),
    )
    op.create_index(
        "turn_idempotency_key", "turn", ["workspace_id", "idempotency_key"], unique=True
    )
    op.create_index(
        "turn_member_admitted",
        "turn",
        ["workspace_id", "conversation_id"],
        unique=False,
        postgresql_where=sa.text("admission_source = 'member'"),
        sqlite_where=sa.text("admission_source = 'member'"),
    )
    op.create_index(
        "turn_parent",
        "turn",
        ["parent_turn_id"],
        unique=False,
        postgresql_where=sa.text("parent_turn_id is not null"),
        sqlite_where=sa.text("parent_turn_id is not null"),
    )
    op.create_index(
        "turn_parked",
        "turn",
        ["workspace_id"],
        unique=False,
        postgresql_where=sa.text("status = 'parked'"),
        sqlite_where=sa.text("status = 'parked'"),
    )
    op.create_index(
        "turn_result_pending",
        "turn",
        ["workspace_id"],
        unique=False,
        postgresql_where=sa.text("result_delivery = 'pending'"),
        sqlite_where=sa.text("result_delivery = 'pending'"),
    )
    op.create_index(
        "turn_retry_at",
        "turn",
        ["retry_at"],
        unique=False,
        postgresql_where=sa.text("status = 'parked' and retry_at is not null"),
        sqlite_where=sa.text("status = 'parked' and retry_at is not null"),
    )
    op.create_index(
        "turn_spoken",
        "turn",
        ["workspace_id", "conversation_id", "speaker_member_id"],
        unique=False,
        postgresql_where=sa.text("speaker_member_id is not null"),
        sqlite_where=sa.text("speaker_member_id is not null"),
    )
    op.create_table(
        "detached_task",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("sandbox_conversation_id", sa.Uuid(), nullable=False),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("runtime_base", sa.Text(), nullable=False),
        sa.Column("follow_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("runtime_base <> ''", name="detached_task_runtime_base_nonempty"),
        sa.CheckConstraint("task <> ''", name="detached_task_task_nonempty"),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversation.id"],
        ),
        sa.ForeignKeyConstraint(
            ["sandbox_conversation_id"],
            ["conversation.id"],
        ),
        sa.ForeignKeyConstraint(["turn_id"], ["turn.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
        ),
        sa.PrimaryKeyConstraint("turn_id", "task"),
    )
    op.create_index("detached_task_workspace", "detached_task", ["workspace_id"], unique=False)
    op.create_table(
        "inbound_message",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("admission_source", sa.Text(), nullable=False),
        sa.Column("context", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("speaker_member_id", sa.Uuid(), nullable=True),
        sa.Column("member_id", sa.Uuid(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("admitted_turn_id", sa.Uuid(), nullable=False),
        sa.Column("consumed_turn_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "admission_source in ('member', 'internal')", name="inbound_message_admission_source"
        ),
        sa.ForeignKeyConstraint(
            ["admitted_turn_id"],
            ["turn.id"],
        ),
        sa.ForeignKeyConstraint(
            ["consumed_turn_id"],
            ["turn.id"],
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversation.id"],
        ),
        sa.ForeignKeyConstraint(
            ["member_id"],
            ["member.id"],
        ),
        sa.ForeignKeyConstraint(
            ["speaker_member_id"],
            ["member.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", "seq"),
    )
    op.create_index(
        "inbound_message_idempotency_key",
        "inbound_message",
        ["workspace_id", "idempotency_key"],
        unique=True,
    )
    op.create_index(
        "inbound_message_pending",
        "inbound_message",
        ["conversation_id"],
        unique=False,
        postgresql_where=sa.text("consumed_turn_id is null"),
        sqlite_where=sa.text("consumed_turn_id is null"),
    )
    op.create_table(
        "ledger",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=True),
        sa.Column("dimension", sa.Text(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("prompt_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "cache_read_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "cache_write_5m_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "cache_write_30m_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "cache_write_1h_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("byok", sa.Boolean(), nullable=True),
        sa.Column(
            "token_classes_complete", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column("priced_micro_usd", sa.BigInteger(), nullable=False),
        sa.Column(
            "debited_micro_usd", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("price_digest", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(LEDGER_DIMENSION, name="ledger_dimension"),
        sa.CheckConstraint(LEDGER_TOKEN_CLASSES_TOTAL, name="ledger_token_classes_total"),
        sa.CheckConstraint(LEDGER_PROMPT_CLASSES_TOTAL, name="ledger_prompt_classes_total"),
        sa.CheckConstraint("not byok or dimension = 'tokens'", name="ledger_byok_dimension"),
        sa.CheckConstraint("amount > 0", name="ledger_amount"),
        sa.CheckConstraint(
            LEDGER_TOKEN_CLASSES_NONNEGATIVE, name="ledger_token_classes_nonnegative"
        ),
        sa.CheckConstraint("priced_micro_usd >= 0", name="ledger_priced"),
        sa.ForeignKeyConstraint(
            ["turn_id"],
            ["turn.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ledger_turn", "ledger", ["turn_id"], unique=False)
    op.create_index(
        "ledger_workspace_created", "ledger", ["workspace_id", "created_at"], unique=False
    )
    op.create_table(
        "mid_turn_reply",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("round_index", sa.Integer(), nullable=False),
        sa.Column("span_index", sa.Integer(), nullable=False),
        sa.Column("message_ref", sa.Uuid(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reply_ref", sa.Text(), nullable=True),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status in ('pending', 'claimed', 'delivered', 'failed')", name="mid_turn_reply_status"
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"],
            ["turn.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "mid_turn_reply_due",
        "mid_turn_reply",
        ["workspace_id", "created_at"],
        unique=False,
        postgresql_where=sa.text("status in ('pending', 'claimed')"),
        sqlite_where=sa.text("status in ('pending', 'claimed')"),
    )
    op.create_table(
        "page",
        sa.Column("uid", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("source_uid", sa.Uuid(), nullable=False),
        sa.Column("source_identity", sa.Text(), nullable=True),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("body_ref", sa.Text(), nullable=False),
        sa.Column("stream", sa.Text(), server_default="", nullable=False),
        sa.Column("title", sa.Text(), server_default="", nullable=False),
        sa.Column("record_created_at", sa.Text(), nullable=True),
        sa.Column("record_updated_at", sa.Text(), nullable=True),
        sa.Column("parent_fields", sa.JSON(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("revision", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("tombstone", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("indexed", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.CheckConstraint("subject = 'shared' or subject like 'member:%'", name="page_subject"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_uid"],
            ["source.workspace_id", "source.uid"],
            name="page_source_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
        ),
        sa.PrimaryKeyConstraint("workspace_id", "uid", name="page_pkey"),
        postgresql_partition_by="HASH (workspace_id)",
    )
    op.create_index("page_feed", "page", ["workspace_id", "revision", "uid"], unique=False)
    op.create_index("page_source", "page", ["workspace_id", "source_uid"], unique=False)
    op.create_index(
        "page_source_identity",
        "page",
        ["workspace_id", "source_uid", "source_identity"],
        unique=True,
        postgresql_where=sa.text("source_identity is not null"),
        sqlite_where=sa.text("source_identity is not null"),
    )
    op.create_table(
        "shared_artifact",
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("blob_key", sa.Text(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=True),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("is_workspace_export", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("request_fingerprint", sa.Text(), nullable=True),
        sa.Column("digest", sa.Text(), nullable=True),
        sa.Column("is_text", sa.Boolean(), nullable=True),
        sa.Column("preview_blob_key", sa.Text(), nullable=True),
        sa.Column("preview_media_type", sa.Text(), nullable=True),
        sa.Column("preview_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("attached_by_member", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("role", sa.Text(), server_default="file", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("role in ('file', 'details')", name="shared_artifact_role"),
        sa.CheckConstraint(SHARED_ARTIFACT_PREVIEW, name="shared_artifact_preview"),
        sa.CheckConstraint("size_bytes >= 0", name="shared_artifact_size"),
        sa.ForeignKeyConstraint(
            ["member_id"],
            ["member.id"],
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"],
            ["turn.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
        ),
        sa.PrimaryKeyConstraint("turn_id", "blob_key"),
        sa.UniqueConstraint("id", name="shared_artifact_id"),
    )
    op.create_index(
        "shared_artifact_files",
        "shared_artifact",
        ["workspace_id", "filename", "created_at", "blob_key"],
        unique=False,
        postgresql_where=sa.text("role = 'file'"),
        sqlite_where=sa.text("role = 'file'"),
    )
    op.create_table(
        "writeback",
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reply_ref", sa.Text(), nullable=True),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status in ('pending', 'claimed', 'delivered', 'failed')", name="writeback_status"
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"],
            ["turn.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
        ),
        sa.PrimaryKeyConstraint("turn_id"),
    )
    op.create_index(
        "writeback_due",
        "writeback",
        ["workspace_id", "created_at"],
        unique=False,
        postgresql_where=sa.text("status in ('pending', 'claimed')"),
        sqlite_where=sa.text("status in ('pending', 'claimed')"),
    )
    op.create_table(
        "ledger_export",
        sa.Column("consumer", sa.Text(), nullable=False),
        sa.Column("ledger_id", sa.Uuid(), nullable=False),
        sa.Column("from_amount", sa.BigInteger(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("to_amount", sa.BigInteger(), nullable=False),
        sa.Column("from_micro_usd", sa.BigInteger(), nullable=False),
        sa.Column("to_micro_usd", sa.BigInteger(), nullable=False),
        sa.Column("byok", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("to_amount > from_amount", name="ledger_export_delta"),
        sa.ForeignKeyConstraint(
            ["ledger_id"],
            ["ledger.id"],
        ),
        sa.PrimaryKeyConstraint("consumer", "ledger_id", "from_amount"),
    )
    op.create_index(
        "ledger_export_pending",
        "ledger_export",
        ["consumer", "workspace_id"],
        unique=False,
        postgresql_where=sa.text("acked_at is null"),
        sqlite_where=sa.text("acked_at is null"),
    )


def _partitions() -> None:
    for table in PARTITIONED:
        for remainder in range(PARTITIONS):
            op.execute(
                f"create table {table}_p{remainder:02d} partition of {table} "
                f"for values with (modulus {PARTITIONS}, remainder {remainder})"
            )


def _postgres_counters() -> None:
    columns = ", ".join(PAGE_REVISION_COLUMNS)
    op.execute(
        ASSIGN_PAGE_REVISION.format(
            new=", ".join(f"new.{column}" for column in PAGE_REVISION_COLUMNS),
            old=", ".join(f"old.{column}" for column in PAGE_REVISION_COLUMNS),
        )
    )
    op.execute(
        f"create trigger page_assign_revision before insert or update of {columns} "
        "on page for each row execute function assign_page_revision()"
    )
    op.execute(BUMP_EGRESS_RULES_GENERATION)
    for table in EGRESS_TABLES:
        op.execute(
            f"create trigger {table}_bump_egress_rules "
            f"after insert or update or delete on {table} "
            "for each row execute function bump_egress_rules_generation()"
        )


def _sqlite_counters() -> None:
    op.execute(SQLITE_PAGE_REVISION_INSERT)
    op.execute(
        SQLITE_PAGE_REVISION_UPDATE.format(
            columns=", ".join(PAGE_REVISION_COLUMNS),
            changed="\n  or ".join(
                f"new.{column} is not old.{column}" for column in PAGE_REVISION_COLUMNS
            ),
        )
    )
    for table in EGRESS_TABLES:
        for event in ("insert", "update", "delete"):
            row = "old" if event == "delete" else "new"
            op.execute(SQLITE_EGRESS.format(table=table, event=event, row=row))


def downgrade() -> None:
    op.drop_index(
        "ledger_export_pending",
        table_name="ledger_export",
        postgresql_where=sa.text("acked_at is null"),
        sqlite_where=sa.text("acked_at is null"),
    )
    op.drop_table("ledger_export")
    op.drop_index(
        "writeback_due",
        table_name="writeback",
        postgresql_where=sa.text("status in ('pending', 'claimed')"),
        sqlite_where=sa.text("status in ('pending', 'claimed')"),
    )
    op.drop_table("writeback")
    op.drop_index(
        "shared_artifact_files",
        table_name="shared_artifact",
        postgresql_where=sa.text("role = 'file'"),
        sqlite_where=sa.text("role = 'file'"),
    )
    op.drop_table("shared_artifact")
    op.drop_index(
        "page_source_identity",
        table_name="page",
        postgresql_where=sa.text("source_identity is not null"),
        sqlite_where=sa.text("source_identity is not null"),
    )
    op.drop_index("page_source", table_name="page")
    op.drop_index("page_feed", table_name="page")
    op.drop_table("page")
    op.drop_index(
        "mid_turn_reply_due",
        table_name="mid_turn_reply",
        postgresql_where=sa.text("status in ('pending', 'claimed')"),
        sqlite_where=sa.text("status in ('pending', 'claimed')"),
    )
    op.drop_table("mid_turn_reply")
    op.drop_index("ledger_workspace_created", table_name="ledger")
    op.drop_index("ledger_turn", table_name="ledger")
    op.drop_table("ledger")
    op.drop_index(
        "inbound_message_pending",
        table_name="inbound_message",
        postgresql_where=sa.text("consumed_turn_id is null"),
        sqlite_where=sa.text("consumed_turn_id is null"),
    )
    op.drop_index("inbound_message_idempotency_key", table_name="inbound_message")
    op.drop_table("inbound_message")
    op.drop_index("detached_task_workspace", table_name="detached_task")
    op.drop_table("detached_task")
    op.drop_index(
        "turn_spoken",
        table_name="turn",
        postgresql_where=sa.text("speaker_member_id is not null"),
        sqlite_where=sa.text("speaker_member_id is not null"),
    )
    op.drop_index(
        "turn_retry_at",
        table_name="turn",
        postgresql_where=sa.text("status = 'parked' and retry_at is not null"),
        sqlite_where=sa.text("status = 'parked' and retry_at is not null"),
    )
    op.drop_index(
        "turn_result_pending",
        table_name="turn",
        postgresql_where=sa.text("result_delivery = 'pending'"),
        sqlite_where=sa.text("result_delivery = 'pending'"),
    )
    op.drop_index(
        "turn_parked",
        table_name="turn",
        postgresql_where=sa.text("status = 'parked'"),
        sqlite_where=sa.text("status = 'parked'"),
    )
    op.drop_index(
        "turn_parent",
        table_name="turn",
        postgresql_where=sa.text("parent_turn_id is not null"),
        sqlite_where=sa.text("parent_turn_id is not null"),
    )
    op.drop_index(
        "turn_member_admitted",
        table_name="turn",
        postgresql_where=sa.text("admission_source = 'member'"),
        sqlite_where=sa.text("admission_source = 'member'"),
    )
    op.drop_index("turn_idempotency_key", table_name="turn")
    op.drop_index(
        "turn_fired_by",
        table_name="turn",
        postgresql_where=sa.text("fired_by_kind is not null"),
        sqlite_where=sa.text("fired_by_kind is not null"),
    )
    op.drop_index(
        "turn_fired",
        table_name="turn",
        postgresql_where=sa.text("fired_by_kind is not null"),
        sqlite_where=sa.text("fired_by_kind is not null"),
    )
    op.drop_index(
        "turn_detached",
        table_name="turn",
        postgresql_where=sa.text("detached_until is not null"),
        sqlite_where=sa.text("detached_until is not null"),
    )
    op.drop_index("turn_conversation_activity", table_name="turn")
    op.drop_index(
        "turn_agent_live",
        table_name="turn",
        postgresql_where=sa.text("terminal is null"),
        sqlite_where=sa.text("terminal is null"),
    )
    op.drop_index("turn_agent_activity", table_name="turn")
    op.drop_table("turn")
    op.drop_index("transcript_access_conversation", table_name="transcript_access")
    op.drop_table("transcript_access")
    op.drop_index("source_feed_handle", table_name="source")
    op.drop_index("source_due", table_name="source")
    op.drop_index("source_authority", table_name="source")
    op.drop_table("source")
    op.drop_index(
        "member_authorization_pending",
        table_name="member_authorization",
        postgresql_where=sa.text("decision is null"),
        sqlite_where=sa.text("decision is null"),
    )
    op.drop_table("member_authorization")
    op.drop_table("conversation_read")
    op.drop_table("conversation_pin")
    op.drop_index("conversation_change_log_age", table_name="conversation_change_log")
    op.drop_table("conversation_change_log")
    op.drop_table("conversation_change")
    op.drop_table("connector_grant")
    op.drop_table("surface_listener_claim")
    op.drop_index(
        "surface_installation_surface_installation_id_key",
        table_name="surface_installation",
        postgresql_where=sa.text("routes_ingress"),
        sqlite_where=sa.text("routes_ingress"),
    )
    op.drop_table("surface_installation")
    op.drop_table("surface_identity")
    op.drop_index(op.f("ix_surface_address_workspace_id"), table_name="surface_address")
    op.drop_table("surface_address")
    op.drop_table("proposal")
    op.drop_index("member_permission_scope_identity", table_name="member_permission")
    op.drop_index("member_permission_member", table_name="member_permission")
    op.drop_table("member_permission")
    op.drop_table("credential_fulfillment")
    op.drop_index("conversation_workspace", table_name="conversation")
    op.drop_index(
        "conversation_sandbox",
        table_name="conversation",
        postgresql_where=sa.text("sandbox_handle is not null"),
        sqlite_where=sa.text("sandbox_handle is not null"),
    )
    op.drop_index(
        "conversation_awaiting_title",
        table_name="conversation",
        postgresql_where=sa.text("not title_summarized"),
        sqlite_where=sa.text("not title_summarized"),
    )
    op.drop_table("conversation")
    op.drop_index(
        "connection_provider_subject",
        table_name="connection",
        postgresql_where=sa.text("identity is not null"),
        sqlite_where=sa.text("identity is not null"),
    )
    op.drop_table("connection")
    op.drop_table("surface_stream_cursor")
    op.drop_index("spend_cap_workspace", table_name="spend_cap")
    op.drop_table("spend_cap")
    op.drop_index("runtime_instance_live", table_name="runtime_instance")
    op.drop_table("runtime_instance")
    op.drop_index("object_change_workspace", table_name="object_change")
    op.drop_table("object_change")
    op.drop_index("member_email", table_name="member")
    op.drop_table("member")
    op.drop_index("ext_store_key", table_name="ext_store")
    op.drop_table("ext_store")
    op.drop_table("credential")
    op.drop_table("conversation_change_cursor")
    op.drop_index(
        "agent_workspace_main",
        table_name="agent",
        postgresql_where=sa.text("is_main"),
        sqlite_where=sa.text("is_main"),
    )
    op.drop_table("agent")
    op.drop_table("workspace")
    op.drop_index("ledger_job_day_workspace", table_name="ledger_job_day")
    op.drop_table("ledger_job_day")
