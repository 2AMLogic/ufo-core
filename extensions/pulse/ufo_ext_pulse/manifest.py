"""What the pulse extension declares: the agent a pulse runs as, five skills it loads on demand, and
the four calls that keep a series' record alive when nobody is watching.

A recurring field brief is a series, not a report. `pulse-handoff` recognises the ask in the
member's own conversation and hands it to the `pulse` agent; `field-pulse` runs the setup there,
writes the first brief and arms a daily gather; `field-report` writes every edition after it, from
the pool those gathers filled and without a search in the request path; `brief-continuity` holds the
covered ledger that makes the next brief carry what the last one did not, and the sightings pool the
report reads; `coverage-honesty` keeps a window nobody could read from being published as a window
where nothing happened.

**The agent is why the handoff exists.** `scheduled_tasks` writes a row's `agent_id` from the turn
that applied it and its manifest `spec` names no agent, so a gather is owned by whoever set the
pulse up. Setting one up from a chat turn arms a daily search that runs as the assistant; setting
one up from a `pulse` turn arms one that runs as `pulse`. `agent.py` holds the row and the reasons;
the handoff is the one turn that reaches it.

**The store is this extension's own, and that is the whole of why it owns tools and a job.** The
ledger, the pool and the coverage state began as workspace files at a workspace-relative path, which
resolves against the directory the turn's carrier started in — the member's own machine for a
conversation bound to a terminal, `workspace_root/<conversation_id>` for one that is not. One series
therefore grew one ledger per tree.
On the demo deploy the `agent-runtimes` series had two of them, 15 rows each, whose 2026-09-28
editions shared no story at all — and the no-repeat rule was enforced against whichever half the
running carrier could see. The rows now land in tables keyed by workspace and series, which every
turn reaches identically.

The file stays, because a member can open a file and cannot open a table — but as a projection
written by `jobs.py`, not by the tools. `ExtensionContext.files` is `None` in every tool handler and
wired for the job runner, so the job is not a preference; it is the only place in this extension
that can write one.

Gathering is still `research`'s, the recurring row still `scheduled_tasks`', and the feed entry
still `report_digest`'s. `requires` names the one seam a brief cannot be written without: a deploy
with pulse active and no search backend fails at boot rather than on the first fire."""

from pathlib import Path

from ufo.sdk.manifest import Manifest, SkillSpec
from ufo.sdk.tools import ToolDef
from ufo_ext_pulse.agent import PROVISION
from ufo_ext_pulse.jobs import JOB
from ufo_ext_pulse.tools import (
    RECALL_TOOL,
    RECORD_COVERAGE_TOOL,
    RECORD_EDITION_TOOL,
    RECORD_SIGHTINGS_TOOL,
    RecallInput,
    RecordCoverageInput,
    RecordEditionInput,
    RecordSightingsInput,
    recall,
    record_coverage,
    record_edition,
    record_sightings,
)

NAME = "pulse"
VERSION = "0.4.0"

SKILLS_ROOT = Path(__file__).parent / "skills"
SKILL_NAMES = (
    "pulse-handoff",
    "field-pulse",
    "field-report",
    "brief-continuity",
    "coverage-honesty",
)


def manifest() -> Manifest:
    return Manifest(
        name=NAME,
        version=VERSION,
        agents=(PROVISION,),
        tools=(
            ToolDef(
                name=RECORD_SIGHTINGS_TOOL,
                description=(
                    "Record every lead a gather surfaced for a brief series, published or not, so "
                    "the next edition can tell a new development from a repeat. Send the whole "
                    "gather in one call."
                ),
                input_model=RecordSightingsInput,
                handler=record_sightings,
                side_effecting=True,
            ),
            ToolDef(
                name=RECORD_EDITION_TOOL,
                description=(
                    "Record the stories one edition of a brief series carried, so later editions "
                    "do not repeat them without a material new development."
                ),
                input_model=RecordEditionInput,
                handler=record_edition,
                side_effecting=True,
            ),
            ToolDef(
                name=RECORD_COVERAGE_TOOL,
                description=(
                    "Record what each source returned on one gather of a brief series — read with "
                    "a count, read and empty, or not read for a named reason — so an edition "
                    "covering several gathers can say which of them each source answered on."
                ),
                input_model=RecordCoverageInput,
                handler=record_coverage,
                side_effecting=True,
            ),
            ToolDef(
                name=RECALL_TOOL,
                description=(
                    "Read a brief series' record: the leads seen recently, the stories the last "
                    "few editions carried, or one lead's whole history when a slug is named."
                ),
                input_model=RecallInput,
                handler=recall,
            ),
        ),
        jobs=(JOB,),
        skills=tuple(SkillSpec(path=SKILLS_ROOT / name) for name in SKILL_NAMES),
        requires=("search_providers",),
    )
