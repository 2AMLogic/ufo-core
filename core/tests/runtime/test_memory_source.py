"""A memory match's one provenance ref and the source list a condensed row carries."""

from uuid import uuid4

from ufo.runtime.memory import MemoryMatch, MemorySource
from ufo.runtime.object_name import ObjectRef


def test_a_readable_page_wins_over_the_conversation() -> None:
    page, chat = uuid4(), uuid4()
    match = MemoryMatch(
        kind="fact",
        text="the runway is teal",
        created_from_page_id=page,
        page_provider="slack",
        page_title="#launch",
        created_from_conversation_id=chat,
    )
    assert match.source == ObjectRef(kind="page", name=str(page))
    assert match.sources == (
        MemorySource(ObjectRef(kind="page", name=str(page)), "slack", "#launch"),
    )


def test_a_conversation_names_the_row_without_a_readable_page() -> None:
    chat = uuid4()
    match = MemoryMatch(
        kind="fact", text="the team ships weekly", created_from_conversation_id=chat
    )
    assert match.source == ObjectRef(kind="conversation", name=str(chat))


def test_a_row_with_neither_names_no_source() -> None:
    match = MemoryMatch(kind="fact", text="the office is in oslo")
    assert match.source is None
    assert match.sources == ()


def test_a_condensed_row_carries_every_distinct_source_after_its_own() -> None:
    chat, page = uuid4(), uuid4()
    page_source = MemorySource(ObjectRef(kind="page", name=str(page)), "gmail", "Offsite")
    chat_source = MemorySource(ObjectRef(kind="conversation", name=str(chat)))
    merged = MemoryMatch(
        kind="semantic",
        text="the offsite moved to june",
        derived_from=(page_source, chat_source),
    )
    corrected = MemoryMatch(
        kind="fact",
        text="the offsite is in june",
        created_from_conversation_id=chat,
        derived_from=(page_source, chat_source),
    )
    assert merged.source is None
    assert merged.sources == (page_source, chat_source)
    assert corrected.source == chat_source.ref
    assert corrected.sources == (chat_source, page_source)
