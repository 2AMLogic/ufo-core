"""A fake homeserver behind an httpx `MockTransport`, and fakes of the two surface contexts that
record what the surface asked core to do. The extension's own tables are real: a surface context's
`transaction` yields an in-memory SQLite connection holding them, so where a turn stands, which
message it answers, and what a member proved are read back the way a deploy reads them. So is the
audience rule: `conversation_for` narrows through core's own `narrow_audience`, and raises where
core raises.

A question is real too: `answerable_question` answers the ask the workspace holds for exactly the
indexes it left open, to exactly the member it names, so the surface's gate is core's gate."""

import asyncio
import functools
import inspect
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from ufo_ext_matrix.answering import ANSWERING_TABLE
from ufo_ext_matrix.asking import ASKING_TABLE
from ufo_ext_matrix.client import AUTHENTICATED_MEDIA_PATH, MEDIA_PATH
from ufo_ext_matrix.crypto_store import CRYPTO_TABLE
from ufo_ext_matrix.delivered import DELIVERED_TABLE
from ufo_ext_matrix.linking import CLAIM_TABLE, LINK_TABLE
from ufo_ext_matrix.since import SINCE_TABLE
from ufo_ext_matrix.surface import HOMESERVER_SLOT, TOKEN_SLOT

from ufo.runtime.turns.audience import (
    SHARED_AUDIENCE,
    audience_member,
    narrow_audience,
    parse_audience,
)
from ufo.sdk.audience import Audience
from ufo.sdk.hub import Activity, LiveFrame, Terminal
from ufo.sdk.surfaces import (
    Admitted,
    AmbientMessage,
    AskUserInput,
    CredentialSlotUnset,
    SharedArtifact,
    TerminalFrame,
    WorkspaceFile,
)

BOT = "@ufo:example.org"
HOMESERVER = "https://matrix.example.org"
def media_id(filename: str) -> str:
    """The id a media repository answers an upload with."""
    return re.sub(r"[^A-Za-z0-9_-]", "", filename) or "media"


TOKEN = "syt_secret_bot_token_value"
ROOM = "!ops:example.org"
DIRECT = "!direct:example.org"
ALICE = "@alice:example.org"
BOB = "@bob:example.org"
OUTSIDER = "@carol:elsewhere.test"
STRANGER = "@mallory:example.org"


def text(event_id: str, sender: str, body: str, **content: object) -> dict[str, Any]:
    return {
        "type": "m.room.message",
        "event_id": event_id,
        "sender": sender,
        "content": {"msgtype": "m.text", "body": body, **content},
    }


def shared_file(
    event_id: str, sender: str, filename: str, url: str, **content: object
) -> dict[str, Any]:
    """One file a member sent, as the event a room carries it by."""
    return {
        "type": "m.room.message",
        "event_id": event_id,
        "sender": sender,
        "content": {
            "msgtype": "m.image",
            "body": filename,
            "url": url,
            "info": {"mimetype": "image/png", "size": 4},
            **content,
        },
    }


def mention(event_id: str, sender: str, body: str) -> dict[str, Any]:
    return text(event_id, sender, body, **{"m.mentions": {"user_ids": [BOT]}})


def reply(event_id: str, sender: str, body: str, to: str) -> dict[str, Any]:
    """One message sent as a rich reply to another, which is how a member answers a question."""
    return text(event_id, sender, body, **{"m.relates_to": {"m.in_reply_to": {"event_id": to}}})


def tap(event_id: str, sender: str, poll_id: str, *answers: str) -> dict[str, Any]:
    """One member's tap on a poll, as their client sends it."""
    return {
        "type": "m.poll.response",
        "event_id": event_id,
        "sender": sender,
        "content": {
            "m.relates_to": {"rel_type": "m.reference", "event_id": poll_id},
            "m.poll.response": {"answers": list(answers)},
        },
    }


def asking(title: str, *questions: dict[str, Any], target: UUID | None = None) -> AskUserInput:
    """One `ask_user` as a terminal frame carries it."""
    return AskUserInput.model_validate(
        {"title": title, "questions": list(questions), "target_member_id": target}
    )


def question(text_: str, *options: str, multi_select: bool = False) -> dict[str, Any]:
    return {
        "question": text_,
        "options": [{"label": option} for option in options],
        "multi_select": multi_select,
    }


