"""The Matrix surface: a bot user's `/sync` stream in, and the room messages a turn becomes.

A room is a conversation, keyed by its room id; its speakers are members where one resolves. The
surface is durable and installation-routed — the installation id is the bot's MXID, bound to one
workspace — so ingress is the listener alone and no request is routed to it.

Most lines in a room are not addressed to the agent. A message from a member founds a turn when it
mentions the bot, when it is sent in a direct room (the bot and one other), or when the room already
holds a conversation and `ambient_reply_wanted` says the agent is wanted; anything else is heard,
kept as evidence for the next decision, and admits nothing. A sender who resolves to no member
founds nothing, however it addresses the bot; the one thing the bot reads from it is a code proving
its MXID for a member who claimed it (`linking.py`).

What a turn sends back is three handlers over one transport: `post` writes the terminal reply,
`attach` follows it with the turn's shared files, and `speak` delivers the words a turn marks before
it ends. Each relates its messages to the room message the turn answers, which admission recorded
(`answering.py`), so a reply lands as a reply and a threaded conversation stays in its thread."""

import asyncio
import math
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

import httpx
import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict

from ufo.sdk.audience import (
    Audience,
    foreign_room_audience,
    room_audience,
)
from ufo.sdk.credentials import deploy_env
from ufo.sdk.http import Request
from ufo.sdk.o11y import log, warn
from ufo.sdk.surfaces import (
    AMBIENT_CONTEXT_ELEMENT,
    AMBIENT_HISTORY_MESSAGES,
    ATTACHED_FILES_CLAUSE,
    NOTHING_DELIVERED,
    Admitted,
    AmbientMessage,
    AskUserInput,
    CredentialSlotUnset,
    MidTurnReply,
    NothingDelivered,
    SharedArtifact,
    SurfaceAuth,
    SurfaceContext,
    SurfaceDeliveryError,
    SurfaceInstallationConflict,
    SurfaceListenerContext,
    TurnContext,
    Writeback,
    fence_member_message,
    inbox_name,
    is_silence_sentinel,
    mint_marker,
    writeback_says_nothing,
)
from ufo.sdk.tools import ToolContext, ToolResult
from ufo_ext_matrix.addressed import Bot, addresses
from ufo_ext_matrix.answering import Answering, read_answering, write_answering
from ufo_ext_matrix.asking import Asking, read_asking, write_asking
from ufo_ext_matrix.client import MatrixClient, MatrixError
from ufo_ext_matrix.crypto import (
    UNOBSERVED,
    CryptoUnavailable,
    FileHashMismatch,
    device_for,
    inbound,
    open_file,
    outbound,
    seal_file,
)
from ufo_ext_matrix.delivered import read_delivered, write_delivered
from ufo_ext_matrix.events import (
    MESSAGE_TYPE,
    NOTICE_MSGTYPE,
    POLL_START_TYPE,
    SURFACE,
    TEXT_MSGTYPE,
    PollAnswer,
    RoomFile,
    RoomMessage,
    answer_txn_id,
    file_txn_id,
    gaps,
    invites,
    localpart,
    next_batch,
    part_txn_id,
    permalink,
    poll_answer,
    poll_txn_id,
    question_txn_id,
    room_file,
    room_key,
    room_message,
    room_names,
    say_txn_id,
    server_name,
    txn_id,
)
from ufo_ext_matrix.feedback import attend
from ufo_ext_matrix.linking import Linking, _said, code_in, proof_txn, unlinked
from ufo_ext_matrix.messages import (
    edit_content,
    encrypted_file_content,
    file_content,
    message_content,
    msgtype_for,
    parts,
    reply_relation,
)
from ufo_ext_matrix.questions import (
    Asked,
    Choice,
    answer_words,
    choices,
    poll_choices,
    poll_content,
    question_block,
    settled_block,
)
from ufo_ext_matrix.since import read_since, write_since

BOTS_KEY = "MATRIX_BOTS"
BOTS_ENV = f"UFO_{BOTS_KEY}"
HOMESERVER_SLOT = "matrix_homeserver"
TOKEN_SLOT = "matrix_access_token"
IDLE_SECONDS = 30.0
BACKOFF_SECONDS = (1.0, 2.0, 5.0, 15.0, 30.0, 60.0)
AMBIENT_CONTEXT_LINES = 10
ROSTER_LIMIT = 50
BACKFILL_PAGES = 5
CIPHERTEXT_TYPE = "application/octet-stream"
INBOUND_LIMIT_BYTES = 25 * 1024 * 1024
INBOUND_DIR = "uploads"
SPOKEN_MESSAGES = 50

FAILED_LINE = "This turn failed before it could answer."
CANCELLED_LINE = "This turn was stopped."
ELSEWHERE_LINE = "The next step happens in the workspace"
FILES_LINE = "This turn shared files, which are in the workspace"
REPORT_LINK_TEXT = "Open detailed report"
FILE_ROLE = "file"
DETAILS_ROLE = "details"


class FleetOwnershipLost(RuntimeError):
    """The fleet no longer owns this listener: the runner's to act on, so it ends the stream."""


class ConnectInput(BaseModel):
    """Nothing: the bot is whoever the workspace's access token belongs to. An action bound to an
    object takes exactly the fields it declares, so a call naming a bot of its own is refused."""

    model_config = ConfigDict(extra="forbid")


def installations(configured: str) -> tuple[str, ...]:
    """The bot MXIDs a deploy names, comma- or space-separated, in order and once each."""
    named = configured.replace(",", " ").split()
    return tuple(dict.fromkeys(named))


