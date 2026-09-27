"""The sweep a correction's declared names run over the rows that carry them: every older live
row naming the thing as a whole name is overtaken by the correction, a same-named sibling, a
newer row, a superseded row, and the correction itself are not, a private correction stays in its
own audience, a restated declaration is swept again, and a swept row is swept once. The store is
the real one; every assertion reads memory_item rows back."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from ufo_ext_embed_openai import EMBED_DIM
from ufo_ext_memory.store import MemoryStore, MemoryWrite, memory_item, store_for
from ufo_ext_memory.sweep import Sweep, admit, name_pattern, reach
from ufo_testsupport.index import default_index

from ufo.db import workspace_tx
from ufo.runtime.ext.context import context_for
from ufo.runtime.turns.subjects import SHARED_SUBJECT, member_subject
from ufo.runtime.workspace import ws
from ufo.schema import tables

pytestmark = [
    pytest.mark.usefixtures("database_url"),
    pytest.mark.parametrize("database_url", ["sqlite"], indirect=True),
]

PROBE = tuple(1.0 if index == 0 else 0.0 for index in range(EMBED_DIM))
JULY = datetime(2026, 7, 1, tzinfo=UTC)
SEPTEMBER = datetime(2026, 9, 19, tzinfo=UTC)
OCTOBER = datetime(2026, 10, 1, tzinfo=UTC)
ARCHIVED = "acme/ufo"
DEPRECATION = "acme/ufo — Archived on 18 September; work lands in acme/ufo-web."
MEMBER = member_subject(UUID("00000000-0000-0000-0000-00000000c0de"))


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


def _store() -> MemoryStore:
    return store_for(
        context_for(
            "memory",
            frozenset(),
            index=default_index(),
            embed=StubEmbed(),
        )
    )


async def _commit(
    store: MemoryStore,
    body: str,
    as_of: datetime,
    kind: str = "fact",
    subject: str = SHARED_SUBJECT,
    deprecates: tuple[str, ...] = (),
) -> UUID:
    return await store.commit(
        MemoryWrite(
            subject=subject, body=body, memory_kind=kind, as_of=as_of, deprecates=deprecates
        )
    )


async def _overtaken_by(item_id: UUID) -> UUID | None:
    async with workspace_tx() as connection:
        return (
            await connection.execute(
                sa.select(memory_item.c.overtaken_by).where(memory_item.c.id == item_id)
            )
        ).scalar_one()


async def _swept_at(item_id: UUID) -> datetime | None:
    async with workspace_tx() as connection:
        return (
            await connection.execute(
                sa.select(memory_item.c.swept_at).where(memory_item.c.id == item_id)
            )
        ).scalar_one()


async def _declare(store: MemoryStore, subject: str = SHARED_SUBJECT) -> UUID:
    correction = await _commit(
        store, DEPRECATION, SEPTEMBER, subject=subject, deprecates=(ARCHIVED,)
    )
    await Sweep(store=store).run()
    return correction


def test_a_name_is_read_as_a_whole_name_in_any_case() -> None:
    pattern = name_pattern(ARCHIVED)
    assert pattern.search("PR 12 merged into acme/ufo.")
    assert pattern.search("(acme/ufo) is the main repo")
    assert pattern.search("Acme/ufo issue 1496 was closed as completed.")
    assert not pattern.search("PR 7 opened against acme/ufo-web")
    assert not pattern.search("github.com/acme/ufo-core")
    assert not name_pattern("ufo-core").search("ufo-ai/ufo-core")
    assert name_pattern("ufo-core").search("ported to ufo-core today")


def test_a_shared_correction_reaches_the_audiences_that_read_shared_and_a_private_one_its_own() -> (
    None
):
    room = "room:slack:C0BJURDE76E"
    sealed = "foreign:slack:C0EXTERNAL"
    present = frozenset({SHARED_SUBJECT, MEMBER, room, sealed})
    assert reach(SHARED_SUBJECT, present) == frozenset({SHARED_SUBJECT, MEMBER, room})
    assert reach(MEMBER, present) == frozenset({MEMBER})
    assert reach(sealed, present) == frozenset({sealed})


async def test_the_sweep_overtakes_the_older_rows_naming_the_thing(db: None) -> None:
    workspace_id = await _workspace()
    with ws(workspace_id):
        store = _store()
        merged = await _commit(
            store, f"PR 1839 — Merged into {ARCHIVED} on 17 August.", JULY, "event"
        )
        rule = await _commit(
            store, f"Issues — Default the target repo to {ARCHIVED}.", JULY, "preference"
        )
        capitalised = await _commit(
            store, "Acme/ufo issue 1496 — Closed as completed.", JULY, "event"
        )
        private = await _commit(
            store, f"Marshall's fork — Tracks {ARCHIVED} main.", JULY, subject=MEMBER
        )
        sibling = await _commit(store, f"PR 7 — Opened against {ARCHIVED}-web.", JULY, "event")
        newer = await _commit(
            store, f"{ARCHIVED} — Unarchived briefly for a hotfix.", OCTOBER, "event"
        )
        y = await _declare(store)
        stamps = {
            name: await _overtaken_by(item_id)
            for name, item_id in {
                "merged": merged,
                "rule": rule,
                "capitalised": capitalised,
                "private": private,
                "sibling": sibling,
                "y": y,
                "newer": newer,
            }.items()
        }
        swept = await _swept_at(y)
        pending = await store.unswept()
    assert stamps == {
        "merged": y,
        "rule": y,
        "capitalised": y,
        "private": y,
        "sibling": None,
        "y": None,
        "newer": None,
    }
    assert swept is not None
    assert pending == ()


async def test_a_swept_row_is_swept_once_and_a_restated_declaration_again(db: None) -> None:
    workspace_id = await _workspace()
    with ws(workspace_id):
        store = _store()
        older = await _commit(store, f"{ARCHIVED} — Default branch is main.", JULY)
        y = await _declare(store)
        first_swept = await _swept_at(y)
        stamped_older = await _overtaken_by(older)
        later = await _commit(store, f"{ARCHIVED} — Squash merges only.", JULY)
        await Sweep(store=store).run()
        untouched = await _overtaken_by(later)

        assert await _commit(store, DEPRECATION, SEPTEMBER, deprecates=(ARCHIVED,)) == y
        rearmed = await _swept_at(y)
        await Sweep(store=store).run()
        stamped_later = await _overtaken_by(later)
        second_swept = await _swept_at(y)
    assert first_swept is not None
    assert stamped_older == y
    assert untouched is None
    assert rearmed is None
    assert stamped_later == y
    assert second_swept is not None


async def test_a_superseded_row_and_a_stamped_row_take_no_second_stamp(db: None) -> None:
    workspace_id = await _workspace()
    with ws(workspace_id):
        store = _store()
        old = await _commit(store, f"{ARCHIVED} — Default branch is main.", JULY)
        hidden = await _commit(store, f"{ARCHIVED} — Default branch was master.", JULY)
        assert await store.supersede(hidden, old, frozenset({SHARED_SUBJECT}))
        first = await _commit(
            store, f"{ARCHIVED} — Deprecated on 19 September.", SEPTEMBER, deprecates=(ARCHIVED,)
        )
        await Sweep(store=store).run()
        second = await _commit(store, DEPRECATION, OCTOBER, deprecates=(ARCHIVED,))
        await Sweep(store=store).run()
        old_pointer, hidden_pointer, first_pointer = (
            await _overtaken_by(old),
            await _overtaken_by(hidden),
            await _overtaken_by(first),
        )
    assert old_pointer == first
    assert hidden_pointer is None
    assert first_pointer == second


async def test_a_private_correction_stays_in_its_own_audience(db: None) -> None:
    workspace_id = await _workspace()
    with ws(workspace_id):
        store = _store()
        shared = await _commit(store, f"{ARCHIVED} — Default branch is main.", JULY)
        own = await _commit(store, f"My checkout — Clones {ARCHIVED}.", JULY, subject=MEMBER)
        y = await _declare(store, subject=MEMBER)
        shared_pointer, own_pointer = await _overtaken_by(shared), await _overtaken_by(own)
    assert shared_pointer is None
    assert own_pointer == y


async def test_admission_refuses_a_name_the_row_does_not_carry_or_names_too_much(db: None) -> None:
    workspace_id = await _workspace()
    with ws(workspace_id):
        store = _store()
        await _commit(store, f"{ARCHIVED} — Default branch is main.", JULY)
        await _commit(store, f"PR 1839 — Merged into {ARCHIVED}.", JULY, "event")
        with pytest.raises(ValueError, match="No memory was saved") as missing:
            await admit(store, SHARED_SUBJECT, DEPRECATION, ("ufo-ai/ufo",))
        with pytest.raises(ValueError, match="matches 2 rows") as broad:
            await admit(store, SHARED_SUBJECT, DEPRECATION, (ARCHIVED,), ceiling=1)
        admitted = await admit(store, SHARED_SUBJECT, DEPRECATION, (ARCHIVED,))
    assert admitted == frozenset({SHARED_SUBJECT})
    assert "keep it in `deprecates`" in str(missing.value)
    assert "narrower name in both `body` and `deprecates`" in str(broad.value)
