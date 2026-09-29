"""Coverage state: what each source returned on each gather, and what a window of them adds up to.

One JSON Lines file per series at `pulse/<series>.coverage.jsonl` in the conversation workspace, one
row per source per gather, rendered whole from the record by the projection job. `window` aggregates
every source's states across the gathers a report covers, which is what a multi-gather footer is
written from; `record` is the definition of the row shape that projection must match.

**A row is recorded with `pulse_record_coverage`, not here.** The tool writes the record, which every
turn in the workspace reaches identically; this file is a copy of it. A row appended here is erased
by the next projection rather than kept, so there is deliberately no `record` subcommand and nothing
reachable from a shell can write to the store.

`coverage-honesty` gives a source three states in one run, and a footer written from one gather says
which of them it was. A report covering three gathers holds three sets of those states, and a flat
"not read: the filings index" over them is ambiguous: unread on one day and unread on all three are
different claims about the field, and the reader acts on them differently. Telling them apart needs
each source's state on each gather, rather than the last state to be written.

Three stores, three questions. The covered ledger answers what a series has published, the sightings
pool answers what a gather has seen, and this answers what a gather could read. Its subject is a
source rather than a story, so it carries no slug for a lead: a story nobody saw and a source nobody
could reach are different facts, and one store holding both would answer neither.

The source slug is that source's durable address across gathers, the way a story slug is across
editions. The footer names the source in the reader's words; the store keys it by the slug, which is
what lets three days of one source aggregate as one source.

Nothing is ever removed, for the same reason the pool removes nothing: a gather's states are an
observation of that day, and that stays true afterwards. A retry inside one gather is that gather's
answer — a source that rate-limited the first attempt and answered the second was read that day —
and the read takes the later row wherever two of them sit side by side, which is what an imported
file holds and what the record resolves on write.

The path is workspace-relative, the way the ledger's and the pool's are: only a command's argv is
rewritten to the carrier's workspace directory, never a path inside a script. That is why the file
is a copy and never the store — `pulse/<series>.coverage.jsonl` is one tree's copy, and the tables
are the series.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _jsonl_pool import SLUG, append_row, dated_path  # noqa: E402
from _jsonl_pool import read_rows as _read_rows  # noqa: E402

GATHERED = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
COVERAGE_DIR = "pulse"
READ = "read"
READ_EMPTY = "read-empty"
NOT_READ = "not-read"
STATES = (READ, READ_EMPTY, NOT_READ)
REASONS = ("unreachable", "rate-limited", "budget-exhausted", "unauthorized")


def coverage_path(series: str) -> Path:
    return dated_path(COVERAGE_DIR, series, "coverage.jsonl")


def read_rows(series: str) -> list[dict]:
    return _read_rows(coverage_path(series))


def record(
    series: str, gathered: str, source: str, state: str, items: int = 0, reason: str = ""
) -> Path:
    """The shape of one coverage line, refused unless the row is one of the three states.

