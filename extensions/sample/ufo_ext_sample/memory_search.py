from dataclasses import dataclass
from datetime import datetime

from ufo.sdk.context import ExtensionContext, JsonValue, SourceReader
from ufo.sdk.listings import ListingCursor, ListingPage
from ufo.sdk.memory import MemoryMatch

SAMPLE_MEMORY_TEXT = "the sample memory provider returns a scoped result"
SAMPLE_MEMORY_KIND = "fact"
MEMORY_SEARCH_KEY = "memory_search"
MEMORY_RECENT_KEY = "memory_recent"
MEMORY_SEARCH_PROVIDER = "sample"


@dataclass(frozen=True)
class SampleMemorySearch:
    """Record a scoped search and return one result through the public provider seam."""

    ctx: ExtensionContext

    async def search(
        self,
        queries: tuple[str, ...],
        reader: SourceReader,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> tuple[MemoryMatch, ...]:
        query_values: list[JsonValue] = [query for query in queries]
        subject_values: list[JsonValue] = [subject for subject in sorted(reader.subjects)]
        await self.ctx.store.put(
            MEMORY_SEARCH_KEY,
            {
                "queries": query_values,
                "subjects": subject_values,
                "start": None if start is None else start.isoformat(),
                "end": None if end is None else end.isoformat(),
            },
        )
        return (MemoryMatch(kind=SAMPLE_MEMORY_KIND, text=SAMPLE_MEMORY_TEXT),)

    def listable_kinds(self) -> tuple[str, ...]:
        return (SAMPLE_MEMORY_KIND,)

    async def list_recent(
        self,
        subjects: frozenset[str],
        limit: int,
        kinds: frozenset[str] | None = None,
        cursor: ListingCursor | None = None,
        readers: tuple[SourceReader, ...] = (),
    ) -> ListingPage[MemoryMatch]:
        subject_values: list[JsonValue] = [subject for subject in sorted(subjects)]
        kind_values: list[JsonValue] | None = (
            None if kinds is None else [kind for kind in sorted(kinds)]
        )
        await self.ctx.store.put(
            MEMORY_RECENT_KEY,
            {
                "subjects": subject_values,
                "limit": limit,
                "kinds": kind_values,
                "cursor": None
                if cursor is None
                else {
                    "created_at": cursor.created_at.isoformat(),
                    "item_id": cursor.item_id,
                    "newer": cursor.newer,
                },
            },
        )
        return ListingPage(rows=(MemoryMatch(kind=SAMPLE_MEMORY_KIND, text=SAMPLE_MEMORY_TEXT),))
