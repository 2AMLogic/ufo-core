"""Encryption against a real homeserver: an encrypted round trip between the bot and a second
registered account, and the four answers only a homeserver can give.

`test_matrix_crypto.py` runs the same Olm and Megolm against the fake transport, and keeps the cases
a container reaches at no advantage, such as a device that changes its keys between two queries.
What it cannot answer is the homeserver's own conduct:

| Point | What the homeserver decides |
| --- | --- |
| To-device ordering | Which batch carries a room key, and whether the event it opens is in it |
| One-time key accounting | What the count of a pool it drew on says, and when it is reported |
| Fallback keys | That an emptied pool is served by the fallback key, and served again |
| `device_lists` | That a member's new device is announced to a room encrypted with them |

The module is collected only where `MATRIX_INTEGRATION_HOMESERVER` names a homeserver (see
`conftest.py`), and it needs the `matrix-e2ee` extra. It registers its own throwaway users and
creates its own encrypted room, so the target must allow registration — a private Synapse container
does. The caller, the loop runner and the writeback shape are `test_matrix_integration.py`'s; what
this module adds is a member's device that speaks Olm and Megolm over the real client-server API."""

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import pytest

pytest.importorskip("ufo", reason="install ufo from git to run the integration tests")
pytest.importorskip("vodozemac", reason="install the matrix-e2ee extra to run the crypto tests")
pytest.importorskip("cryptography", reason="install the matrix-e2ee extra to run the crypto tests")

import vodozemac as vz  # noqa: E402
from crypto_fakes import MEGOLM, OLM, STORE_KEY, canonical, padded, unpadded  # noqa: E402
from matrix_fakes import Workspace  # noqa: E402
from test_matrix_integration import Api, on_loop, workspace_ctx, writeback  # noqa: E402
from ufo_ext_matrix import crypto  # noqa: E402
from ufo_ext_matrix.client import MatrixClient  # noqa: E402
from ufo_ext_matrix.crypto import (  # noqa: E402
    ROOM_KEY,
    SIGNED_KEY,
    STORE_KEY_SLOT,
    device_for,
    inbound,
)
from ufo_ext_matrix.events import ENCRYPTED_TYPE, MESSAGE_TYPE, next_batch  # noqa: E402
from ufo_ext_matrix.surface import MatrixSurface  # noqa: E402

HOMESERVER = os.environ["MATRIX_INTEGRATION_HOMESERVER"]
CLIENT = "/_matrix/client/v3"
# The pool a device offers and offers again, which `crypto.Device` takes from its own account: half
# of what the account will hold, so the homeserver is asked to keep a pool it can always be given.
TARGET = vz.Account().max_number_of_one_time_keys // 2


