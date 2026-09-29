---
name: coverage-honesty
description: "Load when a scheduled brief, digest, or watch reports a quiet window, or when a source it reads was unreachable, rate-limited, out of budget, or newly unauthorized. Not for choosing what a brief covers when every source answered."
---
# Coverage honesty

A source a run could not read is not a source with nothing in it. "No new filings this week" and "the
filings index refused us this week" are different claims about the world, and only one of them is
evidence. A brief that renders the second as the first tells the reader the field is quiet on the
strength of having looked at nothing, and the reader acts on it.

Every source in a run ends in one of three states. Two of them are answers.

| State | The claim it supports |
| --- | --- |
| Read, with items | These things happened |
| Read, empty | Nothing happened here in this window |
| Not read | Nothing — this window has no evidence from this source |

Set the state per source as each read returns, before ranking. A state inferred at write-up time from
an empty item list is the confusion this skill exists to prevent: both answers look like zero items.

## A not-read source is named in the brief

Name it, and say which of the four things happened: the source was unreachable, it rate-limited the
run, the run's budget ran out before reaching it, or its authorization has expired. One line, in the
footer, next to the sources that did answer.

    Read: releases (11), papers (4), forums (0 new).
    Not read: the filings index rate-limited this run.

A member cannot act on a failure they cannot see. Expired authorization is the case where they can
act directly, so it says what to do:

    Not read: the analytics account's authorization expired — reconnect it.

## A report covering several gathers says how many of them

A report of one gather writes the lines above, and they describe that gather's states. A report
covering three gathers has three sets of states under it, and one flat "not read: the filings index"
over them is the ambiguity this skill exists to remove: unread on Monday and read on Tuesday and
Wednesday is a different claim from unread all week, and a reader deciding whether the field is
quiet acts on them differently.

    Not read: the filings index rate-limited 2 of the 3 gathers.

A source unread on every gather says so — "the filings index was unreachable throughout" — and a
source read every gather keeps the read line's counts and needs nothing else. Aggregate from the
state each gather recorded, never from the last gather's: the most recent day's failure is one day's
evidence, and the report covers all of them. A series keeps those per-gather states in
`brief-continuity`'s coverage store, which is what makes the count a lookup rather than a
recollection.

## A quiet window is a finding; an unread window is not

When every source was read and the window is genuinely quiet, say so in one line and stop. Do not
promote the strongest of a thin set into a headline it did not earn — a thin window earns a thin
brief, and a reader who learns the brief inflates quiet weeks stops trusting the loud ones.

`report_digest` sets `holds_a_change` false for a quiet report and writes nothing else. A run whose
sources went unread also holds no change, and is not the same entry: it holds no evidence either. Say
which one it is in the line the reader sees.

    Quiet:   Nothing moved in this window.
    Unread:  No brief this window — two of three sources were unreachable.

## When too little was read to publish

A run that read less than half its sources has not covered the window. Publish no brief. Post the
one line saying which sources went unread and why, leave the covered ledger untouched, and let the
next fire carry the window — a story missed this edition is eligible next edition precisely because
nothing recorded it as covered.

Writing a brief from the half that answered is the failure that hides itself: it looks like a normal
edition, so nobody goes back for the half that did not.

## Budgets

| Field | Budget |
| --- | --- |
| The quiet line | 12 words |
| One not-read line | 15 words, naming the source and which of the four |
| One not-read line over several gathers | 18 words, adding how many of the window's gathers |
| The read line | one count per source, nothing else |

Counts are what the run observed, never an estimate, and a source read with zero items is written as
zero rather than left out. A source omitted from the footer reads as a source nobody thought about.

## Traps

- Writing zero items from an unread source as "nothing new".
- Leaving a failure out of the footer because the brief had enough without that source.
- Inflating a quiet window into a headline, which spends the trust the loud weeks need.
- Publishing a brief when most sources went unread, which buries the gap under a normal-looking edition.
- Recording ledger rows for a window that was not covered, which makes the missed stories permanently ineligible.
- Reporting expired authorization without saying to reconnect, so the member reads a fact they cannot act on.
- Deciding a source's state at write-up from an empty list instead of at the read.
- Flattening several gathers into one footer, so unread once and unread throughout read alike.
- Aggregating a window from the last gather's states, which files one day's failure under every day.
