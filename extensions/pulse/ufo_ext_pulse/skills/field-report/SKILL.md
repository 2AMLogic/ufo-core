---
name: field-report
description: "Load when a member asks for an edition of a pulse already gathering — this week's brief, the latest report, whatever has come in since. Not for setting a pulse up or choosing its sources, which is field-pulse, and not for a field nothing has gathered."
metadata:
  depends: [brief-continuity, coverage-honesty]
---
# Field report, written from the pool

Gathering and publishing run on different clocks. A field produces news on its own schedule, so a
gather fires daily; an edition is read when the member decides to read one, so it is written when
they ask. One fire doing both pays for a search on every edition and publishes editions nobody
asked for.

This skill writes the edition, from what the gathers already recorded. Nothing is searched in this
turn, which is what lets the brief arrive in the conversation rather than minutes after it.

## Nothing is searched in this turn

`research-assistant` has no part in this turn. The stories were found, and their sources paid for,
by gathers that ran before the member asked; a search here buys the same window a second time and
turns an answer into a wait.

A row too thin to write two sentences from is a lead, not a story. It stays in the pool, and the
next gather either moves it or it goes quiet — going back to the field to thicken it is the same
search under another name.

## The window, and saying which one it was

| Bound | Value |
| --- | --- |
| Opens | The most recent edition in the covered ledger |
| Closes | Today |
| Reaches back at most | Fourteen days, the span `pulse_recall` calls a lead live |
| Overridden by | A window the member named in the ask |

With no edition in the ledger yet, the pool's own fourteen days are the window. Both bounds are
read rather than assumed:

Call `pulse_recall` with the series. One call answers both: the leads seen in the last fourteen
days, and every story the last five editions carried. Use it rather than the scripts — a script
reads whichever copy of the ledger happens to sit in the directory this turn started in, and that
is one tree's half of the series, not the series.

The reply states the window as a span, and says how much of it the pool holds rows from.

    Window 2026-09-22 to 2026-09-28; the pool holds sightings from six of those seven days.

A reader who is not told the span cannot tell an edition covering a week from one covering a night,
and both are legitimate editions of the same series.

## Rank against the business, not the field

The opening line states the business, and its business sentence is what a story is ranked against —
read it and never ask what the business does. A turn that has no opening line is the setup turn
`pulse-handoff` spawned, and there the business is the one in its payload. `memory_search` for the
series returns what that setup recorded: the field sentence, the confirmed source set, and the
series name.

`brief-continuity` and `coverage-honesty` arrive with this skill and hold the two contracts a series
lives by: what an edition may repeat, and what it may claim about a source it could not read. Read
the covered ledger on `brief-continuity`'s terms before ranking anything — eligibility decides
whether a lead is worth thinking about, so a run that ranks and then filters spends the window on
stories it throws away.

Importance is whether this changes a decision, a plan, a cost, a risk or a dependency for *this*
business — not whether someone working in the field would find it notable. Source signals inform the
ranking and do not decide it. Cluster the pool rows covering one story and rank the story once, on
its own weight rather than the sum of its coverage; rows sharing a slug are one lead's history, and
the movement between them is often the story itself.

Five to ten stories carry an edition, but that is a ceiling rather than a quota. The count is a
consequence of the bar: publish what clears it and stop. Reaching for a number is how a brief about
this business becomes a brief about its field, and the reader stops at the point where ranking
stopped being visible.

Each story gets a headline in the reader's plain language — not the source's headline — and two to
four sentences on why it matters. Say what a thing claims, not what it might mean, and say plainly
when something is early, small, or unproven. No superlatives, and no word the reader would not use
about their own work.

## Write the edition

Load `research-report` and write `<series>-<date>.md`, closed with the footer below. Reply with the
brief itself, never with a promise of one.

Record the published stories with `brief-continuity` once the edition is out. Every later edition's
eligibility read is against these rows, and a lead the pool keeps but no edition carried stays a
lead — the two stores answer different questions and a row in the wrong one loses the answer.

**If a write fails, say so in the brief.** Writing the report file needs a workspace a command can
write to, and a turn bound to a terminal that has since disconnected has none — it reads, ranks and
replies perfectly well, so the edition looks finished while the file was never written. Recording
the ledger is no longer in that category: `pulse_record_edition` writes to the record, which every
turn reaches. Name the file that did not land, and do not describe the ledger as lost when it was
not.

