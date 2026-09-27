from uuid import UUID, uuid4

import sqlalchemy as sa
from ufo_ext_embed_openai import EMBED_DIM
from ufo_ext_memory.store import MemoryIndexer, MemoryStore, MemoryWrite, memory_item
from ufo_testsupport.index import default_index

from ufo.db import workspace_tx
from ufo.runtime.ext.context import context_for
from ufo.runtime.indexing import OWNER_KIND_MEMORY_ITEM, IndexScope, TextChunker
from ufo.runtime.turns.subjects import SHARED_SUBJECT
from ufo.runtime.workspace import ws
from ufo.schema import tables


class _Embed:
    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        return tuple((1.0,) + (0.0,) * (EMBED_DIM - 1) for _ in texts)


async def _workspace() -> UUID:
    workspace_id = uuid4()
    async with workspace_tx() as connection:
        await connection.execute(
            sa.insert(tables.workspace).values(
                id=workspace_id, created_at=sa.func.now(), updated_at=sa.func.now()
            )
        )
    return workspace_id


async def _commit(workspace_id: UUID, body: str) -> UUID:
    ext = context_for("memory", frozenset())
    store = MemoryStore(
        index=default_index(),
        embed=_Embed(),
        transaction=workspace_tx,
        workspace_id=workspace_id,
        page_states=ext.page_states,
        readable_page_states=ext.readable_page_states,
        readable_source_ids=ext.readable_source_ids,
    )
    with ws(workspace_id):
        return await store.commit(MemoryWrite(subject=SHARED_SUBJECT, body=body))


async def test_the_indexer_claims_and_embeds_only_its_own_workspaces_items(db: None) -> None:
    mine, theirs = await _workspace(), await _workspace()
    my_item = await _commit(mine, "the release train leaves on thursdays")
    their_item = await _commit(theirs, "the release train leaves on fridays")
    index = default_index()

    with ws(mine):
        await MemoryIndexer(
            index=index,
            embed=_Embed(),
            transaction=workspace_tx,
            chunker=TextChunker(),
            workspace_id=mine,
            page_states=context_for("memory", frozenset()).page_states,
        ).run()
        assert await index.has_chunks(IndexScope(OWNER_KIND_MEMORY_ITEM, str(my_item)))

    async with workspace_tx() as connection:
        rows = {
            row.id: row
            for row in await connection.execute(
                sa.select(
                    memory_item.c.id,
                    memory_item.c.embedding_digest,
                    memory_item.c.embedding_claimed_at,
                )
            )
        }
    assert rows[my_item].embedding_digest is not None
    assert (rows[their_item].embedding_digest, rows[their_item].embedding_claimed_at) == (
        None,
        None,
    )
    with ws(theirs):
        assert not await index.has_chunks(IndexScope(OWNER_KIND_MEMORY_ITEM, str(their_item)))
