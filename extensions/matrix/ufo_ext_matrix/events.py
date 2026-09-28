"""What a `/sync` batch says, read without the runtime: which events are a member's words, which
rooms invite the bot, and the identifiers the surface derives from them.

Nothing here imports `ufo` or an HTTP client, so the contract tests hold these rules on a checkout
with neither installed. A message founds a turn only if it passes `room_message`; everything the
surface does with it after that is `surface.py`'s."""

import hashlib
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote
from uuid import UUID

SURFACE = "matrix"
MESSAGE_TYPE = "m.room.message"
POLL_START_TYPE = "m.poll.start"
POLL_RESPONSE_TYPE = "m.poll.response"
ENCRYPTED_TYPE = "m.room.encrypted"
TEXT_MSGTYPE = "m.text"
FILE_MSGTYPES = frozenset({"m.file", "m.image", "m.audio", "m.video"})
NOTICE_MSGTYPE = "m.notice"
REPLACE_RELATION = "m.replace"
REFERENCE_RELATION = "m.reference"
THREAD_RELATION = "m.thread"
ROOM_KEY_CHARS = 32
SYNC_TIMELINE_LIMIT = 50
SYNC_FILTER = json.dumps(
    {
        "presence": {"types": []},
        "account_data": {"types": []},
        "room": {
            "timeline": {"limit": SYNC_TIMELINE_LIMIT},
            "ephemeral": {"types": []},
            "account_data": {"types": []},
            "state": {"lazy_load_members": True},
        },
    },
    separators=(",", ":"),
)
BACKFILL_FILTER = json.dumps({"types": [MESSAGE_TYPE, ENCRYPTED_TYPE]}, separators=(",", ":"))


@dataclass(frozen=True)
class RoomMessage:
    """One plain-text message a member sent into a room: the only event shape that may found a
    turn. `mentions` is the MXIDs the sender's client says it addressed, `thread_root` the thread
    the message belongs to — None for a message sent to the room itself — and `replying_to` the
    message it answers, which is how a member points at the question they mean."""

    room_id: str
    event_id: str
    sender: str
    body: str
    formatted_body: str
    mentions: frozenset[str]
    thread_root: str | None = None
    replying_to: str | None = None


@dataclass(frozen=True)
class PollAnswer:
    """One member's tap on a poll: the poll it answers and the answer ids their client chose. A
    poll answers a question the bot asked, so the ids are that question's own labels."""

    room_id: str
    event_id: str
    sender: str
    poll_id: str
    answers: tuple[str, ...]


def server_name(mxid: str) -> str:
    """The homeserver half of a user or room id — everything after the first colon."""
    _, separator, server = mxid.partition(":")
    if not separator or not server:
        raise ValueError(f"not a Matrix identifier: {mxid!r}")
    return server


def localpart(mxid: str) -> str:
    return mxid.partition(":")[0].removeprefix("@")


def room_key(room_id: str) -> str:
    """The colon-free name a room audience carries. A room id is `!opaque:server`, and an audience
    component may not hold a colon, so the room is named by a digest of its id instead."""
    return hashlib.sha256(room_id.encode()).hexdigest()[:ROOM_KEY_CHARS]


def txn_id(turn_id: UUID) -> str:
    """The transaction id a reply is sent under. One turn has one terminal reply, so a retried post
    for the same turn reaches the homeserver as the same transaction and lands once."""
    return f"ufo-{turn_id}"


def part_txn_id(base: str, part: int) -> str:
    """The transaction id the parts after the first of one reply are sent under, so a reply the
    homeserver already holds in full is not doubled a part at a time."""
    return base if part == 1 else f"{base}-{part}"


def file_txn_id(turn_id: UUID, artifact_id: UUID) -> str:
    """The transaction id one shared file is sent under. Delivery repeats `attach` after a crash,
    and a file already sent for this turn reaches the homeserver as the transaction it already
    answered."""
    return f"ufo-file-{turn_id}-{artifact_id}"


