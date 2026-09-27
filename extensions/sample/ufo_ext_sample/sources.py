from dataclasses import dataclass
from typing import ClassVar

from pydantic import BaseModel, Field

from ufo.sdk.sources import Page, SourceAuth, SyncResult

SOURCE_BACKEND = "sample_source"
SOURCE_REF = "sample/handbook"
SOURCE_TOPIC = "the sample source syncs a page about migrating the orbital widget fleet"


class SampleSourceConfig(BaseModel):
    """The sample source's typed per-source config — a distinct shape from the folder backend's, so
    it proves a backend carries its own typed parameters, never a shared bag."""

    topic: str = Field(min_length=1, pattern=r"\S")


@dataclass(frozen=True)
class SampleSource:
    """A canned content-source backend: `fetch` renders one deterministic page from its typed
    config, exercising the seam core drives (register a source, poll it, land its page in memory,
    index it for recall). `auth` is threaded but unused — a folder-like local source resolves no
    provider token."""

    config_model: ClassVar[type[SampleSourceConfig]] = SampleSourceConfig

    async def fetch(
        self, config: SampleSourceConfig, cursor: str | None, auth: SourceAuth
    ) -> SyncResult:
        page = Page(
            source_ref=SOURCE_REF,
            body=config.topic,
            stream="topics",
            title=next(line.strip() for line in config.topic.splitlines() if line.strip()),
        )
        return SyncResult(pages=(page,), next_cursor=None)
