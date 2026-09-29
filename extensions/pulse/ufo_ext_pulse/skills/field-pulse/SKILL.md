---
name: field-pulse
description: "Load when a pulse agent turn has been handed a field to follow: confirm it, propose sources, gather the first window, write the first brief, arm the daily gather. Not for the ask as it arrives in chat, which is pulse-handoff, and not for a later edition, which is field-report."
metadata:
  depends: [field-report, brief-continuity, coverage-honesty]
---
# Field pulse, set up as the agent that runs it

A field pulse watches a domain; `competitive-intel` watches a list of companies. The difference is
what a run is allowed to find: a competitor brief reports on the names it was given, and a field
brief reports whatever moved in the field, including a name nobody had heard of last month. A member
asking to "keep up with" an area wants the second one.

The turn writes the first brief, then asks once whether it should repeat. The brief is the product and
it is what the handoff returns; a recurring row exists only after the member says they want one.

What repeats is the gathering. The recurring row fires daily and records what it finds; every edition
after this one is `field-report`'s, written when the member asks to read one. A field moves every
day, so gathering every day earns its cost — and whether an edition is worth reading today is a
thing the member knows and a schedule does not.

This runs as `pulse`, which is what makes the row it arms `pulse`'s: a scheduled task is owned by
the agent whose turn applied it. The member is not in this conversation — `pulse-handoff` spawned it
from theirs, and every question asked here reaches them through it.

## Read what was handed over before asking

1. The payload states the field in the member's own words, the business a story is ranked against,
   and their local time. There is no opening line and no `<context>` header on a spawned turn, so
   the payload is the whole of what this turn was told. `memory_search` for the field and for any
   pulse already running; finding nothing is silent, not a sentence.
   1a. When the payload's business is empty, the handing turn read its opening line and searched
   memory and neither answered, so the business is genuinely unknown and that is the one case worth
   a question: in one message, ask which business or field to track, in the same short numbered-list
   style as "Ask only what blocks the brief" below. This is not the asking step 1 rules out — it is
   what is left once the two sources that could answer have been tried.
2. Name the field back in one sentence before proposing anything. "Open-source data infrastructure"
   and "the data industry" produce different briefs, and the narrower one is almost always what they
   meant. A field too broad to bound is the one thing worth asking about.
3. Propose the source kinds, do not request them. One web search on the field names its venues:
   where releases land, where its research is posted, where its practitioners argue, which registries
   or indexes record its activity. Ask the member to confirm, drop, or add. "What sources do you
   want?" is the trap — the member came to be told where to look.

## Ask only what blocks the brief

One message, one short numbered list: confirm the field sentence, and confirm the proposed sources as
a yes/no. Cadence is not asked here. "Set it up" and "go ahead" are confirmation of the brief; do not
ask again.

A question ends this turn. It reaches the member in the conversation the handoff came from, and their
answer arrives here as this series' next turn with everything above it still in front of you. So one
question is one round trip through somebody else's conversation, and two of them is the member being
interrupted twice for a brief they have not read yet.

## Gather the first window

Load `research-assistant` and find what changed across the confirmed sources, bounding every query to
the recent past. Set each source's state as its read returns, per `coverage-honesty`'s three states,
and record them under this gather's date with `pulse_record_coverage` — an edition covering more
than one gather writes its footer from those rows, and a state is only writable while the read is in
front of you.

`field-report`, `brief-continuity` and `coverage-honesty` arrive with this skill: the first writes an
edition from a pool, and the other two hold the contracts a series lives by — what an edition may
repeat, and what it may claim about a source it could not read. They are loaded, not optional.

Record every candidate in the sightings pool with `pulse_record_sightings` — everything the gather
surfaced, not only what looks publishable, in one call. A sighting is a fact at the moment the source
returned it; whether it clears the bar is a separate judgement made after, and recording the two at
the same moment is how a pool ends up holding only what an edition already carried. A record call
that fails is named in the reply, per `field-report`'s rule on a write that did not land: a window
whose states never reached the record is one every later edition has to read from silence.

