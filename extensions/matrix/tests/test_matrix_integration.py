"""The delivery handlers against a real homeserver: the bot a workspace connected posts into a real
room as rich text under the message that founded its turn, its files reach the media repository and
come back as what they are, a re-handed mid-turn reply lands once, and another workspace's token is
refused from this workspace's rooms.

The suite is collected only where `MATRIX_INTEGRATION_HOMESERVER` names a homeserver (see
`conftest.py`), and it provisions its own throwaway users, so the target must allow registration —
a private Synapse container does. Nothing here runs against the fake transport; every event is read
back from the homeserver itself."""

import asyncio
import functools
import inspect
import os
from dataclasses import dataclass, field
from typing import Any, Self
from urllib.parse import quote
from uuid import UUID, uuid4

import httpx
import pytest

pytest.importorskip("ufo", reason="install ufo from git to run the integration tests")

from matrix_fakes import Credentials, Workspace, extension_engine
from ufo_ext_matrix.answering import Answering, write_answering
from ufo_ext_matrix.messages import reply_relation
from ufo_ext_matrix.surface import (
    HOMESERVER_SLOT,
    TOKEN_SLOT,
    ConnectInput,
    MatrixSurface,
)

from ufo.sdk.surfaces import (
    MidTurnReply,
    SharedArtifact,
    SurfaceDeliveryError,
    SurfaceInstallationConflict,
    TerminalFrame,
    Writeback,
)

HOMESERVER = os.environ["MATRIX_INTEGRATION_HOMESERVER"]


class Api:
    """A bare client-server caller for setup and for reading events back — the parts the surface's
    own client does not cover: registration, rooms, and reading what the room holds."""

    def __init__(self, token: str | None = None) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._http = httpx.AsyncClient(base_url=HOMESERVER, headers=headers, timeout=30.0)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_status(self, path: str) -> int:
        """The status one raw GET answers with, for asking whether an endpoint serves at all."""
        answer = await self._http.get(path)
        return answer.status_code

    async def register(self, localpart: str, password: str) -> tuple[str, str]:
        answer = (
            await self._http.post(
                "/_matrix/client/v3/register",
                json={
                    "username": localpart,
                    "password": password,
                    "auth": {"type": "m.login.dummy"},
                },
            )
        ).json()
        return answer["user_id"], answer["access_token"]

    async def create_room(self, invite: tuple[str, ...] = ()) -> str:
        answer = (
            await self._http.post(
                "/_matrix/client/v3/createRoom",
                json={"visibility": "private", "invite": list(invite)},
            )
        ).json()
        return answer["room_id"]

    async def send(self, room_id: str, body: str, relates_to: dict[str, Any] | None = None) -> str:
        content: dict[str, Any] = {"msgtype": "m.text", "body": body}
        if relates_to:
            content["m.relates_to"] = relates_to
        answer = (
            await self._http.put(
                f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}"
                f"/send/m.room.message/it-{uuid4().hex}",
                json=content,
            )
        ).json()
        return answer["event_id"]

    async def join(self, room_id: str) -> None:
        await self._http.post(f"/_matrix/client/v3/join/{quote(room_id, safe='')}")

    async def event(self, room_id: str, event_id: str) -> dict[str, Any]:
        answer = (
            await self._http.get(
                f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}"
                f"/event/{quote(event_id, safe='')}"
            )
        ).json()
        return answer

    async def timeline(self, room_id: str, limit: int = 50) -> list[dict[str, Any]]:
        answer = (
            await self._http.get(
                f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/messages",
                params={"dir": "b", "limit": limit},
            )
        ).json()
        return [event for event in answer["chunk"] if isinstance(event, dict)]

    async def download(self, mxc: str) -> bytes:
        """The bytes an `mxc://` names, through the authenticated media endpoint a modern homeserver
        serves media on — the unauthenticated download endpoint refuses authenticated media."""
        _, _, rest = mxc.partition("://")
        server, _, media_id = rest.partition("/")
        answer = await self._http.get(
            f"/_matrix/client/v1/media/download/{server}/{quote(media_id, safe='')}"
        )
        assert answer.status_code == 200, answer.status_code
        return answer.content


@dataclass
class Homes:
    """The users and the room this module's tests share, provisioned once against the real
    homeserver. The member's room has the bot in it and nobody else."""

    bot: tuple[str, str]
    member: tuple[str, str]
    outsider: tuple[str, str]
    room_id: str


@pytest.fixture(scope="module")
def homes() -> Homes:
    async def provision() -> Homes:
        api = Api()
        try:
            suffix = uuid4().hex[:10]
            bot = await api.register(f"sweep7bot{suffix}", f"bot-{suffix}-pass")
            member = await api.register(f"sweep7member{suffix}", f"member-{suffix}-pass")
            outsider = await api.register(f"sweep7other{suffix}", f"other-{suffix}-pass")
            room_id = await Api(member[1]).create_room(invite=(bot[0],))
            await Api(bot[1]).join(room_id)
            return Homes(bot=bot, member=member, outsider=outsider, room_id=room_id)
        finally:
            await api.aclose()

    return asyncio.run(provision())


