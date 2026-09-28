"""The listener and the delivery handlers against a fake homeserver: a stream resumes where it
stood, the bot's own echo and every non-message event found nothing, a declined ambient line and a
non-member's line found nothing, a room's audience only narrows, one bad event or one broken bot
holds nothing else, and every message a turn sends — its reply, its files, and the words it marks
before it ends — lands once, as rich text, under the message it answers.

A question the room answers is here too: a numbered reply and a tap on a poll arrive as the one
choice, `answerable_question` is the only gate on who may answer what, the question is rewritten once
however often its answer is delivered, and a turn that runs shows the room that it is running."""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest

pytest.importorskip("ufo", reason="install ufo from git to run the surface tests")

import httpx
import sqlalchemy as sa
from matrix_fakes import (
    ALICE,
    BOB,
    BOT,
    DIRECT,
    HOMESERVER,
    OUTSIDER,
    ROOM,
    STRANGER,
    TOKEN,
    Credentials,
    Homeserver,
    Listener,
    Workspace,
    asking,
    batch,
    mention,
    on_loop,
    question,
    reply,
    said,
    shared_file,
    tap,
    terminal_frame,
    text,
)
from ufo_ext_matrix.answering import Answering, read_answering
from ufo_ext_matrix.asking import Asking, read_asking, write_asking
from ufo_ext_matrix.client import MatrixClient, MatrixError
from ufo_ext_matrix.events import (
    POLL_START_TYPE,
    RoomFile,
    answer_txn_id,
    file_txn_id,
    part_txn_id,
    poll_txn_id,
    question_txn_id,
    room_key,
    say_txn_id,
    txn_id,
)
from ufo_ext_matrix.feedback import TYPING_TIMEOUT_MS, attend
from ufo_ext_matrix.messages import (
    EVENT_LIMIT_BYTES,
    PART_BUDGET_BYTES,
    reply_relation,
)
from ufo_ext_matrix.questions import LABELLED_HINT, ONE_HINT, question_block
from ufo_ext_matrix.since import read_since, write_since
from ufo_ext_matrix.surface import (
    CANCELLED_LINE,
    FAILED_LINE,
    FILES_LINE,
    HOMESERVER_SLOT,
    IDLE_SECONDS,
    REPORT_LINK_TEXT,
    ROSTER_LIMIT,
    TOKEN_SLOT,
    ConnectInput,
    FleetOwnershipLost,
    Installation,
    MatrixSurface,
    asked_questions,
    installations,
)

from ufo.runtime.turns.audience import SHARED_AUDIENCE
from ufo.sdk.audience import foreign_room_audience, room_audience
from ufo.sdk.hub import Activity
from ufo.sdk.surfaces import (
    AMBIENT_HISTORY_MESSAGES,
    NOTHING_DELIVERED,
    SILENCE_SENTINEL,
    MidTurnReply,
    SharedArtifact,
    SurfaceDeliveryError,
    SurfaceInstallationConflict,
    TerminalFrame,
    Writeback,
    member_message_attachments,
)

ROOM_MEMBERS = [BOT, ALICE, BOB]
INTERNAL = room_audience("matrix", room_key(ROOM))
FOREIGN = foreign_room_audience("matrix", room_key(ROOM))


def rig(server: Homeserver, workspace: Workspace) -> Installation:
    surface = MatrixSurface(transport=server.transport, bots="")
    return Installation(surface, Listener(workspace), BOT)  # type: ignore[arg-type]


async def primed(server: Homeserver, workspace: Workspace) -> Installation:
    """An installation past its first sync, which only fixes where the stream stands."""
    server.members = {ROOM: ROOM_MEMBERS, DIRECT: [BOT, ALICE]}
    server.syncs.setdefault(None, batch("s1", {ROOM: [mention("$old", ALICE, "before")]}))
    installation = rig(server, workspace)
    assert await installation.step() == 0.0
    return installation


def writeback(
    terminal: TerminalFrame,
    turn_id: UUID | None = None,
    artifacts: tuple[SharedArtifact, ...] = (),
) -> Writeback:
    return Writeback(
        turn_id=turn_id or uuid4(),
        conversation_id=uuid4(),
        agent_id=uuid4(),
        queue_key=ROOM,
        terminal=terminal,
        artifacts=artifacts,
        speaker_member_id=uuid4(),
    )


def shared(filename: str, media_type: str, subject: str | None = None) -> SharedArtifact:
    return SharedArtifact(
        id=uuid4(),
        blob_key=f"artifacts/{filename}",
        filename=filename,
        subject=subject,
        media_type=media_type,
        size_bytes=len(filename),
        role="file",
    )


def report(subject: str | None = None) -> SharedArtifact:
    return SharedArtifact(
        id=uuid4(),
        blob_key="artifacts/report.md",
        filename="report.md",
        subject=subject,
        media_type="text/markdown",
        size_bytes=9,
        role="details",
    )


ROLLOUT = asking("Rollout", question("Ship it?", "Ship it", "Hold"))
BOTH = asking(
    "Rollout", question("Ship it?", "Ship it", "Hold"), question("When?", "Today", "Tomorrow")
)
FRUIT = asking("Fruit", question("Which?", "Apples", "Pears", "Plums", multi_select=True))
ASKED = "$asked"
SAID = "$said"
OLDER = "$older"


async def open_question(
    server: Homeserver,
    workspace: Workspace,
    ask: object = ROLLOUT,
    target: UUID | None = None,
    recorded: bool = True,
) -> Installation:
    """An installation whose room holds one turn that ended in a question: an older line of the
    bot's, the member's message, the bot's reply, and the question as a message of its own, recorded
    as the one that turn asked in. Every bot message is heard the way every message is, so a reply
    to any of them is a reply to something the bot said."""
    workspace.question = ask.model_copy(update={"target_member_id": target})  # type: ignore[attr-defined]
    written = question_block(workspace.question.title, asked_questions(workspace.question))
    server.syncs["s1"] = batch(
        "s2",
        {
            ROOM: [
                text(OLDER, BOT, "Last week's numbers are in."),
                mention("$m1", ALICE, "should we ship?"),
                text(SAID, BOT, "Here is where the rollout stands."),
                text(ASKED, BOT, written),
            ]
        },
    )
    installation = await primed(server, workspace)
    assert await installation.step() == 0.0
    if recorded:
        turn = UUID(str(workspace.admitted[-1]["turn_id"]))
        await write_asking(workspace, turn, Asking(ROOM, ASKED))
    return installation


async def reported(installation: Installation) -> None:
    """Wait for the reporters this installation started, so what a room was shown has settled."""
    await asyncio.gather(*tuple(installation.attending))


async def answered(server: Homeserver, workspace: Workspace, event: dict) -> UUID:
    """The turn one room message founded, with the record of the message it answers beside it."""
    installation = await primed(server, workspace)
    server.syncs["s1"] = batch("s2", {ROOM: [event]})
    assert await installation.step() == 0.0
    return UUID(str(workspace.admitted[-1]["turn_id"]))


@on_loop
async def test_the_first_sync_fixes_the_stream_and_founds_nothing(workspace: Workspace) -> None:
    server = Homeserver()
    await primed(server, workspace)
    assert workspace.admitted == []
    assert await read_since(workspace, BOT) == "s1"
    assert server.since_asked() == [None]


@on_loop
async def test_a_restart_resumes_without_replaying(workspace: Workspace) -> None:
    server = Homeserver()
    server.syncs["s1"] = batch("s2", {ROOM: [mention("$m1", ALICE, "summarize the week")]})
    installation = await primed(server, workspace)
    await installation.step()
    assert [a["key"] for a in workspace.admitted] == ["$m1"]
    assert await read_since(workspace, BOT) == "s2"

    restarted = rig(server, workspace)
    await restarted.step()
    assert server.since_asked() == [None, "s1", "s2"]
    assert [a["key"] for a in workspace.admitted] == ["$m1"]


@on_loop
async def test_a_replayed_batch_is_admitted_once(workspace: Workspace) -> None:
    """A crash after admitting but before the since write replays the batch; the event id is the
    admission's idempotency key, so the replay joins the turn it already founded."""
    server = Homeserver()
    server.syncs["s1"] = batch("s2", {ROOM: [mention("$m1", ALICE, "again")]})
    installation = await primed(server, workspace)
    await installation.step()
    await write_since(workspace, BOT, "s1")
    await rig(server, workspace).step()
    assert server.since_asked() == [None, "s1", "s1"]
    assert len(workspace.admitted) == 1


@on_loop
async def test_the_bots_own_echo_founds_nothing(workspace: Workspace) -> None:
    server = Homeserver()
    server.syncs["s1"] = batch("s2", {ROOM: [mention("$echo", BOT, f"{BOT} said this")]})
    installation = await primed(server, workspace)
    await installation.step()
    assert workspace.admitted == []
    assert [h.line.own for h in installation.heard[ROOM]] == [False, True]


@pytest.mark.parametrize(
    "event",
    [
        {"type": "m.room.encrypted", "event_id": "$e", "sender": ALICE, "content": {}},
        {"type": "m.room.redaction", "event_id": "$r", "sender": ALICE, "redacts": "$m1"},
        mention("$edit", ALICE, "* fixed")
        | {
            "content": {
                "msgtype": "m.text",
                "body": "* fixed",
                "m.mentions": {"user_ids": [BOT]},
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$m1"},
            }
        },
    ],
    ids=["encrypted", "redaction", "edit"],
)
@on_loop
async def test_non_messages_found_nothing(workspace: Workspace, event: dict) -> None:
    server = Homeserver()
    server.syncs["s1"] = batch("s2", {ROOM: [event]})
    installation = await primed(server, workspace)
    await installation.step()
    assert workspace.admitted == []
    assert await read_since(workspace, BOT) == "s2"