`field-report`'s business-relative bar makes the pool more valuable rather than less. That bar
rejects more than a field-relative one would, and every rejection is a lead rather than nothing: a
story that changes nothing for this business today is exactly the one that may change something next
month, and the pool is what lets a later edition tell it apart from a lead that has gone quiet for
good.

## Write the first brief with the skill that writes every brief

The pool is filled, so the edition comes from the pool: load `field-report` and write it. The window
is this turn's gather, there is no ledger to read yet, and the rows it records are the series' first.
Ranking, the story count, the headline register, the footer and what to say when a write fails are
that skill's and are not repeated here — one writer of editions is what makes the brief a member
reads today and the brief they ask for in November the same publication.

This is the only turn where a gather and an edition share a fire, and the member is waiting on it in
the conversation the handoff came from. Every edition after it reads a pool that was filled before
they asked.

So this turn knows something the pool does not: it watched the reads happen. A source that did not
answer is named in this edition's footer with which of `coverage-honesty`'s four reasons kept it
out — unreachable, rate-limited, out of budget, or authorization expired — and the coverage row
written as the read returned is what carries that reason to every edition after this one, where the
pool alone can only say a source left no row.

## Ask once whether it repeats

After the brief, and only after it, ask once with `ask_user`: keep this as a one-time brief, or gather
from here on. Say what a yes buys — the sources read every morning, and an edition whenever they ask
for one. Recommend **daily**. A day the field produced nothing costs one search and lands nothing in
the conversation, while a day nobody gathered is a hole in every window that crosses it; the reader
is never made to read an edition they did not ask for, so the cadence is set by the field rather than
by their patience. A member who names a cadence has answered — apply it and ask nothing.

## Apply the task, once they asked for a recurring one

A one-time answer ends here: say the brief is written, name the file and where it is, offer nothing
else. The file is in this conversation's own workspace rather than the member's, which is why the
reply carries the edition itself and not a pointer to it.

For a recurring answer, load `task-scheduling` and apply one manifest named `<field>-gather`. The
row is this agent's because this turn applied it, which is the whole point of running the setup
here. Fire early in the member's morning, before they would think to ask for an edition, read from
the payload's `local_time` and converted to UTC. Carry no `run_now`: this turn gathered the
window already, so an immediate fire would spend a full gather re-reading it. The record itself is
safe — one series, one day, one lead, one address is one row, and one series, one gather, one source
is one row, so a second recording of this window adds nothing — but the gather is the expensive half
and it would be paid for twice.

```yaml
kind: scheduled_task
name: <field>-gather
spec:
  schedule: "0 12 * * *"
  description: <Field> gather, daily
  expires_at: <a quarter from today, YYYY-MM-DD>
  prompt: |
    Gather the <field> window for series <series>. Load research-assistant and find what changed
    across <the confirmed sources> since yesterday, bounding every query to that window. Record
    every candidate in the sightings pool with the pulse_record_sightings tool — everything seen,
    one row per sighting, each with its source, in one call. Use the tool and not a script: a
    script writes to a path resolved against this fire's own working directory, which is not the
    one a terminal-bound conversation starts in, and a series recorded from both keeps two ledgers
    that each look complete. Load coverage-honesty and set each source's state as its read returns,
    recording them under today's date with the pulse_record_coverage tool — every source tried, in
    one call, and the tool again rather than a script for the same reason. The edition that covers
    this window counts those rows, and a state nobody recorded here is a day it cannot account for.
    Rank nothing, write no report file, and record nothing in the covered ledger: the edition is
    written when the member asks for one. The pool is this fire's whole product, so send no brief.
```

Every field is filled from this member's own answers. A source carried over from this example is a
source nobody confirmed.

