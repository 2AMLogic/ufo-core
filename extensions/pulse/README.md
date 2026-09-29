# ufo-ext-pulse

A recurring field brief for [ufo](https://github.com/ufo-ai/ufo-core): an agent that runs one, and
five skills that make a series of briefs behave like a series.

`competitive-intel` watches a list of companies. A field pulse watches a domain, so a run may find a
name nobody had heard of last month. That difference is why this is a separate skill, but it is not
the reason this extension exists.

## What it adds

A recurring brief has three failure modes that a single well-written report does not have, and none
of them is fixed by writing the report better.

**It repeats itself.** The reader has read the last edition, so this one has to answer "what changed
since then" rather than "what is true about this field." `task-scheduling` tells a run to keep an
already-covered ledger in workspace files; nothing defines one. `brief-continuity` does: an
append-only ledger, read before ranking, where a story published in the last five editions is
ineligible unless something happened to it that the earlier reader does not know — a release shipped,
a deal closed, a number confirmed or corrected, a reversal. `covered.py` makes that a lookup instead
of a recollection.

**It reports silence it did not observe.** A source that rate-limited the run and a source with
nothing in it both return zero items, and a brief that renders the first as the second tells the
reader the field is quiet on the strength of having looked at nothing. `coverage-honesty` gives every
source three states rather than two, names each unread source in the footer, and refuses to publish
an edition at all when most sources went unread — because a brief built from the half that answered
looks like a normal edition, so nobody goes back for the other half.

**It pays for research it publishes and publishes research nobody asked for.** A field moves every
day, so research is worth doing every day; an edition is worth reading when the member decides to
read one, which no schedule knows. A single fire that gathers and publishes has to pick one clock for
both, and either answer is wrong: at a reader's cadence it searches on two days in seven, and at the
field's cadence it delivers seven editions a week to someone who wanted two. So the recurring row
gathers and records, and `field-report` writes an edition when it is asked for — from the pool, with
no search in the request path, which is also what makes it fast enough to answer in the conversation.

`field-pulse` runs the setup as `pulse`, writes the first edition in that turn, and arms the daily
gather. It carries three of the others as `metadata.depends`, so a pulse run cannot load the
workflow without the writer or the contracts.

| Skill | Holds |
| --- | --- |
| `pulse-handoff` | Recognising the ask, and handing the field to the `pulse` agent |
| `field-pulse` | Setting one up, the first edition, the daily gather |
| `field-report` | An edition on demand, from the pool, with no fresh search |
| `brief-continuity` | The covered ledger, the sightings pool and the coverage state; what an edition may repeat |
| `coverage-honesty` | What a run may claim about a source it could not read |

The extension declares five skills, one agent, four tools, one job, and the migrations behind them.
The tools exist for one reason: **a brief's carriers do not share a working directory.** The ledger,
the pool and the coverage state were workspace-relative paths, and such a path resolves against the
directory the turn started in. A conversation whose `sandbox_handle` is `client:<cwd>` runs on the member's own
machine in that directory; any other conversation gets `workspace_root/<conversation_id>`. So the
split is by *terminal binding*, not by whether a human was watching. The demo deploy's own census
is eight `client:` conversations sharing one tree, **two** `local:` ones with a tree each, and one
row with no handle at all — a conversation that has not opened a sandbox yet, and which becomes
terminal-bound if it opens one while a terminal is live. Three trees for one deploy, not two.

That is not a hypothesis. On the demo deploy the `agent-runtimes` series had two
`covered.jsonl` files of 15 rows each, and for edition 2026-09-28 they shared **no story at all**:

```
2026-09-27:  deploy-home 9  sandbox 9  shared 5  only-in-one 8
2026-09-28:  deploy-home 6  sandbox 6  shared 0  only-in-one 12
```

Both were plausible. The no-repeat rule was enforced against whichever half the running carrier
could see. Rows keyed by workspace and series are reached identically by every turn, whatever ran it
and wherever it started, and that is the whole of why these tables exist.

Gathering is still `research`'s, the recurring row still `scheduled_tasks`', and the feed entry
still `report_digest`'s.

| Tool | Does |
| --- | --- |
| `pulse_record_sightings` | Records every lead one gather surfaced, in one call |
| `pulse_record_edition` | Records the stories one edition carried |
| `pulse_record_coverage` | Records what each source returned on one gather, in one call |
| `pulse_recall` | Reads back the live leads and the recent editions, or one lead's history |

| Job | Does |
| --- | --- |
| `pulse_project` | Writes the workspace-file copy of a series whose record has moved |

## Who a pulse runs as

A scheduled task is owned by the agent whose turn applied it: `scheduled_tasks` writes `agent_id`
from the creating turn, and its manifest `spec` names no agent. So the daily gather is armed as
whoever set the pulse up. Set up from a chat turn, it searches every morning as the assistant —
under the assistant's prompt, model and tool scope — for the quarter the row lasts, and nothing
anywhere says the series has an owner it was never meant to have.

So the extension ships an agent. `pulse` is an `AgentProvision` on the manifest's `agents` point,
which core's activation pass turns into an ordinary `agent` row the first time a workspace comes up
with pulse active; from that moment the row is the workspace's own configuration and this extension
stops writing it. There is no onboarding step to skip and nothing is created inside a member's turn.

| Field | Value | Why |
| --- | --- | --- |
| `model` | `auto` | The only model a public extension can name; a pinned id the deploy does not serve fails every turn of the row |
| `reasoning` | `high` | Ranking a field's week against one business is the judgement no script holds |
| `internet_access_allowed` | `false` | A gather reaches its sources through `research`'s tools and the backend's own credential egress |
| `visibility` | `workspace` | A spawn of an ownerless row is an admin's alone unless it is workspace-visible, and handing a field over is not an admin operation |
| `tools` | The member-facing set | An allowlist is intersected with the live registry at turn load, so a tool name this extension guessed wrong is a gather that cannot search |

The member never leaves their own conversation. `pulse-handoff` loads where they made the ask and
spawns the agent, and an agent spawn talks back to the conversation that made it — so one sentence
to an assistant is still the whole entry point, and the questions the setup asks arrive as ordinary
messages there.

A spawned turn's inbound is the bare payload its contract promises, with no `<context>` header in
front of it, so the two things the setup would have read off that header travel in the payload
instead — and the row's declared input schema is what refuses a handoff that dropped one.

| Key | Holds | Missing |
| --- | --- | --- |
| `request` | The member's words, verbatim | The spawn is refused |
| `business` | The business a story is ranked against | `field-pulse` asks for one |
| `local_time` | The handing turn's own `time:` line | The spawn is refused |

**One agent serves every series.** A series is separated by its name — the record is keyed by
workspace and series, the row is named `<field>-gather`, and each handoff opens a conversation of
its own — so a second agent row would duplicate a prompt that names no field while separating
nothing that is not separated already.

## Install

```bash
pip install "ufo-integrations @ git+https://github.com/2AMLogic/ufo-integrations"
```

Installing the distribution registers the `ufo.extension` entry point. What activates pulse is the
deploy's active extension set, and `ufoctl init` writes `[pack] name = "assistant"` into `ufo.toml`:
a named pack narrows that set to exactly the extensions it bundles, and the assistant pack bundles
neither `pulse` nor the `research` extension a pulse gathers through. Under it the install is
inactive, and nothing says so.

The way through is a pack of this deploy's own, bundling the assistant set plus both, named in
`ufo.toml` in place of `assistant`. Two files under `packs/deploy/`:

```toml
# packs/deploy/pyproject.toml
[project]
name = "ufo-pack-deploy"
version = "0.1.0"
requires-python = ">=3.12"

[project.entry-points."ufo.pack"]
deploy = "ufo_pack_deploy:pack"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

```python
# packs/deploy/ufo_pack_deploy.py
"""The extensions this deploy brings up together: ufo-core's assistant set, plus pulse and the
research extension a pulse gathers through."""

from ufo.sdk.manifest import Pack
from ufo_pack_assistant import EXTENSIONS as ASSISTANT

NAME = "deploy"
VERSION = "0.1.0"
EXTENSIONS = (*ASSISTANT, "research", "pulse")


def pack() -> Pack:
    return Pack(name=NAME, version=VERSION, extensions=EXTENSIONS)
```

Reading `EXTENSIONS` from the pack ufo-core ships, rather than copying the names it holds, keeps the
set tracking upstream's. Then `pip install ./packs/deploy`, set `[pack] name = "deploy"`, and the
next `ufoctl serve` comes up with pulse active.

## Keys

Each resolves `UFO_<NAME>` from the environment first and the bare `<NAME>` second, so all three
belong in the `.env` beside `ufo.toml` under their `UFO_`-prefixed names — a bare
`ANTHROPIC_API_KEY` or `OPENAI_API_KEY` there is refused by name, because every tool reading `.env`
picks those up. `ufoctl init` echoes one informational line per unset key an active extension
declares as a deploy key, `UFO_OPENAI_API_KEY` among them, and proceeds either way. The model
provider's key and the search slot are not deploy keys, so a deploy reaches `ufoctl serve` with all
three unset and a line about one.

| Key | Holds | Unset |
| --- | --- | --- |
| `UFO_ANTHROPIC_API_KEY` | A model provider's key; `UFO_OPENAI_API_KEY` serves in its place | With neither resolving, `ufoctl serve` refuses to boot: `no model provider key set; the sandbox would have no egress route` |
| `UFO_OPENAI_API_KEY` | Embeddings, whichever provider serves the model | Index derivation raises on every turn, so recall finds nothing |
| `perplexity_api_key` | The search backend, as a workspace credential slot filled in chat, or a deploy-wide `UFO_PERPLEXITY_API_KEY` under it | The deploy boots, and the first gather raises `CredentialSlotUnset` inside the job |

A missing backend and an unfilled slot fail in different places, and the first of the two is what
`requires = ("search_providers",)` is for: boot refuses when `[research] search_provider` is unset,
when no active extension registers the backend it names, or when the credential store itself is
unkeyed — never because a declared slot is empty. The slot is read per request, so an unfilled one
boots clean and fails loud in the turn.

The embed backend is OpenAI's whichever provider serves the model, and it reads its key per embed
call rather than at boot, so a deploy configured entirely with Anthropic boots clean and then raises
inside the indexing job on every turn. A pulse turn opens on `memory_search` for the field and for
any pulse already running; with nothing in the index that search finds nothing, which is also what a
field nobody has asked about looks like. The constraint is upstream's, on any deploy with memory
active, rather than this extension's.

## The three stores

`pulse_ext_covered` holds one row per story per edition, `pulse_ext_sighting` one row per sighting a
gather recorded, and `pulse_ext_coverage` one row per source per gather. They answer different
questions — what the series has published, what it has seen, and what it could read — and a lead
seen four times and never published is not a repeat, while a source nobody reached is not a source
with nothing in it. An edition reads all three: the ledger decides what it may carry, the pool is
everything it has to carry, and the coverage state is what its footer may claim.

Every key is natural rather than surrogate: a sighting is one series, one day, one lead, one
address, a covered row is one series, one edition, one story, and a coverage row is one series, one
gather, one source. So a retried fire, a crash replay, or a gather that surfaces one lead twice
writes one row. That also makes **importing an old file safe to repeat**, which is how a deploy
already carrying divergent ledgers merges them — see below.

A coverage row is the one key two writes can legitimately disagree under: a source that rate-limited
the first attempt and answered the second was read that gather, so the later write is its answer.
The file appended both rows and resolved them on read; the table resolves them on write, and the
window aggregates to the same states from either.

A fourth table, `pulse_ext_series`, is bookkeeping rather than record: which conversation a series
lives in, and how far the workspace-file copy has caught up.

**The files are still here, as a projection.** `pulse_project` renders the whole series from the
tables to `pulse/<series>.seen.jsonl`, `pulse/<series>.covered.jsonl` and
`pulse/<series>.coverage.jsonl`, in the same JSON Lines shape the scripts always appended, so the
scripts below still read them and the member can still open them.

It is a **job** and not part of the write, and that is forced rather than chosen: a workspace file
is written through `ExtensionContext.files`, core wires that seam for the job runner and for surface
contexts, and `host/ext/loader.py`'s `turn_tools` does not wire it at all. `ctx.ext.files` is
therefore `None` in *every* extension tool handler, on every deploy. A projection attempted from a
tool is not unreliable; it is dead code.

The job wakes every five minutes and renders only series whose record has moved since their last
projection, because `ConversationFiles.write` opens a sandbox rather than reusing a live one — an
unconditional projection would start a container per pulse conversation per tick to rewrite files
nothing had changed.

### Merging ledgers a split left behind

The tables start empty; nothing backfills them. A deploy that ran pulse before this change has one
file per carrier, and the way to make them one record is to read each file and record its rows:

```bash
cat pulse/<series>.covered.jsonl                                   # the deploy-home copy
cat workspaces/<conversation-id>/pulse/<series>.covered.jsonl      # the sandbox copy
```

then call `pulse_record_edition` with the rows of each. **Importing the same file twice is safe** —
the natural key means a row already present is not a second row — so the two can be imported in
either order, and an interrupted import can simply be repeated.

```bash
python "$UFO_HOME/skills/brief-continuity/covered.py" check --series data-infra --slug acme-1-0
python "$UFO_HOME/skills/brief-continuity/seen.py" fresh --series data-infra --within-days 14
python "$UFO_HOME/skills/brief-continuity/coverage.py" window --series data-infra
```

**All three scripts are read-only.** None has a `record` subcommand, because a row written into a
projected file is erased by the next render rather than kept — and that is not theoretical: a live
fire on 2026-09-28, told by the skill to call `pulse_record_edition` and holding the tool, appended
its two published stories to the file instead. Both were pending erasure, which would have left the
edition reading as never published and the next one free to carry it again. Prose did not move the
model off the script, so the script no longer offers the move.

`check` exits non-zero when the slug is covered in the span, `fresh` lists the leads whose most
recent sighting is inside the window, and `window` states each source across the gathers a report
covers, which is what a multi-gather footer is written from. A story carried again under the
material-new-development exception keeps its original slug, so the rows sharing a slug are that
story's history across the series in either of the story stores.

## Traps

- **The deploy's pack does not bundle `pulse`.** A `[pack] name = "assistant"` narrows the active
  set to that pack's own extensions, and `pulse` is not among them, so its skills never load and no
  boot refuses. The deploy's own pack above is the way through. A lockfile pinning `pulse` reaches
  it only with `[pack] name` unset, because the pack narrows the active set after the lockfile has
  filled it.
- **`ufoctl init` writes `ufo.toml` only when it is absent.** The pack line is a hand edit made
  once: a re-run leaves `[pack] name = "deploy"` exactly where it was and stops at
  `already initialized` before it reaches anything else. Nothing restores `assistant` over the
  deploy's own pack, and nothing reports the line is wrong either.
- **The assistant set does not hold `research` either.** It ships with ufo-core, so no install
  brings it in, and a pack naming `pulse` without it leaves a pulse turn nothing to gather through.
- **`research` owns a migration branch.** `ufoctl init` applies core's schema plus the active set's
  branches, and under `assistant` that set excludes `research`, so a deploy that changed
  `[pack] name` after init runs `ufoctl migrate` again before the next `ufoctl serve`.
- **A lockfile pins content, not installs.** Where a deploy pins its extension set rather than
  running everything discovered, the pin is a hash over the installed package's files:
  reinstalling the same content keeps it, and reinstalling changed content fails boot loud until
  the pin is rewritten.
- **Recall is empty rather than unavailable.** With no `UFO_OPENAI_API_KEY` nothing reaches the
  index, and the silence a `memory_search` returns is the same silence a new field returns. This is
  a missing key, not a business the member never stated — `pulse-handoff` reads the opening line
  first and searches memory only where that line carries no business sentence, and `field-pulse`'s
  step 1a asks only once the payload arrived with none. An unavailable index next to a populated
  opening line is this trap, and the handoff hands the business over regardless.
- **An agent named `pulse` that a member made keeps the name.** Activation identifies a shipped row
  by the extension and the declared name rather than by the row's own, so a name already in use
  sends the shipped agent to a free variant — nothing is overwritten and nothing is stuck. But
  `pulse-handoff` names its target as `agent:pulse`, which resolves by name, so the handoff would
  reach the member's agent and the setup would run under a prompt nobody wrote it for. A workspace
  wanting an agent of its own by that name renames one of the two.
- **The activation pass creates the row and stops owning it.** The workspace's copy is the live
  configuration from that moment: a later version of this extension carrying a different prompt,
  model or input schema reaches new workspaces only, and carries forward nothing but the purpose and
  the setup it declares. A deploy adopting a changed row edits it, and `select provisioned_version
  from agent` says which declaration the one it holds came from.
- **The projection follows the last write.** A setup runs in the agent's own conversation, so the
  first `pulse/<series>.*.jsonl` set lands in that conversation's tree rather than in a terminal's
  working directory. An edition written where the member asked re-binds the series and the next
  projection lands with them. The record is unaffected either way — the rows are keyed by workspace
  and series — so this decides where the readable copy is, never what it says.
- **A workspace-relative path is not one place.** `bash`, `read` and `write` are core builtins
  (`host/tools/builtins.py`), present for every turn including a scheduled fire, and
  `ToolContext.sandbox` is non-optional — so a fire *can* run a script, and does. What differs is
  where it starts, and `conversation.sandbox_handle` is what says: `client:<cwd>` runs on the
  member's machine in that directory, anything else under `workspace_root/<conversation_id>`. A
  store at `pulse/<series>.jsonl` is a different file in each. An earlier version of this README
  said the opposite — that a fire had "no filesystem at all" — and that claim was wrong and cost a
  whole design built on it. `select id, sandbox_handle from conversation` is the check that settles
  it.
- **The tables are per workspace, the files are per conversation.** `pulse_recall` is the record;
  a file is a copy of it that lands where the series is being worked on. Moving a brief to a new
  conversation moves the file with it on the next projection and moves no rows, which is the
  intended behaviour and worth knowing before wondering where the old file went.
- **An armed gather keeps the rule it was set up under.** `task-scheduling` stores `field-pulse`'s
  manifest prompt on the scheduled row at apply time; editing the skill changes only what the next
  setup writes, not a row already armed. Re-applying `<field>-gather` upserts it in place and
  re-points its reporting to whichever conversation ran the re-apply, so adopting an edit means
  re-applying from the pulse's own conversation. An edit to `field-report` has no armed row to
  reach, and reaches the next edition the deploy's own `serve` process has booted for. Same family
  as #70's running `serve` process keeping the skills it booted with.
- **A running `serve` answers from the skills it booted with.** `ufoctl serve` resolves the active
  set at boot and materializes each skill into `$UFO_HOME/skills/<name>/`; every turn reads that
  copy. Editing the source reaches none of it, an editable install included, where the source the
  entry point resolves to is the working tree itself. The copy carries a recent mtime and reads as
  current, so the deploy quotes the old rule back with nothing in the log to say so. A `serve`
  restart picks the edit up and nothing else does. Same family as the armed row above (#66): a
  durable copy of instructions a later edit does not reach.

## Tests

```bash
pytest tests/
```

## License

Apache-2.0.