def batch(token: str, rooms: Mapping[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    return {
        "next_batch": token,
        "rooms": {
            "join": {
                room: {"timeline": {"events": events}} for room, events in (rooms or {}).items()
            }
        },
    }


@dataclass
class Homeserver:
    """Serves `/sync` from a script keyed by the `since` it is asked for, the room member lists, and
    idempotent sends."""

    syncs: dict[str | None, dict[str, Any]] = field(default_factory=dict)
    members: dict[str, list[str]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    sent: dict[str, dict[str, Any]] = field(default_factory=dict)
    joined: list[str] = field(default_factory=list)
    failure: httpx.Response | None = None
    forbidden: set[str] = field(default_factory=set)
    history: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    uploaded: list[tuple[str, bytes]] = field(default_factory=list)
    refused_uploads: set[str] = field(default_factory=set)
    limited_uploads: set[str] = field(default_factory=set)
    refused_sends: set[str] = field(default_factory=set)
    typing: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    receipts: list[tuple[str, str]] = field(default_factory=list)
    display_name: str = "ufo"
    encrypted: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"errcode": "M_UNKNOWN_TOKEN"})
        if self.failure is not None:
            return self.failure
        if request.url.path == f"{MEDIA_PATH}/upload":
            return self.uploaded_media(request)
        if request.url.path.startswith(f"{AUTHENTICATED_MEDIA_PATH}/download/"):
            return self.downloaded_media(request)
        path = request.url.path.removeprefix("/_matrix/client/v3")
        match request.method, path.split("/")[1:]:
            case "GET", ["sync"]:
                since = request.url.params.get("since")
                return httpx.Response(200, json=self.syncs.get(since, batch(since or "s0")))
            case "GET", ["account", "whoami"]:
                return httpx.Response(200, json={"user_id": BOT})
            case ("GET", ["rooms", room, "joined_members"]) | ("POST", ["join", room]) if (
                room in self.forbidden
            ):
                return httpx.Response(403, json={"errcode": "M_FORBIDDEN"})
            case "GET", ["rooms", room, "joined_members"]:
                return httpx.Response(
                    200, json={"joined": {m: {} for m in self.members.get(room, [])}}
                )
            case "GET", ["rooms", room, "messages"]:
                start = request.url.params.get("from", "")
                return httpx.Response(200, json=self.history.get((room, start), {"chunk": []}))
            case "GET", ["profile", _user, "displayname"]:
                return httpx.Response(200, json={"displayname": self.display_name})
            case "PUT", ["rooms", room, "typing", user]:
                self.typing.append((room, user, json.loads(request.content)))
                return httpx.Response(200, json={})
            case "POST", ["rooms", room, "receipt", "m.read", event_id]:
                self.receipts.append((room, event_id))
                return httpx.Response(200, json={})
            case "PUT", ["rooms", room, "send", event_type, txn] if (
                event_type in self.refused_sends
            ):
                return httpx.Response(403, json={"errcode": "M_FORBIDDEN"})
            case "PUT", ["rooms", room, "send", event_type, txn]:
                if txn not in self.sent:
                    self.sent[txn] = {
                        "room": room,
                        "type": event_type,
                        "event_id": f"$sent{len(self.sent)}",
                        **json.loads(request.content),
                    }
                return httpx.Response(200, json={"event_id": self.sent[txn]["event_id"]})
            case "POST", ["join", room]:
                self.joined.append(room)
                return httpx.Response(200, json={"room_id": room})
            case "GET", ["rooms", room, "state", "m.room.encryption", ""]:
                if room in self.encrypted:
                    return httpx.Response(200, json=self.encrypted[room])
                return httpx.Response(404, json={"errcode": "M_NOT_FOUND"})
        return httpx.Response(404, json={"errcode": "M_UNRECOGNIZED"})

    def uploaded_media(self, request: httpx.Request) -> httpx.Response:
        filename = request.url.params.get("filename", "")
        if filename in self.refused_uploads:
            return httpx.Response(413, json={"errcode": "M_TOO_LARGE"})
        if filename in self.limited_uploads:
            return httpx.Response(429, json={"errcode": "M_LIMIT_EXCEEDED", "retry_after_ms": 1500})
        self.uploaded.append((filename, request.content))
        return httpx.Response(200, json={"content_uri": f"mxc://example.org/{media_id(filename)}"})

    def downloaded_media(self, request: httpx.Request) -> httpx.Response:
        """What `upload` stored, addressed the way an `mxc://` addresses it: one media id, under
        the server that answered the upload."""
        asked = request.url.path.rsplit("/", 1)[-1]
        for filename, content in self.uploaded:
            if media_id(filename) == asked:
                return httpx.Response(200, content=content)
        return httpx.Response(404, json={"errcode": "M_NOT_FOUND"})

    def sent_under(self, prefix: str) -> list[dict[str, Any]]:
        """Every message sent under a transaction id starting `prefix`, in the order it was sent."""
        return [sent for txn, sent in self.sent.items() if txn.startswith(prefix)]

    def edits(self) -> list[dict[str, Any]]:
        """Every event sent as an edit of another, in the order it was sent."""
        return [
            sent
            for sent in self.sent.values()
            if sent.get("m.relates_to", {}).get("rel_type") == "m.replace"
        ]

    def typing_said(self) -> list[bool]:
        """Whether each typing call said the bot was typing, in the order the calls were made."""
        return [bool(body["typing"]) for _room, _user, body in self.typing]

    def since_asked(self) -> list[str | None]:
        return [r.url.params.get("since") for r in self.requests if r.url.path.endswith("/sync")]


async def extension_engine() -> AsyncEngine:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    metadata = sa.MetaData()
    sa.Table("workspace", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    for table in (
        SINCE_TABLE,
        ANSWERING_TABLE,
        ASKING_TABLE,
        CLAIM_TABLE,
        LINK_TABLE,
        CRYPTO_TABLE,
        DELIVERED_TABLE,
    ):
        table.to_metadata(metadata)
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    return engine


@dataclass
class Blob:
    """The workspace's blob store as `attach` reads it. A key the store does not hold raises, the
    way a file whose bytes went missing does."""

    objects: dict[str, bytes] = field(default_factory=dict)

    async def get(self, key: str) -> bytes:
        if key not in self.objects:
            raise FileNotFoundError(key)
        return self.objects[key]


@dataclass
class Credentials:
    """The extension context's credential slots as the surface reads them. A slot no deploy filled
    raises, the way core raises for a slot nobody set."""

    values: dict[str, str]

    async def get(self, slot: str) -> str:
        if slot not in self.values:
            raise CredentialSlotUnset(slot)
        return self.values[slot]


@dataclass
class Workspace:
    """A surface context's reach into core, recorded."""

    engine: AsyncEngine
    workspace_id: UUID = field(default_factory=uuid4)
    credentials: dict[str, str] = field(
        default_factory=lambda: {HOMESERVER_SLOT: HOMESERVER, TOKEN_SLOT: TOKEN}
    )
    domain: str | None = "example.org"
    members: dict[str, UUID] = field(
        default_factory=lambda: {"alice@example.org": uuid4(), "bob@example.org": uuid4()}
    )
    linked: dict[str, UUID] = field(default_factory=dict)
    conversations: dict[str, tuple[UUID, str]] = field(default_factory=dict)
    wanted: bool = True
    asked: list[tuple[AmbientMessage, tuple[AmbientMessage, ...]]] = field(default_factory=list)
    admitted: list[dict[str, Any]] = field(default_factory=list)
    broken: set[str] = field(default_factory=set)
    lost: dict[str, BaseException] = field(default_factory=dict)
    blob: Blob = field(default_factory=Blob)
    portal: bool = True
    question: AskUserInput | None = None
    # Which question indexes are still open, None meaning all of them. Core keeps this record behind
    # `answerable_question` and closes an index when an answer lands; here a test sets it outright,
    # and an admitted answer closes nothing, so a redelivery is gated by the admission key alone.
    open_questions: frozenset[int] | None = None
    frames: tuple[LiveFrame, ...] = (Activity(text="reading the week"),)
    endless: bool = False
    tailed: list[UUID] = field(default_factory=list)
    read: list[str] = field(default_factory=list)
    refused: list[tuple[int, UUID]] = field(default_factory=list)

    async def credential(self, slot: str) -> str:
        if slot not in self.credentials:
            raise CredentialSlotUnset(slot)
        return self.credentials[slot]

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncConnection]:
        async with self.engine.begin() as connection:
            yield connection

    def home_url(self, fragment: str = "") -> str | None:
        return f"https://ufo.example.org/surface/web{fragment}"

    async def report_url(self, conversation_id: UUID, artifact: SharedArtifact) -> str | None:
        """The portal link core's rule allows: no portal offers none, and nor does a room the
        portal shows nobody — the gate `SurfaceContext.report_url` applies."""
        if not self.portal:
            return None
        audience = next(
            (audience for held, audience in self.conversations.values() if held == conversation_id),
            None,
        )
        if audience is None:
            return None
        parsed = parse_audience(audience)
        if parsed != SHARED_AUDIENCE and audience_member(parsed) is None:
            return None
        return self.home_url(f"#/c/{conversation_id}?report={artifact.id}")

    async def linked_member(self, external_id: str) -> UUID | None:
        return self.linked.get(external_id)

    async def workspace_domain(self) -> str | None:
        return self.domain

    async def link_member(self, external_id: str, email: str) -> UUID | None:
        member = self.members.get(email)
        if member is not None:
            self.linked[external_id] = member
        return member

    async def link_member_id(self, external_id: str, member_id: UUID) -> UUID | None:
        if member_id not in self.members.values():
            return None
        self.linked.setdefault(external_id, member_id)
        return member_id

    async def find_conversation(self, queue_key: str) -> UUID | None:
        found = self.conversations.get(queue_key)
        return None if found is None else found[0]

    async def conversation_for(self, queue_key: str, audience: Audience, **kwargs: object) -> UUID:
        found = self.conversations.get(queue_key)
        if found is None:
            self.conversations[queue_key] = (uuid4(), audience)
        else:
            self.conversations[queue_key] = (found[0], narrow_audience(found[1], audience))
        return self.conversations[queue_key][0]

    inbound_files: dict[str, bytes] = field(default_factory=dict)
    delivered: list[tuple[UUID, str, str]] = field(default_factory=list)
    workspace: dict[UUID, dict[str, str]] = field(default_factory=dict)
    member_files: list[tuple[UUID, tuple[str, ...]]] = field(default_factory=list)
    artifacts: set[tuple[UUID, str]] = field(default_factory=set)
    write_limit_bytes: int | None = None

    async def store_inbound_file(self, filename: str, chunks: AsyncIterator[bytes]) -> str:
        """The store's own bound refuses rather than truncating, which is what the surface has to
        survive: a file too large for the workspace costs that file, never the turn."""
        body = b"".join([chunk async for chunk in chunks])
        if self.write_limit_bytes is not None and len(body) > self.write_limit_bytes:
            raise ValueError("file exceeds the workspace write limit")
        key = f"artifact-{len(self.inbound_files)}"
        self.inbound_files[key] = body
        return key

    async def deliver_attachment(self, conversation_id: UUID, blob_key: str, rel: str) -> None:
        """A workspace path is a place, and core writes to it rather than beside it: the sandbox
        write this resolves to takes the last caller of a path and keeps nothing of the one."""
        self.delivered.append((conversation_id, blob_key, rel))
        self.workspace.setdefault(conversation_id, {})[rel] = blob_key

    async def list_workspace_files(self, conversation_id: UUID) -> tuple[WorkspaceFile, ...]:
        """What the conversation's workspace holds now, path-sorted, and empty for a conversation
        with no sandbox yet — core answers a conversation it does not own the same way."""
        return tuple(
            WorkspaceFile(
                path=rel,
                size_bytes=len(self.inbound_files.get(key, b"")),
                modified_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
            for rel, key in sorted(self.workspace.get(conversation_id, {}).items())
        )

    async def attach_member_files(
        self, turn_id: UUID, blob_keys: tuple[str, ...], *, member_id: UUID | None
    ) -> None:
        """Core inserts one shared-artifact row per key and conflicts on `(turn_id, blob_key)`,
        doing nothing on a repeat, so the same file attached twice to one turn is one row."""
        self.member_files.append((turn_id, tuple(blob_keys)))
        for blob_key in blob_keys:
            self.artifacts.add((turn_id, blob_key))

    async def ambient_reply_wanted(
        self, message: AmbientMessage, history: tuple[AmbientMessage, ...]
    ) -> bool:
        self.asked.append((message, history))
        return self.wanted

    async def admit(
        self,
        conversation_id: UUID,
        body: str,
        idempotency_key: str | None = None,
        context: object = None,
        *,
        speaker_member_id: UUID | None,
    ) -> Admitted:
        if idempotency_key in self.broken:
            raise ValueError("admission refused this event")
        if idempotency_key in self.lost:
            raise self.lost[idempotency_key]
        for prior in self.admitted:
            if prior["key"] == idempotency_key:
                return Admitted(turn_id=prior["turn_id"], opened_run=False)
        turn_id = uuid4()
        self.admitted.append(
            {
                "key": idempotency_key,
                "turn_id": turn_id,
                "conversation_id": conversation_id,
                "body": body,
                "context": context,
                "speaker": speaker_member_id,
            }
        )
        return Admitted(turn_id=turn_id, opened_run=True)

    async def latest_turn(self, conversation_id: UUID) -> UUID | None:
        """The last turn admitted to a conversation, which is the one that may hold a question."""
        for entry in reversed(self.admitted):
            if entry["conversation_id"] == conversation_id:
                return UUID(str(entry["turn_id"]))
        return None

    async def answerable_question(
        self, conversation_id: UUID, turn_id: UUID, question_index: int, member_id: UUID
    ) -> AskUserInput | None:
        """The ask this workspace holds, where that turn asked it, the index is one it left open,
        and the member is the one it was put to."""
        if self.question is None or await self.latest_turn(conversation_id) != turn_id:
            return None
        open_indexes = self.open_questions
        if open_indexes is None:
            open_indexes = frozenset(range(len(self.question.questions)))
        target = self.question.target_member_id
        if question_index not in open_indexes or target not in (None, member_id):
            self.refused.append((question_index, member_id))
            return None
        return self.question

    async def admitted_body(self, idempotency_key: str) -> str | None:
        for entry in self.admitted:
            if entry["key"] == idempotency_key:
                return str(entry["body"])
        return None

    @asynccontextmanager
    async def tail(self, turn_id: UUID, since: str = "") -> AsyncIterator[Any]:
        """One turn's frames, each recorded as it is read, so a reader that stops at the terminal
        frame is told apart from one the end of the script stopped."""
        self.tailed.append(turn_id)

        async def frames() -> AsyncIterator[tuple[str, LiveFrame]]:
            for cursor, frame in enumerate(self.frames):
                self.read.append(str(cursor))
                yield str(cursor), frame
            if self.endless:
                await asyncio.Event().wait()

        yield frames()


def terminal_frame(text_: str = "done") -> Terminal:
    return Terminal(frame=TerminalFrame(status="done", text=text_))


def said(result: object) -> str:
    """What a tool told the member: the text of the one block its result carries."""
    return result.content[0].text  # type: ignore[attr-defined]


@dataclass
class Listener:
    """The fleet gate: binds the bot's workspace when the bot is bound, and nothing otherwise."""

    workspace_ctx: Workspace
    bound: frozenset[str] = frozenset({BOT})
    surface: str = "matrix"
    owned: bool = True

    @asynccontextmanager
    async def workspace(self, installation_id: str) -> AsyncIterator[Workspace | None]:
        if not self.owned:
            raise RuntimeError("surface listener 'matrix' lost fleet ownership")
        yield self.workspace_ctx if installation_id in self.bound else None


async def settle() -> None:
    """End every reporter a test left running, so none of them outlives the database it reads."""
    running = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in running:
        task.cancel()
    await asyncio.gather(*running, return_exceptions=True)


def on_loop(test: Callable[..., Awaitable[None]]) -> Callable[..., None]:
    """Run an async test on its own loop with a fresh `Workspace` passed as `workspace`, so the
    suite needs no async pytest plugin and every engine lives and dies on the loop that made it."""
    params = [p for p in inspect.signature(test).parameters.values() if p.name != "workspace"]

    @functools.wraps(test)
    def run(*args: object, **kwargs: object) -> None:
        async def scenario() -> None:
            engine = await extension_engine()
            try:
                await test(*args, workspace=Workspace(engine=engine), **kwargs)
            finally:
                await settle()
                await engine.dispose()

        asyncio.run(scenario())

    run.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]
    return run