@on_loop
async def test_an_unaddressed_line_in_a_room_with_no_conversation_asks_nothing(
    workspace: Workspace,
) -> None:
    server = Homeserver()
    server.syncs["s1"] = batch("s2", {ROOM: [text("$lunch", ALICE, "lunch?")]})
    installation = await primed(server, workspace)
    await installation.step()
    assert workspace.asked == []
    assert workspace.admitted == []


@on_loop
async def test_a_declined_ambient_line_founds_no_turn(workspace: Workspace) -> None:
    server = Homeserver()
    server.syncs["s1"] = batch(
        "s2", {ROOM: [mention("$m1", ALICE, "draft it"), text("$m2", BOB, "nice weather")]}
    )
    workspace.wanted = False
    installation = await primed(server, workspace)
    await installation.step()
    assert [a["key"] for a in workspace.admitted] == ["$m1"]
    [(asked, history)] = workspace.asked
    assert (asked.speaker, asked.text) == (BOB, "nice weather")
    assert [m.text for m in history] == ["before", "draft it"]


@on_loop
async def test_a_wanted_ambient_line_founds_a_turn_with_its_room_context(
    workspace: Workspace,
) -> None:
    server = Homeserver()
    server.syncs["s1"] = batch(
        "s2",
        {
            ROOM: [
                mention("$m1", ALICE, "draft it"),
                text("$m2", BOB, "the deadline moved"),
                text("$m3", ALICE, "can you redo it for friday"),
            ]
        },
    )
    installation = await primed(server, workspace)
    await installation.step()
    assert [a["key"] for a in workspace.admitted] == ["$m1", "$m2", "$m3"]
    body = workspace.admitted[2]["body"]
    assert "can you redo it for friday" in body
    assert "draft it" not in body.split("<member_message")[0]


@on_loop
async def test_a_direct_room_is_addressed_and_links_its_member(workspace: Workspace) -> None:
    alice = workspace.members["alice@example.org"]
    server = Homeserver()
    server.syncs["s1"] = batch("s2", {DIRECT: [text("$d1", ALICE, "what's on today")]})
    installation = await primed(server, workspace)
    await installation.step()
    [admitted] = workspace.admitted
    assert admitted["speaker"] == alice
    assert workspace.linked[ALICE] == alice
    assert workspace.conversations[DIRECT][1] == room_audience("matrix", room_key(DIRECT))
    assert workspace.asked == []


@on_loop
async def test_a_room_with_a_non_member_in_it_is_foreign(workspace: Workspace) -> None:
    server = Homeserver()
    server.syncs["s1"] = batch("s2", {ROOM: [mention("$m1", ALICE, "hi")]})
    server.syncs["s2"] = batch("s3", {ROOM: [mention("$m2", ALICE, "and again")]})
    installation = await primed(server, workspace)
    server.members[ROOM] = [*ROOM_MEMBERS, OUTSIDER]
    await installation.step()
    assert workspace.conversations[ROOM][1] == FOREIGN
    server.members[ROOM] = [*ROOM_MEMBERS, STRANGER]
    await installation.step()
    assert workspace.conversations[ROOM][1] == FOREIGN


@on_loop
async def test_a_stranger_on_the_bots_own_homeserver_makes_a_room_foreign(
    workspace: Workspace,
) -> None:
    server = Homeserver()
    server.syncs["s1"] = batch("s2", {ROOM: [mention("$m1", ALICE, "hi")]})
    installation = await primed(server, workspace)
    server.members[ROOM] = [*ROOM_MEMBERS, STRANGER]
    await installation.step()
    assert workspace.conversations[ROOM][1] == FOREIGN


@on_loop
async def test_a_workspace_with_no_domain_holds_only_foreign_rooms(workspace: Workspace) -> None:
    workspace.domain = None
    workspace.linked = {ALICE: uuid4(), BOB: uuid4()}
    server = Homeserver()
    server.syncs["s1"] = batch("s2", {ROOM: [mention("$m1", ALICE, "hi")]})
    installation = await primed(server, workspace)
    await installation.step()
    assert workspace.conversations[ROOM][1] == FOREIGN


@on_loop
async def test_a_room_past_the_roster_limit_is_foreign_without_asking(
    workspace: Workspace,
) -> None:
    crowd = [f"@m{n}:example.org" for n in range(ROSTER_LIMIT)]
    server = Homeserver()
    server.syncs["s1"] = batch("s2", {ROOM: [mention("$m1", ALICE, "hi")]})
    installation = await primed(server, workspace)
    server.members[ROOM] = [*ROOM_MEMBERS, *crowd]
    await installation.step()
    assert workspace.conversations[ROOM][1] == FOREIGN
    assert set(workspace.linked) == {ALICE}


@on_loop
async def test_a_rooms_audience_only_narrows_as_people_come_and_go(workspace: Workspace) -> None:
    """A direct room gains a member, then a non-member, who then leaves: the conversation stays
    one, goes foreign, and stays foreign — no step raises and every line is admitted."""
    server = Homeserver()
    server.syncs["s1"] = batch("s2", {DIRECT: [text("$d1", ALICE, "just us")]})
    server.syncs["s2"] = batch("s3", {DIRECT: [mention("$d2", ALICE, "bob is here")]})
    server.syncs["s3"] = batch("s4", {DIRECT: [mention("$d3", ALICE, "carol too")]})
    server.syncs["s4"] = batch("s5", {DIRECT: [mention("$d4", ALICE, "carol left")]})
    installation = await primed(server, workspace)
    key = room_key(DIRECT)
    seen = []
    for joined in ([BOT, ALICE], [BOT, ALICE, BOB], [BOT, ALICE, BOB, OUTSIDER], [BOT, ALICE, BOB]):
        server.members[DIRECT] = joined
        assert await installation.step() == 0.0
        seen.append(workspace.conversations[DIRECT][1])
    internal, foreign = room_audience("matrix", key), foreign_room_audience("matrix", key)
    assert seen == [internal, internal, foreign, foreign]
    assert [a["key"] for a in workspace.admitted] == ["$d1", "$d2", "$d3", "$d4"]
    assert len({a["conversation_id"] for a in workspace.admitted}) == 1


@on_loop
async def test_a_non_member_founds_nothing_and_costs_nothing(workspace: Workspace) -> None:
    server = Homeserver()
    server.syncs["s1"] = batch(
        "s2",
        {
            DIRECT: [text("$d1", STRANGER, "hello?")],
            ROOM: [mention("$m1", ALICE, "draft it"), text("$m2", OUTSIDER, "me too")],
        },
    )
    installation = await primed(server, workspace)
    server.members[DIRECT] = [BOT, STRANGER]
    await installation.step()
    assert [a["key"] for a in workspace.admitted] == ["$m1"]
    assert workspace.asked == []
    assert STRANGER not in workspace.linked
    assert await read_since(workspace, BOT) == "s2"


@on_loop
async def test_a_sender_off_the_workspace_domain_founds_nothing(workspace: Workspace) -> None:
    workspace.domain = "another.test"
    server = Homeserver()
    server.syncs["s1"] = batch("s2", {ROOM: [mention("$m1", ALICE, "hi")]})
    installation = await primed(server, workspace)
    await installation.step()
    assert workspace.admitted == []
    assert workspace.conversations == {}


@on_loop
async def test_a_failing_event_is_skipped_and_the_stream_moves_on(
    workspace: Workspace, caplog: pytest.LogCaptureFixture
) -> None:
    workspace.broken = {"$bad"}
    server = Homeserver()
    server.syncs["s1"] = batch(
        "s2", {ROOM: [mention("$bad", ALICE, "boom"), mention("$ok", BOB, "fine")]}
    )
    installation = await primed(server, workspace)
    with caplog.at_level(logging.DEBUG):
        assert await installation.step() == 0.0
    assert [a["key"] for a in workspace.admitted] == ["$ok"]
    assert await read_since(workspace, BOT) == "s2"
    assert "matrix.message_skipped" in caplog.text
    assert "boom" not in caplog.text


async def _read_again_rather_than_skipped(workspace: Workspace, error: BaseException) -> None:
    """One database failure during admission: nothing admitted, and the position where it was."""
    workspace.lost = {"$gone": error}
    server = Homeserver()
    server.syncs["s1"] = batch("s2", {ROOM: [mention("$gone", ALICE, "hi")]})
    installation = await primed(server, workspace)
    before = await read_since(workspace, BOT)
    with pytest.raises(sa.exc.SQLAlchemyError):
        await installation.step()
    assert workspace.admitted == []
    assert await read_since(workspace, BOT) == before


@on_loop
async def test_a_dropped_connection_is_read_again_rather_than_skipped(
    workspace: Workspace,
) -> None:
    """A dropped connection reaches `admit` as `InterfaceError`, which is a sibling of
    `DatabaseError` and so never an `OperationalError`."""
    await _read_again_rather_than_skipped(
        workspace, sa.exc.InterfaceError("admit", None, OSError("connection lost"))
    )


@on_loop
async def test_an_exhausted_pool_is_read_again_rather_than_skipped(workspace: Workspace) -> None:
    """An exhausted pool reaches `admit` as `TimeoutError`, which reaches `SQLAlchemyError` without
    passing through `DBAPIError` at all. A guard narrowed to connection shapes drops it."""
    await _read_again_rather_than_skipped(workspace, sa.exc.TimeoutError("pool exhausted"))