So a failed write is part of the brief, not a detail to swallow: name what could not be written and
that continuity is broken for this edition. An edition that could not record itself is worth more
when it says so. This is the same rule `coverage-honesty` applies to a source that did not answer,
pointed at the series' own record instead of its inputs.

## The footer reads the gathers' own states, and names where there were none

Each gather recorded every source's state with `pulse_record_coverage` as its read returned, so the
footer is a lookup rather than an inference. The states are on the record; this reads the projected
copy of them, over the same span the window line states:

```bash
python "$UFO_HOME/skills/brief-continuity/coverage.py" window \
  --series <series> --since <the window opens> --until <today>
```

`window` states each source across the gathers it covers: read on all of them, read on two of three,
not read throughout, and which of `coverage-honesty`'s four reasons kept it out. An edition covering
one gather takes that skill's single-run lines; one covering several says how many of them each
state held, which is the count it requires and the ambiguity it refuses.

    Read: releases on all 6 gathers (14 items), papers on 4 of 6 gathers (5 items).
    Not read: the filings index rate-limited 5 of the 6 gathers.

A window can reach back past the gathers a series has states from, and a gather that recorded none
left none. The file is a projection of the record rendered whole and a few minutes behind it, and it
lands in the conversation the series last recorded from — so a turn in a conversation that has never
recorded for this series may find no file where the record holds rows. There the pool is the only
witness, and it records sightings rather than reads: a source with rows was read at least once, and
a source with none may have been read and empty or never reached. Say which store answered, because
the two support different claims — a recorded state names the failure, and the pool's silence
supports only `coverage-honesty`'s third state, that this edition has no evidence from that source.

    No evidence: the filings index left no row on 2026-09-22, which recorded no states.

The confirmed source set is what makes that line possible. A source left out of the footer because
neither store mentioned it reads as a source nobody thought about, which is the claim
`coverage-honesty` refuses. Its threshold reads the same way here: a window most of the confirmed
sources went unread or unrecorded across is a window this edition has not covered, and it publishes
the footer's lines rather than a brief built from the part that answered.

An edition where nothing in the pool clears the bar is a finished edition, not a failed one. Say
that nothing in the window cleared the bar, name the window and the sources that left rows, and
never lower the bar to fill a page. A reader who is told "nothing this week" and can believe it is
the reader this brief is for.

## A silent pool is a gather that stopped

A window whose pool holds no rows at all is not a quiet field. Publish no edition: say which span is
silent, and when the pool last took a row. A daily gather carries an `expires_at`, so a pulse nobody
re-applies stops paying for search on its own, and this line is where that shows — in the
conversation, which is the one place the member can re-apply it from.

## Close

The edition, the window line, the footer, and the file's name. Nothing after them: the member asked
to read a brief.

## Traps

- Loading `research-assistant`, which buys a window the gather already paid for and makes the member
  wait for it.
- Going back to the field to thicken a thin row, rather than leaving it in the pool for the next
  gather to move.
- Ranking the pool before reading the covered ledger, then discarding the winners.
- Publishing an edition without saying which span it covers, so a week and a night read the same.
- Writing a source's silence in the pool as "nothing new", when a read-and-empty source and an
  unreached one leave the same absence of rows.
- Reading the footer out of the pool's silence over gathers that recorded their states, which turns
  a named failure back into an absence and loses the count of days it lasted.
- Reporting a quiet field over a window whose gathers did not run.
- Running the scripts from a subdirectory, which reads an empty pool and an empty ledger and
  publishes an edition with nothing behind it.
- Deriving the series name instead of reading the one the setup recorded, which opens a second pool
  and a second ledger beside the real ones.
- Padding a thin window to reach a story count, which turns a brief about this business back into a
  brief about its field.
- Writing a headline in the source's words rather than the reader's.
- Recording ledger rows while drafting, so a story cut in the last pass is recorded as published and
  is never carried at all.
- Writing this edition's stories into the pool rather than the ledger, which records an edition as
  merely seen and leaves the next one free to publish it again.
- Replying with a finished-looking edition after a write failed, which leaves the next edition to
  publish it a second time.