def reply_text(
    writeback: Writeback,
    workspace_url: str | None,
    reports: Sequence[str] = (),
    *,
    unlinked: bool = False,
) -> str:
    """The reply a terminal turn becomes. A failed turn says so in the surface's own words; a
    cancelled turn posts the reason core gave — an archived conversation, a revoked seat — or, with
    none, says it was stopped; a detailed write-up is a link under the words the turn wrote it
    under, or points at the workspace where the deploy offers no link; and what a room cannot carry
    — a connect or credential handoff, a shared file — points at the workspace. A question is not
    part of it: `post` sends the question as a message of its own, the one message an answer may
    rewrite."""
    terminal = writeback.terminal
    text = terminal.text.strip()
    if terminal.status == "failed":
        return FAILED_LINE
    if terminal.status == "cancelled":
        return text if text and not is_silence_sentinel(text) else CANCELLED_LINE
    said: list[str] = []
    if text and not is_silence_sentinel(text):
        said.append(text)
    said.extend(reports)
    where = f": {workspace_url}" if workspace_url else "."
    if terminal.connect_request is not None or terminal.credential_request is not None:
        said.append(ELSEWHERE_LINE + where)
    if any(artifact.role == FILE_ROLE for artifact in writeback.artifacts) or unlinked:
        said.append(FILES_LINE + where)
    return "\n\n".join(said)


def open_ask(writeback: Writeback) -> AskUserInput | None:
    """The question a finished turn leaves the room. A failed or cancelled turn asks nothing: its
    reply is the surface's own line, and no answer is waiting for it."""
    terminal = writeback.terminal
    return terminal.question if terminal.status == "done" else None


def asked_questions(question: AskUserInput) -> tuple[Asked, ...]:
    """The ask as a room renders it: each question's words, the options it offers under the labels
    the room answers by, and whether it takes more than one of them. A question core marked for words
    offers none of its options, since the member answers it by writing."""
    return tuple(
        Asked(
            question=asked.question,
            options=() if asked.free_text_only else tuple(o.label for o in asked.options or ()),
            multi_select=bool(asked.multi_select),
        )
        for asked in question.questions
    )


async def report_links(ctx: SurfaceContext, writeback: Writeback) -> tuple[str, ...]:
    """The detailed write-ups this reply links to, under the words the turn linked them with or the
    surface's own where it named none. A deploy whose portal shows nobody this conversation offers
    no link, and the write-up reaches the member through the workspace instead."""
    links: list[str] = []
    for artifact in writeback.artifacts:
        if artifact.role != DETAILS_ROLE:
            continue
        url = await ctx.report_url(writeback.conversation_id, artifact)
        if url is None:
            continue
        links.append(f"[{artifact.subject or REPORT_LINK_TEXT}]({url})")
    return tuple(links)


@asynccontextmanager
async def delivering() -> AsyncIterator[None]:
    """Every homeserver failure a writeback handler meets, as the delivery error core retries on: a
    rate limit carries the wait the homeserver itself asked for, and nothing repeats the body of the
    answer the homeserver sent. A room this bot cannot encrypt for is the same kind of refusal: the
    reply is not delivered rather than delivered in the clear."""
    try:
        yield
    except CryptoUnavailable as error:
        raise SurfaceDeliveryError(str(error)) from None
    except MatrixError as error:
        retry = error.retry_after_ms
        raise SurfaceDeliveryError(
            str(error), retry_after_seconds=None if retry is None else math.ceil(retry / 1000)
        ) from None
    except httpx.HTTPError as error:
        raise SurfaceDeliveryError(f"send failed: {type(error).__name__}") from None


@dataclass
class Heard:
    """One room line as the ambient decision reads it, and whether it already founded a turn — a
    line the transcript holds is not repeated in the next turn's room context."""

    line: AmbientMessage
    admitted: bool = False


def room_context(marker: str, heard: Sequence[Heard]) -> str:
    """The room lines the agent did not take part in, newest last, as the ambient element one
    admitted message carries ahead of its own words."""
    lines = [
        f"{h.line.speaker}: {h.line.text}" for h in heard if not h.line.own and not h.admitted
    ][-AMBIENT_CONTEXT_LINES:]
    if not lines:
        return ""
    element = f"{AMBIENT_CONTEXT_ELEMENT}_{marker}"
    return f"<{element}>\n" + "\n".join(lines) + f"\n</{element}>\n"


def audience_for(room_id: str, internal: bool) -> Audience:
    """Who a room's conversation may disclose to: the room, and workspace-shared memory with it, when
    everyone in it is a member; otherwise the room alone, sealed foreign. A direct room is a room
    like any other, so a room's audience only ever narrows — room to foreign when a non-member
    joins, and foreign for good after that."""
    key = room_key(room_id)
    return room_audience(SURFACE, key) if internal else foreign_room_audience(SURFACE, key)


def _refused(error: MatrixError) -> bool:
    """A refusal no retry changes — the bot was removed from the room, the invite was withdrawn.
    The batch goes on without that room rather than stalling the stream behind it; a rate limit or
    a server error raises, and the batch is read again."""
    return 400 <= error.status < 500 and error.status != 429 and not error.unauthorized


def _server(mxid: str) -> str | None:
    try:
        return server_name(mxid)
    except ValueError:
        return None


@dataclass
class Roster:
    """Who in one batch resolves to a member, each MXID asked once. A linked MXID is its member; on
    first contact an MXID on the workspace's own domain links to the member whose email is
    `localpart@domain` — the homeserver serving that domain vouches for its users as the mail
    server does for addresses. An MXID an admin unlinked, and any other, is nobody."""

    ctx: SurfaceContext
    known: dict[str, UUID | None] = field(default_factory=dict)
    _domain: list[str | None] = field(default_factory=list)

    async def domain(self) -> str | None:
        if not self._domain:
            self._domain.append(await self.ctx.workspace_domain())
        return self._domain[0]

    async def member(self, mxid: str) -> UUID | None:
        if mxid not in self.known:
            self.known[mxid] = await self._resolve(mxid)
        return self.known[mxid]

    async def internal(self, bot: str, joined: frozenset[str]) -> bool:
        """Whether everyone joined but the bot is a member. A workspace with no domain of its own,
        and a room past `ROSTER_LIMIT`, are foreign without asking."""
        others = sorted(joined - {bot})
        if len(others) > ROSTER_LIMIT or await self.domain() is None:
            return False
        for mxid in others:
            if await self.member(mxid) is None:
                return False
        return True

    async def _resolve(self, mxid: str) -> UUID | None:
        if await unlinked(self.ctx, mxid):
            return None
        linked = await self.ctx.linked_member(mxid)
        if linked is not None:
            return linked
        domain = await self.domain()
        if domain is None or _server(mxid) != domain:
            return None
        return await self.ctx.link_member(mxid, f"{localpart(mxid)}@{domain}")


