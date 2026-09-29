---
name: brief-continuity
description: "Load when a recurring brief, digest, or watch must not repeat what an earlier edition of the same series already published, when a member says a report keeps telling them what they already know, or to track leads already seen. Not for a first brief with no prior edition."
---
# Brief continuity

A recurring brief is a series. Its reader has read the last one, so the question this edition answers
is not "what is true about this field" but "what changed since the edition before it." Those two
questions select different stories, and a run that answers the first one publishes last week's brief
again with today's date on it.

The covered ledger is what makes the difference decidable: one row per story the series has
published, written by the run that published it and read by the run after.

**Write it with `pulse_record_edition` and read it with `pulse_recall`.** Both are tools, and that
is forced rather than stylistic: a script writes to a path resolved against the directory the turn
started in, and a brief's carriers do not share one. A conversation bound to a terminal runs in that
terminal's directory; one that is not has a tree of its own. A series run from both grows **two**
ledgers, each looking complete, and the no-repeat rule then governs whichever half happens to be
visible. That is measured, not feared — one series on the demo deploy had two ledgers whose most
recent edition shared no story at all. Never in memory either: `task-scheduling` keeps an
already-covered ledger out of memory facts because a per-run snapshot written as a memory fact is
injected into unrelated later turns.

The workspace files are still written, by a job, a few minutes behind the record:
`pulse/<series>.covered.jsonl`, `pulse/<series>.seen.jsonl` and `pulse/<series>.coverage.jsonl`, in
the same shape the scripts below read. They are the copy the member opens, and they are always a
copy. **Never treat a file as the record** — read it for a member, ask `pulse_recall` for a
decision.

**A row written into one of these files is erased, not kept.** The job renders the whole file from
the record, so anything the file holds that the record does not is gone at the next render. This is
not a warning about tidiness: a real fire appended two published stories to the file instead of
calling `pulse_record_edition`, and both would have vanished at the next projection — the edition
would have read as never published and the next one would have carried it again. There is no
`record` subcommand on any of the three scripts for this reason. If a row belongs in the series, it
goes through the tool, and there is no second way to do it.

## Read the ledger before ranking, not after

Read it first. A story's eligibility decides whether it is worth researching further, so a run that
ranks and then filters spends the whole window's effort on stories it then throws away.

Call `pulse_recall` with the series. It answers with the leads seen recently and every story the
last five editions carried, which is both halves of the ranking decision in one call.

The member's own reader over the projected file is the same answer, for a turn that has a command
tool and wants it on the terminal:

```bash
python "$UFO_HOME/skills/brief-continuity/covered.py" recent --series <series> --editions 5
```

Treat what it prints as this tree's copy rather than as the series. A file that is empty, short, or
a few minutes behind the record is all normal. Never conclude anything about eligibility from it —
ask `pulse_recall`, which reads the record itself.

The last five editions are the baseline. Older coverage has left the reader's head and may be
carried again as new; five editions is the span a returning reader holds.

## Covered means ineligible

A story any of the last five editions published is ineligible for this one. It is ineligible as a
headline, as a one-line mention, and as a footnote, and it stays ineligible when it resurfaces with
more engagement than it had the first time. Engagement measures how many people are still reading it,
which is not the same claim as something having happened.

Re-surfacing is the normal case, not the exception. A multi-day window overlaps the previous window's
tail, discussion of a launch peaks days after the launch, and an aggregator's ranking lifts a story
twice. The published series is the baseline for this decision — never the gathering window, which
only says what was fetched.

## The one exception is a material new development

A covered story becomes eligible again when something happened to it that a reader of the earlier
edition does not know. Four things qualify:

| Development | Not a development |
| --- | --- |
| A release shipped that was announced before | More coverage of the announcement |
| A deal, raise, or acquisition closed | A rumor restated by a second outlet |
| A number was confirmed, or corrected | A number quoted again |
| A reversal, withdrawal, or shutdown | Commentary on the original |

Then lead with what is new. The earlier edition carries the background, so an entry that opens by
recapping the original is spending the reader's first line on what they read last week.

    Bad:   Acme's open compiler, announced in March as a rewrite of its
           toolchain, has now shipped its first release
    Good:  Acme's compiler rewrite shipped, six weeks after the March announcement

