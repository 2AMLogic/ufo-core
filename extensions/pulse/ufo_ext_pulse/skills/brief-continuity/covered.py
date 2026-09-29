"""The covered ledger: what a brief series has already published, and what it may carry again.

One JSON Lines file per series at `pulse/<series>.covered.jsonl` in the conversation workspace,
append-only, one row per story per edition. `record` appends a row; `recent` reads the rows belonging
to the last N editions; `check` answers whether one slug is inside that span. A slug carried again
under the material-new-development exception appears once per edition that carried it, so the history
of a story is the rows sharing its slug.

The path is workspace-relative, and that is what keeps the ledger where the member can open it and
where every carrier lets a command write. Only a command's argv is rewritten to the carrier's own
workspace directory, never a path inside a script, so the ledger resolves against the working
directory a sandbox command starts in: the workspace root.

Append-only is what makes the ledger readable as history: a row states that an edition published a
story, which stays true after the story develops.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _jsonl_pool import SLUG, append_row, dated_path  # noqa: E402
from _jsonl_pool import read_rows as _read_rows  # noqa: E402

EDITION = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
LEDGER_DIR = "pulse"
DEFAULT_EDITIONS = 5


def ledger_path(series: str) -> Path:
    return dated_path(LEDGER_DIR, series, "covered.jsonl")


def read_rows(series: str) -> list[dict]:
    return _read_rows(ledger_path(series))


def recent_rows(series: str, editions: int) -> list[dict]:
    """Every row belonging to the most recent `editions` distinct edition dates.

    The span is counted in editions rather than days because a series sets its own cadence: five
    editions is five briefs the reader has seen, whether they fell over one week or three.
    """
    rows = read_rows(series)
    dates = sorted({row["edition"] for row in rows}, reverse=True)[:editions]
    return [row for row in rows if row["edition"] in set(dates)]


def record(series: str, edition: str, slug: str, title: str, url: str) -> Path:
    """The shape of one ledger line.

**This function is not how a row is recorded.** `pulse_record_sightings` and `pulse_record_edition`
write the record; this file is a projection rendered whole from it, so a row appended here is erased
by the next projection rather than kept. It survives as the definition of the row shape the
projection must match — `tests/test_projection_shape.py` asserts the two are byte-identical — and
there is deliberately no `record` subcommand, so nothing reachable from a shell can write here.
    """
    if not EDITION.fullmatch(edition):
        raise ValueError(f"edition must be YYYY-MM-DD, got {edition!r}")
    if not SLUG.fullmatch(slug):
        raise ValueError(f"slug must be a lowercase hyphenated slug, got {slug!r}")
    path = ledger_path(series)
    row = {"edition": edition, "slug": slug, "title": title, "url": url}
    append_row(path, row)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_recent = sub.add_parser("recent", help="rows from the last N editions")
    p_recent.add_argument("--series", required=True)
    p_recent.add_argument("--editions", type=int, default=DEFAULT_EDITIONS)

    p_check = sub.add_parser("check", help="whether a slug is covered in the last N editions")
    p_check.add_argument("--series", required=True)
    p_check.add_argument("--slug", required=True)
    p_check.add_argument("--editions", type=int, default=DEFAULT_EDITIONS)

    args = parser.parse_args(argv)

    if args.command == "recent":
        rows = recent_rows(args.series, args.editions)
        if not rows:
            print("no editions recorded")
            return 0
        for row in rows:
            print(f"{row['edition']}  {row['slug']:<40}{row['title']}")
        return 0

    rows = recent_rows(args.series, args.editions)
    hits = [row for row in rows if row["slug"] == args.slug]
    if hits:
        editions = ", ".join(sorted({row["edition"] for row in hits}))
        print(f"covered in {editions} — ineligible without a material new development")
        return 1
    print("not covered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
