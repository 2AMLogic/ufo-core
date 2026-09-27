"""A context-rollover history journal on the local filesystem. The engine, hook, and rollover tests
roll a conversation over without a sandbox, so the journal a sandbox would hold as a file is held
here on disk instead and read back line by line."""

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FileJournal:
    """The history file on the local filesystem — what the artifact leaves roll over into, since
    they run no sandbox, and what the grader reads back line by line."""

    path: Path

    async def display_path(self) -> str:
        return str(self.path)

    async def append(self, lines: tuple[str, ...], after: int) -> tuple[int, int]:
        held = await asyncio.to_thread(self._read_lines)
        kept = (*held[:after], *lines)
        await asyncio.to_thread(self.path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(
            self.path.write_text, "".join(f"{line}\n" for line in kept), "utf-8"
        )
        return len(held), len(kept)

    async def text(self, first: int, last: int) -> str:
        """The rendered text of lines `first`..`last`, inclusive, joined — the surface the grader
        holds a fact against."""
        held = await asyncio.to_thread(self._read_lines)
        return "\n".join(json.loads(line)["text"] for line in held[max(first, 1) - 1 : last])

    async def lines(self) -> int:
        return len(await asyncio.to_thread(self._read_lines))

    def _read_lines(self) -> list[str]:
        if not self.path.is_file():
            return []
        return self.path.read_text("utf-8").splitlines()
