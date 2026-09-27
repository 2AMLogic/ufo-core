"""Who a source-page read runs as: the agent, the member behind it, and the subjects it holds."""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class SourceReader:
    agent_id: UUID
    requesting_member_id: UUID | None
    subjects: frozenset[str]