def say_txn_id(reply_id: UUID) -> str:
    """The transaction id one mid-turn reply is sent under, named by the row core hands over, so a
    re-handed row posts once."""
    return f"ufo-say-{reply_id}"


def answer_txn_id(event_id: str) -> str:
    """The transaction id the rewritten question is sent under, named by the answering event, so a
    redelivered answer rewrites the message it already rewrote."""
    return f"ufo-answered-{event_id}"


def question_txn_id(turn_id: UUID) -> str:
    """The transaction id one turn's question is sent under, apart from its reply. A turn asks once,
    so a repeated delivery reaches the homeserver as the transaction it already answered and gets
    back the event id the question already carries."""
    return f"ufo-question-{turn_id}"


def poll_txn_id(turn_id: UUID) -> str:
    """The transaction id one turn's poll is sent under. A turn asks once, so a repeated delivery
    reaches the homeserver as the transaction it already answered."""
    return f"ufo-poll-{turn_id}"


def permalink(room_id: str, event_id: str) -> str:
    return f"https://matrix.to/#/{quote(room_id, safe='!:')}/{quote(event_id, safe='$:')}"


@dataclass(frozen=True)
class RoomFile:
    """One file a member sent into a room.

    `caption` is what the member said and `filename` is what the file is called. A file sent with
    words carries both — the caption in `body` and the name in `filename` — and one sent bare
    carries its name in `body` alone, so a caption that exists is never the name and the name is
    never lost.

    `sealed` is the `EncryptedFile` an encrypted room carries a file by, and `url` the `mxc://` a
    plain one names. Exactly one of them is set: a message offering both is refused rather than
    resolved, because the two disagree about whether the bytes are sealed and picking either lets a
    sender decide which."""

    room_id: str
    event_id: str
    sender: str
    filename: str
    media_type: str
    size_bytes: int
    caption: str = ""
    url: str = ""
    sealed: Mapping[str, Any] | None = None
    mentions: frozenset[str] = frozenset()
    thread_root: str | None = None
    replying_to: str | None = None


def room_file(room_id: str, event: Mapping[str, Any]) -> RoomFile | None:
    """The event as a file a member shared, or None when it is not one.

    The same refusals `room_message` makes — a redaction, an edit, a state event — and then a
    msgtype naming a file rather than words. In an encrypted room this reads the event the device
    already decrypted, so a sealed file and a plain one arrive here the same shape apart from which
    of `file` and `url` they carry."""
    if event.get("type") != MESSAGE_TYPE or event.get("state_key") is not None:
        return None
    unsigned = event.get("unsigned")
    if isinstance(unsigned, Mapping) and "redacted_because" in unsigned:
        return None
    content = event.get("content")
    if not isinstance(content, Mapping) or content.get("msgtype") not in FILE_MSGTYPES:
        return None
    relates = content.get("m.relates_to")
    if isinstance(relates, Mapping) and relates.get("rel_type") == REPLACE_RELATION:
        return None
    if "m.new_content" in content:
        return None
    event_id, sender = event.get("event_id"), event.get("sender")
    if not isinstance(event_id, str) or not isinstance(sender, str):
        return None
    url, sealed = content.get("url"), content.get("file")
    plain = isinstance(url, str) and bool(url)
    encrypted = isinstance(sealed, Mapping)
    if plain == encrypted:
        return None
    body = content.get("body")
    named = content.get("filename")
    filename = named if isinstance(named, str) and named else body
    if not isinstance(filename, str) or not filename.strip():
        return None
    info = content.get("info")
    info = info if isinstance(info, Mapping) else {}
    media_type = info.get("mimetype")
    size = info.get("size")
    return RoomFile(
        room_id=room_id,
        event_id=event_id,
        sender=sender,
        filename=filename,
        caption=body if isinstance(body, str) else filename,
        media_type=media_type if isinstance(media_type, str) else "",
        size_bytes=size if isinstance(size, int) and size >= 0 else 0,
        url=url if plain else "",
        sealed=sealed if encrypted else None,
        mentions=_mentions(content),
        thread_root=_thread_root(content),
        replying_to=_replying_to(content),
    )


