"""Linking an MXID by proof against a fake homeserver: a member claims an MXID in chat, the code
arrives from that MXID in a direct room, and the MXID speaks for that member from the next message
on. A wrong, expired, spent, or misdirected code links nothing; the proving message founds no turn,
replayed or not; the bot joins a non-member's room only while a claim on that MXID is live; and an
admin's unlink makes an MXID nobody until it is proved again."""

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

pytest.importorskip("ufo", reason="install ufo from git to run the linking tests")

from matrix_fakes import (
    ALICE,
    BOB,
    BOT,
    OUTSIDER,
    ROOM,
    Homeserver,
    Listener,
    Workspace,
    batch,
    mention,
    on_loop,
    said,
    text,
)
from sqlalchemy.ext.asyncio import AsyncConnection
from ufo_ext_matrix.linking import (
    CLAIM_MINUTES,
    CODE_ATTEMPTS,
    EXPIRED_LINE,
    HELD_LINE,
    LINKED_LINE,
    SPENT_LINE,
    WRONG_LINE,
    Linking,
    LinkInput,
    UnlinkInput,
    code_in,
    mint_code,
    proof_txn,
)
from ufo_ext_matrix.since import write_since
from ufo_ext_matrix.surface import Installation, MatrixSurface

from ufo.sdk.tools import SpeakerRequired

DM = "!carol-dm:elsewhere.test"
OTHER = "@dave:elsewhere.test"
OTHER_DM = "!dave-dm:elsewhere.test"
START = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


@dataclass
class Clock:
    now: datetime = START

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Installations:
    workspace: Workspace
    bot: str | None = BOT

    async def installation(self, surface: str) -> str | None:
        return self.bot

    async def linked_members(self, surface: str) -> dict[UUID, str]:
        return {member: mxid for mxid, member in self.workspace.linked.items()}


@dataclass
class Ext:
    workspace: Workspace
    installations: Installations

    @property
    def workspace_id(self) -> UUID:
        return self.workspace.workspace_id

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncConnection]:
        async with self.workspace.transaction() as connection:
            yield connection


@dataclass
class Tool:
    ext: Ext
    speaker_member_id: UUID | None
    admin: bool = False

    def require_speaker(self, gate: str = "") -> UUID:
        if self.speaker_member_id is None:
            raise SpeakerRequired(gate)
        return self.speaker_member_id

    async def require_speaking_admin(self, gate: str) -> bool:
        self.require_speaker(gate)
        return self.admin


@dataclass
class Rig:
    workspace: Workspace
    server: Homeserver = field(default_factory=Homeserver)
    clock: Clock = field(default_factory=Clock)

    def __post_init__(self) -> None:
        self.server.members = {ROOM: [BOT, ALICE, BOB], DM: [BOT, OUTSIDER], OTHER_DM: [BOT, OTHER]}
        self.surface = MatrixSurface(
            transport=self.server.transport, bots="", linking=Linking(clock=self.clock)
        )
        self.installation = Installation(self.surface, Listener(self.workspace), BOT)  # type: ignore[arg-type]

    def member(self, email: str) -> UUID:
        return self.workspace.members[email]

    def tool(self, email: str | None = "alice@example.org", *, admin: bool = False) -> Tool:
        speaker = None if email is None else self.member(email)
        return Tool(Ext(self.workspace, Installations(self.workspace)), speaker, admin)

    async def claim(self, mxid: str = OUTSIDER, email: str = "alice@example.org") -> str:
        result = await self.surface.linking.link_account(self.tool(email), LinkInput(mxid=mxid))  # type: ignore[arg-type]
        assert not result.is_error, said(result)
        found = re.search(r"send (\w{4}-\w{4}) to", said(result))
        assert found is not None
        return found[1]

    async def unlink(self, mxid: str, *, admin: bool = True) -> str:
        tool = self.tool("bob@example.org", admin=admin)
        return said(await self.surface.linking.unlink_account(tool, UnlinkInput(mxid=mxid)))  # type: ignore[arg-type]

    async def sync(self, *rounds: dict) -> None:
        """Prime the stream, then deliver each round as the next batch."""
        self.server.syncs[None] = batch("s1")
        for n, rooms in enumerate(rounds, 1):
            self.server.syncs[f"s{n}"] = batch(f"s{n + 1}", rooms)
        for _ in range(len(rounds) + 1):
            await self.installation.step()

    def replies(self) -> list[tuple[str, str]]:
        return [(sent["room"], sent["body"]) for sent in self.server.sent.values()]


def admitted(workspace: Workspace) -> list[tuple[str, UUID | None]]:
    return [(a["key"], a["speaker"]) for a in workspace.admitted]


@on_loop
async def test_a_proved_mxid_speaks_for_its_member_from_the_next_message(
    workspace: Workspace,
) -> None:
    rig = Rig(workspace)
    code = await rig.claim()
    await rig.sync(
        {DM: [text("$proof", OUTSIDER, code.lower())]},
        {DM: [text("$next", OUTSIDER, "what is on today?")]},
    )
    alice = rig.member("alice@example.org")
    assert workspace.linked[OUTSIDER] == alice
    assert admitted(workspace) == [("$next", alice)]
    assert rig.replies() == [(DM, LINKED_LINE)]
    assert list(rig.server.sent) == [proof_txn("$proof")]
    assert code.lower() not in workspace.admitted[0]["body"]


