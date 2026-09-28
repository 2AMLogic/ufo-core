"""A homeserver that carries end-to-end encryption, and a member's client that speaks it for real.

`E2EHomeserver` adds what a device needs to the fake homeserver: key upload, query and claim,
to-device queues delivered on `/sync`, and rooms with `m.room.encryption` state. `Peer` is one
member's device over its own vodozemac account — it publishes keys, opens Olm sessions with the
bot's claimed one-time keys, shares Megolm room keys, and reads what the bot sends back — so every
message in these tests is real Olm and Megolm ciphertext, and nothing is mocked below the wire.
`Verifier` is that client's half of a SAS exchange, spelling the spec's commitment and MAC out for
itself so the bot's half is checked against the protocol rather than against its own arithmetic."""

import base64
import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import vodozemac as vz
from matrix_fakes import BOT, TOKEN, Homeserver

BOT_DEVICE = "BOTDEVICE"
STORE_KEY = "a-store-key-of-at-least-thirty-two-characters"
OLM = "m.olm.v1.curve25519-aes-sha2"
MEGOLM = "m.megolm.v1.aes-sha2"

VERIFICATION = "m.key.verification"
REQUEST = f"{VERIFICATION}.request"
READY = f"{VERIFICATION}.ready"
START = f"{VERIFICATION}.start"
ACCEPT = f"{VERIFICATION}.accept"
KEY = f"{VERIFICATION}.key"
MAC = f"{VERIFICATION}.mac"
DONE = f"{VERIFICATION}.done"
CANCEL = f"{VERIFICATION}.cancel"
SAS = "m.sas.v1"
SAS_MAC = "hkdf-hmac-sha256.v2"


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def unpadded(raw: bytes) -> str:
    return base64.b64encode(raw).decode().rstrip("=")


def padded(text: str) -> bytes:
    return base64.b64decode(text + "=" * (-len(text) % 4))


def mac_info(sender: str, device_id: str, peer: str, peer_device: str, transaction: str) -> str:
    """The MAC info string of one direction of an exchange: the sender's user and device, the
    receiver's, then the transaction id."""
    return f"MATRIX_KEY_VERIFICATION_MAC{sender}{device_id}{peer}{peer_device}{transaction}"


