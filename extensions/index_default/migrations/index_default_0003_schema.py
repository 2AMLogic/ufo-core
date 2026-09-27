"""The chunk index.

Postgres carries the vector and the full-text column on the table itself: `halfvec` from `pgvector`
under an HNSW index for the nearest-neighbour half, and a stored `tsvector` under a GIN index for
the lexical half. SQLite has neither, so the embedding is an opaque blob the reader scores in
process and the lexical half is an `fts5` virtual table keyed back to the row.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "index_default_0003"
down_revision: str | None = None
branch_labels: tuple[str, ...] | None = ("index_default",)
depends_on: str | None = "20260927025054"

CREATE_EXTENSION_VECTOR = "create extension if not exists vector"
CREATE_CHUNK_PG = """
create table chunk (
    workspace_id uuid not null,
    chunk_digest text not null,
    owner_kind text not null,
    owner_id text not null,
    subject text not null,
    ordinal integer not null,
    text text not null,
    embedding halfvec(3072),
    tsv tsvector generated always as (to_tsvector('english', text)) stored,
    primary key (workspace_id, chunk_digest)
)
"""
CREATE_CHUNK_TSV_GIN = "create index chunk_tsv on chunk using gin (tsv)"
CREATE_CHUNK_EMBEDDING_HNSW = (
    "create index chunk_embedding on chunk using hnsw (embedding halfvec_cosine_ops)"
)
CREATE_CHUNK_FTS_SQLITE = (
    "create virtual table chunk_fts using fts5 "
    "(workspace_id unindexed, chunk_digest unindexed, text)"
)


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(CREATE_EXTENSION_VECTOR)
        op.execute(CREATE_CHUNK_PG)
        op.execute(CREATE_CHUNK_TSV_GIN)
        op.execute(CREATE_CHUNK_EMBEDDING_HNSW)
        op.create_index("chunk_subject", "chunk", ["subject"])
        return
    op.create_table(
        "chunk",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_digest", sa.Text(), nullable=False),
        sa.Column("owner_kind", sa.Text(), nullable=False),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", sa.LargeBinary(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "chunk_digest"),
    )
    op.create_index("chunk_subject", "chunk", ["subject"])
    op.execute(CREATE_CHUNK_FTS_SQLITE)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("drop table chunk")
        return
    op.execute("drop table chunk_fts")
    op.drop_index("chunk_subject", "chunk")
    op.drop_table("chunk")