@dataclass
class Installations:
    """The fleet's installation registry, shared across the workspaces one test drives. A bot
    another workspace already holds is the conflict core answers with."""

    registry: dict[str, str] = field(default_factory=dict)
    bound: list[tuple[str, str]] = field(default_factory=list)

    async def bind(self, surface: str, installation_id: str) -> None:
        if installation_id in self.registry:
            raise SurfaceInstallationConflict(surface)
        self.registry[installation_id] = surface
        self.bound.append((surface, installation_id))


@dataclass
class Ext:
    credentials: Credentials
    installations: Installations


@dataclass
class Tool:
    ext: Ext


def workspace_ctx(engine: Any, token: str) -> Workspace:
    """The surface context fake from the unit fakes, holding this workspace's real credentials."""
    return Workspace(engine=engine, credentials={HOMESERVER_SLOT: HOMESERVER, TOKEN_SLOT: token})


def on_loop(test: Any) -> Any:
    """Run one async test on its own loop, with the extension's real tables in memory. `engine` is
    injected, the way the unit fakes inject `workspace`."""

    params = [p for p in inspect.signature(test).parameters.values() if p.name != "engine"]

    @functools.wraps(test)
    def run(*args: Any, **kwargs: Any) -> None:
        async def scenario() -> None:
            engine = await extension_engine()
            try:
                await test(*args, engine=engine, **kwargs)
            finally:
                await engine.dispose()

        asyncio.run(scenario())

    run.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]
    return run


def writeback(turn_id: UUID, room_id: str, artifacts: tuple[SharedArtifact, ...] = ()) -> Writeback:
    return Writeback(
        turn_id=turn_id,
        conversation_id=uuid4(),
        agent_id=uuid4(),
        queue_key=room_id,
        terminal=TerminalFrame(status="done", text="The answer, in full."),
        artifacts=artifacts,
        speaker_member_id=uuid4(),
    )


def shared(filename: str, media_type: str, body: bytes) -> SharedArtifact:
    return SharedArtifact(
        id=uuid4(),
        blob_key=f"artifacts/{filename}",
        filename=filename,
        subject=None,
        media_type=media_type,
        size_bytes=len(body),
        role="file",
    )


@on_loop
async def test_connect_binds_the_bot_the_token_belongs_to(engine: Any, homes: Homes) -> None:
    surface = MatrixSurface(bots="")
    installations = Installations(registry={})
    tool = Tool(
        Ext(Credentials({HOMESERVER_SLOT: HOMESERVER, TOKEN_SLOT: homes.bot[1]}), installations)
    )
    result = await surface.connect(tool, ConnectInput())  # type: ignore[arg-type]
    assert not result.is_error
    assert installations.bound == [("matrix", homes.bot[0])]

    taken = Tool(
        Ext(Credentials({HOMESERVER_SLOT: HOMESERVER, TOKEN_SLOT: homes.bot[1]}), installations)
    )
    again = await surface.connect(taken, ConnectInput())  # type: ignore[arg-type]
    assert again.is_error and "another workspace" in again.content[0].text  # type: ignore[index]


@on_loop
async def test_a_reply_reaches_its_room_as_rich_text_under_the_message(
    engine: Any, homes: Homes
) -> None:
    async with Api(homes.member[1]) as member:
        asked = await member.send(homes.room_id, "what does the week look like?")
    ctx = workspace_ctx(engine, homes.bot[1])
    turn = uuid4()
    await write_answering(ctx, turn, Answering(homes.room_id, asked))
    surface = MatrixSurface(bots="")
    wb = writeback(turn, homes.room_id)
    reference = await surface.post(ctx, wb)  # type: ignore[arg-type]

    async with Api(homes.bot[1]) as bot:
        event = await bot.event(homes.room_id, reference)
    content = event["content"]
    assert content["msgtype"] == "m.text"
    assert content["body"] == "The answer, in full."
    assert content["format"] == "org.matrix.custom.html"
    assert content["formatted_body"] == "<p>The answer, in full.</p>"
    assert content["m.relates_to"] == reply_relation(asked, None)


