"""The agent a pulse runs as, shipped through the manifest's `agents` point.

**Why an agent row rather than a skill the assistant loads.** A scheduled task belongs to the agent
whose turn applied it — `scheduled_tasks` writes `agent_id` from the creating turn and its manifest
`spec` names no agent — so a gather armed from a chat turn fires as the assistant, under the
assistant's prompt and model, for a job that is nobody's idea of an assistant's. The row here is
what a gather can be owned by: its own prompt, its own reasoning level, its own conversation. The
setup runs as `pulse`, and the daily gather it arms is `pulse`'s because of that and nothing else.

**Activation creates it.** `AgentProvision` is core's own mechanism: a workspace's first pass over
the active extension set turns each provision into an ordinary `agent` row and then stops owning
it — the workspace's copy is the live configuration from that moment, and a later version of this
extension writes it again only to carry forward the purpose and the setup it declares. So a deploy
that names `pulse` in its pack has the agent; nothing is created lazily in a member's turn, and
there is no onboarding step to skip.

**One agent, every series.** A series is separated by its name, not by the agent that runs it: the
record is keyed by workspace and series, the scheduled row is named `<field>-gather`, and each
handoff opens a conversation of its own. A second row would duplicate a prompt that names no field
while separating nothing the series does not already separate.

**Why `visibility` is set here.** A spawn of an ownerless agent row — a provisioned one, like this —
is refused to anyone but a workspace admin unless the row is workspace-visible. A member handing
their own field over is not an admin operation, so the row is `workspace` and the refusal never
fires on the one path that matters.

**Why the payload is declared.** A spawned turn's inbound is the bare payload its contract promises,
with no `<context>` header in front of it: no opening line, no `time:` line. Both are things
`field-pulse` reads, so both travel in the payload, and the contract is what refuses a handoff that
left one out — a gather armed against a time nobody supplied would fire at the deploy's midnight in
a member's evening.
"""

from pathlib import Path

from ufo.sdk.manifest import AgentProvision, AgentSetup, AgentSpec

AGENT_NAME = "pulse"

AGENT_PURPOSE = "Follows a field you name and writes you a brief on what changed in it."

# `auto` follows the deploy's configured model, which is the only model a public extension can name:
# a pinned id the deploy's registry does not serve fails every turn of this agent, the turn that
# would repair it included. A deploy wanting one edits the row, which activation has stopped owning.
AGENT_MODEL = "auto"

# Ranking a field's week against one business is the judgement the series exists for, and it is the
# one step no script can hold: eligibility is a lookup, importance is not.
AGENT_REASONING = "high"

AGENT_PROMPT = (Path(__file__).parent / "prompts" / "pulse_agent.md").read_text().strip()

AGENT_SETUP_INSTRUCTIONS = (
    "Fill the search backend's credential slot for this workspace in chat — a gather reads "
    "nothing without it, and the slot is read per request, so an unfilled one fails inside the "
    "gather rather than at boot. Then name a field to follow; this agent arms its own daily "
    "gather once the member has read the first brief and asked for it to repeat."
)

REQUEST_KEY = "request"
BUSINESS_KEY = "business"
LOCAL_TIME_KEY = "local_time"

AGENT_INPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        REQUEST_KEY: {
            "type": "string",
            "description": "What the member asked for, in their own words.",
        },
        BUSINESS_KEY: {
            "type": "string",
            "description": (
                "The business a story is ranked against, from the handing turn's opening line or "
                "its memory. Empty where neither answered, which this agent asks about."
            ),
        },
        LOCAL_TIME_KEY: {
            "type": "string",
            "description": (
                "The handing turn's own `time:` line, verbatim: the member's clock, which the "
                "daily gather is set early in the morning of."
            ),
        },
    },
    "required": [REQUEST_KEY, LOCAL_TIME_KEY],
    "additionalProperties": False,
}

PROVISION = AgentProvision(
    name=AGENT_NAME,
    spec=AgentSpec(
        model=AGENT_MODEL,
        reasoning=AGENT_REASONING,
        # A gather reaches its sources through `research`'s tools and the search backend's own
        # credential egress, which this permits; the public-internet capability is a wider grant
        # than a brief has ever needed.
        internet_access_allowed=False,
        visibility="workspace",
        prompt=AGENT_PROMPT,
        purpose=AGENT_PURPOSE,
        input_schema=AGENT_INPUT_SCHEMA,
    ),
    # The member-facing tool set. An allowlist here would be this extension naming the tools of two
    # others — `research`'s search and `scheduled_tasks`' apply — and an allowlist is intersected
    # with the live registry at turn load, so a name guessed wrong is not an error but a gather that
    # cannot search.
    tools=None,
    # The search credential is named in prose rather than as a declared slot: a declared name is
    # read back as filled or unfilled, and the slot belongs to whichever backend the deploy
    # selected, so naming one would report a deploy on another backend unready. No schedule is
    # offered either — a `SetupSchedule` carries one fixed name and one prompt, and a gather's
    # prompt names the field, the sources and the series this member confirmed.
    setup=AgentSetup(instructions=AGENT_SETUP_INSTRUCTIONS),
)