async def refuse_requests(_request: Request, _auth: SurfaceAuth) -> None:
    """Matrix reaches this surface through its listener; no inbound request names a workspace."""
    return None


@dataclass
class MatrixSurface:
    """The surface's handlers over one HTTP transport. A deploy uses the default instance; a test
    builds one over a fake homeserver."""

    transport: httpx.AsyncBaseTransport | None = None
    bots: str | None = None
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    linking: Linking = field(default_factory=Linking)

    def listening(self) -> tuple[str, ...]:
        """The bot MXIDs this deploy's listener runs, from `MATRIX_BOTS`; a test names them
        directly."""
        named = self.bots if self.bots is not None else (deploy_env(BOTS_KEY) or "")
        return installations(named)

    def client(self, homeserver: str, access_token: str) -> MatrixClient:
        return MatrixClient(homeserver, access_token, transport=self.transport)

    async def listen(self, listener: SurfaceListenerContext) -> None:
        bots = self.listening()
        if not bots:
            log("matrix.no_installations", env=BOTS_ENV)
            await asyncio.Event().wait()
        await asyncio.gather(*(Installation(self, listener, bot).run() for bot in bots))

    async def slots(self, ctx: SurfaceContext) -> tuple[str, str]:
        """The workspace's homeserver and its bot's token. An empty slot is a delivery failure:
        a member fills it in chat, and the writeback waits for them."""
        try:
            return await ctx.credential(HOMESERVER_SLOT), await ctx.credential(TOKEN_SLOT)
        except CredentialSlotUnset as unset:
            raise SurfaceDeliveryError(f"matrix slot {unset.args[0]} is empty") from None

    async def post(self, ctx: SurfaceContext, writeback: Writeback) -> str | NothingDelivered:
        """The turn's answer as the room reads it: the words, the same words as HTML, and a relation
        to the message the turn answers. A reply longer than one event carries is written in parts,
        and the first part's event id is the reference core records — what `attach` hangs the turn's
        files under, whatever else the reply became. A question follows the reply as a message of
        its own, and is the reference where the turn said nothing else."""
        if writeback_says_nothing(writeback):
            return NOTHING_DELIVERED
        links = await report_links(ctx, writeback)
        details = sum(1 for artifact in writeback.artifacts if artifact.role == DETAILS_ROLE)
        body = reply_text(writeback, ctx.home_url(), links, unlinked=details > len(links))
        homeserver, token = await self.slots(ctx)
        answering = await read_answering(ctx, writeback.turn_id)
        async with delivering():
            async with self.client(homeserver, token) as client:
                reference = None
                if body:
                    reference = await self.say(
                        ctx,
                        client,
                        writeback.queue_key,
                        txn_id(writeback.turn_id),
                        body,
                        answering,
                    )
                asked = await self.ask(ctx, client, writeback, answering)
                await self.poll(ctx, client, writeback, answering)
                reference = reference or asked
                if reference is None:
                    return NOTHING_DELIVERED
                return reference

    async def ask(
        self,
        ctx: SurfaceContext,
        client: MatrixClient,
        writeback: Writeback,
        answering: Answering | None,
    ) -> str | None:
        """The turn's question as a numbered list, sent apart from the reply and recorded as the one
        message the answer rewrites. A question too long for one event is sent in parts and recorded
        nowhere, since a rewrite of its first part would leave the rest standing unmarked."""
        question = open_ask(writeback)
        if question is None:
            return None
        block = question_block(question.title, asked_questions(question))
        sent = await self.say(
            ctx, client, writeback.queue_key, question_txn_id(writeback.turn_id), block, answering
        )
        if len(parts(block)) == 1:
            await write_asking(ctx, writeback.turn_id, Asking(writeback.queue_key, sent))
        return sent

    async def poll(
        self,
        ctx: SurfaceContext,
        client: MatrixClient,
        writeback: Writeback,
        answering: Answering | None,
    ) -> None:
        """A tappable form of the question the reply wrote out, beside those words rather than
        instead of them: a client that draws no poll reads the numbered list, and a tap and a typed
        number arrive as the one choice. One poll carries one single-select question, so an ask of
        several, or one that takes several answers, is the numbered list alone."""
        question = open_ask(writeback)
        if question is None:
            return
        asked = asked_questions(question)
        if len(asked) != 1 or not asked[0].options or asked[0].multi_select:
            return
        relation = (
            None if answering is None else reply_relation(answering.event_id, answering.thread_root)
        )
        await self.send(
            ctx,
            client,
            writeback.queue_key,
            poll_txn_id(writeback.turn_id),
            poll_content(question.title, asked[0], relation),
            event_type=POLL_START_TYPE,
        )

    async def attach(self, ctx: SurfaceContext, writeback: Writeback, reply_ref: str) -> None:
        """The turn's shared files, each its own message under the reply core recorded. Every file
        is best effort: a refusal the homeserver will not take back is logged and the rest go on,
        since a room that carries three of four files says more than one that carries none — but a
        failure a retry can fix, a rate limit or a lost database among them, raises, so the file is
        not discarded to a transient outage. Delivery repeats after a crash, and a file already
        sent is sent under the transaction id it was sent under before."""
        # A detailed write-up is not a shared file: it reaches the member as the link the
        # reply carries, so only FILE_ROLE artifacts become messages of their own.
        files = tuple(a for a in writeback.artifacts if a.role == FILE_ROLE)
        if not files:
            return
        homeserver, token = await self.slots(ctx)
        answering = await read_answering(ctx, writeback.turn_id)
        thread = answering.thread_root if answering is not None else None
        relation = reply_relation(reply_ref, thread)
        async with self.client(homeserver, token) as client:
            for artifact in files:
                try:
                    await self.hand_over(ctx, client, writeback, artifact, relation)
                except MatrixError as error:
                    if error.retry_after_ms is not None or error.status >= 500:
                        retry = error.retry_after_ms
                        raise SurfaceDeliveryError(
                            str(error),
                            retry_after_seconds=(
                                None if retry is None else math.ceil(retry / 1000)
                            ),
                        ) from None
                    warn(
                        "matrix.file_undelivered",
                        media_type=artifact.media_type,
                        error_class=type(error).__name__,
                        http_status=error.status,
                    )
                except sa.exc.SQLAlchemyError:
                    raise
                except httpx.HTTPError as error:
                    raise SurfaceDeliveryError(f"send failed: {type(error).__name__}") from None
                except Exception as error:
                    warn(
                        "matrix.file_undelivered",
                        media_type=artifact.media_type,
                        error_class=type(error).__name__,
                    )

    async def hand_over(
        self,
        ctx: SurfaceContext,
        client: MatrixClient,
        writeback: Writeback,
        artifact: SharedArtifact,
        relation: Mapping[str, Any],
    ) -> None:
        """One shared file into the room: its bytes to the media repository, then the message that
        carries the `mxc://` the repository answered. A repeated upload leaves an unreferenced
        object behind, which the media repository is welcome to.

        In an encrypted room the bytes are sealed before they are uploaded and the key travels
        inside the Megolm payload, so the media repository holds ciphertext and the timeline names
        no url that opens it.

        The room's state is read once here and handed to `send`. The file and the message naming it
        are two events, and reading a mutable remote value twice can disagree — a room that turns
        encryption on between them would seal the message over bytes already uploaded in the clear,
        which is the failure sealing the bytes exists to prevent."""
        room_id = writeback.queue_key
        settings = await client.encryption(room_id)
        data = await ctx.blob.get(artifact.blob_key)
        msgtype = msgtype_for(artifact.media_type)
        if settings is None:
            url = await client.upload(artifact.filename, artifact.media_type, data)
            content = file_content(
                msgtype,
                artifact.filename,
                artifact.subject,
                artifact.media_type,
                artifact.size_bytes,
                url,
                relation,
            )
        else:
            ciphertext, sealed = seal_file(data)
            url = await client.upload(artifact.filename, CIPHERTEXT_TYPE, ciphertext)
            content = encrypted_file_content(
                msgtype,
                artifact.filename,
                artifact.subject,
                artifact.media_type,
                artifact.size_bytes,
                {**sealed, "url": url},
                relation,
            )
        await self.send(
            ctx,
            client,
            room_id,
            file_txn_id(writeback.turn_id, artifact.id),
            content,
            settings=settings,
        )

    async def speak(self, ctx: SurfaceContext, reply: MidTurnReply) -> str:
        """One reply delivered while the turn is still running, under the same relation its terminal
        reply will carry. A notice core sends because a member commented from another surface is the
        room's own aside rather than the agent's words, so the room reads it as one."""
        homeserver, token = await self.slots(ctx)
        answering = await read_answering(ctx, reply.turn_id)
        msgtype = NOTICE_MSGTYPE if reply.is_comment else TEXT_MSGTYPE
        async with delivering():
            async with self.client(homeserver, token) as client:
                return await self.say(
                    ctx,
                    client,
                    reply.queue_key,
                    say_txn_id(reply.id),
                    reply.text,
                    answering,
                    msgtype=msgtype,
                )

    async def send(
        self,
        ctx: SurfaceContext,
        client: MatrixClient,
        room_id: str,
        txn: str,
        content: Mapping[str, Any],
        event_type: str = MESSAGE_TYPE,
        settings: Any = UNOBSERVED,
    ) -> str:
        """One event into the room: itself in a plain room, Megolm ciphertext in an encrypted one.
        Every event this surface sends leaves through here, so no delivery path puts a room's own
        words on the wire in the clear because it did not think to ask whether the room is
        encrypted. A room the bot has no device keys for raises `CryptoUnavailable` rather than
        falling back to cleartext.

        `settings` carries a room state a caller has already read, so a file and the message naming
        it are decided by one observation rather than two."""
        sealed, event = await outbound(ctx, client, room_id, event_type, content, settings)
        return await client.send_event(room_id, sealed, txn, event)

    async def say(
        self,
        ctx: SurfaceContext,
        client: MatrixClient,
        room_id: str,
        base: str,
        body: str,
        answering: Answering | None,
        msgtype: str = TEXT_MSGTYPE,
    ) -> str:
        """One message, or the parts of one too long to be one, in order. Every part relates to the
        same message, so a client threading the reply threads all of it, and the first part's event
        id is the reference the caller returns."""
        relation = (
            None if answering is None else reply_relation(answering.event_id, answering.thread_root)
        )
        sent: list[str] = []
        for part, text in enumerate(parts(body), 1):
            sent.append(
                await self.send(
                    ctx,
                    client,
                    room_id,
                    part_txn_id(base, part),
                    message_content(text, msgtype, relation),
                )
            )
        return sent[0]

    async def connect(self, ctx: ToolContext, _args: ConnectInput) -> ToolResult:
        """Bind the workspace's bot to this workspace: ask the homeserver whose token the slot
        holds, and make that MXID the installation the listener routes by."""
        ext = ctx.ext
        if ext is None:
            raise RuntimeError("matrix_connect dispatched without its ExtensionContext")
        try:
            homeserver = await ext.credentials.get(HOMESERVER_SLOT)
            token = await ext.credentials.get(TOKEN_SLOT)
        except CredentialSlotUnset as unset:
            return _said(f"The {unset.args[0]} credential is empty. Fill it, then connect again.")
        try:
            async with self.client(homeserver, token) as client:
                bot = await client.whoami()
        except MatrixError as error:
            return _said(f"The homeserver refused the bot's token: {error}.", error=True)
        except httpx.HTTPError as error:
            return _said(f"The homeserver did not answer: {type(error).__name__}.", error=True)
        try:
            await ext.installations.bind(SURFACE, bot)
        except SurfaceInstallationConflict:
            return _said(f"{bot} is already connected to another workspace.", error=True)
        listed = bot in self.listening()
        tail = (
            "It is listening." if listed else f"It listens once the deploy's {BOTS_ENV} names it."
        )
        return _said(f"Connected {bot}. {tail}")