**This function is not how a row is recorded.** `pulse_record_coverage` writes the record; this file
is a projection rendered whole from it, so a row appended here is erased by the next projection
rather than kept. It survives as the definition of the row shape the projection must match —
`tests/test_projection_shape.py` asserts the two are byte-identical — and as the reader an old file
is imported through, and there is deliberately no `record` subcommand.

    A read with no items is `read-empty`, which is an answer, and a not-read row names which of the
    four failures it was — the two distinctions the footer is written from.
    """
    if not GATHERED.fullmatch(gathered):
        raise ValueError(f"gathered must be YYYY-MM-DD, got {gathered!r}")
    if not SLUG.fullmatch(source):
        raise ValueError(f"source must be a lowercase hyphenated slug, got {source!r}")
    if state not in STATES:
        raise ValueError(f"state must be one of {', '.join(STATES)}, got {state!r}")
    if state == NOT_READ:
        if reason not in REASONS:
            raise ValueError(
                f"a not-read source names one of {', '.join(REASONS)}, got {reason!r}"
            )
        if items:
            raise ValueError(f"a source that went unread returned no items, got {items}")
    else:
        if reason:
            raise ValueError(f"{state} is an answer and carries no reason, got {reason!r}")
        if state == READ and items < 1:
            raise ValueError("a source read with nothing in it is read-empty, which is an answer")
        if state == READ_EMPTY and items:
            raise ValueError(f"read-empty is the state for no items, got {items}")
    path = coverage_path(series)
    append_row(
        path,
        {
            "gathered": gathered,
            "source": source,
            "state": state,
            "items": items,
            "reason": reason,
        },
    )
    return path


def _inside(gathered: str, since: str, until: str) -> bool:
    return (not since or gathered >= since) and (not until or gathered <= until)


def gathers(series: str, since: str = "", until: str = "") -> list[str]:
    """The distinct gather dates inside the window, oldest first: the denominator of every count.

    A gather that recorded no source state at all is not one of them, so a window counts the gathers
    that reported rather than the days that passed.
    """
    dates = {row["gathered"] for row in read_rows(series) if _inside(row["gathered"], since, until)}
    return sorted(dates)


def window(series: str, since: str = "", until: str = "") -> list[dict]:
    """Every source's states across the gathers in the window, by source slug.

    Each entry carries the window's gather count, this source's count in each state, the gathers it
    recorded no state on, its items, and the reasons its unread gathers named. Those counts are what
    separate "unread on two of three" from "unread throughout" and from "read every gather".
    """
    dates = set(gathers(series, since, until))
    answers: dict[str, dict[str, dict]] = {}
    for row in read_rows(series):
        if row["gathered"] in dates:
            answers.setdefault(row["source"], {})[row["gathered"]] = row

    aggregates = []
    for source in sorted(answers):
        rows = list(answers[source].values())
        reasons: dict[str, int] = {}
        for row in rows:
            if row["state"] == NOT_READ:
                reasons[row["reason"]] = reasons.get(row["reason"], 0) + 1
        aggregates.append(
            {
                "source": source,
                "gathers": len(dates),
                "states": {state: sum(row["state"] == state for row in rows) for state in STATES},
                "unrecorded": len(dates) - len(rows),
                "items": sum(row["items"] for row in rows),
                "reasons": dict(sorted(reasons.items(), key=lambda pair: (-pair[1], pair[0]))),
            }
        )
    return aggregates


def _reasons_of(aggregate: dict) -> str:
    reasons = aggregate["reasons"]
    if len(reasons) == 1:
        return next(iter(reasons))
    return ", ".join(f"{reason} ({count})" for reason, count in reasons.items())


def _of(count: int, total: int) -> str:
    return f"all {total} gathers" if count == total else f"{count} of {total} gathers"


def _items(count: int) -> str:
    return f"{count} item" if count == 1 else f"{count} items"


def state_line(aggregate: dict) -> str:
    """One source's state across the window, in the words a footer is written from.

    A window of exactly one gather reads as one run, which is what `coverage-honesty`'s single-run
    footer says; a longer window says how many of its gathers each state held.
    """
    total, states = aggregate["gathers"], aggregate["states"]
    if total == 1:
        if states[NOT_READ]:
            return f"not read — {_reasons_of(aggregate)}"
        if states[READ]:
            return f"read ({_items(aggregate['items'])})"
        return "read, nothing in it"

    segments = []
    answered = states[READ] + states[READ_EMPTY]
    if answered:
        empty = ", nothing in it" if answered == 1 else ", nothing in them"
        found = f" ({_items(aggregate['items'])})" if aggregate["items"] else empty
        segments.append(f"read on {_of(answered, total)}{found}")
    if states[NOT_READ]:
        segments.append(f"not read on {_of(states[NOT_READ], total)} — {_reasons_of(aggregate)}")
    if aggregate["unrecorded"]:
        segments.append(f"no state recorded on {_of(aggregate['unrecorded'], total)}")
    return "; ".join(segments)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    # No `record` subcommand: the record is written with `pulse_record_coverage`, and a row a shell
    # appended to the projected file is erased by the next render of it.
    p_window = sub.add_parser("window", help="every source's states across the gathers covered")
    p_window.add_argument("--series", required=True)
    p_window.add_argument("--since", default="")
    p_window.add_argument("--until", default="")

    args = parser.parse_args(argv)

    until = args.until or date.today().isoformat()
    dates = gathers(args.series, args.since, until)
    if not dates:
        print("no gathers recorded in the window")
        return 0
    span = dates[0] if len(dates) == 1 else f"{dates[0]}..{dates[-1]}"
    print(f"{span}  {len(dates)} gather{'' if len(dates) == 1 else 's'}")
    for aggregate in window(args.series, args.since, until):
        # Two spaces minimum, as the pool pads its slugs: a source slug exactly as wide as the pad
        # would otherwise butt straight against its state.
        print(f"{aggregate['source']:<24}  {state_line(aggregate)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
