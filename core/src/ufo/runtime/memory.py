"""The typed memory-search seam shared by extensions that provide and consume recall."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from ufo.runtime.ext.source_reader import SourceReader
from ufo.runtime.listings import ListingCursor, ListingPage
from ufo.runtime.object_name import ObjectRef

DEFAULT_MEMORY_SEARCH_PROVIDER = "default"
PAGE_SOURCE_KIND = "page"
CONVERSATION_SOURCE_KIND = "conversation"


@dataclass(frozen=True)
class MemorySource:
    """One place a memory came from, as the ref a surface opens: `page/<uid>` with the page's
    `provider` and `title`, or `conversation/<id>` with neither."""

    ref: ObjectRef
    provider: str | None = None
    title: str | None = None


@dataclass(frozen=True)
class MemoryMatch:
    """One provider-neutral memory result ready for a consumer to inject. `ref` is the durable
    object behind the hit — search finds, `object_get` opens — and `created_at` is its recency,
    rendered beside the snippet so hits are triaged without opening them; both are None only for
    a provider whose results are not object-backed.

    `created_from_page_id`, `page_provider` and `page_title` name the synced page a hit came from,
    and only where the reader that asked may read that page. `created_from_conversation_id` is the
    conversation a tool write happened in; a consumer still checks that its viewer may open it.
    `derived_from` is the union of the sources of the rows a condensed row stands for, its pages
    fenced the same way and its conversations still for the consumer to check."""

    kind: str
    text: str
    ref: ObjectRef | None = None
    created_at: datetime | None = None
    subject: str | None = None
    page_provider: str | None = None
    page_title: str | None = None
    created_from_conversation_id: UUID | None = None
    created_from_page_id: UUID | None = None
    derived_from: tuple[MemorySource, ...] = ()

    @property
    def source(self) -> ObjectRef | None:
        """The row's own provenance as one ref: its page where the reader may read it, else the
        conversation it was written in."""
        if self.created_from_page_id is not None:
            return ObjectRef(kind=PAGE_SOURCE_KIND, name=str(self.created_from_page_id))
        if self.created_from_conversation_id is not None:
            return ObjectRef(
                kind=CONVERSATION_SOURCE_KIND, name=str(self.created_from_conversation_id)
            )
        return None

    @property
    def sources(self) -> tuple[MemorySource, ...]:
        """The row's own source first, then every distinct source of the rows it stands for."""
        own = self.source
        named = (
            ()
            if own is None
            else (MemorySource(own, self.page_provider, self.page_title),)
            if own.kind == PAGE_SOURCE_KIND
            else (MemorySource(own),)
        )
        distinct: dict[ObjectRef, MemorySource] = {}
        for entry in (*named, *self.derived_from):
            distinct.setdefault(entry.ref, entry)
        return tuple(distinct.values())


class MemorySearchProvider(Protocol):
    """A memory extension's workspace-ambient search implementation. `list_recent` is the
    browse half: the live memory items a subject set may read in recency order, no query and no
    similarity — source pages stay search-only, so it takes subjects rather than a reader, and
    `readers` fence only which origin pages a row may name: a page is named when one of them may
    read it, and no reader names none. It pages by keyset (`cursor`), never by offset: items land
    while a member reads, and an offset would repeat or skip a row across that write. `kinds`
    narrows to item classes, and `listable_kinds` is the closed set a consumer offers — the
    provider's own classes, so a class it starts writing cannot go missing from the filter."""

    async def search(
        self,
        queries: tuple[str, ...],
        reader: SourceReader,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> tuple[MemoryMatch, ...]: ...

    async def list_recent(
        self,
        subjects: frozenset[str],
        limit: int,
        kinds: frozenset[str] | None = None,
        cursor: ListingCursor | None = None,
        readers: tuple[SourceReader, ...] = (),
    ) -> ListingPage[MemoryMatch]: ...

    def listable_kinds(self) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class MemorySearch:
    """Dispatch one exact readable subject set to the selected provider."""

    provider: MemorySearchProvider

    async def search(
        self,
        reader: SourceReader,
        queries: tuple[str, ...],
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> tuple[MemoryMatch, ...]:
        return await self.provider.search(queries, reader, start, end)

    async def list_recent(
        self,
        subjects: frozenset[str],
        limit: int,
        kinds: frozenset[str] | None = None,
        cursor: ListingCursor | None = None,
        readers: tuple[SourceReader, ...] = (),
    ) -> ListingPage[MemoryMatch]:
        return await self.provider.list_recent(subjects, limit, kinds, cursor, readers)

    def listable_kinds(self) -> tuple[str, ...]:
        return self.provider.listable_kinds()