@on_loop
async def test_a_gap_in_the_timeline_is_filled_from_history(workspace: Workspace) -> None:
    """A room that saw more than a sync's worth of events comes back `limited`; the missing messages
    are paged back from `prev_batch` to where the stream stood, and admitted oldest first."""
    server = Homeserver()
    cut = batch("s2", {ROOM: [mention("$m3", ALICE, "third")]})
    cut["rooms"]["join"][ROOM]["timeline"] |= {"limited": True, "prev_batch": "p2"}
    server.syncs["s1"] = cut
    server.history[(ROOM, "p2")] = {
        "chunk": [mention("$m2", BOB, "second"), mention("$m1", ALICE, "first")],
        "end": "p1",
    }
    server.history[(ROOM, "p1")] = {"chunk": [mention("$m3", ALICE, "third")], "end": "p0"}
    installation = await primed(server, workspace)
    await installation.step()
    assert [a["key"] for a in workspace.admitted] == ["$m1", "$m2", "$m3"]
    [asked] = [r for r in server.requests if r.url.path.endswith("/messages")][:1]
    assert (asked.url.params["dir"], asked.url.params["to"]) == ("b", "s1")


@on_loop
async def test_an_invite_from_the_home_server_is_joined(workspace: Workspace) -> None:
    def invited(room: str, sender: str) -> dict:
        member = {
            "type": "m.room.member",
            "state_key": BOT,
            "sender": sender,
            "content": {"membership": "invite"},
        }
        return {"invite_state": {"events": [member]}}

    server = Homeserver()
    server.syncs[None] = {
        "next_batch": "s1",
        "rooms": {
            "invite": {
                "!mine:example.org": invited("!mine:example.org", ALICE),
                "!theirs:elsewhere.test": invited("!theirs:elsewhere.test", OUTSIDER),
            }
        },
    }
    await primed(server, workspace)
    assert server.joined == ["!mine:example.org"]


@on_loop
async def test_an_invite_from_a_non_member_is_left_standing(workspace: Workspace) -> None:
    member = {
        "type": "m.room.member",
        "state_key": BOT,
        "sender": STRANGER,
        "content": {"membership": "invite"},
    }
    server = Homeserver()
    server.syncs[None] = {
        "next_batch": "s1",
        "rooms": {"invite": {"!trap:example.org": {"invite_state": {"events": [member]}}}},
    }
    await primed(server, workspace)
    assert server.joined == []


@on_loop
async def test_a_room_the_bot_was_removed_from_does_not_stall_the_stream(
    workspace: Workspace,
) -> None:
    server = Homeserver()
    server.syncs["s1"] = batch(
        "s2", {ROOM: [mention("$gone", ALICE, "hi")], DIRECT: [text("$d1", ALICE, "still here")]}
    )
    installation = await primed(server, workspace)
    server.forbidden = {ROOM}
    await installation.step()
    assert [a["key"] for a in workspace.admitted] == ["$d1"]
    assert await read_since(workspace, BOT) == "s2"


@on_loop
async def test_an_unbound_bot_waits_without_calling_out(workspace: Workspace) -> None:
    server = Homeserver()
    installation = Installation(
        MatrixSurface(transport=server.transport, bots=""),
        Listener(workspace, bound=frozenset()),  # type: ignore[arg-type]
        BOT,
    )
    assert await installation.step() == IDLE_SECONDS
    assert server.requests == []


@on_loop
async def test_an_empty_slot_waits_without_calling_out(workspace: Workspace) -> None:
    del workspace.credentials[TOKEN_SLOT]
    server = Homeserver()
    assert await rig(server, workspace).step() == IDLE_SECONDS
    assert server.requests == []


@on_loop
async def test_the_token_reaches_no_log(
    workspace: Workspace, caplog: pytest.LogCaptureFixture
) -> None:
    server = Homeserver(failure=httpx.Response(500, json={"errcode": "M_UNKNOWN"}))
    installation = rig(server, workspace)
    naps: list[float] = []

    async def nap(seconds: float) -> None:
        naps.append(seconds)
        if len(naps) == 2:
            raise RuntimeError("stop")

    installation.surface.sleep = nap
    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError, match="stop"):
        await installation.run()
    assert naps == [1.0, 2.0]
    assert "matrix.sync_failed" in caplog.text
    assert TOKEN not in caplog.text
    assert TOKEN not in repr(installation.surface.client("https://hs.test", TOKEN))


@on_loop
async def test_one_broken_bot_backs_off_and_never_ends_the_listener(
    workspace: Workspace, caplog: pytest.LogCaptureFixture
) -> None:
    server = Homeserver()
    server.syncs[None] = {"rooms": {}}
    installation = rig(server, workspace)
    naps: list[float] = []

    async def nap(seconds: float) -> None:
        naps.append(seconds)
        if len(naps) == 3:
            raise asyncio.CancelledError

    installation.surface.sleep = nap
    with caplog.at_level(logging.DEBUG), pytest.raises(asyncio.CancelledError):
        await installation.run()
    assert naps == [1.0, 2.0, 5.0]
    assert caplog.text.count("matrix.sync_failed") == 3


@on_loop
async def test_losing_fleet_ownership_ends_the_stream(workspace: Workspace) -> None:
    server = Homeserver()
    installation = Installation(
        MatrixSurface(transport=server.transport, bots=""),
        Listener(workspace, owned=False),  # type: ignore[arg-type]
        BOT,
    )
    with pytest.raises(FleetOwnershipLost, match="fleet ownership"):
        await installation.run()
    assert server.requests == []


@on_loop
async def test_a_runtime_error_inside_a_round_backs_off_and_never_ends_the_listener(
    workspace: Workspace, caplog: pytest.LogCaptureFixture
) -> None:
    """`NotImplementedError` is a `RuntimeError`; raised past the listener gate it is one bot's
    failure, not lost ownership."""
    server = Homeserver()
    server.syncs[None] = batch("s1", {})
    installation = rig(server, workspace)

    async def unfinished(*_args: object) -> None:
        raise NotImplementedError

    installation.deliver = unfinished  # type: ignore[method-assign]
    naps: list[float] = []

    async def nap(seconds: float) -> None:
        naps.append(seconds)
        if len(naps) == 2:
            raise asyncio.CancelledError

    installation.surface.sleep = nap
    with caplog.at_level(logging.DEBUG), pytest.raises(asyncio.CancelledError):
        await installation.run()
    assert naps == [1.0, 2.0]
    assert caplog.text.count("matrix.sync_failed") == 2
    assert await read_since(workspace, BOT) is None


@on_loop
async def test_a_post_is_idempotent_under_its_turn(workspace: Workspace) -> None:
    server = Homeserver()
    surface = MatrixSurface(transport=server.transport, bots="")
    wb = writeback(TerminalFrame(status="done", text="The week in one line."))
    first = await surface.post(workspace, wb)  # type: ignore[arg-type]
    again = await surface.post(workspace, wb)  # type: ignore[arg-type]
    assert first == again == "$sent0"
    assert list(server.sent) == [txn_id(wb.turn_id)]
    assert server.sent[txn_id(wb.turn_id)]["body"] == "The week in one line."
    assert server.sent[txn_id(wb.turn_id)]["room"] == ROOM


@on_loop
async def test_silence_delivers_nothing(workspace: Workspace) -> None:
    server = Homeserver()
    surface = MatrixSurface(transport=server.transport, bots="")
    wb = writeback(TerminalFrame(status="done", text=SILENCE_SENTINEL))
    assert await surface.post(workspace, wb) is NOTHING_DELIVERED  # type: ignore[arg-type]
    assert server.requests == []


@on_loop
async def test_a_failed_turn_posts_the_surfaces_own_line(workspace: Workspace) -> None:
    server = Homeserver()
    surface = MatrixSurface(transport=server.transport, bots="")
    wb = writeback(TerminalFrame(status="failed", text="Traceback: secret internals"))
    await surface.post(workspace, wb)  # type: ignore[arg-type]
    assert server.sent[txn_id(wb.turn_id)]["body"] == FAILED_LINE


@pytest.mark.parametrize(
    ("text", "posted"),
    [
        ("I can only answer workspace members.", "I can only answer workspace members."),
        ("", CANCELLED_LINE),
    ],
    ids=["core-gave-a-reason", "stopped"],
)
@on_loop
async def test_a_cancelled_turn_posts_cores_reason(
    workspace: Workspace, text: str, posted: str
) -> None:
    server = Homeserver()
    surface = MatrixSurface(transport=server.transport, bots="")
    wb = writeback(TerminalFrame(status="cancelled", text=text))
    await surface.post(workspace, wb)  # type: ignore[arg-type]
    assert server.sent[txn_id(wb.turn_id)]["body"] == posted


@on_loop
async def test_a_rate_limit_carries_the_homeservers_wait(workspace: Workspace) -> None:
    server = Homeserver(
        failure=httpx.Response(429, json={"errcode": "M_LIMIT_EXCEEDED", "retry_after_ms": 2500})
    )
    surface = MatrixSurface(transport=server.transport, bots="")
    with pytest.raises(SurfaceDeliveryError) as raised:
        await surface.post(workspace, writeback(TerminalFrame(status="done", text="hi")))  # type: ignore[arg-type]
    assert raised.value.retry_after_seconds == 3
    assert TOKEN not in str(raised.value)


def test_the_deploy_names_its_bots_once_each() -> None:
    assert installations(f"{BOT}, @two:example.org {BOT}") == (BOT, "@two:example.org")
    assert installations("") == ()


@dataclass
class Installations:
    """Core's writer as a tool reaches it: a bind lands unless another workspace holds that
    installation, and binding a bot this workspace already holds replaces what it held."""

    bound: list[tuple[str, str]] = field(default_factory=list)
    taken: bool = False

    async def bind(self, surface: str, installation_id: str) -> None:
        if self.taken:
            raise SurfaceInstallationConflict(surface)
        pair = (surface, installation_id)
        if pair not in self.bound:
            self.bound.append(pair)


@dataclass
class Ext:
    credentials: Credentials
    installations: Installations = field(default_factory=Installations)


@dataclass
class Tool:
    ext: Ext


