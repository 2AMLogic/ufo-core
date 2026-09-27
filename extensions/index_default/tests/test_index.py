from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from ufo_ext_embed_openai import EMBED_DIM
from ufo_ext_index_default import (
    pack_embedding,
    pgvector_literal,
    unpack_embedding,
)
from ufo_testsupport.index import default_index

from ufo.db import current_workspace, workspace_tx
from ufo.runtime.indexing import Chunk, IndexScope
from ufo.runtime.workspace import ws

WORKSPACE = UUID("11111111-1111-1111-1111-111111111111")
SUBJECT = "member:me"
FOREIGN = "member:other"


def vec(*axes: tuple[int, float]) -> tuple[float, ...]:
    values = [0.0] * EMBED_DIM
    for index, value in axes:
        values[index] = value
    return tuple(values)


@pytest.fixture
async def ambient_workspace(db: None) -> AsyncIterator[None]:
    token = current_workspace.set(WORKSPACE)
    try:
        yield
    finally:
        current_workspace.reset(token)


async def test_upsert_lexical_and_vector_return_ordered_hits(
    ambient_workspace: None, database_url: str
) -> None:
    backend = default_index()
    e0 = vec((0, 1.0))
    e01 = vec((0, 1.0), (1, 1.0))
    await backend.upsert(
        (
            Chunk("d-alpha", "memory_item", "m1", SUBJECT, 0, "apple apple apple", e0),
            Chunk("d-beta", "memory_item", "m2", SUBJECT, 0, "apple cherry date", e01),
            Chunk("d-foreign", "memory_item", "m3", FOREIGN, 0, "apple apple apple", e0),
        )
    )

    lexical = await backend.lexical("apple", frozenset({SUBJECT}), "memory_item", 10)
    assert [hit.chunk_digest for hit in lexical] == ["d-alpha", "d-beta"]
    assert all(hit.subject == SUBJECT for hit in lexical)

    vector = await backend.vector(vec((0, 1.0)), frozenset({SUBJECT}), "memory_item", 10)
    assert [hit.chunk_digest for hit in vector] == ["d-alpha", "d-beta"]
    assert vector[0].score == pytest.approx(1.0, abs=1e-2)
    assert vector[1].score == pytest.approx(0.7071, abs=1e-2)


PUNCTUATED_RECALL_QUERIES = (
    "velvet, harbor.",
    'said "velvet harbor"',
    'she "said velvet',
    'said"velvet harbor',
    "velvet - harbor",
    "(velvet) harbor",
    "velvet AND harbor",
)
NO_TOKEN_QUERIES = ('"', '" "', ",")
LITERAL_MISS_QUERIES = ('vel"vet', "NEAR(wild rumpus)", "wild*", "^wild", "title:wild", "NOT wild")


async def test_lexical_recalls_through_punctuated_queries(
    ambient_workspace: None, database_url: str
) -> None:
    backend = default_index()
    await backend.upsert(
        (
            Chunk(
                "d-punct",
                "memory_item",
                "m1",
                SUBJECT,
                0,
                "she said velvet harbor and moved on",
                vec((0, 1.0)),
            ),
        )
    )
    for query in PUNCTUATED_RECALL_QUERIES:
        hits = await backend.lexical(query, frozenset({SUBJECT}), "memory_item", 10)
        assert [hit.chunk_digest for hit in hits] == ["d-punct"], query
    for query in NO_TOKEN_QUERIES:
        assert await backend.lexical(query, frozenset({SUBJECT}), "memory_item", 10) == (), query
    for query in LITERAL_MISS_QUERIES:
        assert await backend.lexical(query, frozenset({SUBJECT}), "memory_item", 10) == (), query


async def test_lexical_words_a_chunk_holding_only_some_of_the_query_terms(
    ambient_workspace: None, database_url: str
) -> None:
    """Term overlap, not a conjunction."""
    backend = default_index()
    await backend.upsert(
        (
            Chunk(
                "d-orders",
                "memory_item",
                "m1",
                SUBJECT,
                0,
                "payment retries duplicate an order",
                vec((0, 1.0)),
            ),
            Chunk("d-lookup", "memory_item", "m2", SUBJECT, 0, "order lookup", vec((0, 1.0))),
            Chunk(
                "d-export",
                "memory_item",
                "m3",
                SUBJECT,
                0,
                "csv export truncates large accounts",
                vec((0, 1.0)),
            ),
        )
    )
    hits = await backend.lexical(
        "payment retries breaking order", frozenset({SUBJECT}), "memory_item", 10
    )
    assert [hit.chunk_digest for hit in hits] == ["d-orders", "d-lookup"]
    assert await backend.lexical("qzxlv wkbrm", frozenset({SUBJECT}), "memory_item", 10) == ()