def room_message(room_id: str, event: Mapping[str, Any]) -> RoomMessage | None:
    """The event as a member's words, or None when it can found no turn: any type but
    `m.room.message` (an encrypted event is `m.room.encrypted`, a redaction `m.room.redaction`), a
    msgtype other than `m.text` (a bot's `m.notice` among them), an edit, a redacted message, or one
    with no text."""
    if event.get("type") != MESSAGE_TYPE or event.get("state_key") is not None:
        return None
    unsigned = event.get("unsigned")
    if isinstance(unsigned, Mapping) and "redacted_because" in unsigned:
        return None
    content = event.get("content")
    if not isinstance(content, Mapping) or content.get("msgtype") != TEXT_MSGTYPE:
        return None
    relates = content.get("m.relates_to")
    if isinstance(relates, Mapping) and relates.get("rel_type") == REPLACE_RELATION:
        return None
    if "m.new_content" in content:
        return None
    body = content.get("body")
    event_id = event.get("event_id")
    sender = event.get("sender")
    if not isinstance(body, str) or not body.strip():
        return None
    if not isinstance(event_id, str) or not isinstance(sender, str):
        return None
    formatted = content.get("formatted_body")
    return RoomMessage(
        room_id=room_id,
        event_id=event_id,
        sender=sender,
        body=body,
        formatted_body=formatted if isinstance(formatted, str) else "",
        mentions=_mentions(content),
        thread_root=_thread_root(content),
        replying_to=_replying_to(content),
    )


def poll_answer(room_id: str, event: Mapping[str, Any]) -> PollAnswer | None:
    """The event as a member's tap on a poll, or None when it is not one: any type but
    `m.poll.response`, a response relating to nothing, and one naming no answer."""
    if event.get("type") != POLL_RESPONSE_TYPE or event.get("state_key") is not None:
        return None
    content = event.get("content")
    event_id = event.get("event_id")
    sender = event.get("sender")
    if not isinstance(content, Mapping) or not isinstance(event_id, str):
        return None
    if not isinstance(sender, str):
        return None
    relates = content.get("m.relates_to")
    response = content.get(POLL_RESPONSE_TYPE)
    if not isinstance(relates, Mapping) or relates.get("rel_type") != REFERENCE_RELATION:
        return None
    poll_id = relates.get("event_id")
    if not isinstance(poll_id, str) or not isinstance(response, Mapping):
        return None
    chosen = response.get("answers")
    answers = tuple(a for a in chosen if isinstance(a, str)) if isinstance(chosen, list) else ()
    if not answers:
        return None
    return PollAnswer(
        room_id=room_id, event_id=event_id, sender=sender, poll_id=poll_id, answers=answers
    )


def _thread_root(content: Mapping[str, Any]) -> str | None:
    """The thread a message was sent in, read from its own relation: a room conversation is a
    thread exactly where the message that founds a turn is in one, and the reply that answers it
    belongs under the same root."""
    relates = content.get("m.relates_to")
    if not isinstance(relates, Mapping) or relates.get("rel_type") != THREAD_RELATION:
        return None
    root = relates.get("event_id")
    return root if isinstance(root, str) and root else None


def _replying_to(content: Mapping[str, Any]) -> str | None:
    """The message this one answers, from its own relation: a rich reply names it directly, and a
    threaded message names it beside the thread it falls back from."""
    relates = content.get("m.relates_to")
    if not isinstance(relates, Mapping):
        return None
    replied = relates.get("m.in_reply_to")
    if not isinstance(replied, Mapping):
        return None
    event_id = replied.get("event_id")
    return event_id if isinstance(event_id, str) and event_id else None


