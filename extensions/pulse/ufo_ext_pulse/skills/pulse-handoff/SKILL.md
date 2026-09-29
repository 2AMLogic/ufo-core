---
name: pulse-handoff
description: "Load when a member asks to follow an industry, field, market, ecosystem or research area — the ask goes to the pulse agent, which runs the series. Not for named competitors, which is competitive-intel, not for one question about a field, and not for an edition, which is field-report."
---
# The ask arrives here, and the series belongs to the pulse agent

A field pulse outlives the turn it was asked for: sources confirmed once, a gather armed for a
quarter, an edition whenever the member asks. What it is armed as is decided in one place — a
scheduled task is owned by the agent whose turn applied it, and its manifest names no agent — so a
gather set up from this conversation searches every morning as the assistant, and a gather set up
from a `pulse` turn searches as `pulse`. That is the whole reason this turn hands over instead of
answering.

`spawn` is what hands over. An agent target runs as itself — its prompt, its model, its own
conversation — and what it asks and what it answers arrive back here, in the conversation the member
is already sitting in. So one sentence to their assistant is the whole entry point, and the brief
comes back to where they said it.

This turn confirms nothing, proposes no sources and writes no brief. That workflow is `field-pulse`'s
and it runs on the other side of the spawn, which is why this skill does not pull it: a copy of it
loaded here is a second turn that could run the setup, in the one agent whose gather would be wrong.

## What travels in the payload

A spawned turn's inbound is the payload and nothing else — no opening line, no `<context>` header.
Everything the setup reads off this conversation has to be handed over, and the agent's contract is
what refuses a handoff that left one out.

| Key | Holds | Missing |
| --- | --- | --- |
| `request` | The member's own words, verbatim | The spawn is refused |
| `business` | The business sentence from this turn's opening line | The agent asks the member for one |
| `local_time` | This turn's `time:` line, verbatim | The spawn is refused |

`local_time` is the member's own clock and the gather fires early in their morning, so hand the line
over as it reads and let the setup convert it. An agent left to guess arms every pulse at the
deploy's midnight.

`business` is what a story is ranked against, and it is read rather than asked for: the opening
line's business sentence is the enrichment's own summary of the company. Where that line carries no
business sentence, `memory_search` for it here — this conversation is where the workspace's record
is already being read from — and hand over an empty string only once that came back empty too.

## The handoff

    spawn(target="agent:pulse", payload={"request": "...", "business": "...", "local_time": "..."})

Name the target in its qualified form. `agent:pulse` resolves against the workspace's agent rows
whatever else a deploy has registered, where the bare name is ambiguous the day a subagent profile
takes it.

The spawn answers with its own identity at once, and the series is `pulse`'s from there. Four things
belong to this turn and nothing else does:

1. **One line that the pulse agent is setting it up.** A second sentence describing what it will ask
   is a sentence the agent then has to contradict.
2. **`memory_update` one item holding the field and the spawn id.** The id addresses the series' own
   conversation, and it is how a later turn here reaches that one rather than opening a second.
3. **Relay, never answer.** The agent ends a turn asking which sources to read, and whether the pulse
   repeats. The question arrives here: put it to the member with `ask_user` and send their words back
   with `message_spawn`. The field and the sources are theirs to confirm.
4. **Pass the brief on whole.** The first edition arrives as the agent's answer, walled as data
   because a brief is written from what the open web said. It is the product: give the member the
   edition, not a summary of it and not a note that one was written.

## An edition is not a handoff

A member asking to read an edition is answered here, by `field-report`. The record a gather fills is
keyed by workspace and series rather than by conversation, so the edition this turn writes reads the
same rows the agent's own gathers recorded — and it answers in the conversation the member asked in,
with no spawn in the request path.

## Traps

- Confirming the field, proposing sources or writing the brief in this turn. The pulse it sets up is
  the assistant's, and the daily gather it arms searches under the assistant's name for the quarter
  the row lasts.
- Handing over a field the member did not name, when the ask was one question about a domain. A
  question is answered; a pulse is a standing order they have to want.
- Handing over a list of named companies, which is `competitive-intel` and watches names rather than
  a domain.
- Spawning `pulse` by its bare name. Once a `pulse` agent row exists — true from the first time a
  gather activated it — the runtime finds both a profile and an agent row for that name and refuses
  the spawn outright, naming `profile:pulse` and `agent:pulse` in the error. Only in the narrow
  window before any `pulse` agent row has been provisioned does the bare name resolve to the profile
  and run the setup under this turn's own agent, silently.
- Dropping `local_time`, which the contract refuses, and paraphrasing it, which arms the gather in
  the wrong half of the day.
- Answering the agent's question instead of the member's, which confirms a source set nobody chose.
- Losing the spawn id, which leaves the series reachable only from the portal: a later ask here
  opens a second conversation, and the pulse the member is already paying for keeps gathering
  without them.
- Spawning a second handoff for a field already running, rather than messaging the one that is.
