from ufo.harness.models.interface import Message, TextBlock, ToolResultBlock, ToolUseBlock
from ufo.runtime.turns.record import SUBAGENT_EVENT_LIMIT, subagent_activity
from ufo.schema.records import ActivityEvent


def test_a_run_states_its_words_then_its_work_in_order() -> None:
    events = subagent_activity(
        (
            Message(role="user", content="{}"),
            Message(
                role="assistant",
                content=(
                    TextBlock(text="  Reading the changelog first.  "),
                    ToolUseBlock(id="call-1", name="fetch_url", input={"url": "https://x/y"}),
                    ToolUseBlock(id="call-2", name="grep", input={"pattern": "shipped"}),
                ),
            ),
            Message(
                role="user",
                content=(
                    ToolResultBlock(
                        tool_use_id="call-1",
                        content="…",
                        activity=True,
                        activity_text="Reading the changelog.",
                    ),
                ),
            ),
        )
    )

    assert events == (
        ActivityEvent(kind="note", text="Reading the changelog first."),
        ActivityEvent(kind="activity", text="Reading the changelog."),
    )


def test_a_call_whose_generated_activity_was_empty_leaves_no_event() -> None:
    events = subagent_activity(
        (
            Message(
                role="assistant",
                content=(ToolUseBlock(id="call-1", name="bash", input={"command": "ls"}),),
            ),
            Message(
                role="user",
                content=(
                    ToolResultBlock(
                        tool_use_id="call-1", content="README.md", activity=True, activity_text=""
                    ),
                ),
            ),
            Message(role="assistant", content="Done."),
        )
    )

    assert events == ()


def test_the_description_on_a_call_names_it_when_no_activity_text_landed() -> None:
    events = subagent_activity(
        (
            Message(
                role="assistant",
                content=(
                    ToolUseBlock(
                        id="call-1", name="probe", input={"user_description": " Checking it "}
                    ),
                ),
            ),
            Message(
                role="user",
                content=(ToolResultBlock(tool_use_id="call-1", content="ok", activity=True),),
            ),
        )
    )

    assert events == (ActivityEvent(kind="activity", text="Checking it"),)


def test_events_are_bounded() -> None:
    notes = tuple(TextBlock(text=f"note {at}") for at in range(SUBAGENT_EVENT_LIMIT + 5))
    events = subagent_activity((Message(role="assistant", content=notes),))

    assert len(events) == SUBAGENT_EVENT_LIMIT