@on_loop
async def test_a_message_after_the_proof_in_the_same_batch_founds_a_turn(
    workspace: Workspace,
) -> None:
    rig = Rig(workspace)
    code = await rig.claim()
    await rig.sync({DM: [text("$proof", OUTSIDER, code), text("$next", OUTSIDER, "hi")]})
    assert admitted(workspace) == [("$next", rig.member("alice@example.org"))]
    assert [h.line.text for h in rig.installation.heard[DM]] == ["hi"]


@on_loop
async def test_a_wrong_code_links_nothing_and_says_so(workspace: Workspace) -> None:
    rig = Rig(workspace)
    await rig.claim()
    await rig.sync({DM: [text("$guess", OUTSIDER, "AAAA-AAAA")]})
    assert OUTSIDER not in workspace.linked
    assert admitted(workspace) == []
    assert rig.replies() == [(DM, WRONG_LINE.format(left=CODE_ATTEMPTS - 1))]


@on_loop
async def test_an_expired_claim_links_nothing(workspace: Workspace) -> None:
    rig = Rig(workspace)
    code = await rig.claim()
    rig.clock.now = START + timedelta(minutes=CLAIM_MINUTES, seconds=1)
    await rig.sync({DM: [text("$late", OUTSIDER, code)]}, {DM: [text("$again", OUTSIDER, code)]})
    assert OUTSIDER not in workspace.linked
    assert admitted(workspace) == []
    assert rig.replies() == [(DM, EXPIRED_LINE)]


@on_loop
async def test_a_code_from_another_mxid_links_nothing_and_gets_no_answer(
    workspace: Workspace,
) -> None:
    rig = Rig(workspace)
    code = await rig.claim()
    await rig.sync({OTHER_DM: [text("$stolen", OTHER, code)]})
    assert workspace.linked == {}
    assert admitted(workspace) == []
    assert rig.replies() == []


@on_loop
async def test_a_code_outside_a_direct_room_proves_nothing(workspace: Workspace) -> None:
    rig = Rig(workspace)
    rig.server.members[DM] = [BOT, OUTSIDER, ALICE]
    code = await rig.claim()
    await rig.sync({DM: [mention("$crowded", OUTSIDER, code)]})
    assert OUTSIDER not in workspace.linked
    assert rig.replies() == []


@on_loop
async def test_the_proving_message_is_never_admitted_even_replayed(workspace: Workspace) -> None:
    rig = Rig(workspace)
    code = await rig.claim()
    await rig.sync({DM: [text("$proof", OUTSIDER, code)]})
    await write_since(workspace, BOT, "s1")
    await rig.installation.step()
    assert admitted(workspace) == []
    assert rig.replies() == [(DM, LINKED_LINE)]
    assert DM not in rig.installation.heard or not rig.installation.heard[DM]


@on_loop
async def test_a_linked_member_sending_a_code_shaped_line_founds_a_turn(
    workspace: Workspace,
) -> None:
    rig = Rig(workspace)
    rig.server.members[DM] = [BOT, ALICE]
    await rig.sync({DM: [text("$word", ALICE, "tomorrow")]})
    assert admitted(workspace) == [("$word", rig.member("alice@example.org"))]
    assert rig.replies() == []


def invite(room: str, sender: str) -> dict:
    member = {
        "type": "m.room.member",
        "state_key": BOT,
        "sender": sender,
        "content": {"membership": "invite"},
    }
    return {"invite_state": {"events": [member]}}


@on_loop
async def test_the_bot_joins_a_non_members_room_only_while_a_claim_is_live(
    workspace: Workspace,
) -> None:
    rig = Rig(workspace)
    invited = {"next_batch": "s1", "rooms": {"invite": {DM: invite(DM, OUTSIDER)}}}
    rig.server.syncs[None] = rig.server.syncs["s1"] = invited
    await rig.installation.step()
    assert rig.server.joined == []

    await rig.claim()
    await rig.installation.step()
    assert rig.server.joined == [DM]

    rig.clock.now = START + timedelta(minutes=CLAIM_MINUTES, seconds=1)
    await rig.installation.step()
    assert rig.server.joined == [DM]


@on_loop
async def test_a_claim_on_one_mxid_opens_no_door_for_another(workspace: Workspace) -> None:
    rig = Rig(workspace)
    await rig.claim()
    rig.server.syncs[None] = {
        "next_batch": "s1",
        "rooms": {"invite": {OTHER_DM: invite(OTHER_DM, OTHER)}},
    }
    await rig.installation.step()
    assert rig.server.joined == []