async def test_foreign_subject_is_excluded(ambient_workspace: None, database_url: str) -> None:
    backend = default_index()
    await backend.upsert(
        (
            Chunk("mine", "memory_item", "m1", SUBJECT, 0, "shared secret token", vec((0, 1.0))),
            Chunk("theirs", "memory_item", "m2", FOREIGN, 0, "shared secret token", vec((0, 1.0))),
        )
    )
    lexical = await backend.lexical("secret", frozenset({SUBJECT}), "memory_item", 10)
    assert [hit.chunk_digest for hit in lexical] == ["mine"]
    vector = await backend.vector(vec((0, 1.0)), frozenset({SUBJECT}), "memory_item", 10)
    assert [hit.chunk_digest for hit in vector] == ["mine"]


async def test_vector_returns_a_small_owner_kinds_rows_under_a_dominant_corpus(
    ambient_workspace: None, database_url: str
) -> None:
    if database_url.startswith("sqlite"):
        pytest.skip("the starvation shape is Postgres-only")
    backend = default_index()
    await backend.upsert(
        (
            Chunk("target-0", "memory_item", "t0", SUBJECT, 0, "ledger decision", vec((1, 1.0))),
            Chunk("target-1", "memory_item", "t1", SUBJECT, 0, "orion rollout", vec((1, 1.0))),
        )
    )
    async with workspace_tx() as connection:
        await connection.execute(
            sa.text(
                "insert into chunk"
                " (workspace_id, chunk_digest, owner_kind, owner_id, subject, ordinal, text,"
                " embedding)"
                " select :workspace_id, 'noise-' || n, 'page',"
                " 'noise-' || n, :subject, 0, 'noise', cast(:embedding as halfvec)"
                " from generate_series(1, 1500) as n"
            ).bindparams(sa.bindparam("workspace_id", type_=sa.Uuid)),
            {
                "workspace_id": WORKSPACE,
                "subject": SUBJECT,
                "embedding": pgvector_literal(vec((0, 1.0))),
            },
        )
        await connection.execute(sa.text("analyze chunk"))
    hits = await backend.vector(vec((0, 1.0)), frozenset({SUBJECT}), "memory_item", 8)
    assert {hit.chunk_digest for hit in hits} == {"target-0", "target-1"}


async def test_delete_removes_only_its_scope(ambient_workspace: None, database_url: str) -> None:
    backend = default_index()
    await backend.upsert(
        (
            Chunk("keep", "memory_item", "keep-owner", SUBJECT, 0, "apple", vec((0, 1.0))),
            Chunk("drop", "memory_item", "drop-owner", SUBJECT, 0, "apple", vec((0, 1.0))),
        )
    )
    await backend.delete(IndexScope("memory_item", "drop-owner"))
    lexical = await backend.lexical("apple", frozenset({SUBJECT}), "memory_item", 10)
    assert [hit.chunk_digest for hit in lexical] == ["keep"]
    vector = await backend.vector(vec((0, 1.0)), frozenset({SUBJECT}), "memory_item", 10)
    assert [hit.chunk_digest for hit in vector] == ["keep"]


async def test_has_chunks_reports_only_a_scope_that_holds_a_chunk(
    ambient_workspace: None, database_url: str
) -> None:
    backend = default_index()
    await backend.upsert(
        (Chunk("only", "memory_item", "owner", SUBJECT, 0, "apple", vec((0, 1.0))),)
    )
    assert await backend.has_chunks(IndexScope("memory_item", "owner"))
    assert not await backend.has_chunks(IndexScope("memory_item", "empty-owner"))
    await backend.delete(IndexScope("memory_item", "owner"))
    assert not await backend.has_chunks(IndexScope("memory_item", "owner"))


async def test_prune_drops_the_scopes_chunks_outside_the_keep_set(
    ambient_workspace: None, database_url: str
) -> None:
    backend = default_index()
    await backend.upsert(
        (
            Chunk("stale", "page", "p1", SUBJECT, 0, "apple", vec((0, 1.0))),
            Chunk("fresh", "page", "p1", SUBJECT, 1, "apple", vec((0, 1.0))),
            Chunk("other", "page", "p2", SUBJECT, 0, "apple", vec((0, 1.0))),
        )
    )
    await backend.prune(IndexScope("page", "p1"), frozenset({"fresh"}))
    lexical = await backend.lexical("apple", frozenset({SUBJECT}), "page", 10)
    assert sorted(hit.chunk_digest for hit in lexical) == ["fresh", "other"]
    vector = await backend.vector(vec((0, 1.0)), frozenset({SUBJECT}), "page", 10)
    assert sorted(hit.chunk_digest for hit in vector) == ["fresh", "other"]