def connecting(values: dict[str, str], *, taken: bool = False) -> Tool:
    return Tool(Ext(Credentials(values), Installations(taken=taken)))


def test_connect_binds_the_bot_the_token_belongs_to() -> None:
    server = Homeserver()
    surface = MatrixSurface(transport=server.transport, bots=BOT)
    tool = connecting({HOMESERVER_SLOT: "https://matrix.example.org", TOKEN_SLOT: TOKEN})
    result = asyncio.run(surface.connect(tool, ConnectInput()))  # type: ignore[arg-type]
    assert not result.is_error
    assert tool.ext.installations.bound == [("matrix", BOT)]
    assert said(result) == f"Connected {BOT}. It is listening."


def test_connect_names_the_empty_slot_and_binds_nothing() -> None:
    server = Homeserver()
    surface = MatrixSurface(transport=server.transport, bots="")
    tool = connecting({HOMESERVER_SLOT: "https://matrix.example.org"})
    result = asyncio.run(surface.connect(tool, ConnectInput()))  # type: ignore[arg-type]
    assert TOKEN_SLOT in said(result)
    assert tool.ext.installations.bound == []
    assert server.requests == []


def test_connect_refuses_a_bad_token_without_repeating_it() -> None:
    server = Homeserver()
    surface = MatrixSurface(transport=server.transport, bots="")
    wrong = "syt_not_the_token"
    tool = connecting({HOMESERVER_SLOT: "https://matrix.example.org", TOKEN_SLOT: wrong})
    result = asyncio.run(surface.connect(tool, ConnectInput()))  # type: ignore[arg-type]
    assert result.is_error
    assert wrong not in said(result)
    assert tool.ext.installations.bound == []


def test_connect_refuses_a_bot_another_workspace_holds() -> None:
    """One sentence, naming the bot and saying where it does not belong. Which workspace holds it is
    not this workspace's to know, so the refusal says nothing that would identify one."""
    server = Homeserver()
    surface = MatrixSurface(transport=server.transport, bots="")
    tool = connecting(
        {HOMESERVER_SLOT: "https://matrix.example.org", TOKEN_SLOT: TOKEN}, taken=True
    )
    result = asyncio.run(surface.connect(tool, ConnectInput()))  # type: ignore[arg-type]
    assert result.is_error
    answer = said(result)
    assert answer == f"{BOT} is already connected to another workspace."
    assert tool.ext.installations.bound == []


def test_connect_again_in_the_same_workspace_replaces_the_binding() -> None:
    """A rotated token for the same bot is the same installation, so a second connect lands rather
    than conflicting with what this workspace already holds."""
    server = Homeserver()
    surface = MatrixSurface(transport=server.transport, bots=BOT)
    tool = connecting({HOMESERVER_SLOT: "https://matrix.example.org", TOKEN_SLOT: TOKEN})
    first = asyncio.run(surface.connect(tool, ConnectInput()))  # type: ignore[arg-type]
    second = asyncio.run(surface.connect(tool, ConnectInput()))  # type: ignore[arg-type]
    assert not first.is_error and not second.is_error
    assert tool.ext.installations.bound == [("matrix", BOT)]


def test_no_connect_answer_carries_the_token(caplog: pytest.LogCaptureFixture) -> None:
    """The admin fills the token through `request_credentials`, so no answer may put it back into the
    transcript — not the one that binds, and none of the ones that refuse."""
    slots = {HOMESERVER_SLOT: "https://matrix.example.org", TOKEN_SLOT: TOKEN}
    server = Homeserver()
    refusing = Homeserver(failure=httpx.Response(500, json={"errcode": "M_UNKNOWN"}))
    answers = []
    with caplog.at_level(logging.DEBUG):
        for homeserver, tool in (
            (server, connecting(slots)),
            (server, connecting(slots, taken=True)),
            (refusing, connecting(slots)),
            (server, connecting({HOMESERVER_SLOT: "https://matrix.example.org"})),
        ):
            surface = MatrixSurface(transport=homeserver.transport, bots="")
            answers.append(asyncio.run(surface.connect(tool, ConnectInput())))  # type: ignore[arg-type]
    assert [answer.is_error for answer in answers] == [False, True, True, False]
    for answer in answers:
        assert TOKEN not in said(answer)
    assert TOKEN not in caplog.text


@on_loop
async def test_a_reply_renders_as_html_under_the_message_it_answers(workspace: Workspace) -> None:
    server = Homeserver()
    turn = await answered(server, workspace, mention("$m1", ALICE, "summarize the week"))
    surface = MatrixSurface(transport=server.transport, bots="")
    wb = writeback(TerminalFrame(status="done", text="**one** line"), turn)
    assert await surface.post(workspace, wb) == "$sent0"  # type: ignore[arg-type]
    sent = server.sent[txn_id(turn)]
    assert sent["body"] == "**one** line"
    assert sent["format"] == "org.matrix.custom.html"
    assert sent["formatted_body"] == "<p><strong>one</strong> line</p>"
    assert sent["m.relates_to"] == {"m.in_reply_to": {"event_id": "$m1"}}


@on_loop
async def test_a_reply_to_a_threaded_message_hangs_under_its_root(workspace: Workspace) -> None:
    server = Homeserver()
    threaded = mention("$m1", ALICE, "and the numbers?")
    threaded["content"]["m.relates_to"] = {"rel_type": "m.thread", "event_id": "$root"}
    turn = await answered(server, workspace, threaded)
    assert await read_answering(workspace, turn) == Answering(ROOM, "$m1", "$root")  # type: ignore[arg-type]
    surface = MatrixSurface(transport=server.transport, bots="")
    await surface.post(workspace, writeback(TerminalFrame(status="done", text="here"), turn))  # type: ignore[arg-type]
    assert server.sent[txn_id(turn)]["m.relates_to"] == reply_relation("$m1", "$root")


@on_loop
async def test_a_turn_no_message_founded_relates_to_nothing(workspace: Workspace) -> None:
    server = Homeserver()
    surface = MatrixSurface(transport=server.transport, bots="")
    wb = writeback(TerminalFrame(status="done", text="the scheduled brief"))
    await surface.post(workspace, wb)  # type: ignore[arg-type]
    assert "m.relates_to" not in server.sent[txn_id(wb.turn_id)]


@on_loop
async def test_a_long_reply_is_written_in_parts_under_one_reference(workspace: Workspace) -> None:
    server = Homeserver()
    turn = await answered(server, workspace, mention("$m1", ALICE, "the whole report please"))
    surface = MatrixSurface(transport=server.transport, bots="")
    paragraph = "word " * 200
    long_reply = "\n\n".join(f"{n}. {paragraph}" for n in range(8))
    wb = writeback(TerminalFrame(status="done", text=long_reply), turn)
    assert await surface.post(workspace, wb) == "$sent0"  # type: ignore[arg-type]
    written = [txn for txn in server.sent if txn.startswith(txn_id(turn))]
    assert len(written) > 1
    assert written[0] == txn_id(turn) and written[1] == part_txn_id(txn_id(turn), 2)
    for txn in written:
        assert len(json.dumps(server.sent[txn]).encode()) < EVENT_LIMIT_BYTES
        assert len(server.sent[txn]["body"].encode()) <= PART_BUDGET_BYTES
        assert server.sent[txn]["m.relates_to"] == {"m.in_reply_to": {"event_id": "$m1"}}
    assert sum(server.sent[txn]["body"].count("word") for txn in written) == 1600


@on_loop
async def test_the_detailed_report_is_a_link_and_is_never_uploaded(workspace: Workspace) -> None:
    server = Homeserver()
    surface = MatrixSurface(transport=server.transport, bots="")
    wb = writeback(
        TerminalFrame(status="done", text="Short answer."), artifacts=(report("the write-up"),)
    )
    # Only the shared audience — or a member's own — earns a portal link, so a room conversation
    # seeded with one reads the way core's audience gate lets it read.
    workspace.conversations[ROOM] = (wb.conversation_id, SHARED_AUDIENCE)
    reply_ref = await surface.post(workspace, wb)  # type: ignore[arg-type]
    await surface.attach(workspace, wb, str(reply_ref))  # type: ignore[arg-type]
    body = server.sent[txn_id(wb.turn_id)]["body"]
    url = f"https://ufo.example.org/surface/web#/c/{wb.conversation_id}?report={wb.artifacts[0].id}"
    assert body == f"Short answer.\n\n[the write-up]({url})"
    assert FILES_LINE not in body
    assert server.uploaded == []
    assert list(server.sent) == [txn_id(wb.turn_id)]


@on_loop
async def test_a_report_the_turn_named_nothing_reads_as_the_surfaces_own_words(
    workspace: Workspace,
) -> None:
    server = Homeserver()
    surface = MatrixSurface(transport=server.transport, bots="")
    wb = writeback(TerminalFrame(status="done", text="Short answer."), artifacts=(report(),))
    workspace.conversations[ROOM] = (wb.conversation_id, SHARED_AUDIENCE)
    await surface.post(workspace, wb)  # type: ignore[arg-type]
    assert f"[{REPORT_LINK_TEXT}](" in server.sent[txn_id(wb.turn_id)]["body"]


@on_loop
async def test_a_details_report_the_portal_shows_nobody_points_at_the_workspace(
    workspace: Workspace,
) -> None:
    """A room audience is neither the shared one nor a member's own, so core's gate offers no
    link: the write-up then reads as the workspace pointer rather than vanishing — a turn whose
    words were silence must never post an empty event."""
    server = Homeserver()
    surface = MatrixSurface(transport=server.transport, bots="")
    wb = writeback(TerminalFrame(status="done", text=SILENCE_SENTINEL), artifacts=(report(),))
    workspace.conversations[ROOM] = (wb.conversation_id, room_audience("matrix", room_key(ROOM)))
    await surface.post(workspace, wb)  # type: ignore[arg-type]
    body = server.sent[txn_id(wb.turn_id)]["body"]
    assert f"[{REPORT_LINK_TEXT}](" not in body
    assert body == f"{FILES_LINE}: https://ufo.example.org/surface/web"


