"""Supersession as a judgement rather than a restatement: `supersede` stamps a live row in its own
audience and hands it to the index job, a page re-derivation leaves the stamp where a member's
restatement clears it, the indexer withdraws a superseded row's chunks, and a summary decays like
the facts it stands for. The DefaultIndex and the stub embed are real dependencies of the indexer,
never the asserted thing: every assertion reads memory_item rows or the index back."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from ufo_ext_embed_openai import EMBED_DIM
from ufo_ext_memory.store import (
    EPISODIC,
    FACT,
    HALFLIFE_DAYS,
    OVERVIEW,
    SECTION,
    SEMANTIC,
    MemoryIndexer,
    MemoryStore,
    MemoryWrite,
    SupersededChunkDrain,
    half_life_days,
    memory_item,
)
from ufo_ext_memory.sweep import name_pattern
from ufo_testsupport.index import default_index

from ufo.db import workspace_tx
from ufo.runtime.ext.context import PageState, ScopedStore, context_for
from ufo.runtime.ext.source_reader import SourceReader
from ufo.runtime.indexing import OWNER_KIND_MEMORY_ITEM, IndexScope, TextChunker
from ufo.runtime.turns.subjects import SHARED_SUBJECT, member_subject
from ufo.runtime.workspace import ws
from ufo.schema import tables

pytestmark = [
    pytest.mark.usefixtures("database_url"),
    pytest.mark.parametrize("database_url", ["sqlite"], indirect=True),
]

PROBE = tuple(1.0 if index == 0 else 0.0 for index in range(EMBED_DIM))
SHARED = frozenset({SHARED_SUBJECT})


class StubEmbed:
    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        return tuple(PROBE for _ in texts)


async def _workspace() -> UUID:
    workspace_id = uuid4()
    async with workspace_tx() as connection:
        await connection.execute(
            sa.insert(tables.workspace).values(
                id=workspace_id, created_at=sa.func.now(), updated_at=sa.func.now()
            )
        )
    return workspace_id


def _store(workspace_id: UUID) -> MemoryStore:
    ext = context_for("memory", frozenset())

    async def readable(page_ids: tuple[UUID, ...], reader: SourceReader) -> dict[UUID, PageState]:
        return await ext.page_states(page_ids)

    async def readable_ids(reader: SourceReader) -> frozenset[UUID]:
        return frozenset()

    return MemoryStore(
        index=default_index(),
        embed=StubEmbed(),
        transaction=workspace_tx,
        workspace_id=workspace_id,
        page_states=ext.page_states,
        readable_page_states=readable,
        readable_source_ids=readable_ids,
    )


async def _row(item_id: UUID) -> sa.Row:
    async with workspace_tx() as connection:
        return (
            await connection.execute(
                sa.select(
                    memory_item.c.superseded_by,
                    memory_item.c.overtaken_by,
                    memory_item.c.embedding_digest,
                    memory_item.c.created_at,
                ).where(memory_item.c.id == item_id)
            )
        ).one()


def test_half_life_covers_summaries_and_spares_the_wiki_paragraphs() -> None:
    assert half_life_days(SEMANTIC, "decision") == HALFLIFE_DAYS["decision"]
    assert half_life_days(FACT, "event") == HALFLIFE_DAYS["event"]
    assert half_life_days(SECTION, "decision") is None
    assert half_life_days(OVERVIEW, "fact") is None
    assert half_life_days(EPISODIC, "fact") is None


async def test_supersede_stamps_a_live_row_in_its_audience_and_makes_it_due(db: None) -> None:
    workspace_id = await _workspace()
    store = _store(workspace_id)
    with ws(workspace_id):
        older = await store.commit(
            MemoryWrite(subject=SHARED_SUBJECT, body="the release review is on thursday")
        )
        private = await store.commit(
            MemoryWrite(subject=member_subject(uuid4()), body="my review is on thursday")
        )
        newer = await store.commit(
            MemoryWrite(subject=SHARED_SUBJECT, body="the release review moved to wednesday")
        )
        async with workspace_tx() as connection:
            await connection.execute(
                sa.update(memory_item)
                .values(embedding_digest="sha256:settled")
                .where(memory_item.c.id.in_([older, private]))
            )

        assert await store.supersede(older, newer, SHARED)
        assert not await store.supersede(private, newer, SHARED)
        assert not await store.supersede(newer, newer, SHARED)
        assert not await store.supersede(older, uuid4(), SHARED)

    stamped, untouched, head = await _row(older), await _row(private), await _row(newer)
    assert stamped.superseded_by == newer
    assert stamped.embedding_digest is None
    assert untouched.superseded_by is None
    assert untouched.embedding_digest == "sha256:settled"
    assert head.superseded_by is None


async def test_a_page_re_derivation_leaves_a_superseded_row_superseded(db: None) -> None:
    workspace_id = await _workspace()
    store = _store(workspace_id)
    page_id, source_id = uuid4(), uuid4()
    derived = MemoryWrite(
        subject=SHARED_SUBJECT,
        body="control — The workflow is active in acme/ufo.",
        created_from_page_id=page_id,
        created_from_page_revision=3,
        source_id=source_id,
    )
    with ws(workspace_id):
        row_id = await store.commit(derived)
        verdict = await store.commit(
            MemoryWrite(subject=SHARED_SUBJECT, body="acme/ufo — Archived on 18 September.")
        )
        assert await store.supersede(row_id, verdict, SHARED)
        before = await _row(row_id)
        assert await store.commit(derived) == row_id

    after = await _row(row_id)
    assert after.superseded_by == verdict
    assert after.created_at == before.created_at


async def test_supersede_clears_the_stamp_the_sweep_left(db: None) -> None:
    workspace_id = await _workspace()
    store = _store(workspace_id)
    july = datetime(2026, 7, 1, tzinfo=UTC)
    september = datetime(2026, 9, 19, tzinfo=UTC)
    name = "acme/ufo"
    with ws(workspace_id):
        dated = await store.commit(
            MemoryWrite(
                subject=SHARED_SUBJECT,
                body="pull requests open against acme/ufo",
                as_of=july,
            )
        )
        correction = await store.commit(
            MemoryWrite(
                subject=SHARED_SUBJECT,
                body="acme/ufo — Archived on 18 September.",
                as_of=september,
            )
        )
        replacement = await store.commit(
            MemoryWrite(subject=SHARED_SUBJECT, body="pull requests open against ufo-ai/ufo")
        )
        assert await store.overtake_naming(name, correction, SHARED, name_pattern(name)) == 1
        assert (await _row(dated)).overtaken_by == correction

        assert await store.supersede(dated, replacement, SHARED)

    hidden = await _row(dated)
    assert hidden.superseded_by == replacement
    assert hidden.overtaken_by is None


async def test_a_members_restatement_revives_a_superseded_row_and_makes_it_due(db: None) -> None:
    workspace_id = await _workspace()
    store = _store(workspace_id)
    indexer = MemoryIndexer(
        index=store.index,
        embed=store.embed,
        transaction=workspace_tx,
        chunker=TextChunker(),
        workspace_id=workspace_id,
        page_states=store.page_states,
    )
    stated = MemoryWrite(subject=SHARED_SUBJECT, body="pull requests open against acme/ufo")
    scope = None
    with ws(workspace_id):
        row_id = await store.commit(stated)
        verdict = await store.commit(
            MemoryWrite(subject=SHARED_SUBJECT, body="pull requests open against ufo-ai/ufo")
        )
        await indexer.run()
        scope = IndexScope(OWNER_KIND_MEMORY_ITEM, str(row_id))
        assert await store.index.has_chunks(scope)
        assert await store.supersede(row_id, verdict, SHARED)
        await indexer.run()
        assert not await store.index.has_chunks(scope)
        assert (await _row(row_id)).embedding_digest is not None

        assert await store.commit(stated) == row_id
        revived = await _row(row_id)
        assert revived.superseded_by is None
        assert revived.embedding_digest is None

        await indexer.run()
        assert await store.index.has_chunks(scope)


async def test_the_indexer_withdraws_a_superseded_rows_chunks(db: None) -> None:
    workspace_id = await _workspace()
    store = _store(workspace_id)
    indexer = MemoryIndexer(
        index=store.index,
        embed=store.embed,
        transaction=workspace_tx,
        chunker=TextChunker(),
        workspace_id=workspace_id,
        page_states=store.page_states,
    )
    with ws(workspace_id):
        older = await store.commit(
            MemoryWrite(subject=SHARED_SUBJECT, body="the sync runs hourly on the ledger")
        )
        newer = await store.commit(
            MemoryWrite(subject=SHARED_SUBJECT, body="the sync runs every four hours on the ledger")
        )
        await indexer.run()
        scope = IndexScope(OWNER_KIND_MEMORY_ITEM, str(older))
        assert await store.index.has_chunks(scope)

        assert await store.supersede(older, newer, SHARED)
        assert (await _row(older)).embedding_digest is None
        await indexer.run()

        assert not await store.index.has_chunks(scope)
        assert await store.index.has_chunks(IndexScope(OWNER_KIND_MEMORY_ITEM, str(newer)))
    settled = await _row(older)
    assert settled.embedding_digest is not None
    assert settled.superseded_by == newer


async def test_recall_never_serves_a_superseded_row_from_the_tail(db: None) -> None:
    workspace_id = await _workspace()
    store = _store(workspace_id)
    with ws(workspace_id):
        older = await store.commit(
            MemoryWrite(subject=SHARED_SUBJECT, body="the mascot is named zoltar")
        )
        newer = await store.commit(
            MemoryWrite(subject=SHARED_SUBJECT, body="the mascot is named zephyr")
        )
        assert await store.supersede(older, newer, SHARED)
        recalled = await store.recall(
            "mascot named",
            SHARED,
            10,
            source_reader=SourceReader(
                agent_id=uuid4(), requesting_member_id=None, subjects=SHARED
            ),
        )
    assert [item.memory_id for item in recalled] == [newer]


async def test_the_drain_withdraws_chunks_of_rows_superseded_before_the_indexer_could(
    db: None,
) -> None:
    workspace_id = await _workspace()
    store = _store(workspace_id)
    indexer = MemoryIndexer(
        index=store.index,
        embed=store.embed,
        transaction=workspace_tx,
        chunker=TextChunker(),
        workspace_id=workspace_id,
        page_states=store.page_states,
    )
    with ws(workspace_id):
        old = await store.commit(MemoryWrite(subject=SHARED_SUBJECT, body="the sync runs hourly"))
        head = await store.commit(
            MemoryWrite(subject=SHARED_SUBJECT, body="the sync runs every four hours")
        )
        await indexer.run()
        async with workspace_tx() as connection:
            await connection.execute(
                sa.update(memory_item).values(superseded_by=head).where(memory_item.c.id == old)
            )
        memo = ScopedStore(extension="memory")
        await memo.put("superseded_chunks_drain", {"after": None})
        drain = SupersededChunkDrain(
            index=store.index,
            transaction=workspace_tx,
            store=memo,
            workspace_id=workspace_id,
            marker_key="superseded_chunks_drain",
            batch=100,
        )
        assert await drain.run() == 1
        old_chunks = await store.index.has_chunks(IndexScope(OWNER_KIND_MEMORY_ITEM, str(old)))
        head_chunks = await store.index.has_chunks(IndexScope(OWNER_KIND_MEMORY_ITEM, str(head)))
        marker = await memo.get("superseded_chunks_drain")
        digest = (await _row(old)).embedding_digest
    assert not old_chunks
    assert head_chunks
    assert marker is None
    assert digest is not None
