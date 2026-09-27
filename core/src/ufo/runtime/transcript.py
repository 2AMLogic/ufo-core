"""The conversation's one durable transcript writer: a guard monotonic in seq, then in what the
write knows, over the shared blob contract in `ufo.runtime.turns.transcript`. Only the run that ends
a turn — or the repair flow republishing its committed terminal — writes at that turn's seq, and
the run's own record outranks the repair fallback whichever lands first."""

from dataclasses import dataclass
from uuid import UUID

from ufo.blob import BlobNotFound, WorkspaceBlobStore
from ufo.runtime.turns.transcript import Conversation, decode, encode, transcript_key


@dataclass(frozen=True)
class Transcript:
    blob: WorkspaceBlobStore
    conversation_id: UUID

    async def read(self) -> Conversation | None:
        try:
            body = await self.blob.get(transcript_key(self.conversation_id))
        except BlobNotFound:
            return None
        return decode(body)

    async def write(self, conversation: Conversation) -> bool:
        current = await self.read()
        if current is not None and not _supersedes(conversation, current):
            return False
        await self.blob.put(transcript_key(self.conversation_id), encode(conversation))
        return True


def _supersedes(incoming: Conversation, stored: Conversation) -> bool:
    """The terminal row commits before the blob is written, so a redelivery can reach the fallback
    while the run that owns the seq is still writing."""
    if incoming.seq != stored.seq:
        return incoming.seq > stored.seq
    if not incoming.from_run:
        return False
    if incoming.parked is not None:
        return not stored.from_run or stored.parked is not None
    if stored.from_run:
        if stored.parked is not None:
            return True
        return len(incoming.messages) > len(stored.messages)
    return len(incoming.messages) >= len(stored.messages)
