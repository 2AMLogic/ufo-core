"""Shared JSONL-store mechanics beneath the covered ledger and the sightings pool.

Both stores are the same shape on disk — a workspace-relative, append-only JSON Lines file per
series, named by the same slug pattern, read the same way, appended the same way. This module owns
that shape: the slug pattern, the path builder that validates it, reading, and the append tail. It
owns none of what the two stores mean — `covered.py` and `seen.py` each keep their own field
validation, their own row shape, and all of the fresh/stale/recent/covered domain logic the two
stores' docstrings distinguish.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def dated_path(directory: str, series: str, suffix: str) -> Path:
    """The per-series file for one store: `<directory>/<series>.<suffix>`, slug-validated.

    `suffix` carries the store's own extension (`seen.jsonl`, `covered.jsonl`) so the two stores
    never collide on the same series name.
    """
    if not SLUG.fullmatch(series):
        raise ValueError(f"series must be a lowercase hyphenated slug, got {series!r}")
    return Path(directory) / f"{series}.{suffix}"


def read_rows(path: Path) -> list[dict]:
    """Every row of one store's file, in file order. An unwritten file reads as no rows."""
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def append_row(path: Path, row: dict) -> None:
    """Append one row, creating the store's directory on first write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