@on_loop
async def test_a_threaded_reply_hangs_under_its_root(engine: Any, homes: Homes) -> None:
    async with Api(homes.member[1]) as member:
        root = await member.send(homes.room_id, "the weekly numbers")
        asked = await member.send(
            homes.room_id,
            "and for friday?",
            relates_to={"rel_type": "m.thread", "event_id": root},
        )
    ctx = workspace_ctx(engine, homes.bot[1])
    turn = uuid4()
    await write_answering(ctx, turn, Answering(homes.room_id, asked, root))
    surface = MatrixSurface(bots="")
    reference = await surface.post(ctx, writeback(turn, homes.room_id))  # type: ignore[arg-type]

    async with Api(homes.bot[1]) as bot:
        event = await bot.event(homes.room_id, reference)
    assert event["content"]["m.relates_to"] == reply_relation(asked, root)


@on_loop
async def test_shared_files_reach_the_room_as_what_they_are(engine: Any, homes: Homes) -> None:
    png_body = b"\x89PNG\r\n\x1a\n sweep7 bytes"
    notes_body = b"# sweep7 notes"
    png = shared("chart.png", "image/png", png_body)
    notes = shared("notes.md", "text/markdown", notes_body)
    ctx = workspace_ctx(engine, homes.bot[1])
    ctx.blob.objects = {png.blob_key: png_body, notes.blob_key: notes_body}
    turn = uuid4()
    surface = MatrixSurface(bots="")
    wb = writeback(turn, homes.room_id, (png, notes))
    reference = await surface.post(ctx, wb)  # type: ignore[arg-type]
    async with Api(homes.bot[1]) as bot:
        before = {event["event_id"] for event in await bot.timeline(homes.room_id)}
    await surface.attach(ctx, wb, str(reference))  # type: ignore[arg-type]

    async with Api(homes.bot[1]) as bot:
        fresh = [
            event for event in await bot.timeline(homes.room_id) if event["event_id"] not in before
        ]
        picture = next(e for e in fresh if e["content"]["msgtype"] == "m.image")
        document = next(e for e in fresh if e["content"]["msgtype"] == "m.file")
        assert await bot.download(picture["content"]["url"]) == png_body
    assert picture["content"]["url"].startswith("mxc://")
    assert document["content"]["body"] == "notes.md"


@on_loop
async def test_a_recovered_attach_and_a_rehanded_say_land_once(engine: Any, homes: Homes) -> None:
    notes_body = b"# sweep7 notes"
    notes = shared("notes.md", "text/markdown", notes_body)
    ctx = workspace_ctx(engine, homes.bot[1])
    ctx.blob.objects = {notes.blob_key: notes_body}
    turn = uuid4()
    surface = MatrixSurface(bots="")
    wb = writeback(turn, homes.room_id, (notes,))
    reference = await surface.post(ctx, wb)  # type: ignore[arg-type]
    async with Api(homes.bot[1]) as bot:
        before = {event["event_id"] for event in await bot.timeline(homes.room_id)}
    await surface.attach(ctx, wb, str(reference))  # type: ignore[arg-type]
    async with Api(homes.bot[1]) as bot:
        delivered = [event["event_id"] for event in await bot.timeline(homes.room_id)]
        attachment = next(
            event for event in await bot.timeline(homes.room_id) if event["event_id"] not in before
        )
    await surface.attach(ctx, wb, str(reference))  # type: ignore[arg-type]
    async with Api(homes.bot[1]) as bot:
        recovered = [event["event_id"] for event in await bot.timeline(homes.room_id)]
    assert recovered == delivered  # a repeated attach adds no event the room did not hold

    said = MidTurnReply(
        id=uuid4(),
        turn_id=turn,
        conversation_id=wb.conversation_id,
        agent_id=wb.agent_id,
        queue_key=homes.room_id,
        message_ref=None,
        text="Halfway there.",
    )
    first = await surface.speak(ctx, said)  # type: ignore[arg-type]
    second = await surface.speak(ctx, said)  # type: ignore[arg-type]
    assert first == second

    async with Api(homes.bot[1]) as bot:
        held = [event["event_id"] for event in await bot.timeline(homes.room_id)]
        spoken = await bot.event(homes.room_id, first)
    assert held == [first, *recovered]  # newest first; two speaks of one row land once
    assert attachment["content"]["body"] == "notes.md"
    assert spoken["content"]["body"] == "Halfway there."


@on_loop
async def test_another_workspaces_token_is_rejected_from_this_rooms_delivery(
    engine: Any, homes: Homes
) -> None:
    async with Api(homes.outsider[1]) as outsider:
        await outsider.create_room()  # the outsider's token is real and belongs to someone else
    ctx = workspace_ctx(engine, homes.outsider[1])
    surface = MatrixSurface(bots="")
    with pytest.raises(SurfaceDeliveryError):
        await surface.post(ctx, writeback(uuid4(), homes.room_id))  # type: ignore[arg-type]

    async with Api(homes.outsider[1]) as outsider:
        refused = await outsider._http.get(
            f"/_matrix/client/v3/rooms/{quote(homes.room_id, safe='')}/event/$sweep7none"
        )
        assert refused.status_code in (403, 404)