async def test_prune_with_an_empty_keep_set_drops_the_whole_scope(
    ambient_workspace: None, database_url: str
) -> None:
    backend = default_index()
    await backend.upsert(
        (
            Chunk("gone-a", "page", "p1", SUBJECT, 0, "apple", vec((0, 1.0))),
            Chunk("gone-b", "page", "p1", SUBJECT, 1, "apple", vec((0, 1.0))),
            Chunk("kept", "page", "p2", SUBJECT, 0, "apple", vec((0, 1.0))),
        )
    )
    await backend.prune(IndexScope("page", "p1"), frozenset())
    lexical = await backend.lexical("apple", frozenset({SUBJECT}), "page", 10)
    assert [hit.chunk_digest for hit in lexical] == ["kept"]


DEPLOY = IndexScope("skill", "deploy")
RELEASE = IndexScope("skill", "release")
MINE_HELD = frozenset({("same", SUBJECT, "apple mine"), ("reused", SUBJECT, "apple mine reused")})
THEIR_REUSED = ("reused", SUBJECT, "apple theirs reused")
THEIR_KEPT = ("kept", SUBJECT, "apple theirs kept")

Held = frozenset[tuple[str, str, str]]


async def _two_workspaces() -> tuple[UUID, UUID]:
    backend = default_index()
    mine, theirs = uuid4(), uuid4()
    with ws(mine):
        await backend.upsert(
            (
                Chunk("same", "skill", "deploy", SUBJECT, 0, "apple mine", vec((0, 1.0))),
                Chunk("reused", "skill", "deploy", SUBJECT, 1, "apple mine reused", vec((0, 1.0))),
            )
        )
    with ws(theirs):
        await backend.upsert(
            (
                Chunk("same", "skill", "deploy", SUBJECT, 0, "apple theirs", vec((0, 1.0))),
                Chunk("kept", "skill", "deploy", SUBJECT, 1, "apple theirs kept", vec((0, 1.0))),
                Chunk(
                    "reused", "skill", "release", SUBJECT, 0, "apple theirs reused", vec((0, 1.0))
                ),
            )
        )
    return mine, theirs


async def _reads(workspace_id: UUID) -> tuple[Held, Held]:
    backend = default_index()
    subjects = frozenset({SUBJECT, FOREIGN})
    with ws(workspace_id):
        lexical = await backend.lexical("apple", subjects, "skill", 10)
        vector = await backend.vector(vec((0, 1.0)), subjects, "skill", 10)
    return (
        frozenset((hit.chunk_digest, hit.subject, hit.text) for hit in lexical),
        frozenset((hit.chunk_digest, hit.subject, hit.text) for hit in vector),
    )


async def test_reads_stay_in_the_bound_workspace(db: None) -> None:
    backend = default_index()
    mine, theirs = await _two_workspaces()
    their_held = frozenset({("same", SUBJECT, "apple theirs"), THEIR_KEPT, THEIR_REUSED})
    assert await _reads(mine) == (MINE_HELD, MINE_HELD)
    assert await _reads(theirs) == (their_held, their_held)
    with ws(mine):
        assert await backend.has_chunks(DEPLOY)
        assert not await backend.has_chunks(RELEASE)
    with ws(theirs):
        assert await backend.has_chunks(RELEASE)


async def test_delete_stays_in_the_bound_workspace(db: None) -> None:
    backend = default_index()
    mine, theirs = await _two_workspaces()
    with ws(theirs):
        await backend.delete(DEPLOY)
        assert not await backend.has_chunks(DEPLOY)
    with ws(mine):
        assert await backend.has_chunks(DEPLOY)
    assert await _reads(mine) == (MINE_HELD, MINE_HELD)
    assert await _reads(theirs) == ({THEIR_REUSED}, {THEIR_REUSED})


async def test_prune_stays_in_the_bound_workspace(db: None) -> None:
    backend = default_index()
    mine, theirs = await _two_workspaces()
    with ws(theirs):
        await backend.prune(DEPLOY, frozenset({"kept"}))
    assert await _reads(mine) == (MINE_HELD, MINE_HELD)
    their_held = frozenset({THEIR_KEPT, THEIR_REUSED})
    assert await _reads(theirs) == (their_held, their_held)


async def test_restamp_stays_in_the_bound_workspace(db: None) -> None:
    backend = default_index()
    mine, theirs = await _two_workspaces()
    with ws(theirs):
        assert await backend.restamp(DEPLOY, FOREIGN, frozenset({"same", "kept"}))
    with ws(mine):
        assert not await backend.restamp(DEPLOY, FOREIGN, frozenset({"same", "kept"}))
    assert await _reads(mine) == (MINE_HELD, MINE_HELD)
    their_held = frozenset(
        {("same", FOREIGN, "apple theirs"), ("kept", FOREIGN, "apple theirs kept"), THEIR_REUSED}
    )
    assert await _reads(theirs) == (their_held, their_held)


def test_pack_unpack_embedding_roundtrips() -> None:
    vector = vec((0, 1.0), (7, -0.5), (100, 0.25))
    assert unpack_embedding(pack_embedding(vector)) == pytest.approx(vector)
