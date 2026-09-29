"""The four calls that keep a brief series' record in one place.

Three write, one reads. They exist because the record used to be a file at a workspace-relative
path, which resolves against whatever directory the turn's carrier started in: a conversation bound
to a terminal (`sandbox_handle` of `client:<cwd>`) writes on the member's machine in that directory,
and any other conversation writes under `workspace_root/<conversation_id>`. One series therefore
grew one ledger per tree, each looking complete and none of them whole. These write to tables
instead, keyed by workspace and series, which every turn in the workspace reaches identically.

**Nothing here writes a file.** `ExtensionContext.files` is `None` in every extension tool handler —
core wires that seam for the job runner and for surface contexts, and `turn_tools` does not wire it
at all — so a projection attempted here would be dead code that reported "no carrier" on a deploy
where every carrier is present. `jobs.py` owns the workspace-file copy, as a job, for that reason.

What each write does do is stamp the series row with the conversation it ran in and the moment it
advanced, in the same transaction as the rows. That is what the projection job selects on, so a
committed write always has a projection due behind it and a rolled-back one never does.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from ufo.sdk.tools import TextContent, ToolContext, ToolResult
from ufo_ext_pulse import record
from ufo_ext_pulse.record import Coverage, Sighting, Story

RECORD_SIGHTINGS_TOOL = "pulse_record_sightings"
RECORD_EDITION_TOOL = "pulse_record_edition"
RECORD_COVERAGE_TOOL = "pulse_record_coverage"
RECALL_TOOL = "pulse_recall"


class SightingInput(BaseModel):
    seen: str = Field(description="The date this lead was seen, YYYY-MM-DD.")
    slug: str = Field(description="The lead's stable identity, lowercase and hyphenated.")
    title: str = Field(description="The headline as this sighting states it.")
    url: str = Field(description="The address this sighting was found at. Required.")
    source: str = Field(default="", description="Where it was read: a feed, a site, a forum.")


class StoryInput(BaseModel):
    slug: str = Field(description="The story's stable identity, lowercase and hyphenated.")
    title: str = Field(description="The headline the edition carried.")
    url: str = Field(default="", description="The address the edition cited.")


class CoverageInput(BaseModel):
    source: str = Field(
        description=(
            "The source's stable identity, lowercase and hyphenated — the same slug on every "
            "gather, so three days of one source aggregate as one source."
        )
    )
    state: str = Field(
        description=(
            "read, read-empty, or not-read. A source that answered with nothing is read-empty, "
            "which is an answer; not-read is a source this gather could not reach."
        )
    )
    items: int = Field(default=0, description="How many items a read source returned.")
    reason: str = Field(
        default="",
        description=(
            "Why a not-read source went unread: unreachable, rate-limited, budget-exhausted, or "
            "unauthorized. Empty on a source that answered."
        ),
    )


class RecordSightingsInput(BaseModel):
    series: str = Field(description="The brief series, lowercase and hyphenated.")
    sightings: list[SightingInput] = Field(
        description="Every lead this gather surfaced, published or not. Send them in one call."
    )


class RecordEditionInput(BaseModel):
    series: str = Field(description="The brief series, lowercase and hyphenated.")
    edition: str = Field(description="The edition's date, YYYY-MM-DD.")
    stories: list[StoryInput] = Field(description="Every story this edition carried.")


class RecordCoverageInput(BaseModel):
    series: str = Field(description="The brief series, lowercase and hyphenated.")
    gathered: str = Field(description="The gather's date, YYYY-MM-DD.")
    sources: list[CoverageInput] = Field(
        description="Every source this gather read or tried to read. Send them in one call."
    )


class RecallInput(BaseModel):
    series: str = Field(description="The brief series, lowercase and hyphenated.")
    within_days: int = Field(
        default=record.DEFAULT_WITHIN_DAYS,
        description="How recently a lead must have been seen to count as live.",
    )
    editions: int = Field(
        default=record.DEFAULT_EDITIONS,
        description="How many past editions the no-repeat rule reads back over.",
    )
    slug: str | None = Field(
        default=None,
        description=(
            "One lead. Given, the answer is that lead's whole sighting history and every edition "
            "that carried it, which is the evidence a material new development needs. Omitted, "
            "the answer is the live leads and what the recent editions covered."
        ),
    )


def _said(text: str, *, error: bool = False) -> ToolResult:
    return ToolResult(content=(TextContent(text=text),), is_error=error)


def _lands_in(ctx: ToolContext):
    """The conversation a workspace file belongs to. Core opens a sandbox against
    `sandbox_conversation_id or conversation_id` (`runtime/queue.py`'s `_LateSandbox`), so a turn
    sharing another conversation's sandbox must record that one — otherwise the projection lands
    in a tree the agent never reads."""
    return ctx.turn.sandbox_conversation_id or ctx.turn.conversation_id


def _ext(ctx: ToolContext, tool: str):
    ext = ctx.ext
    if ext is None:
        raise RuntimeError(f"{tool} dispatched without its ExtensionContext")
    return ext


async def record_sightings(ctx: ToolContext, args: RecordSightingsInput) -> ToolResult:
    """Record every lead a gather surfaced, whether or not an edition will carry it."""
    ext = _ext(ctx, RECORD_SIGHTINGS_TOOL)
    if not args.sightings:
        return _said("No sightings given. A gather that found nothing records nothing.", error=True)
    rows = [
        Sighting(seen=s.seen, slug=s.slug, title=s.title, url=s.url, source=s.source)
        for s in args.sightings
    ]
    written = await record.record_sightings(
        ext, args.series, rows, datetime.now(UTC), _lands_in(ctx)
    )
    return _said(
        f"recorded {written} sighting{'' if written == 1 else 's'} in {args.series}. "
        f"The record is durable now and pulse_recall reads it. The workspace copy at "
        f"{record.seen_projection(args.series)} is written separately by a job and may lag, or "
        "wait — a conversation bound to a terminal takes a file only while that terminal is live."
    )


async def record_edition(ctx: ToolContext, args: RecordEditionInput) -> ToolResult:
    """Record what an edition published, so the next one knows what it may not repeat."""
    ext = _ext(ctx, RECORD_EDITION_TOOL)
    if not args.stories:
        return _said(
            "No stories given. An edition that carried nothing still carried a window — say so in "
            "the brief; the ledger records stories.",
            error=True,
        )
    rows = [Story(slug=s.slug, title=s.title, url=s.url) for s in args.stories]
    written = await record.record_covered(
        ext, args.series, args.edition, rows, datetime.now(UTC), _lands_in(ctx)
    )
    return _said(
        f"recorded {written} stor{'y' if written == 1 else 'ies'} in {args.series} "
        f"edition {args.edition}. The record is durable now and pulse_recall reads it. The "
        f"workspace copy at {record.covered_projection(args.series)} is written separately by a "
        "job and may lag, or wait — a conversation bound to a terminal takes a file only while "
        "that terminal is live. It is rendered whole from the record, so nothing written into it "
        "by hand survives."
    )


async def record_coverage(ctx: ToolContext, args: RecordCoverageInput) -> ToolResult:
    """Record what each source returned on one gather, so a later edition can count the days."""
    ext = _ext(ctx, RECORD_COVERAGE_TOOL)
    if not args.sources:
        return _said(
            "No sources given. A gather that reached none of them records not-read rows naming "
            "why, which is the state an edition needs; recording nothing says nothing.",
            error=True,
        )
    rows = [
        Coverage(source=s.source, state=s.state, items=s.items, reason=s.reason)
        for s in args.sources
    ]
    written = await record.record_coverage(
        ext, args.series, args.gathered, rows, datetime.now(UTC), _lands_in(ctx)
    )
    return _said(
        f"recorded {written} source state{'' if written == 1 else 's'} for {args.series} on "
        f"{args.gathered}. The record is durable now and every later edition's footer counts it. "
        f"The workspace copy at {record.coverage_projection(args.series)} is written separately by "
        "a job and may lag, or wait — a conversation bound to a terminal takes a file only while "
        "that terminal is live. It is rendered whole from the record, so nothing written into it "
        "by hand survives."
    )


async def recall(ctx: ToolContext, args: RecallInput) -> ToolResult:
    """Read the record back: what is live, what is spent, or one lead's whole history."""
    ext = _ext(ctx, RECALL_TOOL)
    sightings = await record.read_sightings(ext, args.series)
    covered = await record.read_covered(ext, args.series)

    if args.slug is not None:
        history = [row for row in sightings if row["slug"] == args.slug]
        carried = [row for row in covered if row["slug"] == args.slug]
        if not history and not carried:
            return _said(f"{args.slug}: never seen and never carried in {args.series}.")
        lines = [f"{args.slug} in {args.series}"]
        lines += [
            f"  seen {row['seen']}  {row['title']}"
            + (f"  [{row['source']}]" if row["source"] else "")
            for row in history
        ] or ["  never seen"]
        lines += [f"  carried {row['edition']}  {row['title']}" for row in carried] or [
            "  never carried"
        ]
        return _said("\n".join(lines))

    live = record.fresh(sightings, args.within_days, datetime.now(UTC).date())
    titles = {row["slug"]: row["title"] for row in sightings}
    spent = record.recent_rows(covered, args.editions)
    lines = [
        f"{len(sightings)} sighting{'' if len(sightings) == 1 else 's'} on record in "
        f"{args.series}; {len(live)} lead{'' if len(live) == 1 else 's'} seen in the last "
        f"{args.within_days} days."
    ]
    lines += [f"  live  {seen}  {slug}  {titles.get(slug, '')}" for slug, seen in live] or [
        "  no lead has been seen inside the window"
    ]
    lines.append(
        f"Carried in the last {args.editions} edition"
        f"{'' if args.editions == 1 else 's'} — ineligible without a material new development:"
    )
    lines += [f"  {row['edition']}  {row['slug']}  {row['title']}" for row in spent] or [
        "  nothing; no edition has been recorded"
    ]
    return _said("\n".join(lines))