@dataclass
class Installation:
    """One bot's stream: sync, deliver, record where it stands, repeat. What it has heard in each
    room lives in the process and is evidence only — the durable facts are the stored `since` and
    the turns admission recorded under each event id."""

    surface: MatrixSurface
    listener: SurfaceListenerContext
    bot: str
    heard: dict[str, deque[Heard]] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    spoken: dict[str, deque[str]] = field(default_factory=dict)
    attending: set[asyncio.Task[None]] = field(default_factory=set)
    display_name: str | None = None

    async def run(self) -> None:
        """Sync until cancelled. A failure backs this bot off and leaves every other bot running;
        only `FleetOwnershipLost` ends the stream, since that is the runner's to act on. A stream
        that ends stops reporting the turns it opened, so no room is left typing for a bot that is
        no longer listening."""
        failures = 0
        try:
            while True:
                try:
                    wait = await self.step()
                    failures = 0
                except FleetOwnershipLost:
                    raise
                except Exception as error:
                    wait = self._backoff(error, failures)
                    failures += 1
                    warn(
                        "matrix.sync_failed",
                        installation=self.bot,
                        error_class=type(error).__name__,
                        http_status=getattr(error, "status", None),
                        errcode=getattr(error, "errcode", None),
                    )
                if wait:
                    await self.surface.sleep(wait)
        finally:
            await self.stop_attending()

    async def stop_attending(self) -> None:
        """End every reporter this stream started."""
        reporters = tuple(self.attending)
        for task in reporters:
            task.cancel()
        await asyncio.gather(*reporters, return_exceptions=True)

    def report(self, admitted: Admitted, room_id: str, event_id: str) -> None:
        """Report one turn's progress to its room, where this admission is the one that opened the
        turn's run. One reporter per run, so a redelivery that joins a run doubles no indicator."""
        if not admitted.opened_run:
            return
        task = asyncio.ensure_future(self.reporter(admitted.turn_id, room_id, event_id))
        self.attending.add(task)
        task.add_done_callback(self.attending.discard)

    async def reporter(self, turn_id: UUID, room_id: str, event_id: str) -> None:
        """One turn's progress in its room for as long as the turn runs. The reporter holds a
        workspace scope of its own, since it outlives the batch that started it, and it answers for
        nothing: every failure here costs the room an indicator and the turn nothing."""
        try:
            async with self.listener.workspace(self.bot) as ctx:
                if ctx is None:
                    return
                homeserver = await ctx.credential(HOMESERVER_SLOT)
                token = await ctx.credential(TOKEN_SLOT)
                async with self.surface.client(homeserver, token) as client:
                    async with ctx.tail(turn_id) as frames:
                        await attend(client, room_id, self.bot, event_id, frames)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            warn("matrix.feedback_ended", installation=self.bot, error_class=type(error).__name__)

    def named(self, room_id: str) -> Bot:
        """Who the bot is in one room, as the addressed decision reads it."""
        return Bot(
            mxid=self.bot,
            display_name=self.display_name or "",
            said=frozenset(self.spoken.get(room_id, ())),
        )

    async def _display_name(self, client: MatrixClient) -> str:
        """The name the bot goes by, asked once a stream: a member who types it addresses the bot as
        surely as one who mentions it. A profile the homeserver will not answer for names nothing,
        and a mention, a pill, and the MXID still address the bot."""
        try:
            return await client.display_name(self.bot)
        except (MatrixError, httpx.HTTPError) as error:
            log(
                "matrix.display_name_unknown",
                installation=self.bot,
                error_class=type(error).__name__,
            )
            return ""

    def _backoff(self, error: Exception, failures: int) -> float:
        if isinstance(error, MatrixError):
            if error.retry_after_ms is not None:
                return error.retry_after_ms / 1000
            if error.unauthorized:
                return IDLE_SECONDS
        return BACKOFF_SECONDS[min(failures, len(BACKOFF_SECONDS) - 1)]

    @asynccontextmanager
    async def _bound(self) -> AsyncIterator[SurfaceContext | None]:
        """The listener gate for this bot. The gate reports lost ownership as a bare `RuntimeError`
        on entry; only that entry is read as `FleetOwnershipLost`, so a `RuntimeError` from the body
        backs this bot off like any other failure."""
        async with AsyncExitStack() as stack:
            try:
                ctx = await stack.enter_async_context(self.listener.workspace(self.bot))
            except RuntimeError as error:
                raise FleetOwnershipLost(str(error)) from error
            yield ctx

    async def step(self) -> float:
        """One sync round, returning how long to wait before the next. An unbound bot or an empty
        slot waits and asks again, since both are fixed by a member in chat, not by a restart. The
        first round of a stream only fixes where it stands: history from before the bot was
        listening founds nothing."""
        async with self._bound() as ctx:
            if ctx is None:
                log("matrix.unbound", installation=self.bot)
                return IDLE_SECONDS
            try:
                homeserver = await ctx.credential(HOMESERVER_SLOT)
                token = await ctx.credential(TOKEN_SLOT)
            except CredentialSlotUnset as unset:
                log("matrix.slot_empty", installation=self.bot, slot=unset.args[0])
                return IDLE_SECONDS
            since = await read_since(ctx, self.bot)
        async with self.surface.client(homeserver, token) as client:
            batch = await client.sync(since)
            async with self._bound() as ctx:
                if ctx is None:
                    return IDLE_SECONDS
                await self.deliver(ctx, client, batch, since)
                await write_since(ctx, self.bot, next_batch(batch))
        return 0.0

    async def fetched(
        self, client: MatrixClient, shared: RoomFile, limit: int = INBOUND_LIMIT_BYTES
    ) -> bytes | None:
        """The bytes a member's file names, or None for a file this room does not deliver.

        A dropped file is named in a log and costs that file. It never raises into the stream: a
        member's attachment must not stop their room being read, which is the failure a bound that
        propagates turns into an outage.

        The bound is checked twice, and the first check is not the one that protects anything. A
        sender writes `info.size` themselves, so the declared size is a courtesy that saves a
        download; the fetched length is the one that decides. A file arriving larger than it
        declared is exactly the case the cheap check misses.

        In an encrypted room the ciphertext is checked against the file's own sha256 before
        anything is decrypted, inside `open_file` — so bytes the media repository substituted are
        refused rather than opened."""
        if shared.size_bytes > limit:
            log("matrix.file_declined", installation=self.bot, declared=shared.size_bytes)
            return None
        try:
            body = await client.download(shared.url or (shared.sealed or {}).get("url", ""))
        except MatrixError as error:
            log("matrix.file_unfetched", installation=self.bot, http_status=error.status)
            return None
        if len(body) > limit:
            log("matrix.file_oversize", installation=self.bot, fetched=len(body))
            return None
        if shared.sealed is None:
            return body
        try:
            return open_file(shared.sealed, body)
        except FileHashMismatch as mismatch:
            log("matrix.file_unsealed", installation=self.bot, reason=str(mismatch))
            return None
        except CryptoUnavailable:
            log("matrix.file_sealed_unreadable", installation=self.bot)
            return None

    async def landed(
        self,
        ctx: SurfaceContext,
        client: MatrixClient,
        conversation_id: UUID,
        shared: RoomFile,
    ) -> tuple[tuple[str, str], ...]:
        """Where a member's file landed, as the workspace path the turn reads it by and the key the
        artifact row points at — or nothing at all, for a file that did not land.

        Nothing here raises. A file the room could not deliver is a turn that answers the member's
        words without it, which is the same turn they would have got had they sent no file; a file
        that stopped the turn would cost them the words too.

        The write bound is the store's and it refuses rather than truncating, so an oversized file
        is caught here rather than reaching the sync loop."""
        landed = await read_delivered(ctx, shared.event_id)
        if landed is not None:
            # The batch is replayed with the same event ids when a crash falls between delivering it
            # and writing the `/sync` position, so the file half needs the key `admit` already
            # carries. Answering from the row rather than re-fetching is what keeps the member to one
            # copy; answering with the *recorded* artifact key is what keeps the turn to one row,
            # since core's attachment insert conflicts on `(turn_id, blob_key)` and a freshly minted
            # key would conflict with nothing.
            return (landed,)

        body = await self.fetched(client, shared)
        if body is None:
            return ()

        async def one() -> AsyncIterator[bytes]:
            yield body

        try:
            key = await ctx.store_inbound_file(shared.filename, one())
        except ValueError as refused:
            log("matrix.file_unstored", installation=self.bot, reason=str(refused))
            return ()
        # The name is the sender's to choose, so it is a name and not a path: core's own sanitiser
        # drops path components, collapses what is not a word character, and falls back for a name
        # that is empty or nothing but dots. What it numbers against is the set it is handed, so the
        # set is the directory's own contents rather than an empty one — a second `chart.png` lands
        # beside the first as `chart-1.png`, whether it arrived in the same batch, an hour later, or
        # from the other member in the room. Reading the directory rather than remembering it is
        # what makes that hold across a restart.
        listed = await ctx.list_workspace_files(conversation_id)
        used = {
            PurePosixPath(found.path).name
            for found in listed
            if PurePosixPath(found.path).parent == PurePosixPath(INBOUND_DIR)
        }
        rel = f"{INBOUND_DIR}/{inbox_name(shared.filename, used)}"
        try:
            await ctx.deliver_attachment(conversation_id, key, rel)
        except ValueError as refused:
            log("matrix.file_undelivered", installation=self.bot, reason=str(refused))
            return ()
        # Written after the delivery core accepted, so a row stands for a file on disk rather than
        # one that was about to be. A crash inside that gap replays as it did before; a crash
        # anywhere else in the batch no longer costs the member a second copy.
        await write_delivered(ctx, shared.event_id, rel, key)
        return ((rel, key),)

    async def deliver(
        self,
        ctx: SurfaceContext,
        client: MatrixClient,
        batch: Mapping[str, Any],
        since: str | None,
    ) -> None:
        """Hear every message in the batch and admit the ones that found a turn. A message that
        fails is logged and skipped, so one bad event never holds the stream; a homeserver error
        or any database failure raises, and the batch is read again. A database failure reaches
        here through the session `write_since` then writes through, so that session's state is not
        knowably intact whatever caused it: a failure that recurs parks the stream and names itself
        every cycle, rather than dropping a member's message and advancing the position past it."""
        admitting = since is not None
        roster = Roster(ctx)
        self.names.update(room_names(batch))
        for room_id, inviter in invites(batch, self.bot):
            await self._invited(roster, client, room_id, inviter)
        earlier = await self._backfill(client, batch, since) if since is not None else {}
        if admitting and self.display_name is None:
            self.display_name = await self._display_name(client)
        device = await device_for(ctx, client)
        joined: dict[str, frozenset[str]] = {}
        for room_id, event in await inbound(device, batch, earlier, admitting):
            message = room_message(room_id, event)
            shared = None if message is not None else room_file(room_id, event)
            if shared is not None:
                # A shared file is the message it came as: its caption or its name is what the
                # member said, and everything downstream — ambient history, membership, the proof
                # path — reads it as any other line rather than as a second kind of event.
                message = RoomMessage(
                    room_id=shared.room_id,
                    event_id=shared.event_id,
                    sender=shared.sender,
                    body=shared.caption,
                    formatted_body="",
                    mentions=shared.mentions,
                    thread_root=shared.thread_root,
                    replying_to=shared.replying_to,
                )
            if message is None:
                answer = poll_answer(room_id, event)
                if admitting and answer is not None and answer.sender != self.bot:
                    await self._guarded(self.tapped(ctx, roster, client, answer))
                continue
            own = message.sender == self.bot
            if own:
                self.spoken.setdefault(room_id, deque(maxlen=SPOKEN_MESSAGES)).append(
                    message.event_id
                )
            heard = self.heard.setdefault(room_id, deque(maxlen=AMBIENT_HISTORY_MESSAGES))
            prior = tuple(heard)
            entry = Heard(AmbientMessage(speaker=message.sender, text=message.body, own=own))
            heard.append(entry)
            if own or not admitting:
                continue
            if room_id not in joined:
                try:
                    joined[room_id] = await client.joined_members(room_id)
                except MatrixError as error:
                    if not _refused(error):
                        raise
                    log("matrix.room_unreadable", installation=self.bot, http_status=error.status)
                    joined[room_id] = frozenset()
            if not joined[room_id]:
                continue
            try:
                proved = await self._proved(ctx, client, roster, message, joined[room_id])
            except sa.exc.SQLAlchemyError:
                raise
            except Exception as error:
                # A code whose proof failed is skipped, never admitted as the words it is.
                warn(
                    "matrix.message_skipped",
                    installation=self.bot,
                    error_class=type(error).__name__,
                )
                continue
            if proved:
                heard.pop()
                continue
            entry.admitted = await self._guarded(
                self.heed(ctx, roster, client, message, prior, joined[room_id], shared)
            )

    async def _guarded(
        self, admission: Awaitable[bool], event: str = "matrix.message_skipped"
    ) -> bool:
        """One event's admission, where a failure costs that event alone: it is logged by error class
        and the stream moves on. A database failure is not one — the batch is read again instead."""
        try:
            return await admission
        except sa.exc.SQLAlchemyError:
            raise
        except Exception as error:
            warn(event, installation=self.bot, error_class=type(error).__name__)
            return False

    async def heed(
        self,
        ctx: SurfaceContext,
        roster: Roster,
        client: MatrixClient,
        message: RoomMessage,
        prior: Sequence[Heard],
        joined: frozenset[str],
        shared: RoomFile | None = None,
    ) -> bool:
        """One member message, as the answer it names to a question the room still holds or as the
        message it is. A reply of labels answers; a reply of words is words, which is how a member
        answers a question that asked for words and how they say anything else.

        An answer that fails leaves the message to be admitted as the words it is, under the same
        key: a member whose choice the surface could not settle is still heard."""
        answered = self._guarded(
            self.answered(ctx, roster, client, message), "matrix.answer_skipped"
        )
        return await answered or await self.consider(
            ctx, roster, message, prior, joined, client, shared
        )

    async def _backfill(
        self, client: MatrixClient, batch: Mapping[str, Any], since: str
    ) -> dict[str, list[Mapping[str, Any]]]:
        """The messages each cut-short room held between `since` and its timeline, oldest first,
        walked back at most `BACKFILL_PAGES` pages. A room the bot can no longer read is left
        with its timeline alone."""
        earlier: dict[str, list[Mapping[str, Any]]] = {}
        for room_id, start in gaps(batch).items():
            found: list[Mapping[str, Any]] = []
            page: str | None = start
            for _ in range(BACKFILL_PAGES):
                if page is None:
                    break
                try:
                    events, page = await client.messages_before(room_id, page, since)
                except MatrixError as error:
                    if not _refused(error):
                        raise
                    log("matrix.backfill_refused", installation=self.bot, http_status=error.status)
                    break
                found.extend(events)
            if page is not None:
                log("matrix.backfill_bounded", installation=self.bot, pages=BACKFILL_PAGES)
            earlier[room_id] = found[::-1]
        return earlier

    async def _invited(
        self, roster: Roster, client: MatrixClient, room_id: str, inviter: str
    ) -> None:
        """Join a room a workspace member invited the bot to, or one an MXID a member is proving
        invited it to; an invitation from anyone else is left standing."""
        claimed = self.surface.linking.live_claim
        if await roster.member(inviter) is None and not await claimed(roster.ctx, inviter):
            log("matrix.invite_left", installation=self.bot, inviter_server=_server(inviter))
            return
        try:
            await client.join(room_id)
        except MatrixError as error:
            if not _refused(error):
                raise
            log("matrix.join_refused", installation=self.bot, http_status=error.status)

    async def _proved(
        self,
        ctx: SurfaceContext,
        client: MatrixClient,
        roster: Roster,
        message: RoomMessage,
        joined: frozenset[str],
    ) -> bool:
        """Whether a direct message was a code proving its sender's MXID. It is answered in the
        room and founds no turn."""
        if joined != {self.bot, message.sender} or code_in(message.body) is None:
            return False
        proof = await self.surface.linking.prove(ctx, message, roster.member)
        if proof is None:
            return False
        if proof.member_id is not None:
            roster.known[message.sender] = proof.member_id
        log("matrix.proof_read", installation=self.bot, linked=proof.member_id is not None)
        try:
            await self.surface.send(
                ctx,
                client,
                message.room_id,
                proof_txn(message.event_id),
                message_content(proof.reply, TEXT_MSGTYPE),
            )
        except CryptoUnavailable as error:
            warn("matrix.proof_answer_unsent", error_class=type(error).__name__)
        return True

    async def consider(
        self,
        ctx: SurfaceContext,
        roster: Roster,
        message: RoomMessage,
        prior: Sequence[Heard],
        joined: frozenset[str],
        client: MatrixClient | None = None,
        shared: RoomFile | None = None,
    ) -> bool:
        """Admit one message or decline it, returning whether it founded or joined a turn. A sender
        who is no member is declined before anything is spent on the line. An admitted message is
        recorded as the one its turn answers, since the writeback that answers it names the turn and
        the room but not the event, and every message the turn sends back relates to this one."""
        addressed = (len(joined) == 2 and self.bot in joined) or addresses(
            message, self.named(message.room_id)
        )
        if not addressed and await ctx.find_conversation(message.room_id) is None:
            return False
        member_id = await roster.member(message.sender)
        if member_id is None:
            log("matrix.speaker_unresolved", installation=self.bot)
            return False
        if not addressed:
            asked = AmbientMessage(speaker=message.sender, text=message.body)
            if not await ctx.ambient_reply_wanted(asked, tuple(h.line for h in prior)):
                log("matrix.ambient_declined", installation=self.bot)
                return False
        audience = audience_for(message.room_id, await roster.internal(self.bot, joined))
        conversation_id = await ctx.conversation_for(
            message.room_id, audience, label=self.names.get(message.room_id)
        )
        marker = mint_marker()
        landed = (
            await self.landed(ctx, client, conversation_id, shared)
            if shared is not None and client is not None
            else ()
        )
        paths = tuple(rel for rel, _ in landed)
        admitted = await ctx.admit(
            conversation_id,
            fence_member_message(
                marker,
                room_context(marker, prior),
                message.body,
                ATTACHED_FILES_CLAUSE + ", ".join(paths) if paths else "",
            ),
            idempotency_key=message.event_id,
            context=TurnContext(
                sender=message.sender, source=permalink(message.room_id, message.event_id)
            ),
            speaker_member_id=member_id,
        )
        if landed:
            await ctx.attach_member_files(
                admitted.turn_id, tuple(key for _, key in landed), member_id=member_id
            )
        try:
            await write_answering(
                ctx,
                admitted.turn_id,
                Answering(
                    room_id=message.room_id,
                    event_id=message.event_id,
                    thread_root=message.thread_root,
                ),
            )
        except sa.exc.SQLAlchemyError:
            raise
        except Exception as error:
            # The turn stands without the record: the reply then relates to nothing, and the line
            # is not fed back as room context for a turn it founded.
            warn("matrix.answering_unrecorded", error_class=type(error).__name__)
        self.report(admitted, message.room_id, message.event_id)
        return True

    async def answered(
        self, ctx: SurfaceContext, roster: Roster, client: MatrixClient, message: RoomMessage
    ) -> bool:
        """Admit one reply as the answer it names, returning whether it answered anything. A reply
        naming no label of an open question answers nothing, and the message is admitted as the words
        it is."""
        opened = await self.open_question(ctx, roster, message.room_id, message.sender)
        if opened is None:
            return False
        chosen = await self.gated(ctx, opened, choices(message.body, opened.asked))
        if not chosen:
            return False
        answering = Answering(
            room_id=message.room_id, event_id=message.event_id, thread_root=message.thread_root
        )
        body = await self.answer(ctx, opened, answering, message.sender, chosen)
        await self.settled(ctx, client, opened, answering, body, chosen)
        return True

    async def tapped(
        self, ctx: SurfaceContext, roster: Roster, client: MatrixClient, answer: PollAnswer
    ) -> bool:
        """Admit one tap on a poll as the option it chose. The poll's answer ids are the question's
        own labels, so a response to somebody else's poll names none of them and chooses nothing."""
        opened = await self.open_question(ctx, roster, answer.room_id, answer.sender)
        if opened is None:
            return False
        chosen = await self.gated(ctx, opened, poll_choices(answer.answers, opened.asked))
        if not chosen:
            return False
        answering = Answering(room_id=answer.room_id, event_id=answer.event_id)
        body = await self.answer(ctx, opened, answering, answer.sender, chosen)
        await self.settled(ctx, client, opened, answering, body, chosen)
        return True

    async def open_question(
        self, ctx: SurfaceContext, roster: Roster, room_id: str, sender: str
    ) -> "Open | None":
        """The question a room's last turn left open to one sender, or None where it left none.
        `answerable_question` is the whole gate: a turn that asked nothing, a question already
        answered, and one naming another member each answer None, and the surface asks it again for
        every question index a reply names."""
        conversation_id = await ctx.find_conversation(room_id)
        if conversation_id is None:
            return None
        turn_id = await ctx.latest_turn(conversation_id)
        if turn_id is None:
            return None
        member_id = await roster.member(sender)
        if member_id is None:
            return None
        question = await ctx.answerable_question(conversation_id, turn_id, 0, member_id)
        if question is None:
            return None
        return Open(
            conversation_id=conversation_id,
            turn_id=turn_id,
            member_id=member_id,
            question=question,
            asked=asked_questions(question),
        )

    async def gated(
        self, ctx: SurfaceContext, opened: "Open", named: Sequence[Choice]
    ) -> tuple[Choice, ...]:
        """The choices this member may still make, each asked of `answerable_question` under its own
        question index. A question past the ask's own range, one already answered, and one another
        member was asked are refused there and nowhere here."""
        kept: list[Choice] = []
        for choice in named:
            question = await ctx.answerable_question(
                opened.conversation_id, opened.turn_id, choice.question, opened.member_id
            )
            if question is None:
                log("matrix.answer_refused", installation=self.bot)
                continue
            kept.append(choice)
        return tuple(kept)

    async def answer(
        self,
        ctx: SurfaceContext,
        opened: "Open",
        answering: Answering,
        sender: str,
        chosen: Sequence[Choice],
    ) -> str:
        """Admit the options one member chose as the words they said, keyed by the event that carried
        them, and return those words. The answer founds its own turn, whose reply belongs under the
        answer rather than under the question, so the message that turn answers is this event."""
        marker = mint_marker()
        body = fence_member_message(marker, "", answer_words(opened.asked, chosen), "")
        admitted = await ctx.admit(
            opened.conversation_id,
            body,
            idempotency_key=answering.event_id,
            context=TurnContext(
                sender=sender, source=permalink(answering.room_id, answering.event_id)
            ),
            speaker_member_id=opened.member_id,
        )
        await write_answering(ctx, admitted.turn_id, answering)
        self.report(admitted, answering.room_id, answering.event_id)
        return body

    async def settled(
        self,
        ctx: SurfaceContext,
        client: MatrixClient,
        opened: "Open",
        answering: Answering,
        body: str,
        chosen: Sequence[Choice],
    ) -> None:
        """Rewrite the question the member answered, so the room shows which answer landed. The one
        message rewritten is the one `post` recorded as carrying this turn's question — never the
        message the answer happened to reply to, since the bot's other messages are words the room
        still reads — and only with the words this event actually admitted: `admitted_body` answers
        what the key admitted, and a key holding other words belongs to a delivery that got there
        first. The rewrite is sent under a transaction id of the answering event, so a redelivered
        answer rewrites the message it already rewrote, and a homeserver that refuses it costs the
        room a mark and the turn nothing."""
        asked_in = await read_asking(ctx, opened.turn_id)
        if asked_in is None or asked_in.room_id != answering.room_id:
            return
        if await ctx.admitted_body(answering.event_id) != body:
            log("matrix.answer_raced", installation=self.bot)
            return
        content = edit_content(
            settled_block(opened.question.title, opened.asked, chosen), asked_in.event_id
        )
        try:
            await self.surface.send(
                ctx, client, answering.room_id, answer_txn_id(answering.event_id), content
            )
        except (MatrixError, httpx.HTTPError, CryptoUnavailable) as error:
            warn("matrix.answer_unmarked", installation=self.bot, error_class=type(error).__name__)


@dataclass(frozen=True)
class Open:
    """The question a room still holds, as the member who may answer it meets it: the conversation
    and turn that carry it, the member `answerable_question` opened it to, and the ask itself."""

    conversation_id: UUID
    turn_id: UUID
    member_id: UUID
    question: AskUserInput
    asked: tuple[Asked, ...]
