"""The projection: the member-readable copy of a series' record, written by a job.

**Why a job and not the tool that recorded the row.** A workspace file is written through
`ExtensionContext.files`, and that seam is only wired where core passes `sandboxes=` when it builds
the context — `serve.py` does so for the job runner, the page-change runner and surface contexts, and
`host/ext/loader.py`'s `turn_tools` does not. So `ctx.ext.files` is `None` in *every* extension tool
handler, unconditionally. A projection attempted there is not unreliable; it is dead code that
reports "no carrier" on a deploy where every carrier is present.

**Why it wakes on work rather than on the clock.** `ConversationFiles.write` opens a sandbox rather
than reusing a live one, so a job that rendered every series each tick would start a container per
pulse conversation per tick to rewrite files nothing had changed. `due_projections` is the select
`owner_candidates` runs instead: a workspace with no series past its projection mark is never bound,
and a series is rendered once per advance rather than once per tick.

**What a failure means here.** The record is already durable when this runs — the rows were
committed by the tool that wrote them. A conversation whose sandbox cannot be opened therefore costs
a stale copy of a file, never a lost row, and the right response is to leave the mark unmoved and
let the next tick try again. So one series' failure is logged and skipped rather than raised, which
would abandon every series after it in the same workspace.

This is an ordinary state rather than an alarm: a terminal-bound conversation takes a file only
while that terminal is connected, so a brief whose member has closed their session stays due until
they open one. That is why the failure is logged per series and the series left due, and why nothing
here escalates on a run of them.
"""

from __future__ import annotations

import logging

from ufo.sdk.context import ExtensionContext
from ufo.sdk.jobs import JobFault, JobSpec, owner_candidates
from ufo_ext_pulse import record

log = logging.getLogger(__name__)

JOB_NAME = "pulse_project"
# Seconds first. Every five minutes: often enough that a member who asks for an edition and then
# opens the file finds it current, rare enough that a series nothing touched costs one select.
SCHEDULE = "0 */5 * * * *"


async def project(ext: ExtensionContext) -> None:
    """Write every workspace file for each series in the bound workspace whose record has moved."""
    for series, conversation_id, through in await record.due_series(ext):
        if ext.files is None:
            # The job runner wires this seam; a context without it is a deploy wired differently,
            # and silently marking the series projected would strand the file forever.
            log.warning("pulse projection has no workspace file seam; series %s left", series)
            return
        try:
            seen = record.seen_lines(await record.read_sightings(ext, series))
            covered = record.covered_lines(await record.read_covered(ext, series))
            coverage = record.coverage_lines(await record.read_coverage(ext, series))
            await ext.files.write(conversation_id, record.seen_projection(series), seen.encode())
            await ext.files.write(
                conversation_id, record.covered_projection(series), covered.encode()
            )
            await ext.files.write(
                conversation_id, record.coverage_projection(series), coverage.encode()
            )
        except JobFault:
            raise
        except Exception as failure:
            # Left unmarked on purpose: the next tick finds this series still due and tries again.
            # Named rather than re-raised, because raising would abandon every series after this one
            # in the same workspace for a fault that is this conversation's alone.
            log.warning(
                "pulse projection failed for series %s: %s: %s",
                series,
                type(failure).__name__,
                failure,
            )
            continue
        await record.mark_projected(ext, series, through)


JOB = JobSpec(
    name=JOB_NAME,
    schedule=SCHEDULE,
    handler=project,
    candidates=owner_candidates(record.due_projections),
)