One clause of background is the budget, and it goes after the new fact, not before it.

## Write the ledger when the edition publishes

Every story that reaches the published edition gets a row, and the rows are written once the edition
is out — not while drafting, or a story cut in the last pass reads as covered to the next run and is
never carried at all.

Call `pulse_record_edition` once with the series, the edition date, and every story the edition
carried. One call, not one per story.

The slug is the story's durable address across editions, so it names the thing that happened and not
the way this edition phrased it: `acme-compiler-1-0-shipped`, never `big-week-for-acme`. A story
carried again under the material-new-development exception keeps its original slug — that is what
makes the second entry findable as a second entry.

A cluster of sources covering one story is one row. The row carries the primary link; the edition
carries the rest.

## The sightings pool is a second store, not a second ledger

The sightings pool records what a gather has **seen**. The covered ledger records what an edition
**published**. They answer different questions and must not be merged: a lead seen four times and
never published is not a repeat, and a story published once is not a lead.

**Write it with `pulse_record_sightings`** — one call carrying every candidate the gather surfaced,
each with a date, a slug, a title, and a url. **Read it with `pulse_recall`**: with no slug it names
the live leads, and with one it answers that lead's whole sighting history and every edition that
carried it, which is the evidence the material-new-development exception needs.

Two diagnostics have no tool and are run over the projected file, so they need a carrier:

```bash
python "$UFO_HOME/skills/brief-continuity/seen.py" stale     --series <s> --slug <slug>
python "$UFO_HOME/skills/brief-continuity/seen.py" reconcile --series <s>
```

Fourteen days is the window both `pulse_recall` and `stale` default to, and it is the same kind of number
as the five-edition span above: two weeks is roughly how long a lead stays worth chasing before its
silence is the story. Pass `--within-days` where a field moves faster or slower.

Nothing is ever removed. Age is a reason not to pursue a lead, never a reason to forget it:
`stale` exits non-zero for a lead that has gone quiet, and the lead stays in the pool with its whole
history, so when it moves again that history is still attached. A pool that expired it would have
discarded it at exactly the moment it became interesting.

Recording the same sighting twice is safe. One series, one day, one lead, one address is one row, so
a retried fire, a replayed batch, or a gather that surfaces a lead twice in one run adds nothing the
second time. That is a property of the record, not of care taken at the call site.

**That is also how a split ledger is repaired.** A series that ran before the record moved into
tables has one file per tree — typically `pulse/<series>.covered.jsonl` where a member's session
starts, and `workspaces/<conversation-id>/pulse/<series>.covered.jsonl` where a fire's did. Read
each one and record its rows; because a repeat is not a second row, the files can be imported in
either order, twice, or after an interruption. Do this before trusting a series that predates the
tables, and say in the reply how many rows each file held.

A url is required on every sighting, and whitespace does not count. The slug is this run's judgement of what a
story is, so two runs need not agree on it. The url is the best external key a row carries: better
than the title, which legitimately drifts between sightings, and than the source, which cannot tell
two stories from one publisher apart. It is not the only external field and a thin row is not beyond
manual repair — it is the one a reconciler cannot repair automatically, which is why `record`
refuses it rather than storing a row that looks fine and answers nothing.

Point it at the story, not at where the story was found. A blog index or a repository root passes
this check and identifies nothing: the next story from the same index carries the same url, so two
different stories reconcile as one. That failure is quieter than a missing url, because the row
looks complete.

Rows written into an old file before a url was required stay readable by `stale` and `reconcile`,
and a url-less one cannot be imported — `pulse_record_sightings` refuses it. Repair it by hand from
the title, or leave it in the file as history the record does not carry, and say which. Nothing
backfills the tables on its own.

`reconcile` reads the pool against itself and exits non-zero where rows cannot be told apart. It
reports; it does not diagnose, and the difference is the point.

**One url under several slugs has two causes and the pool cannot separate them.** Either a story was
renamed between sightings — the trap below, which the ledger's ineligibility rule cannot see because
that rule keys on the slug — or the url names a *page* rather than a story, a blog index or a
repository root, and the slugs are different stories found at one address. Both are ordinary. The
report names both and chooses neither.