@on_loop
async def test_a_deploy_with_no_portal_offers_no_report_link(workspace: Workspace) -> None:
    server = Homeserver()
    workspace.portal = False
    surface = MatrixSurface(transport=server.transport, bots="")
    wb = writeback(TerminalFrame(status="done", text="Short answer."), artifacts=(report("here"),))
    workspace.conversations[ROOM] = (wb.conversation_id, SHARED_AUDIENCE)
    await surface.post(workspace, wb)  # type: ignore[arg-type]
    assert server.sent[txn_id(wb.turn_id)]["body"] == (
        f"Short answer.\n\n{FILES_LINE}: https://ufo.example.org/surface/web"
    )


@on_loop
async def test_shared_files_follow_the_reply_as_what_they_are(workspace: Workspace) -> None:
    server = Homeserver()
    turn = await answered(server, workspace, mention("$m1", ALICE, "chart it"))
    chart = shared("chart.png", "image/png", "Last week")
    notes = shared("notes.md", "text/markdown")
    workspace.blob.objects = {chart.blob_key: b"\x89PNG", notes.blob_key: b"# notes"}
    wb = writeback(TerminalFrame(status="done", text="Two files."), turn, (chart, notes))
    surface = MatrixSurface(transport=server.transport, bots="")
    reply_ref = await surface.post(workspace, wb)  # type: ignore[arg-type]
    await surface.attach(workspace, wb, str(reply_ref))  # type: ignore[arg-type]
    assert server.uploaded == [("chart.png", b"\x89PNG"), ("notes.md", b"# notes")]
    picture = server.sent[file_txn_id(turn, chart.id)]
    assert picture["msgtype"] == "m.image"
    assert picture["body"] == "Last week" and picture["filename"] == "chart.png"
    assert picture["url"] == "mxc://example.org/chartpng"
    assert picture["m.relates_to"] == reply_relation(str(reply_ref), None)
    assert server.sent[file_txn_id(turn, notes.id)]["msgtype"] == "m.file"
    assert FILES_LINE in server.sent[txn_id(turn)]["body"]


@on_loop
async def test_one_refused_upload_leaves_its_siblings_delivered(workspace: Workspace) -> None:
    server = Homeserver(refused_uploads={"huge.bin"})
    huge = shared("huge.bin", "application/octet-stream")
    small = shared("small.txt", "text/plain")
    gone = shared("gone.txt", "text/plain")
    workspace.blob.objects = {huge.blob_key: b"x" * 9, small.blob_key: b"ok"}
    wb = writeback(TerminalFrame(status="done", text="Three files."), artifacts=(huge, small, gone))
    surface = MatrixSurface(transport=server.transport, bots="")
    reply_ref = await surface.post(workspace, wb)  # type: ignore[arg-type]
    await surface.attach(workspace, wb, str(reply_ref))  # type: ignore[arg-type]
    assert server.uploaded == [("small.txt", b"ok")]
    assert file_txn_id(wb.turn_id, small.id) in server.sent
    assert file_txn_id(wb.turn_id, huge.id) not in server.sent
    assert file_txn_id(wb.turn_id, gone.id) not in server.sent


@on_loop
async def test_a_rate_limited_upload_raises_so_the_file_is_not_discarded(
    workspace: Workspace,
) -> None:
    """A refusal the homeserver will take back is not a file gone for good: it raises the delivery
    error core retries on, carrying the wait the homeserver asked for."""
    server = Homeserver(limited_uploads={"slow.png"})
    slow = shared("slow.png", "image/png")
    workspace.blob.objects = {slow.blob_key: b"png"}
    wb = writeback(TerminalFrame(status="done", text="One file."), artifacts=(slow,))
    surface = MatrixSurface(transport=server.transport, bots="")
    reply_ref = await surface.post(workspace, wb)  # type: ignore[arg-type]
    with pytest.raises(SurfaceDeliveryError) as raised:
        await surface.attach(workspace, wb, str(reply_ref))  # type: ignore[arg-type]
    assert raised.value.retry_after_seconds == 2
    assert server.uploaded == []
    assert list(server.sent) == [txn_id(wb.turn_id)]