class Keys(Api):
    """The calls a device makes that the delivery suite's caller has no need of: who a token is and
    which device it is bound to, the keys the device publishes, the claim that opens an Olm session
    with another, a to-device send, the sync a device reads its own to-device queue from, and an
    encrypted event into a room."""

    async def whoami(self) -> tuple[str, str]:
        answer = (await self._http.get(f"{CLIENT}/account/whoami")).json()
        return answer["user_id"], answer["device_id"]

    async def login(self, localpart: str, password: str) -> tuple[str, str]:
        """A second session for a user who already holds one, which is how a member gains a second
        device: the access token and the device id the homeserver mints for it."""
        answer = (
            await self._http.post(
                f"{CLIENT}/login",
                json={
                    "type": "m.login.password",
                    "identifier": {"type": "m.id.user", "user": localpart},
                    "password": password,
                },
            )
        ).json()
        return answer["access_token"], answer["device_id"]

    async def create_encrypted_room(self, invite: tuple[str, ...] = ()) -> str:
        answer = (
            await self._http.post(
                f"{CLIENT}/createRoom",
                json={
                    "visibility": "private",
                    "invite": list(invite),
                    "initial_state": [
                        {
                            "type": "m.room.encryption",
                            "state_key": "",
                            "content": {"algorithm": MEGOLM},
                        }
                    ],
                },
            )
        ).json()
        return answer["room_id"]

    async def upload(self, body: dict[str, Any]) -> dict[str, Any]:
        return (await self._http.post(f"{CLIENT}/keys/upload", json=body)).json()

    async def query(self, user: str) -> dict[str, Any]:
        answer = (
            await self._http.post(f"{CLIENT}/keys/query", json={"device_keys": {user: []}})
        ).json()
        return answer["device_keys"].get(user, {})

    async def claim(self, user: str, device_id: str) -> dict[str, Any] | None:
        """One signed key the homeserver hands out for another device, or None where it has none
        left to hand out. A fallback key names itself in what it hands back."""
        answer = (
            await self._http.post(
                f"{CLIENT}/keys/claim",
                json={"one_time_keys": {user: {device_id: SIGNED_KEY}}},
            )
        ).json()
        offered = (answer["one_time_keys"].get(user) or {}).get(device_id) or {}
        return next(iter(offered.values()), None)

    async def to_device(self, event_type: str, messages: dict[str, Any]) -> None:
        await self._http.put(
            f"{CLIENT}/sendToDevice/{event_type}/it-{uuid4().hex}", json={"messages": messages}
        )

    async def to_device_events(self, since: str | None) -> tuple[list[dict[str, Any]], str]:
        params = {"timeout": "0" if since is None else "5000"}
        if since is not None:
            params["since"] = since
        answer = (await self._http.get(f"{CLIENT}/sync", params=params)).json()
        section = answer.get("to_device") or {}
        return list(section.get("events") or []), answer["next_batch"]

    async def send_encrypted(self, room_id: str, content: dict[str, Any]) -> str:
        answer = (
            await self._http.put(
                f"{CLIENT}/rooms/{quote(room_id, safe='')}/send/{ENCRYPTED_TYPE}/it-{uuid4().hex}",
                json=content,
            )
        ).json()
        return answer["event_id"]