@on_loop
async def test_wrong_codes_run_out_and_void_the_claim(workspace: Workspace) -> None:
    rig = Rig(workspace)
    code = await rig.claim()
    guesses = [text(f"$g{n}", OUTSIDER, "AAAA-AAAA") for n in range(CODE_ATTEMPTS)]
    await rig.sync({DM: guesses}, {DM: [text("$right", OUTSIDER, code)]})
    assert OUTSIDER not in workspace.linked
    assert admitted(workspace) == []
    bodies = [body for _, body in rig.replies()]
    assert bodies == [
        *(WRONG_LINE.format(left=CODE_ATTEMPTS - n) for n in range(1, CODE_ATTEMPTS)),
        SPENT_LINE,
    ]


@on_loop
async def test_a_new_claim_replaces_the_old_code(workspace: Workspace) -> None:
    rig = Rig(workspace)
    first = await rig.claim()
    second = await rig.claim()
    assert first != second
    await rig.sync({DM: [text("$old", OUTSIDER, first)]}, {DM: [text("$new", OUTSIDER, second)]})
    assert workspace.linked[OUTSIDER] == rig.member("alice@example.org")


@on_loop
async def test_an_admin_unlink_silences_an_mxid_until_it_is_proved_again(
    workspace: Workspace,
) -> None:
    rig = Rig(workspace)
    rig.server.members[DM] = [BOT, ALICE]
    assert "Unlinked" in await rig.unlink(ALICE)
    code = await rig.claim(ALICE)
    await rig.sync(
        {DM: [text("$cut", ALICE, "are you there?")]},
        {DM: [text("$proof", ALICE, code)]},
        {DM: [text("$back", ALICE, "and now?")]},
    )
    assert admitted(workspace) == [("$back", rig.member("alice@example.org"))]
    assert rig.replies() == [(DM, LINKED_LINE)]


@on_loop
async def test_only_an_admin_unlinks(workspace: Workspace) -> None:
    rig = Rig(workspace)
    assert "Only a workspace admin" in await rig.unlink(ALICE, admin=False)
    rig.server.members[DM] = [BOT, ALICE]
    await rig.sync({DM: [text("$still", ALICE, "hello")]})
    assert admitted(workspace) == [("$still", rig.member("alice@example.org"))]


@on_loop
async def test_an_mxid_linked_to_one_member_does_not_move_to_another(
    workspace: Workspace,
) -> None:
    rig = Rig(workspace)
    code = await rig.claim()
    await rig.sync({DM: [text("$proof", OUTSIDER, code)]})
    result = await rig.surface.linking.link_account(
        rig.tool("bob@example.org"),  # type: ignore[arg-type]
        LinkInput(mxid=OUTSIDER),
    )
    assert result.is_error
    assert "another member" in said(result)


@on_loop
async def test_a_proof_for_an_mxid_core_holds_for_someone_else_is_refused(
    workspace: Workspace,
) -> None:
    """Core keeps the first link it recorded; an unlinked MXID proved by another member stays
    unlinked, and the member is told why."""
    rig = Rig(workspace)
    workspace.linked[OUTSIDER] = rig.member("bob@example.org")
    await rig.unlink(OUTSIDER)
    workspace.linked.clear()
    code = await rig.claim()
    workspace.linked[OUTSIDER] = rig.member("bob@example.org")
    await rig.sync({DM: [text("$proof", OUTSIDER, code)]}, {DM: [text("$next", OUTSIDER, "hi")]})
    assert admitted(workspace) == []
    assert rig.replies() == [(DM, HELD_LINE)]


@pytest.mark.parametrize(
    ("mxid", "bot", "fragment"),
    [
        ("carol", BOT, "not a Matrix ID"),
        (OUTSIDER, None, "not connected"),
        (BOT, BOT, "own bot"),
    ],
    ids=["not-an-mxid", "no-bot", "the-bot"],
)
@on_loop
async def test_a_claim_is_refused_before_a_code_is_minted(
    workspace: Workspace, mxid: str, bot: str | None, fragment: str
) -> None:
    rig = Rig(workspace)
    tool = rig.tool()
    tool.ext.installations.bot = bot
    result = await rig.surface.linking.link_account(tool, LinkInput(mxid=mxid))  # type: ignore[arg-type]
    assert result.is_error
    assert fragment in said(result)


@on_loop
async def test_a_claim_needs_a_speaker(workspace: Workspace) -> None:
    rig = Rig(workspace)
    with pytest.raises(SpeakerRequired):
        await rig.surface.linking.link_account(rig.tool(None), LinkInput(mxid=OUTSIDER))  # type: ignore[arg-type]


@on_loop
async def test_the_code_is_stored_hashed(workspace: Workspace) -> None:
    rig = Rig(workspace)
    code = await rig.claim()
    async with workspace.transaction() as connection:
        dump = "\n".join(
            str(tuple(row))
            for row in await connection.exec_driver_sql("select * from matrix_ext_claim")
        )
    assert code.replace("-", "") not in dump
    assert code not in dump


def test_a_code_reads_however_it_is_typed() -> None:
    code = mint_code()
    assert code_in(code) == code
    assert code_in(f" {code[:4].lower()}-{code[4:].lower()} ") == code
    assert code_in(f"{code[:4]} {code[4:]}") == code
    assert code_in(f"my code is {code}") is None
    assert len({mint_code() for _ in range(50)}) == 50
