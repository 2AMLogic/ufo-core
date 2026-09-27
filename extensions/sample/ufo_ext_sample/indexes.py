from dataclasses import dataclass, field, replace

from ufo.sdk.index import Chunk, Hit, IndexScope

INDEX_BACKEND = "sample_index"


@dataclass(frozen=True)
class SampleIndex:
    """A trivial in-process IndexBackend the probe registers through the `indexes` Manifest point:
    it stores chunks in a dict, ranks lexical by term count and vector by dot product, each filtered
    to the queried owner kind and subjects, and prunes a scope's chunks outside a keep-set. A real
    backend consumed through the protocol, so a test drives it exactly as core does; the
    dialect-native backends keep their own retrieval proofs."""

    chunks: dict[str, Chunk] = field(default_factory=dict)

    async def upsert(self, chunks: tuple[Chunk, ...]) -> None:
        for chunk in chunks:
            self.chunks[chunk.chunk_digest] = chunk

    async def delete(self, scope: IndexScope) -> None:
        for digest in [digest for digest, chunk in self.chunks.items() if _in_scope(chunk, scope)]:
            del self.chunks[digest]

    async def has_chunks(self, scope: IndexScope) -> bool:
        return any(_in_scope(chunk, scope) for chunk in self.chunks.values())

    async def restamp(self, scope: IndexScope, subject: str, keep: frozenset[str]) -> bool:
        held = {digest for digest, chunk in self.chunks.items() if _in_scope(chunk, scope)}
        if held != keep:
            return False
        for digest in held:
            self.chunks[digest] = replace(self.chunks[digest], subject=subject)
        return True

    async def prune(self, scope: IndexScope, keep: frozenset[str]) -> None:
        for digest in [
            digest
            for digest, chunk in self.chunks.items()
            if _in_scope(chunk, scope) and digest not in keep
        ]:
            del self.chunks[digest]

    async def lexical(
        self, query: str, subjects: frozenset[str], owner_kind: str, limit: int
    ) -> tuple[Hit, ...]:
        terms = frozenset(query.lower().split())
        scored = [
            _hit(chunk, float(count))
            for chunk in self._scoped(subjects, owner_kind)
            if (count := sum(chunk.text.lower().count(term) for term in terms)) > 0
        ]
        return tuple(sorted(scored, key=lambda hit: hit.score, reverse=True)[:limit])

    async def vector(
        self, embedding: tuple[float, ...], subjects: frozenset[str], owner_kind: str, limit: int
    ) -> tuple[Hit, ...]:
        scored = [
            _hit(chunk, score)
            for chunk in self._scoped(subjects, owner_kind)
            if (score := _dot(chunk.embedding, embedding)) > 0
        ]
        return tuple(sorted(scored, key=lambda hit: hit.score, reverse=True)[:limit])

    def _scoped(self, subjects: frozenset[str], owner_kind: str) -> list[Chunk]:
        return [
            chunk
            for chunk in self.chunks.values()
            if chunk.owner_kind == owner_kind and chunk.subject in subjects
        ]


def _in_scope(chunk: Chunk, scope: IndexScope) -> bool:
    return chunk.owner_kind == scope.owner_kind and chunk.owner_id == scope.owner_id


def _dot(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if not left or not right:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True))


def _hit(chunk: Chunk, score: float) -> Hit:
    return Hit(
        chunk_digest=chunk.chunk_digest,
        owner_kind=chunk.owner_kind,
        owner_id=chunk.owner_id,
        subject=chunk.subject,
        ordinal=chunk.ordinal,
        text=chunk.text,
        score=score,
    )