@dataclass
class Peer:
    """One of the member's devices, over its own vodozemac account and its own access token.
    `crypto_fakes.Peer` is this device against the fake transport; here every key it publishes and
    claims, every to-device message it sends, and every event it puts in the room goes over the
    client-server API, and `claimed` holds what the homeserver handed out for the bot's device."""

    keys: Keys
    user: str
    device_id: str
    bot: str
    bot_device: str
    account: vz.Account = field(default_factory=vz.Account)
    olm: dict[str, list[vz.Session]] = field(default_factory=dict)
    outbound: dict[str, vz.GroupSession] = field(default_factory=dict)
    inbound: dict[str, vz.InboundGroupSession] = field(default_factory=dict)
    claimed: list[dict[str, Any]] = field(default_factory=list)
    since: str | None = None

    @property
    def curve(self) -> str:
        return self.account.curve25519_key.to_base64()

    @property
    def ed(self) -> str:
        return self.account.ed25519_key.to_base64()

    def signed(self, value: dict[str, Any]) -> dict[str, Any]:
        signature = self.account.sign(canonical(value)).to_base64()
        return {**value, "signatures": {self.user: {f"ed25519:{self.device_id}": signature}}}

    async def publish(self, count: int = 5) -> None:
        self.account.generate_one_time_keys(count)
        device_keys = self.signed(
            {
                "user_id": self.user,
                "device_id": self.device_id,
                "algorithms": [OLM, MEGOLM],
                "keys": {
                    f"curve25519:{self.device_id}": self.curve,
                    f"ed25519:{self.device_id}": self.ed,
                },
            }
        )
        await self.keys.upload(
            {
                "device_keys": device_keys,
                "one_time_keys": {
                    f"{SIGNED_KEY}:{key_id}": self.signed({"key": key.to_base64()})
                    for key_id, key in self.account.one_time_keys.items()
                },
            }
        )
        self.account.mark_keys_as_published()

    async def bot_keys(self) -> tuple[str, str]:
        shown = (await self.keys.query(self.bot))[self.bot_device]["keys"]
        return shown[f"curve25519:{self.bot_device}"], shown[f"ed25519:{self.bot_device}"]

    async def to_bot(self, event_type: str, content: dict[str, Any]) -> None:
        """Olm-encrypt one to-device event to the bot's device, opening a session with whatever key
        the homeserver hands out the first time."""
        curve, ed = await self.bot_keys()
        if curve not in self.olm:
            offered = await self.keys.claim(self.bot, self.bot_device)
            assert offered is not None, "the homeserver handed out no key for the bot's device"
            self.claimed.append(offered)
            self.olm[curve] = [
                self.account.create_outbound_session(
                    vz.Curve25519PublicKey.from_base64(curve),
                    vz.Curve25519PublicKey.from_base64(offered["key"]),
                )
            ]
        payload = {
            "type": event_type,
            "content": content,
            "sender": self.user,
            "sender_device": self.device_id,
            "keys": {"ed25519": self.ed},
            "recipient": self.bot,
            "recipient_keys": {"ed25519": ed},
        }
        kind, body = self.olm[curve][0].encrypt(canonical(payload)).to_parts()
        await self.keys.to_device(
            ENCRYPTED_TYPE,
            {
                self.bot: {
                    self.bot_device: {
                        "algorithm": OLM,
                        "sender_key": self.curve,
                        "ciphertext": {curve: {"type": kind, "body": unpadded(body)}},
                    }
                }
            },
        )

    def session(self, room_id: str) -> vz.GroupSession:
        if room_id not in self.outbound:
            self.outbound[room_id] = vz.GroupSession()
        return self.outbound[room_id]

    async def share(self, room_id: str) -> None:
        """Share this device's Megolm session for the room with the bot, as a client does before its
        first message there."""
        session = self.session(room_id)
        await self.to_bot(
            ROOM_KEY,
            {
                "algorithm": MEGOLM,
                "room_id": room_id,
                "session_id": session.session_id,
                "session_key": session.session_key.to_base64(),
            },
        )

    async def say(self, room_id: str, body: str) -> str:
        """One Megolm-encrypted `m.text` into the real room, and the event id it landed under."""
        session = self.session(room_id)
        plain = {
            "type": MESSAGE_TYPE,
            "content": {"msgtype": "m.text", "body": body},
            "room_id": room_id,
        }
        return await self.keys.send_encrypted(
            room_id,
            {
                "algorithm": MEGOLM,
                "sender_key": self.curve,
                "device_id": self.device_id,
                "session_id": session.session_id,
                "ciphertext": session.encrypt(canonical(plain)).to_base64(),
            },
        )

    async def read_to_device(self) -> list[dict[str, Any]]:
        """Decrypt every to-device event the homeserver has for this device, keep the room keys they
        carry, and return the plaintext payloads."""
        events, self.since = await self.keys.to_device_events(self.since)
        payloads = []
        for event in events:
            if event["type"] != ENCRYPTED_TYPE:
                payloads.append(event)
                continue
            ours = event["content"]["ciphertext"][self.curve]
            message = vz.AnyOlmMessage.from_parts(ours["type"], padded(ours["body"]))
            pre_key = message.to_pre_key()
            sessions = self.olm.setdefault(event["content"]["sender_key"], [])
            for session in sessions:
                if pre_key is None or session.session_matches(pre_key):
                    plaintext = session.decrypt(message)
                    break
            else:
                assert pre_key is not None
                session, plaintext = self.account.create_inbound_session(
                    vz.Curve25519PublicKey.from_base64(event["content"]["sender_key"]), pre_key
                )
                sessions.insert(0, session)
            payload = json.loads(plaintext)
            assert payload["recipient"] == self.user
            assert payload["recipient_keys"]["ed25519"] == self.ed
            if payload["type"] == ROOM_KEY:
                key = payload["content"]
                self.inbound[key["session_id"]] = vz.InboundGroupSession(
                    vz.SessionKey(key["session_key"])
                )
            payloads.append(payload)
        return payloads

    def decrypt(self, content: dict[str, Any]) -> dict[str, Any]:
        """The plaintext payload of a Megolm event the bot sent."""
        session = self.inbound[content["session_id"]]
        decrypted = session.decrypt(vz.MegolmMessage.from_base64(content["ciphertext"]))
        return json.loads(decrypted.plaintext)