@dataclass
class E2EHomeserver(Homeserver):
    """Keys and to-device messages for every device, the bot's included. `device_uploads` counts
    the times the bot published device keys, so a test sees a restart keep its device."""

    device_id: str = BOT_DEVICE
    devices: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    one_time_keys: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    fallback_keys: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    inbox: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)
    device_uploads: int = 0
    to_device_sent: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return super().handle(request)
        if not request.url.path.startswith("/_matrix/client/v3"):
            return super().handle(request)
        path = request.url.path.removeprefix("/_matrix/client/v3").split("/")[1:]
        body = json.loads(request.content) if request.content else {}
        match request.method, path:
            case "GET", ["account", "whoami"]:
                self.requests.append(request)
                return httpx.Response(200, json={"user_id": BOT, "device_id": self.device_id})
            case "GET", ["sync"]:
                answer = copy.deepcopy(json.loads(super().handle(request).content))
                events = self.inbox.pop((BOT, self.device_id), [])
                answer["to_device"] = {"events": events}
                held = len(self.one_time_keys.get((BOT, self.device_id), {}))
                answer["device_one_time_keys_count"] = {"signed_curve25519": held}
                return httpx.Response(200, json=answer)
            case "GET", ["rooms", room, "state", "m.room.encryption", ""]:
                self.requests.append(request)
                if room in self.encrypted:
                    return httpx.Response(200, json=self.encrypted[room])
                return httpx.Response(404, json={"errcode": "M_NOT_FOUND"})
            case "POST", ["keys", "upload"]:
                self.requests.append(request)
                return httpx.Response(200, json=self.upload(BOT, self.device_id, body))
            case "POST", ["keys", "query"]:
                self.requests.append(request)
                listed = {u: self.devices.get(u, {}) for u in body["device_keys"]}
                return httpx.Response(200, json={"device_keys": listed})
            case "POST", ["keys", "claim"]:
                self.requests.append(request)
                return httpx.Response(
                    200, json={"one_time_keys": self.claim(body["one_time_keys"])}
                )
            case "PUT", ["sendToDevice", event_type, _]:
                self.requests.append(request)
                self.deliver(BOT, event_type, body["messages"])
                return httpx.Response(200, json={})
        return super().handle(request)

    def upload(self, user: str, device_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if "device_keys" in body:
            self.devices.setdefault(user, {})[device_id] = body["device_keys"]
            if user == BOT:
                self.device_uploads += 1
        self.one_time_keys.setdefault((user, device_id), {}).update(body.get("one_time_keys", {}))
        if body.get("fallback_keys"):
            self.fallback_keys[(user, device_id)] = body["fallback_keys"]
        held = len(self.one_time_keys[(user, device_id)])
        return {"one_time_key_counts": {"signed_curve25519": held}}

    def claim(self, wanted: dict[str, dict[str, str]]) -> dict[str, Any]:
        claimed: dict[str, Any] = {}
        for user, devices in wanted.items():
            for device_id in devices:
                keys = self.one_time_keys.get((user, device_id), {})
                if keys:
                    key_id = next(iter(keys))
                    claimed.setdefault(user, {})[device_id] = {key_id: keys.pop(key_id)}
                elif (user, device_id) in self.fallback_keys:
                    claimed.setdefault(user, {})[device_id] = self.fallback_keys[(user, device_id)]
        return claimed

    def deliver(self, sender: str, event_type: str, messages: dict[str, dict[str, Any]]) -> None:
        for user, devices in messages.items():
            for device_id, content in devices.items():
                targets = self.devices.get(user, {}) if device_id == "*" else [device_id]
                for target in targets:
                    event = {"type": event_type, "sender": sender, "content": content}
                    self.inbox.setdefault((user, target), []).append(event)
                    self.to_device_sent.append((f"{user}|{target}", event))

    def bot_keys(self) -> tuple[str, str]:
        keys = self.devices[BOT][self.device_id]["keys"]
        return keys[f"curve25519:{self.device_id}"], keys[f"ed25519:{self.device_id}"]


@dataclass
class Peer:
    """One member's device. Its Olm and Megolm state is vodozemac's own, held in memory."""

    server: E2EHomeserver
    user: str
    device_id: str
    account: vz.Account = field(default_factory=vz.Account)
    olm: dict[str, list[vz.Session]] = field(default_factory=dict)
    outbound: dict[str, vz.GroupSession] = field(default_factory=dict)
    inbound: dict[str, vz.InboundGroupSession] = field(default_factory=dict)
    origin: dict[str, vz.InboundGroupSession] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.publish()

    @property
    def curve(self) -> str:
        return self.account.curve25519_key.to_base64()

    @property
    def ed(self) -> str:
        return self.account.ed25519_key.to_base64()

    def signed(self, value: dict[str, Any]) -> dict[str, Any]:
        signature = self.account.sign(canonical(value)).to_base64()
        return {**value, "signatures": {self.user: {f"ed25519:{self.device_id}": signature}}}

    def publish(self) -> None:
        self.account.generate_one_time_keys(5)
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
        otks = {
            f"signed_curve25519:{k}": self.signed({"key": v.to_base64()})
            for k, v in self.account.one_time_keys.items()
        }
        self.server.upload(
            self.user, self.device_id, {"device_keys": device_keys, "one_time_keys": otks}
        )
        self.account.mark_keys_as_published()

    def to_bot(self, event_type: str, content: dict[str, Any]) -> None:
        """Olm-encrypt one to-device event to the bot's device, opening a session with a claimed
        one-time key the first time."""
        curve, ed = self.server.bot_keys()
        if curve not in self.olm:
            [(_, key)] = self.server.claim({BOT: {self.server.device_id: "signed_curve25519"}})[
                BOT
            ][self.server.device_id].items()
            session = self.account.create_outbound_session(
                vz.Curve25519PublicKey.from_base64(curve),
                vz.Curve25519PublicKey.from_base64(key["key"]),
            )
            self.olm[curve] = [session]
        payload = {
            "type": event_type,
            "content": content,
            "sender": self.user,
            "sender_device": self.device_id,
            "keys": {"ed25519": self.ed},
            "recipient": BOT,
            "recipient_keys": {"ed25519": ed},
        }
        kind, body = self.olm[curve][0].encrypt(canonical(payload)).to_parts()
        encrypted = {
            "algorithm": OLM,
            "sender_key": self.curve,
            "ciphertext": {curve: {"type": kind, "body": unpadded(body)}},
        }
        self.server.deliver(
            self.user, "m.room.encrypted", {BOT: {self.server.device_id: encrypted}}
        )

    def in_the_clear(self, event_type: str, content: dict[str, Any]) -> None:
        """One to-device event with no Olm around it, as a client that opens a verification in the
        clear sends."""
        self.server.deliver(self.user, event_type, {BOT: {self.server.device_id: content}})

    def session(self, room_id: str) -> vz.GroupSession:
        """The device's Megolm session for the room, and a copy of it from index 0 to forward."""
        if room_id not in self.outbound:
            self.outbound[room_id] = vz.GroupSession()
            self.origin[room_id] = vz.InboundGroupSession(self.outbound[room_id].session_key)
        return self.outbound[room_id]

    def share(self, room_id: str) -> vz.GroupSession:
        """Share this device's Megolm session for the room with the bot, as a client does before
        its first message there."""
        session = self.session(room_id)
        self.to_bot(
            "m.room_key",
            {
                "algorithm": MEGOLM,
                "room_id": room_id,
                "session_id": session.session_id,
                "session_key": session.session_key.to_base64(),
            },
        )
        return session

    def forward(self, room_id: str) -> None:
        """Answer a key request: send the session again as a forwarded key from index 0."""
        session = self.outbound[room_id]
        exported = self.origin[room_id].export_at(0)
        assert exported is not None
        self.to_bot(
            "m.forwarded_room_key",
            {
                "algorithm": MEGOLM,
                "room_id": room_id,
                "sender_key": self.curve,
                "sender_claimed_ed25519_key": self.ed,
                "session_id": session.session_id,
                "session_key": exported.to_base64(),
                "forwarding_curve25519_key_chain": [],
            },
        )

    def say(self, room_id: str, event_id: str, body: str, **content: Any) -> dict[str, Any]:
        """One Megolm-encrypted `m.text` timeline event from this device."""
        session = self.session(room_id)
        plain = {
            "type": "m.room.message",
            "content": {"msgtype": "m.text", "body": body, **content},
            "room_id": room_id,
        }
        return {
            "type": "m.room.encrypted",
            "event_id": event_id,
            "sender": self.user,
            "content": {
                "algorithm": MEGOLM,
                "sender_key": self.curve,
                "device_id": self.device_id,
                "session_id": session.session_id,
                "ciphertext": session.encrypt(canonical(plain)).to_base64(),
            },
        }

    def babble(self, room_id: str, event_id: str, plaintext: bytes = b"not an event") -> dict:
        """One Megolm event this device's session decrypts to something that is not a Matrix event,
        as a buggy or a hostile client sends."""
        session = self.session(room_id)
        return {
            "type": "m.room.encrypted",
            "event_id": event_id,
            "sender": self.user,
            "content": {
                "algorithm": MEGOLM,
                "sender_key": self.curve,
                "device_id": self.device_id,
                "session_id": session.session_id,
                "ciphertext": session.encrypt(plaintext).to_base64(),
            },
        }

    def read_inbox(self) -> list[dict[str, Any]]:
        """Decrypt every to-device event waiting for this device, keep the room keys it carries,
        and return the plaintext payloads."""
        payloads = []
        for event in self.server.inbox.pop((self.user, self.device_id), []):
            if event["type"] != "m.room.encrypted":
                payloads.append(event)
                continue
            content = event["content"]
            ours = content["ciphertext"][self.curve]
            message = vz.AnyOlmMessage.from_parts(ours["type"], padded(ours["body"]))
            pre_key = message.to_pre_key()
            sessions = self.olm.setdefault(content["sender_key"], [])
            for session in sessions:
                if pre_key is None or session.session_matches(pre_key):
                    plaintext = session.decrypt(message)
                    break
            else:
                assert pre_key is not None
                session, plaintext = self.account.create_inbound_session(
                    vz.Curve25519PublicKey.from_base64(content["sender_key"]), pre_key
                )
                sessions.insert(0, session)
            payload = json.loads(plaintext)
            assert payload["recipient"] == self.user
            assert payload["recipient_keys"]["ed25519"] == self.ed
            if payload["type"] == "m.room_key":
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
class Verifier:
    """A member's client driving its own half of a SAS exchange. `answer` replies as a client does,
    and with `honest` false the MAC covers a key this device does not hold."""

    peer: Peer
    transaction: str = "t-verify"
    honest: bool = True
    sas: vz.Sas = field(default_factory=vz.Sas)
    start: dict[str, Any] = field(default_factory=dict)
    commitment: str | None = None
    strings: list[str] = field(default_factory=list)
    established: Any = None
    verified: bool = False
    done: bool = False
    cancelled: str | None = None

    @property
    def bot_device(self) -> str:
        return self.peer.server.device_id

    def request(self, *, clear: bool = False) -> None:
        send = self.peer.in_the_clear if clear else self.peer.to_bot
        send(
            REQUEST,
            {
                "transaction_id": self.transaction,
                "from_device": self.peer.device_id,
                "methods": [SAS],
                "timestamp": 0,
            },
        )

    def send_start(self, *, clear: bool = False) -> None:
        self.start = {
            "transaction_id": self.transaction,
            "from_device": self.peer.device_id,
            "method": SAS,
            "key_agreement_protocols": ["curve25519-hkdf-sha256"],
            "hashes": ["sha256"],
            "message_authentication_codes": [SAS_MAC],
            "short_authentication_string": ["emoji", "decimal"],
        }
        send = self.peer.in_the_clear if clear else self.peer.to_bot
        send(START, self.start)

    def answer(self) -> list[str]:
        """Read this device's to-device events, reply to each, and name what arrived."""
        seen = []
        for payload in self.peer.read_inbox():
            kind, content = payload["type"], payload["content"]
            seen.append(kind)
            if content.get("transaction_id") != self.transaction:
                continue
            if kind == READY:
                self.send_start()
            elif kind == ACCEPT:
                self.commitment = content["commitment"]
                self.strings = content["short_authentication_string"]
                self.peer.to_bot(
                    KEY,
                    {
                        "transaction_id": self.transaction,
                        "key": self.sas.public_key.to_base64(),
                    },
                )
            elif kind == KEY:
                self.agree(content["key"])
            elif kind == MAC:
                self.check(content)
            elif kind == DONE:
                self.done = True
                self.peer.to_bot(DONE, {"transaction_id": self.transaction})
            elif kind == CANCEL:
                self.cancelled = content["code"]
        return seen

    def agree(self, public: str) -> None:
        """Check the commitment the bot made before it saw this end's key, agree the secret, and
        send the MAC the member's confirmation turns into."""
        digest = hashlib.sha256(public.encode() + canonical(self.start)).digest()
        assert unpadded(digest) == self.commitment
        self.established = self.sas.diffie_hellman(vz.Curve25519PublicKey.from_base64(public))
        info = mac_info(self.peer.user, self.peer.device_id, BOT, self.bot_device, self.transaction)
        key_id = f"ed25519:{self.peer.device_id}"
        held = self.peer.ed if self.honest else vz.Account().ed25519_key.to_base64()
        self.peer.to_bot(
            MAC,
            {
                "transaction_id": self.transaction,
                "keys": self.established.calculate_mac(key_id, info + "KEY_IDS"),
                "mac": {key_id: self.established.calculate_mac(held, info + key_id)},
            },
        )

    def check(self, content: dict[str, Any]) -> None:
        """The bot's MAC against the device keys the homeserver published for it."""
        _, ed = self.peer.server.bot_keys()
        info = mac_info(BOT, self.bot_device, self.peer.user, self.peer.device_id, self.transaction)
        key_id = f"ed25519:{self.bot_device}"
        self.established.verify_mac(key_id, info + "KEY_IDS", content["keys"])
        self.established.verify_mac(ed, info + key_id, content["mac"][key_id])
        self.verified = True