`task-scheduling` bounds tasks that fire daily or more often with an `expires_at`, and a daily gather
is one of those: set it a quarter out. A pulse the member stops reading then stops paying for search
on its own, and `field-report` is where the lapse surfaces — a window whose pool is silent reports the
day the pool last took a row, in the conversation the member can re-apply from.

## Record what lasts

Before applying, `memory_update` one item for the field sentence, one for the confirmed source set,
and one for the series name. `field-report` reads all three: the first is what a story is ranked
against, the second is what lets a footer name a source that left no row, and the third is the address
of both the pool and the ledger — an edition that derives a second series name opens a second pool
beside the real one. A spawned conversation carries the audience of the one that spawned it, so an
item written here is the same item read where the member asks; the series name in particular is what
an edition written in their own conversation resolves the record by. Per-edition findings are the
reply and the report file, never a memory item.

## Close

Four lines at most: what it watches, that the first edition is the one above, that it gathers every
morning from now on, and that an edition comes whenever they ask for one, in their own conversation.
The reply is what the handoff returns to the member, so it is the brief and those four lines and
nothing about this conversation.

## Traps

- Running this for a list of named companies, which is `competitive-intel`.
- Asking which sources to watch instead of proposing them.
- Accepting a field too broad to bound, which produces a brief about everything.
- Asking about cadence before the member has read an edition.
- Letting the gather's prompt rank, write a report file or record the covered ledger. That is an
  edition nobody asked for, and its ledger rows make every story in it ineligible for the edition
  they do ask for.
- Gathering less often than daily, which leaves a hole in every window that crosses the missing day
  and nothing in the pool to recover it from.
- Applying a daily gather with no `expires_at`, which `task-scheduling` bounds, so an abandoned
  pulse keeps paying for search with nobody reading it.
- Carrying `run_now: true`, which records this turn's sightings a second time and makes `history`
  read as a story that moved.
- Writing the first brief by hand rather than loading `field-report`, which fills a pool nothing
  reads and leaves the ledger without its first rows.
- Letting an edition go back to the field for a fresh search instead of reading the pool, which pays
  twice for the window the gather already covered and makes the member wait for it.
- Letting the schedule's prompt name the sources while the series name is left as a placeholder.
- Recording only what looks publishable, which leaves the pool blind to every lead the bar rejected.
- Leaving a gather's source states unrecorded, so an edition spanning several of them can report
  only the last one's failures.
- Deriving a field, or proceeding silently, when the payload's business is empty — the turn has
  nothing to ask a field or rank stories against, and step 1a's question is the only correct move.
- Running this workflow in the conversation the member is sitting in. The row it arms belongs to
  whichever agent applied it, so a setup run outside `pulse` arms a daily search under the
  assistant's name for the quarter the row lasts; the ask is handed over by `pulse-handoff`.
- Reading a clock off this turn rather than the payload's `local_time`. A spawned turn carries no
  `<context>` header, so the only time available is the deploy's, and a gather set from it fires in
  the member's evening.
- Asking twice before the brief. Each question is a round trip through the member's own
  conversation, and the second one is an interruption for a brief they have not read.
- Ending on a pointer to the report file. A file is written where the turn ran and this turn ran
  here, so the member cannot open it; the record is what the series keeps, and the edition itself is
  what the reply carries. An edition they ask for in their own conversation writes into their tree.
- Assuming an edit to this skill reaches an armed row. `task-scheduling`'s manifest stores its
  `prompt` on the scheduled row at apply time, and a fire runs that stored copy, not this file — an
  armed gather keeps the rule it was set up under until someone re-applies the manifest. Re-applying
  `<field>-gather` upserts it in place and re-points its reporting to whichever conversation ran the
  re-apply, so a member who wants the new rule re-applies from the pulse's own conversation, not from
  wherever the skill was edited. Same family as #70's running `serve` process keeping the skills it
  booted with — a durable copy an edit does not reach, different surface.
