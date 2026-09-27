"""A correction's declared names swept over the rows that carry them.

`memory_update` takes `deprecates`: names from older rows that the correction makes out of date.
The handler admits each name when it names no more live rows than one sweep stamps, and the names
land on the row itself, unswept. `Sweep.run`, the per-minute job, takes every declared
row not yet swept: each live row strictly older than it that carries one of its names as a whole
name, under the audiences the correction reaches, is stamped `overtaken_by` it, and the row is
marked swept. Recall then serves each stamped row with the correction quoted under it. No model
reads anything here: the declaration is the judgement, and the stamp is one UPDATE per name.
"""

import logging
import re
from dataclasses import dataclass

from ufo.sdk.audience import audience_subjects, parse_audience
from ufo.sdk.subjects import SHARED_SUBJECT
from ufo_ext_memory.store import SWEEP_MAX_ROWS, MemoryStore

logger = logging.getLogger(__name__)

NAME_MAX_CHARS = 120
NAMES_MAX = 8


def name_pattern(name: str) -> re.Pattern[str]:
    """`name` as a whole name, in any case. A name inside a longer path or slug — `acme/ufo` inside
    `acme/ufo-web`, `ufo-core` inside `ufo-ai/ufo-core` — is another thing; the same name
    capitalised at a sentence start is the same thing."""
    return re.compile(rf"(?<![\w/]){re.escape(name)}(?![\w-])", re.IGNORECASE)


def reach(subject: str, present: frozenset[str]) -> frozenset[str]:
    """The audiences a correction stamps. A shared correction reaches every present audience whose
    readers read the shared atom — the workspace, its members, its rooms — and never a sealed
    foreign channel, whose readers would meet the stamp but never the correction it quotes. Any
    other correction reaches only its own audience, since a private statement never rewrites shared
    memory."""
    if subject != SHARED_SUBJECT:
        return frozenset({subject})
    return frozenset(
        present_subject
        for present_subject in present
        if SHARED_SUBJECT in audience_subjects(parse_audience(present_subject))
    )


async def admit(
    store: MemoryStore,
    subject: str,
    names: tuple[str, ...],
    ceiling: int = SWEEP_MAX_ROWS,
) -> frozenset[str]:
    """Reject declarations that name too many live rows in the audiences they reach."""
    subjects = reach(subject, await store.subjects_present())
    for name in names:
        count = await store.naming_count(name, subjects)
        if count > ceiling:
            raise ValueError(
                f"No memory was saved. `deprecates` name {name!r} matches {count} rows. "
                "Retry with a narrower name in `deprecates`; omitting "
                "`deprecates` leaves the old memory active."
            )
    return subjects


@dataclass(frozen=True)
class Sweep:
    """The declared rows not yet swept, taken one tick at a time."""

    store: MemoryStore

    async def run(self) -> None:
        """Stamp each unswept declaration's names, then mark the row swept. A row is marked only
        after its names are stamped, so a tick that stops midway leaves the rest for the next one,
        and a stamp reaches only rows carrying no pointer, so a row swept twice stamps nothing
        new."""
        present = await self.store.subjects_present()
        for declared in await self.store.unswept():
            subjects = reach(declared.subject, present)
            for name in declared.names:
                stamped = await self.store.overtake_naming(
                    name, declared.id, subjects, name_pattern(name)
                )
                logger.info(
                    "memory.sweep",
                    extra={"by": str(declared.id), "name": name, "stamped": stamped},
                )
            await self.store.swept(declared.id)