@dataclass
class Bot:
    """The bot's stream, held where `Installation.step` holds it: sync the homeserver, open the
    device, hear the batch through `inbound`, keep the position. `batches` is what the homeserver
    said, so a test reads its accounting out of the answers themselves."""

    ctx: Workspace
    token: str
    since: str | None = None
    batches: list[dict[str, Any]] = field(default_factory=list)

    async def hear(self) -> list[tuple[str, Any]]:
        """One round: what the batch carried, as the surface hears it. The first round of a stream
        admits nothing — it only fixes where the stream stands — and it is where a device with no
        account yet mints one and publishes its keys."""
        async with MatrixClient(HOMESERVER, self.token) as client:
            batch = await client.sync(self.since)
            device = await device_for(self.ctx, client)  # type: ignore[arg-type]
            assert device is not None
            heard = await inbound(device, batch, {}, self.since is not None)
        self.batches.append(dict(batch))
        self.since = next_batch(batch)
        return heard

    async def until(self, body: str, rounds: int = 4) -> list[tuple[str, Any]]:
        """Everything heard up to and including the batch that carried this message. A room key and
        the event it opens are the homeserver's to order, and either arrangement ends here: in one
        batch the event is decrypted where it stands, and across two it is parked and heard once the
        key lands."""
        heard: list[tuple[str, Any]] = []
        for _ in range(rounds):
            heard += await self.hear()
            if body in bodies(heard):
                break
        return heard

    def counts(self) -> list[int | None]:
        """What each batch said the homeserver holds of this device's one-time keys."""
        return [
            (batch.get("device_one_time_keys_count") or {}).get(SIGNED_KEY)
            for batch in self.batches
        ]

    def changed(self) -> list[str]:
        """Every user the last batch named as having changed their devices."""
        return list((self.batches[-1].get("device_lists") or {}).get("changed") or [])

    def fallback_unused(self) -> list[list[str] | None]:
        """Which of this device's fallback keys each batch said the homeserver still has to hand
        out — an empty list where the one it held has been handed out already."""
        return [batch.get("device_unused_fallback_key_types") for batch in self.batches]


def bodies(heard: list[tuple[str, Any]]) -> list[str]:
    """The message bodies in what was heard, ciphertext being no message at all."""
    return [
        event["content"]["body"] for _room, event in heard if event.get("type") == MESSAGE_TYPE
    ]


async def drain(keys: Keys, user: str, device_id: str) -> list[dict[str, Any]]:
    """Claim keys for a device until the homeserver hands out its fallback key, which is what it
    reaches for once the pool is empty. The claims are the one-time keys it had left and the
    fallback key behind them, in the order they were handed out."""
    claimed = []
    for _ in range(2 * TARGET + 2):
        offered = await keys.claim(user, device_id)
        assert offered is not None, "the homeserver ran out of keys and offered no fallback"
        claimed.append(offered)
        if offered.get("fallback"):
            return claimed
    raise AssertionError("the homeserver handed out one-time keys past the pool a device publishes")


def bot_ctx(engine: Any, token: str) -> Workspace:
    """The surface context fake holding this bot's real credentials, and the key its crypto store is
    sealed under."""
    ctx = workspace_ctx(engine, token)
    ctx.credentials[STORE_KEY_SLOT] = STORE_KEY
    return ctx


@dataclass
class Pair:
    """The two accounts and the encrypted room one test drives, registered fresh for it: the bot's
    device is the one its token is bound to, and so is the member's."""

    bot: str
    bot_token: str
    bot_device: str
    member: str
    member_token: str
    member_device: str
    password: str
    localpart: str
    room_id: str


@pytest.fixture
def pair() -> Pair:
    async def provision() -> Pair:
        suffix = uuid4().hex[:10]
        localpart, password = f"sweep8member{suffix}", f"member-{suffix}-pass"
        async with Keys() as anonymous:
            bot, bot_token = await anonymous.register(f"sweep8bot{suffix}", f"bot-{suffix}-pass")
            member, member_token = await anonymous.register(localpart, password)
        async with Keys(bot_token) as at_bot, Keys(member_token) as at_member:
            _, bot_device = await at_bot.whoami()
            _, member_device = await at_member.whoami()
            room_id = await at_member.create_encrypted_room(invite=(bot,))
            await at_bot.join(room_id)
        return Pair(
            bot=bot,
            bot_token=bot_token,
            bot_device=bot_device,
            member=member,
            member_token=member_token,
            member_device=member_device,
            password=password,
            localpart=localpart,
            room_id=room_id,
        )

    return asyncio.run(provision())