def _mentions(content: Mapping[str, Any]) -> frozenset[str]:
    mentions = content.get("m.mentions")
    if not isinstance(mentions, Mapping):
        return frozenset()
    users = mentions.get("user_ids")
    if not isinstance(users, list):
        return frozenset()
    return frozenset(user for user in users if isinstance(user, str))


def timeline(
    batch: Mapping[str, Any], earlier: Mapping[str, Sequence[Mapping[str, Any]]] | None = None
) -> Iterator[tuple[str, Mapping[str, Any]]]:
    """Every timeline event of every joined room, in the order the homeserver sent them. `earlier`
    is what a room's gap was filled with, oldest first; it reads ahead of the room's timeline, and
    an event the timeline also carries reads once."""
    for room_id, room in _joined(batch).items():
        events = [e for e in room.get("timeline", {}).get("events", []) if isinstance(e, Mapping)]
        seen = {e.get("event_id") for e in events}
        filled = [e for e in (earlier or {}).get(room_id, ()) if e.get("event_id") not in seen]
        for event in (*filled, *events):
            yield room_id, event


def gaps(batch: Mapping[str, Any]) -> dict[str, str]:
    """Each room whose timeline the homeserver cut short (`limited`), with the `prev_batch` token
    its missing events end at."""
    cut: dict[str, str] = {}
    for room_id, room in _joined(batch).items():
        section = room.get("timeline", {})
        prev = section.get("prev_batch")
        if section.get("limited") is True and isinstance(prev, str) and prev:
            cut[room_id] = prev
    return cut


def room_names(batch: Mapping[str, Any]) -> dict[str, str]:
    """The name each room carries as of this batch, where the batch names one."""
    names: dict[str, str] = {}
    for room_id, room in _joined(batch).items():
        for section in ("state", "timeline"):
            for event in room.get(section, {}).get("events", []):
                if not isinstance(event, Mapping) or event.get("type") != "m.room.name":
                    continue
                name = event.get("content", {}).get("name")
                if isinstance(name, str) and name.strip():
                    names[room_id] = name.strip()
    return names


def invites(batch: Mapping[str, Any], bot: str) -> Iterator[tuple[str, str]]:
    """Each room the bot is invited to in this batch, with the MXID that invited it."""
    rooms = batch.get("rooms", {}).get("invite", {})
    if not isinstance(rooms, Mapping):
        return
    for room_id, room in rooms.items():
        for event in room.get("invite_state", {}).get("events", []):
            if (
                isinstance(event, Mapping)
                and event.get("type") == "m.room.member"
                and event.get("state_key") == bot
                and event.get("content", {}).get("membership") == "invite"
                and isinstance(event.get("sender"), str)
            ):
                yield room_id, event["sender"]
                break


def next_batch(batch: Mapping[str, Any]) -> str:
    token = batch.get("next_batch")
    if not isinstance(token, str) or not token:
        raise ValueError("sync response carries no next_batch")
    return token


def _joined(batch: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
    joined = batch.get("rooms", {}).get("join", {})
    return joined if isinstance(joined, Mapping) else {}


MXC_SCHEME = "mxc://"
MEDIA_ID = re.compile(r"\A[A-Za-z0-9_-]+\Z")
SERVER_NAME = re.compile(r"\A[A-Za-z0-9._\-\[\]:]+\Z")


def media_parts(uri: str) -> tuple[str, str] | None:
    """The server and media id an `mxc://` names, or None for anything that is not one.

    A file's location arrives inside an event, so it is a claim by whoever sent it rather than a
    value this surface chose. Both parts are matched against what they are allowed to be rather
    than searched for what they are not: a ban lists the shapes someone has thought of, and `..` is
    a path of its own while containing no character a ban would name."""
    if not uri.startswith(MXC_SCHEME):
        return None
    server, slash, media_id = uri[len(MXC_SCHEME) :].partition("/")
    if not slash or not SERVER_NAME.match(server) or not MEDIA_ID.match(media_id):
        return None
    if not any(character.isalnum() for character in server):
        return None
    return server, media_id