@on_loop
async def test_a_failed_answering_record_does_not_unadmit_the_message(
    workspace: Workspace, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The turn stands when its answering row fails to write: the line is not fed back as room
    context for a turn it founded, and the stream moves on."""

    async def unrecorded(ctx: object, turn_id: object, answering: object) -> None:
        raise ValueError("answering table unavailable")

    monkeypatch.setattr("ufo_ext_matrix.surface.write_answering", unrecorded)
    server = Homeserver()
    server.syncs["s1"] = batch("s2", {ROOM: [mention("$m1", ALICE, "hi")]})
    installation = await primed(server, workspace)
    with caplog.at_level(logging.DEBUG):
        await installation.step()
    assert [a["key"] for a in workspace.admitted] == ["$m1"]
    assert "matrix.answering_unrecorded" in caplog.text
    assert await read_since(workspace, BOT) == "s2"


@on_loop
async def test_a_recovered_delivery_re_attaches_without_re_posting(workspace: Workspace) -> None:
    """Core records the reply before `attach` runs, so recovery repeats `attach` alone. Each file is
    sent under the transaction id it was sent under before, so the room holds it once."""
    server = Homeserver()
    notes = shared("notes.md", "text/markdown")
    workspace.blob.objects = {notes.blob_key: b"# notes"}
    wb = writeback(TerminalFrame(status="done", text="One file."), artifacts=(notes,))
    surface = MatrixSurface(transport=server.transport, bots="")
    reply_ref = await surface.post(workspace, wb)  # type: ignore[arg-type]
    await surface.attach(workspace, wb, str(reply_ref))  # type: ignore[arg-type]
    await surface.attach(workspace, wb, str(reply_ref))  # type: ignore[arg-type]
    assert len(server.uploaded) == 2
    assert list(server.sent) == [txn_id(wb.turn_id), file_txn_id(wb.turn_id, notes.id)]


@on_loop
async def test_a_turn_that_shared_nothing_uploads_nothing(workspace: Workspace) -> None:
    server = Homeserver()
    surface = MatrixSurface(transport=server.transport, bots="")
    wb = writeback(TerminalFrame(status="done", text="Nothing shared."))
    await surface.attach(workspace, wb, "$sent0")  # type: ignore[arg-type]
    assert server.requests == []


@on_loop
async def test_a_mid_turn_reply_posts_once_under_the_row_it_came_from(workspace: Workspace) -> None:
    server = Homeserver()
    turn = await answered(server, workspace, mention("$m1", ALICE, "keep me posted"))
    surface = MatrixSurface(transport=server.transport, bots="")
    said = MidTurnReply(
        id=uuid4(),
        turn_id=turn,
        conversation_id=uuid4(),
        agent_id=uuid4(),
        queue_key=ROOM,
        message_ref=None,
        text="Halfway through.",
    )
    first = await surface.speak(workspace, said)  # type: ignore[arg-type]
    again = await surface.speak(workspace, said)  # type: ignore[arg-type]
    assert first == again
    assert list(server.sent) == [say_txn_id(said.id)]
    sent = server.sent[say_txn_id(said.id)]
    assert sent["msgtype"] == "m.text"
    assert sent["formatted_body"] == "<p>Halfway through.</p>"
    assert sent["m.relates_to"] == {"m.in_reply_to": {"event_id": "$m1"}}


@on_loop
async def test_a_comment_from_another_surface_reads_as_the_rooms_own_notice(
    workspace: Workspace,
) -> None:
    server = Homeserver()
    surface = MatrixSurface(transport=server.transport, bots="")
    said = MidTurnReply(
        id=uuid4(),
        turn_id=uuid4(),
        conversation_id=uuid4(),
        agent_id=uuid4(),
        queue_key=ROOM,
        message_ref=None,
        text="Alice commented in the workspace.",
        is_comment=True,
    )
    await surface.speak(workspace, said)  # type: ignore[arg-type]
    assert server.sent[say_txn_id(said.id)]["msgtype"] == "m.notice"


@on_loop
async def test_a_numbered_reply_answers_the_question_and_marks_what_landed(
    workspace: Workspace,
) -> None:
    server = Homeserver()
    installation = await open_question(server, workspace)
    server.syncs["s2"] = batch("s3", {ROOM: [reply("$a1", ALICE, "1", ASKED)]})
    await installation.step()
    assert [a["key"] for a in workspace.admitted] == ["$m1", "$a1"]
    assert "Ship it" in workspace.admitted[-1]["body"]
    assert workspace.admitted[-1]["speaker"] == workspace.members["alice@example.org"]
    [edit] = server.edits()
    assert edit["m.relates_to"]["event_id"] == ASKED
    assert "Ship it \u2713" in edit["m.new_content"]["body"]
    assert ONE_HINT not in edit["m.new_content"]["body"]


@on_loop
async def test_one_reply_answers_every_question_of_an_ask(workspace: Workspace) -> None:
    server = Homeserver()
    installation = await open_question(server, workspace, BOTH)
    server.syncs["s2"] = batch("s3", {ROOM: [reply("$a1", ALICE, "1a 2b", ASKED)]})
    await installation.step()
    body = workspace.admitted[-1]["body"]
    assert "Ship it?: Ship it" in body and "When?: Tomorrow" in body


@on_loop
async def test_one_reply_takes_several_choices_where_the_question_does(
    workspace: Workspace,
) -> None:
    server = Homeserver()
    installation = await open_question(server, workspace, FRUIT)
    server.syncs["s2"] = batch("s3", {ROOM: [reply("$a1", ALICE, "1, 3", ASKED)]})
    await installation.step()
    assert "Apples, Plums" in workspace.admitted[-1]["body"]


@on_loop
async def test_a_tap_on_a_poll_answers_the_question_it_carries(workspace: Workspace) -> None:
    server = Homeserver()
    installation = await open_question(server, workspace)
    server.syncs["s2"] = batch("s3", {ROOM: [tap("$tap", ALICE, "$poll", "2")]})
    await installation.step()
    assert [a["key"] for a in workspace.admitted] == ["$m1", "$tap"]
    assert "Hold" in workspace.admitted[-1]["body"]
    [edit] = server.edits()
    assert edit["m.relates_to"]["event_id"] == ASKED
    assert "Hold \u2713" in edit["m.new_content"]["body"]


@on_loop
async def test_a_tap_on_somebody_elses_poll_answers_nothing(workspace: Workspace) -> None:
    server = Homeserver()
    installation = await open_question(server, workspace)
    server.syncs["s2"] = batch("s3", {ROOM: [tap("$tap", ALICE, "$poll", "a1b2c3")]})
    await installation.step()
    assert [a["key"] for a in workspace.admitted] == ["$m1"]


@on_loop
async def test_a_question_put_to_one_member_is_not_answered_by_another(
    workspace: Workspace,
) -> None:
    """The refusal is `answerable_question`'s. Their words are still theirs, so the reply is
    admitted as the words it is, and the question keeps waiting for the member it names."""
    server = Homeserver()
    bob = workspace.members["bob@example.org"]
    installation = await open_question(server, workspace, target=bob)
    server.syncs["s2"] = batch("s3", {ROOM: [reply("$a1", ALICE, "1", ASKED)]})
    await installation.step()
    assert [index for index, _member in workspace.refused] == [0]
    assert server.edits() == []
    assert workspace.admitted[-1]["key"] == "$a1"
    assert "Ship it" not in workspace.admitted[-1]["body"]


@on_loop
async def test_a_question_already_answered_leaves_a_number_as_words(workspace: Workspace) -> None:
    server = Homeserver()
    installation = await open_question(server, workspace)
    workspace.open_questions = frozenset()
    server.syncs["s2"] = batch("s3", {ROOM: [reply("$a1", ALICE, "1", ASKED)]})
    await installation.step()
    assert server.edits() == []
    assert "Ship it" not in workspace.admitted[-1]["body"]


@on_loop
async def test_only_the_questions_left_open_are_answered(workspace: Workspace) -> None:
    server = Homeserver()
    installation = await open_question(server, workspace, BOTH)
    workspace.open_questions = frozenset({0})
    server.syncs["s2"] = batch("s3", {ROOM: [reply("$a1", ALICE, "1a 2b", ASKED)]})
    await installation.step()
    body = workspace.admitted[-1]["body"]
    assert "Ship it?: Ship it" in body and "When?" not in body
    assert [index for index, _member in workspace.refused] == [1]


@on_loop
async def test_a_redelivered_answer_lands_once_and_rewrites_the_question_once(
    workspace: Workspace,
) -> None:
    """A crash after the answer admitted but before the position was stored reads the batch again:
    the event id is the admission key, and the rewrite's transaction id is that event's."""
    server = Homeserver()
    installation = await open_question(server, workspace)
    server.syncs["s2"] = batch("s3", {ROOM: [reply("$a1", ALICE, "1", ASKED)]})
    await installation.step()
    await write_since(workspace, BOT, "s2")
    await installation.step()
    assert [a["key"] for a in workspace.admitted] == ["$m1", "$a1"]
    assert len(server.edits()) == 1
    assert answer_txn_id("$a1") in server.sent


@on_loop
async def test_words_keep_answering_a_question(workspace: Workspace) -> None:
    server = Homeserver()
    installation = await open_question(server, workspace)
    server.syncs["s2"] = batch("s3", {ROOM: [reply("$a1", ALICE, "hold off until friday", ASKED)]})
    await installation.step()
    assert "hold off until friday" in workspace.admitted[-1]["body"]
    assert server.edits() == []


@on_loop
async def test_an_answer_replying_to_a_member_marks_the_question(workspace: Workspace) -> None:
    server = Homeserver()
    installation = await open_question(server, workspace)
    server.syncs["s2"] = batch("s3", {ROOM: [reply("$a1", ALICE, "1", "$m1")]})
    await installation.step()
    assert "Ship it" in workspace.admitted[-1]["body"]
    assert [edit["m.relates_to"]["event_id"] for edit in server.edits()] == [ASKED]


@on_loop
async def test_an_answer_that_replies_to_nothing_marks_the_question(workspace: Workspace) -> None:
    server = Homeserver()
    installation = await open_question(server, workspace)
    server.syncs["s2"] = batch("s3", {ROOM: [text("$a1", ALICE, "2")]})
    await installation.step()
    assert "Hold" in workspace.admitted[-1]["body"]
    assert [edit["m.relates_to"]["event_id"] for edit in server.edits()] == [ASKED]


async def answered_under(workspace: Workspace, replied: str) -> list[str]:
    """The messages rewritten when a member answers `1` as a reply to `replied`."""
    server = Homeserver()
    installation = await open_question(server, workspace)
    server.syncs["s2"] = batch("s3", {ROOM: [reply("$a1", ALICE, "1", replied)]})
    await installation.step()
    assert "Ship it" in workspace.admitted[-1]["body"]
    return [edit["m.relates_to"]["event_id"] for edit in server.edits()]


@on_loop
async def test_an_answer_replying_to_an_older_bot_message_leaves_it_as_it_was(
    workspace: Workspace,
) -> None:
    """The bot's other lines are words the room still reads, so an answer sent as a reply to one
    answers the question and rewrites the question alone."""
    assert await answered_under(workspace, OLDER) == [ASKED]


@on_loop
async def test_an_answer_replying_to_the_reply_leaves_the_reply_as_it_was(
    workspace: Workspace,
) -> None:
    assert await answered_under(workspace, SAID) == [ASKED]


@on_loop
async def test_a_question_no_message_was_recorded_for_is_answered_and_left_unmarked(
    workspace: Workspace,
) -> None:
    server = Homeserver()
    installation = await open_question(server, workspace, recorded=False)
    server.syncs["s2"] = batch("s3", {ROOM: [reply("$a1", ALICE, "1", ASKED)]})
    await installation.step()
    assert "Ship it" in workspace.admitted[-1]["body"]
    assert server.edits() == []


@on_loop
async def test_the_reply_a_question_came_with_survives_its_answer(workspace: Workspace) -> None:
    """End to end: `post` sends the reply and the question apart and records the question, the bot
    hears both, and a member answering by replying to the reply rewrites the question alone."""
    server = Homeserver()
    workspace.question = ROLLOUT
    installation = await primed(server, workspace)
    server.syncs["s1"] = batch("s2", {ROOM: [mention("$m1", ALICE, "should we ship?")]})
    await installation.step()
    turn = UUID(str(workspace.admitted[-1]["turn_id"]))
    wb = writeback(
        TerminalFrame(status="done", text="Here is where the rollout stands.", question=ROLLOUT),
        turn_id=turn,
    )
    reference = await installation.surface.post(workspace, wb)  # type: ignore[arg-type]
    [said] = server.sent_under(txn_id(turn))
    [asked] = server.sent_under(question_txn_id(turn))
    assert reference == said["event_id"] != asked["event_id"]
    assert said["body"] == "Here is where the rollout stands."
    heard = [
        text(said["event_id"], BOT, said["body"]),
        text(asked["event_id"], BOT, asked["body"]),
        reply("$a1", ALICE, "1", said["event_id"]),
    ]
    server.syncs["s2"] = batch("s3", {ROOM: heard})
    await installation.step()
    [edit] = server.edits()
    assert edit["m.relates_to"]["event_id"] == asked["event_id"]
    assert "Ship it \u2713" in edit["m.new_content"]["body"]
    assert "rollout stands" not in edit["m.new_content"]["body"]
    assert server.sent_under(txn_id(turn)) == [said]


@on_loop
async def test_a_homeserver_that_refuses_the_rewrite_keeps_the_answer(
    workspace: Workspace,
) -> None:
    server = Homeserver()
    installation = await open_question(server, workspace)
    server.refused_sends = {"m.room.message"}
    server.syncs["s2"] = batch("s3", {ROOM: [reply("$a1", ALICE, "1", ASKED)]})
    await installation.step()
    assert "Ship it" in workspace.admitted[-1]["body"]
    assert server.edits() == []


@on_loop
async def test_a_question_reaches_the_room_as_a_numbered_list_and_a_poll(
    workspace: Workspace,
) -> None:
    server = Homeserver()
    surface = MatrixSurface(transport=server.transport, bots="")
    wb = writeback(TerminalFrame(status="done", text="Ready.", question=ROLLOUT))
    reference = await surface.post(workspace, wb)  # type: ignore[arg-type]
    [said] = server.sent_under(txn_id(wb.turn_id))
    assert reference == said["event_id"] and said["body"] == "Ready."
    [message] = server.sent_under(question_txn_id(wb.turn_id))
    recorded = await read_asking(workspace, wb.turn_id)  # type: ignore[arg-type]
    assert recorded == Asking(ROOM, message["event_id"])
    assert message["body"].splitlines() == [
        "Rollout",
        "",
        "Ship it?",
        "1. Ship it",
        "2. Hold",
        "",
        ONE_HINT,
    ]
    [poll] = server.sent_under(poll_txn_id(wb.turn_id))
    assert poll["type"] == POLL_START_TYPE
    assert [answer["m.id"] for answer in poll["m.poll"]["answers"]] == ["1", "2"]


@on_loop
async def test_an_ask_of_several_questions_reaches_the_room_without_a_poll(
    workspace: Workspace,
) -> None:
    server = Homeserver()
    surface = MatrixSurface(transport=server.transport, bots="")
    wb = writeback(TerminalFrame(status="done", question=BOTH))
    reference = await surface.post(workspace, wb)  # type: ignore[arg-type]
    assert server.sent_under(txn_id(wb.turn_id)) == []
    [message] = server.sent_under(question_txn_id(wb.turn_id))
    assert reference == message["event_id"]
    assert "1a. Ship it" in message["body"] and message["body"].endswith(LABELLED_HINT)
    assert server.sent_under(poll_txn_id(wb.turn_id)) == []


@on_loop
async def test_a_failed_turn_asks_nothing(workspace: Workspace) -> None:
    server = Homeserver()
    surface = MatrixSurface(transport=server.transport, bots="")
    wb = writeback(TerminalFrame(status="failed", question=ROLLOUT))
    await surface.post(workspace, wb)  # type: ignore[arg-type]
    assert [m["body"] for m in server.sent_under(txn_id(wb.turn_id))] == [FAILED_LINE]
    assert server.sent_under(question_txn_id(wb.turn_id)) == []
    assert server.sent_under(poll_txn_id(wb.turn_id)) == []
    assert await read_asking(workspace, wb.turn_id) is None  # type: ignore[arg-type]


@on_loop
async def test_a_running_turn_is_read_and_typed_at_until_it_ends(workspace: Workspace) -> None:
    server = Homeserver()
    workspace.frames = (Activity(text="reading the week"), terminal_frame(), Activity(text="past"))
    server.syncs["s1"] = batch("s2", {ROOM: [mention("$m1", ALICE, "summarize the week")]})
    installation = await primed(server, workspace)
    await installation.step()
    await reported(installation)
    assert workspace.tailed == [UUID(str(workspace.admitted[0]["turn_id"]))]
    assert workspace.read == ["0", "1"]
    assert server.receipts == [(ROOM, "$m1")]
    assert server.typing_said() == [True, False]
    assert {(room, user) for room, user, _body in server.typing} == {(ROOM, BOT)}


@on_loop
async def test_a_declined_ambient_line_is_never_typed_at(workspace: Workspace) -> None:
    server = Homeserver()
    workspace.wanted = False
    server.syncs["s1"] = batch(
        "s2", {ROOM: [mention("$m1", ALICE, "draft it"), text("$m2", BOB, "nice weather")]}
    )
    installation = await primed(server, workspace)
    await installation.step()
    await reported(installation)
    assert workspace.tailed == [UUID(str(workspace.admitted[0]["turn_id"]))]
    assert server.receipts == [(ROOM, "$m1")]


@on_loop
async def test_a_redelivered_message_reports_once(workspace: Workspace) -> None:
    server = Homeserver()
    server.syncs["s1"] = batch("s2", {ROOM: [mention("$m1", ALICE, "again")]})
    installation = await primed(server, workspace)
    await installation.step()
    await reported(installation)
    await write_since(workspace, BOT, "s1")
    await installation.step()
    await reported(installation)
    assert len(workspace.tailed) == 1


@on_loop
async def test_typing_is_refreshed_while_the_turn_runs(workspace: Workspace) -> None:
    server = Homeserver()
    surface = MatrixSurface(transport=server.transport, bots="")

    async def frames():
        yield "0", Activity(text="still reading")
        await asyncio.sleep(0.05)
        yield "1", terminal_frame()

    async with surface.client(HOMESERVER, TOKEN) as client:
        await attend(client, ROOM, BOT, "$m1", frames(), refresh=0.005)
    said = server.typing_said()
    assert said.count(True) > 1 and said[-1] is False
    assert {body.get("timeout") for _room, _user, body in server.typing if body["typing"]} == {
        TYPING_TIMEOUT_MS
    }


@on_loop
async def test_a_stream_that_ends_stops_reporting(workspace: Workspace) -> None:
    server = Homeserver()
    workspace.endless = True
    server.syncs["s1"] = batch("s2", {ROOM: [mention("$m1", ALICE, "summarize the week")]})
    installation = await primed(server, workspace)
    await installation.step()
    await asyncio.sleep(0.01)
    assert server.typing_said() == [True]
    await installation.stop_attending()
    assert installation.attending == set()


@on_loop
async def test_a_homeserver_that_refuses_typing_costs_the_turn_nothing(
    workspace: Workspace,
) -> None:
    server = Homeserver()
    surface = MatrixSurface(transport=server.transport, bots="")

    async def frames():
        yield "0", terminal_frame()

    async with surface.client(HOMESERVER, TOKEN) as client:
        server.failure = httpx.Response(500, json={"errcode": "M_UNKNOWN"})
        await attend(client, ROOM, BOT, "$m1", frames())
    assert server.typing == []


@on_loop
async def test_the_bots_display_name_addresses_it(workspace: Workspace) -> None:
    server = Homeserver()
    server.display_name = "Ufo"
    server.syncs["s1"] = batch("s2", {ROOM: [text("$m1", ALICE, "ufo, summarize the week")]})
    installation = await primed(server, workspace)
    await installation.step()
    assert [a["key"] for a in workspace.admitted] == ["$m1"]
    assert workspace.asked == []


@on_loop
async def test_a_reply_to_the_bot_addresses_it(workspace: Workspace) -> None:
    server = Homeserver()
    server.syncs["s1"] = batch("s2", {ROOM: [text("$said", BOT, "here is the week")]})
    installation = await primed(server, workspace)
    await installation.step()
    server.syncs["s2"] = batch("s3", {ROOM: [reply("$m1", ALICE, "redo the middle part", "$said")]})
    await installation.step()
    assert [a["key"] for a in workspace.admitted] == ["$m1"]
    assert workspace.asked == []


@on_loop
async def test_a_line_naming_somebody_else_is_ambient(workspace: Workspace) -> None:
    server = Homeserver()
    named = text("$m2", BOB, f"{ALICE} can you look?", **{"m.mentions": {"user_ids": [ALICE]}})
    server.syncs["s1"] = batch("s2", {ROOM: [mention("$m1", ALICE, "draft it"), named]})
    workspace.wanted = False
    installation = await primed(server, workspace)
    await installation.step()
    assert [a["key"] for a in workspace.admitted] == ["$m1"]
    assert [asked.speaker for asked, _history in workspace.asked] == [BOB]


@on_loop
async def test_the_ambient_history_is_the_rooms_last_lines_and_who_said_them(
    workspace: Workspace,
) -> None:
    server = Homeserver()
    said = [text(f"$h{n}", BOB, f"line {n}") for n in range(AMBIENT_HISTORY_MESSAGES + 5)]
    server.syncs["s1"] = batch("s2", {ROOM: [mention("$m1", ALICE, "draft it"), *said]})
    workspace.wanted = False
    installation = await primed(server, workspace)
    await installation.step()
    _asked, history = workspace.asked[-1]
    assert len(history) == AMBIENT_HISTORY_MESSAGES
    assert history[-1].text == f"line {len(said) - 2}"
    assert [line.speaker for line in history if line.own] == []


@on_loop
async def test_a_file_uploaded_comes_back_by_its_mxc(workspace: Workspace) -> None:
    """`download` is `upload`'s counterpart: the bytes the media repository took are the bytes it
    answers with, addressed by the `mxc://` the upload returned."""
    server = Homeserver()
    async with MatrixClient(HOMESERVER, TOKEN, transport=server.transport) as client:
        uri = await client.upload("ledger.md", "text/markdown", b"# what the turn covered")
        assert await client.download(uri) == b"# what the turn covered"


@on_loop
async def test_a_download_of_nothing_raises_rather_than_returning_empty(workspace: Workspace) -> None:
    """A media id the repository does not hold is an error, not zero bytes — an empty file and an
    absent one read identically to a caller that only checks the length."""
    server = Homeserver()
    async with MatrixClient(HOMESERVER, TOKEN, transport=server.transport) as client:
        with pytest.raises(MatrixError):
            await client.download("mxc://example.org/never-uploaded")


@on_loop
async def test_a_uri_that_is_not_an_mxc_never_reaches_the_homeserver(workspace: Workspace) -> None:
    """The uri is an event's claim about where a file lives. One that names a path of its own is
    refused before a request is made, so the homeserver is never asked to resolve it."""
    server = Homeserver()
    async with MatrixClient(HOMESERVER, TOKEN, transport=server.transport) as client:
        before = len(server.requests)
        for uri in (
            "https://example.org/x",
            "mxc://example.org/../secret",
            "mxc://example.org",
            "mxc://../id",
            "mxc://./id",
            "mxc://../config",
            "mxc://example.org/..",
        ):
            with pytest.raises(MatrixError):
                await client.download(uri)
        assert len(server.requests) == before


def _shared(url: str = "", sealed: dict | None = None, size: int = 4) -> RoomFile:
    return RoomFile(
        room_id=ROOM,
        event_id="$f",
        sender=ALICE,
        filename="chart.png",
        media_type="image/png",
        size_bytes=size,
        url=url,
        sealed=sealed,
    )


@on_loop
async def test_a_member_file_is_fetched(workspace: Workspace) -> None:
    """The plain case: the bytes the media repository holds are the bytes the turn gets."""
    server = Homeserver()
    installation = rig(server, workspace)
    async with MatrixClient(HOMESERVER, TOKEN, transport=server.transport) as client:
        uri = await client.upload("chart.png", "image/png", b"\x89PNG")
        got = await installation.fetched(client, _shared(url=uri))
    assert got == b"\x89PNG"


@on_loop
async def test_a_file_larger_than_it_declared_is_dropped(workspace: Workspace) -> None:
    """`info.size` is written by the sender, so the declared size saves a download and protects
    nothing. A file arriving larger than it claimed is the case the cheap check misses, and the
    fetched length is what decides."""
    server = Homeserver()
    installation = rig(server, workspace)
    async with MatrixClient(HOMESERVER, TOKEN, transport=server.transport) as client:
        uri = await client.upload("chart.png", "image/png", b"x" * 64)
        got = await installation.fetched(client, _shared(url=uri, size=1), limit=16)
    assert got is None


@on_loop
async def test_a_file_declaring_too_much_is_never_fetched(workspace: Workspace) -> None:
    """The cheap check earns its place by costing no request at all."""
    server = Homeserver()
    installation = rig(server, workspace)
    async with MatrixClient(HOMESERVER, TOKEN, transport=server.transport) as client:
        before = len(server.requests)
        got = await installation.fetched(client, _shared(url="mxc://example.org/x", size=99), limit=8)
        assert len(server.requests) == before
    assert got is None


@on_loop
async def test_a_file_the_repository_does_not_hold_is_dropped_not_raised(workspace: Workspace) -> None:
    """A member's attachment must not stop their room being read: the fetch failure costs that
    file and the stream goes on."""
    server = Homeserver()
    installation = rig(server, workspace)
    async with MatrixClient(HOMESERVER, TOKEN, transport=server.transport) as client:
        got = await installation.fetched(client, _shared(url="mxc://example.org/never"))
    assert got is None


@on_loop
async def test_a_member_file_lands_in_the_workspace_and_is_named_to_the_turn(
    workspace: Workspace,
) -> None:
    """The whole inbound path: a member shares a file, the bytes land under a workspace path, the
    turn's own text names that path in its attachments element, and the artifact row points at what
    landed. `member_message_attachments` is what a projection reads a member's files from, so a
    path that never reaches the fence is a file the turn cannot see."""
    server = Homeserver()
    installation = await primed(server, workspace)
    async with MatrixClient(HOMESERVER, TOKEN, transport=server.transport) as client:
        uri = await client.upload("chart.png", "image/png", b"\x89PNG")
    server.syncs["s1"] = batch("s2", {DIRECT: [shared_file("$f1", ALICE, "chart.png", uri)]})
    assert await installation.step() == 0.0

    body = await workspace.admitted_body("$f1")
    assert body is not None
    assert member_message_attachments(body) == ("uploads/chart.png",)
    assert list(workspace.inbound_files.values()) == [b"\x89PNG"]
    [(_, key, rel)] = workspace.delivered
    assert rel == "uploads/chart.png"
    [(_, keys)] = workspace.member_files
    assert keys == (key,)


@on_loop
async def test_a_file_the_workspace_refuses_costs_the_file_and_not_the_turn(
    workspace: Workspace,
) -> None:
    """The store's write bound refuses rather than truncating. A member whose attachment is too
    large still gets a turn on their words — the failure this avoids is the one where a raise
    reaches the sync loop and their room stops being read at all."""
    workspace.write_limit_bytes = 2
    server = Homeserver()
    installation = await primed(server, workspace)
    async with MatrixClient(HOMESERVER, TOKEN, transport=server.transport) as client:
        uri = await client.upload("big.png", "image/png", b"\x89PNG")
    server.syncs["s1"] = batch("s2", {DIRECT: [shared_file("$f2", ALICE, "big.png", uri)]})
    assert await installation.step() == 0.0

    body = await workspace.admitted_body("$f2")
    assert body is not None, "the turn must still be admitted"
    assert member_message_attachments(body) == ()
    assert workspace.delivered == []
    assert workspace.member_files == []


@on_loop
async def test_a_member_cannot_name_a_path_out_of_the_uploads_directory(
    workspace: Workspace,
) -> None:
    """The filename is the sender's to choose and it reaches a workspace path, so it is a name and
    never a path. `../notes.md` landing as `uploads/../notes.md` resolves to the workspace root — in
    bounds, and still an arbitrary write chosen by whoever is in the room."""
    server = Homeserver()
    installation = await primed(server, workspace)
    async with MatrixClient(HOMESERVER, TOKEN, transport=server.transport) as client:
        uri = await client.upload("x", "image/png", b"\x89PNG")
    escape = shared_file("$f3", ALICE, "a caption", uri)
    escape["content"]["filename"] = "../../notes.md"
    server.syncs["s1"] = batch("s2", {DIRECT: [escape]})
    assert await installation.step() == 0.0

    [(_, _, rel)] = workspace.delivered
    assert rel == "uploads/notes.md"
    assert ".." not in rel
    body = await workspace.admitted_body("$f3")
    assert body is not None and member_message_attachments(body) == ("uploads/notes.md",)


@on_loop
async def test_two_files_under_one_name_land_beside_each_other(workspace: Workspace) -> None:
    """A workspace path is a place, so a second `chart.png` delivered to the path the first holds
    replaces it — and the turn reading `uploads/chart.png` reads the wrong member's bytes under a
    fence naming the right member's file. The sanitiser numbers a name against the ones already
    there, and what it is handed is the directory rather than an empty set."""
    server = Homeserver()
    installation = await primed(server, workspace)
    async with MatrixClient(HOMESERVER, TOKEN, transport=server.transport) as client:
        first = await client.upload("chart.png", "image/png", b"\x89PNG-one")
        second = await client.upload("chart.png", "image/png", b"\x89PNG-two")
    mine = shared_file("$g1", ALICE, "the first chart", first)
    mine["content"]["filename"] = "chart.png"
    also = shared_file("$g2", ALICE, "the second chart", second)
    also["content"]["filename"] = "chart.png"
    server.syncs["s1"] = batch("s2", {DIRECT: [mine, also]})
    assert await installation.step() == 0.0

    rels = [rel for _, _, rel in workspace.delivered]
    assert rels == ["uploads/chart.png", "uploads/chart-1.png"]
    conversation = workspace.delivered[0][0]
    assert sorted(workspace.workspace[conversation]) == sorted(rels)
    for event, rel in (("$g1", "uploads/chart.png"), ("$g2", "uploads/chart-1.png")):
        body = await workspace.admitted_body(event)
        assert body is not None and member_message_attachments(body) == (rel,)


@on_loop
async def test_a_name_already_in_uploads_is_numbered_after_a_restart(
    workspace: Workspace,
) -> None:
    """The set the sanitiser numbers against is read off the directory, not remembered, so a bot
    that was restarted between the two files still lands the second beside the first."""
    server = Homeserver()
    installation = await primed(server, workspace)
    async with MatrixClient(HOMESERVER, TOKEN, transport=server.transport) as client:
        first = await client.upload("chart.png", "image/png", b"\x89PNG-one")
        second = await client.upload("chart.png", "image/png", b"\x89PNG-two")
    mine = shared_file("$h1", ALICE, "the first chart", first)
    mine["content"]["filename"] = "chart.png"
    server.syncs["s1"] = batch("s2", {DIRECT: [mine]})
    assert await installation.step() == 0.0

    restarted = await primed(server, workspace)
    also = shared_file("$h2", ALICE, "the second chart", second)
    also["content"]["filename"] = "chart.png"
    server.syncs["s2"] = batch("s3", {DIRECT: [also]})
    assert await restarted.step() == 0.0

    assert [rel for _, _, rel in workspace.delivered] == [
        "uploads/chart.png",
        "uploads/chart-1.png",
    ]


@on_loop
async def test_a_replayed_file_event_lands_once(workspace: Workspace) -> None:
    """`deliver` runs before the `/sync` position is written, so a crash between the two replays the
    batch with the same event ids. Core admits the message once on that id; the file half answers
    from the row it wrote rather than fetching, storing and delivering a second time."""
    server = Homeserver()
    installation = await primed(server, workspace)
    async with MatrixClient(HOMESERVER, TOKEN, transport=server.transport) as client:
        uri = await client.upload("chart.png", "image/png", b"\x89PNG")
    once = shared_file("$r1", ALICE, "the chart", uri)
    once["content"]["filename"] = "chart.png"
    server.syncs["s1"] = batch("s2", {DIRECT: [once]})
    server.syncs["s2"] = batch("s3", {DIRECT: [once]})
    assert await installation.step() == 0.0
    assert await installation.step() == 0.0

    rels = [rel for _, _, rel in workspace.delivered]
    assert rels == ["uploads/chart.png"], "the replay must not deliver a second copy"
    conversation = workspace.delivered[0][0]
    assert sorted(workspace.workspace[conversation]) == ["uploads/chart.png"]
    # One artifact row for one message: the replay answers with the key it recorded, and core's
    # insert conflicts on it. A freshly minted key would have conflicted with nothing.
    assert len(workspace.artifacts) == 1
    assert len(workspace.inbound_files) == 1, "the replay must not re-store the bytes"


@on_loop
async def test_a_second_file_after_a_replay_is_still_numbered(workspace: Workspace) -> None:
    """The replay key is the event's, not the name's, so a genuinely different file under a name
    already used is still numbered beside it."""
    server = Homeserver()
    installation = await primed(server, workspace)
    async with MatrixClient(HOMESERVER, TOKEN, transport=server.transport) as client:
        first = await client.upload("chart.png", "image/png", b"\x89PNG-one")
        second = await client.upload("chart.png", "image/png", b"\x89PNG-two")
    once = shared_file("$r1", ALICE, "the chart", first)
    once["content"]["filename"] = "chart.png"
    other = shared_file("$r2", ALICE, "another chart", second)
    other["content"]["filename"] = "chart.png"
    server.syncs["s1"] = batch("s2", {DIRECT: [once]})
    server.syncs["s2"] = batch("s3", {DIRECT: [once, other]})
    assert await installation.step() == 0.0
    assert await installation.step() == 0.0

    assert [rel for _, _, rel in workspace.delivered] == [
        "uploads/chart.png",
        "uploads/chart-1.png",
    ]