@on_loop
async def test_an_encrypted_room_round_trips_through_a_real_homeserver(
    engine: Any, pair: Pair
) -> None:
    ctx = bot_ctx(engine, pair.bot_token)
    bot = Bot(ctx, pair.bot_token)
    await bot.hear()  # the first sync fixes where the stream stands, and publishes the device

    async with Keys(pair.member_token) as at_member:
        peer = Peer(at_member, pair.member, pair.member_device, pair.bot, pair.bot_device)
        await peer.publish()
        await peer.share(pair.room_id)
        said = await peer.say(pair.room_id, "what does the week look like?")
        heard = await bot.until("what does the week look like?")
        assert bodies(heard) == ["what does the week look like?"]
        assert [event["event_id"] for _room, event in heard if event["type"] == MESSAGE_TYPE] == [
            said
        ]
        ciphertext = (await at_member.event(pair.room_id, said))["content"]["ciphertext"]
        assert ciphertext in str(bot.batches)  # the batch held ciphertext
        assert ciphertext not in str(heard)  # and what was heard of it is words

        surface = MatrixSurface(environ={})
        wb = writeback(uuid4(), pair.room_id)
        reference = await surface.post(ctx, wb)  # type: ignore[arg-type]
        answer = await at_member.event(pair.room_id, reference)
        assert answer["type"] == ENCRYPTED_TYPE
        assert "The answer" not in str(answer)
        [room_key] = [p for p in await peer.read_to_device() if p["type"] == ROOM_KEY]
        assert room_key["content"]["room_id"] == pair.room_id
        payload = peer.decrypt(answer["content"])
        assert payload["room_id"] == pair.room_id
        assert payload["content"]["body"] == "The answer, in full."
        assert payload["content"]["formatted_body"] == "<p>The answer, in full.</p>"


@on_loop
async def test_the_homeserver_says_when_the_key_pool_has_run_out(engine: Any, pair: Pair) -> None:
    """The count is the homeserver's to keep: the bot is told what the pool holds in a batch it was
    sent for another reason, and offers a fresh pool where what it is told is short of one. A
    homeserver hands a device's one-time keys out on its own account, so nothing but its own count
    says when they are gone."""
    ctx = bot_ctx(engine, pair.bot_token)
    bot = Bot(ctx, pair.bot_token)
    await bot.hear()

    async with Keys(pair.member_token) as at_member:
        peer = Peer(at_member, pair.member, pair.member_device, pair.bot, pair.bot_device)
        await peer.publish()
        await drain(at_member, pair.bot, pair.bot_device)
        await peer.share(pair.room_id)  # a batch to answer, carrying the empty pool's count with it
        await bot.hear()
        assert bot.counts()[-1] == 0

        await peer.say(pair.room_id, "and what does the count say now?")
        assert "and what does the count say now?" in bodies(
            await bot.until("and what does the count say now?")
        )
    assert bot.counts()[-1] == TARGET  # offered again, and the homeserver holds them


@on_loop
async def test_a_drained_pool_is_served_by_the_fallback_key(engine: Any, pair: Pair) -> None:
    """A homeserver with no one-time key left to hand out reaches for the fallback key instead, and
    hands out that same key as often as it is asked — which is what a device publishes one for. The
    fake takes a fallback key and never reaches for it, so only a real homeserver's claim says the
    bot's own is any use."""
    ctx = bot_ctx(engine, pair.bot_token)
    bot = Bot(ctx, pair.bot_token)
    await bot.hear()

    async with Keys(pair.member_token) as at_member:
        peer = Peer(at_member, pair.member, pair.member_device, pair.bot, pair.bot_device)
        await peer.publish()
        drained = await drain(at_member, pair.bot, pair.bot_device)
        assert [key.get("fallback") for key in drained[:-1]] == [None] * (len(drained) - 1)
        assert drained[-1]["fallback"] is True

        await peer.share(pair.room_id)  # the session this opens rests on the fallback key
        assert peer.claimed[-1]["fallback"] is True
        assert peer.claimed[-1]["key"] == drained[-1]["key"]  # handed out again, as a fallback is
        await peer.say(pair.room_id, "on the fallback key")
        assert "on the fallback key" in bodies(await bot.until("on the fallback key"))
        assert bot.fallback_unused()[-1] == []  # the one it published is spent, and it publishes