That is also the reason to give a story's own address rather than the page it was found on. A url
that names an index cannot be reconciled with anything, which is the same loss as omitting it.

One slug over several urls is the quieter direction: two stories filed as one lead, the second
hidden behind the first's history.

What it stays silent about is not thereby clean — a story syndicated at three addresses is three
urls and reads as three leads. It never writes: a check that repaired what it found would be the
expiry this pool rules out.

**Staleness keys on the most recent sighting, not the first.** A lead first seen thirty days ago and
seen again this morning is live. Asking when it first appeared answers a different question.

Rows sharing a slug are that lead's history, exactly as they are in the ledger. Two sightings of one
story in different states are two rows rather than an overwrite, and the difference between them is
the evidence the material-new-development exception wants — better evidence than the ledger alone
gives, because it records how a story moved between editions rather than only that an edition
carried it.

Every read is a pure read. `stale`, `fresh` and `history` never write, mark or delete a row; a
staleness check that wrote anything would be the expiry this design rules out, wearing a different
name.

`record` is deliberately not idempotent. A sighting is an observation, not a fact about a story, so
seeing one story twice in a day is two observations and both are recorded. `fresh` and `stale` read
the most recent sighting and are unaffected; `history` is where the repetition shows, which is the
place it is worth seeing — a lead a source keeps re-running is behaving differently from one that
appeared once.

## Coverage state is a third store, one row per source per gather

This store records what each source returned on each gather: read with items, read and empty, or not
read for one of `coverage-honesty`'s four reasons. The ledger's subject is a story and the pool's is
a lead; this one's is a source, which is why it carries no slug for a story and answers what neither
of the others can.

**Write it with `pulse_record_coverage`** — one call per gather, carrying every source it read or
tried to read, each with its state, its item count if it answered, and its reason if it did not. The
states are only writable while the reads are in front of the run, so the gather makes this call and
no later turn can.

The footer's own read has no tool and is run over the projected file, so it needs a carrier:

```bash
python "$UFO_HOME/skills/brief-continuity/coverage.py" window \
  --series <s> --since <date> --until <date>
```

`window` states every source across the gathers between those dates: read on all three, not read on
two of three, not read throughout. An edition covering one gather has one set of states to report and
an edition covering three has three, and `coverage-honesty` is what the footer follows in either case.

The source slug is that source's durable address across gathers, exactly as a story slug is across
editions. The footer names the source in the reader's words and the store keys it by the slug, which
is what lets three days of one source aggregate as one source rather than as three.

A retry inside one gather is that gather's answer: a source that rate-limited the first attempt and
answered the second was read that day, so the second recording stands in place of the first. One
series, one gather, one source is one row, which is why a replayed fire adds nothing. Every read
here is a pure read, exactly as in the pool.

## Traps

- Ranking before reading the ledger, then discarding the winners.
- Treating the gathering window as the baseline, so a story fetched twice publishes twice.
- Letting fresh engagement reopen a covered story with nothing new under it.
- Recapping the original before the new fact, under the exception.
- Giving a carried story a new slug, which hides that the series has covered it before.
- Writing rows while drafting, so a cut story is recorded as published.
- Running the script from a subdirectory, which starts a second ledger the next edition cannot find.
- Saving the ledger as a memory fact, which pushes single-run snapshots into unrelated turns.
- Reading more than five editions back and calling a genuinely old story ineligible forever.
- Recording a sighting in the covered ledger, which marks an unpublished lead as already covered.
- Recording a blog index or a repository root as a story's url, which reconciles two different
  stories into one and looks correct while doing it. A placeholder like `n/a` or `-` passes the
  check for the same reason and identifies even less.
- Deleting or rewriting a pool row to express staleness, rather than letting `stale` answer it.
- Keying a source by the words one footer used, so one source across three gathers aggregates as three.
- Writing a coverage row into the projected file rather than calling `pulse_record_coverage`, which
  leaves a state the next render erases and a gather no later edition can account for.
- Leaving a source out of the call because it answered as expected, which makes a gather that read
  it indistinguishable from one that never tried.