@on_loop
async def test_the_homeserver_announces_a_members_second_device(engine: Any, pair: Pair) -> None:
    """A member's new device is the homeserver's news to carry: it names the member in
    `device_lists.changed` to every room that shares encryption with them, and the bot outdates what
    it had pinned for that member and shares the room's session with the device it then finds."""
    ctx = bot_ctx(engine, pair.bot_token)
    bot = Bot(ctx, pair.bot_token)
    await bot.hear()
    surface = MatrixSurface(environ={})

    async with Keys(pair.member_token) as at_member:
        phone = Peer(at_member, pair.member, pair.member_device, pair.bot, pair.bot_device)
        await phone.publish()
        await phone.share(pair.room_id)
        await phone.say(pair.room_id, "from the phone")
        assert "from the phone" in bodies(await bot.until("from the phone"))
        told = writeback(uuid4(), pair.room_id)
        first = await surface.post(ctx, told)  # type: ignore[arg-type]
        assert [p["type"] for p in await phone.read_to_device()] == [ROOM_KEY]

        token, device_id = await at_member.login(pair.localpart, pair.password)
        async with Keys(token) as at_laptop:
            laptop = Peer(at_laptop, pair.member, device_id, pair.bot, pair.bot_device)
            await laptop.publish()
            await bot.hear()
            assert pair.member in bot.changed()

            again = writeback(uuid4(), pair.room_id)
            second = await surface.post(ctx, again)  # type: ignore[arg-type]
            assert [p["type"] for p in await laptop.read_to_device()] == [ROOM_KEY]
            answer = await at_laptop.event(pair.room_id, second)
            assert laptop.decrypt(answer["content"])["content"]["body"] == "The answer, in full."
        assert first != second
        assert phone.decrypt((await at_member.event(pair.room_id, second))["content"])


@on_loop
async def test_the_media_repository_holds_ciphertext_and_serves_it_where_we_ask(
    engine: Any, pair: Pair
) -> None:
    """The two claims about sealed media that only a homeserver can answer.

    **Where the bytes are served from.** `download` reads the authenticated client endpoint rather
    than the media repository's own, because a homeserver holding `enable_authenticated_media` — the
    Synapse default — does not serve authenticated media on the unauthenticated one.

    The fake refuses the old endpoint too, so the fake suite catches a client that reverts to it.
    What the fake cannot catch is the two of them being wrong together: its routing and the client's
    path were written from one belief about which endpoint serves, so they agree by construction. A
    fake cannot falsify the assumption it was built from. Only the homeserver can, and this asks it.

    **What the repository holds.** An `EncryptedFile` is sealed before it is uploaded, so the object
    behind the `mxc://` is ciphertext and not the file. A fake serves back whatever it stored, which
    is true whether or not anything sealed it."""
    plaintext = b"the quarterly numbers, in confidence" * 8
    ciphertext, sealed = crypto.seal_file(plaintext)

    async with MatrixClient(HOMESERVER, pair.bot_token) as client:
        uri = await client.upload("q3.bin", "application/octet-stream", ciphertext)
        fetched = await client.download(uri)

    server, media_id = uri.removeprefix("mxc://").split("/", 1)
    async with Api(pair.bot_token) as api:
        unauthenticated = await api.get_status(
            f"/_matrix/media/v3/download/{quote(server, safe='')}/{quote(media_id, safe='')}"
        )

    assert unauthenticated >= 400, (
        "the unauthenticated media endpoint served authenticated media, so the endpoint "
        "`download` reads is no longer the one that has to be read"
    )
    assert fetched == ciphertext
    assert fetched != plaintext
    assert crypto.open_file({**sealed, "url": uri}, fetched) == plaintext
