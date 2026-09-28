"""End-to-end encryption for the bot's one device, over vodozemac's Olm and Megolm.

The device is the one the bot's access token is bound to, as `/account/whoami` names it, so a
restart keeps the device its peers already know. Its account and sessions live in the crypto store,
sealed under the `matrix_store_key` slot; with that slot empty the bot has no device keys, hears
nothing in an encrypted room, and refuses to post into one rather than posting in the clear.

Inbound, a `/sync` batch's to-device events are read first — room keys arrive there, Olm-encrypted
to this device — then each `m.room.encrypted` timeline event is decrypted into the plain event it
carries, which the surface reads exactly as it reads an unencrypted one. An event whose session key
has not arrived is parked, its key is asked for, and it is heard once the key lands; past
`PENDING_SECONDS` it is dropped with a warning. An event that will not decrypt is a dropped event
that names itself in a log, never a stopped stream. Ciphertext is never heard.

Outbound, a message into an encrypted room is Megolm-encrypted, and the session's key is first
shared over Olm with every device of every joined member that has not had it. Devices are trusted on
first use: the keys a device id first shows are pinned, and a device that later shows other keys is
neither sent keys nor believed.

A member verifies the bot's device from their own client, and the bot answers: an
`m.key.verification.*` exchange over SAS is to-device protocol from end to end, so no command, no
keyword and no endpoint carries it, and the bot never starts one. A device that finishes the
exchange is recorded verified beside its pin, which gates nothing — first use is what decides who
is sent a room key, whether a member verifies or never does.

Olm and Megolm are `vodozemac`, which the `matrix-e2ee` extra installs. This module imports without
it — every name it needs from the library is reached inside a call, and annotations are deferred
— so the surface loads and an unencrypted room is served whether the extra is there or not."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from ufo.sdk.o11y import log, warn
from ufo.sdk.surfaces import CredentialSlotUnset, SurfaceContext
from ufo_ext_matrix.client import MatrixClient, MatrixError
from ufo_ext_matrix.crypto_store import SEALING, CryptoStore, Rows, Sealer, StoreLocked
from ufo_ext_matrix.e2ee import EXTRA, INSTALL, CryptoUnavailable, ExtraMissing
from ufo_ext_matrix.events import ENCRYPTED_TYPE, timeline

try:
    import vodozemac as vz

    VODOZEMAC = True
except ImportError:
    VODOZEMAC = False

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    CIPHERS = True
except ImportError:
    CIPHERS = False

INSTALLED = VODOZEMAC and SEALING

UNOBSERVED = object()
STORE_KEY_SLOT = "matrix_store_key"
FILE_ALGORITHM = "A256CTR"
FILE_VERSION = "v2"
FILE_KEY_BYTES = 32
FILE_IV_BYTES = 16
FILE_COUNTER_BYTES = 8
OLM = "m.olm.v1.curve25519-aes-sha2"
MEGOLM = "m.megolm.v1.aes-sha2"
SIGNED_KEY = "signed_curve25519"
ROOM_KEY = "m.room_key"
FORWARDED_ROOM_KEY = "m.forwarded_room_key"
KEY_REQUEST = "m.room_key_request"
PENDING_SECONDS = 600
PENDING_LIMIT = 200
OLM_SESSIONS_KEPT = 5
ROTATION_MS = 7 * 24 * 3600 * 1000
ROTATION_MSGS = 100

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
SAS_AGREEMENT = "curve25519-hkdf-sha256"
SAS_HASH = "sha256"
SAS_MAC = "hkdf-hmac-sha256.v2"
SAS_STRINGS = ("emoji", "decimal")
KEY_IDS = "KEY_IDS"
MAC_INFO = "MATRIX_KEY_VERIFICATION_MAC"
EXCHANGE_SECONDS = 600
EXCHANGE_LIMIT = 32
CANCELLED = {
    "m.unknown_method": f"this device verifies over {SAS} with {SAS_MAC}",
    "m.unknown_transaction": "this device holds no exchange under that transaction id",
    "m.key_mismatch": "the key this exchange confirms is not the key pinned here",
    "m.invalid_message": "a verification event reached this device in the clear",
}

ACCOUNT = "account"
DEVICES = "devices"
OLM_SESSIONS = "olm"
INBOUND = "inbound"
OUTBOUND = "outbound"
PENDING = "pending"


class KeyMissing(Exception):
    """An event's Megolm session has not reached this device."""


def now_ms() -> int:
    return int(time.time() * 1000)


def canonical(value: Any) -> bytes:
    """Matrix canonical JSON: sorted keys, no whitespace, UTF-8."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode().rstrip("=")


def decode(text: str) -> bytes:
    return base64.b64decode(text + "=" * (-len(text) % 4))


def urlsafe(raw: bytes) -> str:
    """A JWK carries its key in unpadded base64url, which is a different alphabet from the padded
    base64 an event's other fields use."""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def unurlsafe(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class FileHashMismatch(Exception):
    """Fetched bytes are not the bytes the sender sealed, so the file is dropped rather than
    decrypted."""


def _stream(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-CTR is its own inverse, so sealing and opening are the same call."""
    if not CIPHERS:
        raise ExtraMissing
    cipher = Cipher(algorithms.AES(key), modes.CTR(iv))
    worker = cipher.encryptor()
    return worker.update(data) + worker.finalize()


def seal_file(data: bytes) -> tuple[bytes, dict[str, Any]]:
    """One file sealed for an encrypted room: the ciphertext to upload, and the `EncryptedFile` the
    message carries it by.

    A fresh key and a fresh counter per file, because AES-CTR reuses a keystream whenever the pair
    repeats and two files sealed under one pair are readable from their XOR alone. The counter's low
    half starts at zero, so the stream runs forward from the block the file begins at."""
    key = os.urandom(FILE_KEY_BYTES)
    iv = os.urandom(FILE_IV_BYTES - FILE_COUNTER_BYTES) + bytes(FILE_COUNTER_BYTES)
    ciphertext = _stream(key, iv, data)
    sealed = {
        "key": {
            "kty": "oct",
            "key_ops": ["encrypt", "decrypt"],
            "alg": FILE_ALGORITHM,
            "k": urlsafe(key),
            "ext": True,
        },
        "iv": encode(iv),
        "hashes": {"sha256": encode(hashlib.sha256(ciphertext).digest())},
        "v": FILE_VERSION,
    }
    return ciphertext, sealed


def open_file(sealed: Mapping[str, Any], ciphertext: bytes) -> bytes:
    """The bytes an `EncryptedFile` names, once the ciphertext is the one it was sealed over.

    The hash is checked before anything is decrypted. What the media repository answers with is not
    yet what the sender sealed — the repository is the homeserver's, and the hash is the only part
    of that claim the sender signed for. A mismatch raises rather than returning bytes nobody
    vouched for.

    Every field is a hostile sender's to choose — the argument itself included, since it arrives as
    an event's `file` and `json.loads` hands back whatever was typed there — so each is read as a
    shape rather than trusted to be one. Anything malformed leaves as `FileHashMismatch`, which is
    the one failure a reader handles by dropping the file — an escaping `AttributeError` would turn
    a dropped attachment into an unhandled exception in whoever is reading the room.

    `compare_digest` is used because it is the right default for "is this the value I was supposed
    to get", not because a leak of this digest would matter: the attacker chose the ciphertext, so
    they already know its hash, and knowing it buys no second preimage."""
    if not isinstance(sealed, Mapping):
        raise FileHashMismatch("the file is not an object")
    hashes = sealed.get("hashes")
    expected = hashes.get("sha256") if isinstance(hashes, Mapping) else None
    if not isinstance(expected, str):
        raise FileHashMismatch("the file carries no sha256")
    try:
        digest = decode(expected)
    except ValueError as unreadable:
        raise FileHashMismatch("the file's sha256 is not base64") from unreadable
    if not hmac.compare_digest(digest, hashlib.sha256(ciphertext).digest()):
        raise FileHashMismatch("the fetched bytes do not match the file's sha256")
    key = sealed.get("key")
    named = key.get("alg") if isinstance(key, Mapping) else None
    if named != FILE_ALGORITHM:
        raise FileHashMismatch(f"the file names {named!r} rather than {FILE_ALGORITHM}")
    secret, iv = key.get("k"), sealed.get("iv")
    if not isinstance(secret, str) or not isinstance(iv, str):
        raise FileHashMismatch("the file names no key or no iv")
    try:
        raw_key, raw_iv = unurlsafe(secret), decode(iv)
    except ValueError as unreadable:
        raise FileHashMismatch("the file's key or iv is not base64") from unreadable
    if len(raw_key) != FILE_KEY_BYTES or len(raw_iv) != FILE_IV_BYTES:
        raise FileHashMismatch("the file's key or iv is the wrong length")
    return _stream(raw_key, raw_iv, ciphertext)


def verified_device(user_id: str, device_id: str, keys: Any) -> dict[str, str] | None:
    """The Curve25519 and Ed25519 keys a `/keys/query` entry lists, if it is the device it says it
    is and its own Ed25519 key signed it."""
    try:
        if keys["user_id"] != user_id or keys["device_id"] != device_id:
            return None
        curve = keys["keys"][f"curve25519:{device_id}"]
        ed = keys["keys"][f"ed25519:{device_id}"]
        signature = keys["signatures"][user_id][f"ed25519:{device_id}"]
        signed = {k: v for k, v in keys.items() if k not in ("signatures", "unsigned")}
        vz.Ed25519PublicKey.from_base64(ed).verify_signature(
            canonical(signed), vz.Ed25519Signature.from_base64(signature)
        )
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(curve, str):
        return None
    return {"curve25519": curve, "ed25519": ed}


def _mac_info(sender: str, device_id: str, peer: str, peer_device: str, transaction: str) -> str:
    """The MAC info string of one direction of an exchange, as the spec composes it: the sender's
    user and device, then the receiver's, then the transaction id."""
    return f"{MAC_INFO}{sender}{device_id}{peer}{peer_device}{transaction}"


@dataclass
class Verifying:
    """One SAS exchange in flight: the ephemeral key pair that agrees the secret, the device on the
    other end, and when it began.

    It lives in this process's memory rather than the store. A `vodozemac.Sas` holds an ephemeral
    Curve25519 secret it neither pickles nor takes back, so there is nothing to seal into a row; an
    exchange a restart interrupts is one the member's client starts again, which is a prompt they
    are already looking at. `EXCHANGE_SECONDS` is how long one is answered for, read at every event
    of it and not only swept when the next exchange starts, and `EXCHANGE_LIMIT` is how many one
    workspace holds at once, so a client that walks away mid-exchange costs its own workspace a
    single entry and nobody else's."""

    peer_device: str
    public: str
    sas: vz.Sas
    established: vz.EstablishedSas | None = None
    at: int = field(default_factory=now_ms)

    def aged(self, now: int) -> bool:
        """An exchange `EXCHANGE_SECONDS` old is one this device has let go of, whether it is read
        or swept."""
        return now - self.at >= EXCHANGE_SECONDS * 1000


_EXCHANGES: dict[tuple[str, str, str, str], Verifying] = {}


def _verified_key(user_id: str, device_id: str, ed25519: str, key: Any) -> str | None:
    """A claimed one-time key, if the device's Ed25519 key signed it."""
    try:
        signature = key["signatures"][user_id][f"ed25519:{device_id}"]
        signed = {k: v for k, v in key.items() if k not in ("signatures", "unsigned")}
        vz.Ed25519PublicKey.from_base64(ed25519).verify_signature(
            canonical(signed), vz.Ed25519Signature.from_base64(signature)
        )
        value = key["key"]
    except (KeyError, TypeError, ValueError):
        return None
    return value if isinstance(value, str) else None


@dataclass
class Device:
    """The bot's device over one store and one homeserver session."""

    store: CryptoStore
    client: MatrixClient
    account: vz.Account

    @property
    def user_id(self) -> str:
        return self.store.user_id

    @property
    def device_id(self) -> str:
        return self.store.device_id

    @property
    def identity_key(self) -> str:
        return self.account.curve25519_key.to_base64()

    @property
    def signing_key(self) -> str:
        return self.account.ed25519_key.to_base64()

    @property
    def _pickle_key(self) -> bytes:
        return self.store.sealer.pickle_key

    def __repr__(self) -> str:
        """The device id and nothing else. `MatrixClient` holds the same discipline: whatever a
        vodozemac `Account` chooses to show is the binding's choice, not this extension's, and an
        f-string in a log line or an exception must not be where that is decided."""
        return f"Device({self.device_id})"

    @classmethod
    async def open(cls, store: CryptoStore, client: MatrixClient) -> "Device":
        """The device's account from the store, or a new one — created once, then published —
        where the store holds none for this `(user, device)`.

        One device has one account, whichever process opens it: the listener and the writeback path
        both open a device, so two of them meeting a first encrypted event together is an ordinary
        interleaving. The account row is written with `create`, which the table's own primary key
        arbitrates, and a caller whose mint loses reads the winner's account rather than publishing
        its own keys over it — two accounts for one device id is how a bot silently stops
        decrypting.

        The upload happens once for the same reason: the caller whose insert lost leaves the publish
        to the one that won, so two of them do not each generate a batch of one-time keys and upload
        it over the other's. Only one of those batches survives in the store, and a peer that claims
        a key from the other opens a session the bot cannot answer. An upload that fails leaves the
        account unpublished, and the next open — the next sync — offers the same keys again."""
        async with store.rows() as rows:
            record = await rows.get(ACCOUNT, lock=True)
            publishing = True
            if record is None:
                fresh = vz.Account()
                record = {"pickle": fresh.pickle(store.sealer.pickle_key), "published": False}
                if await rows.create(ACCOUNT, value=record):
                    log("matrix.crypto_device_new", device_id=store.device_id)
                else:
                    held = await rows.get(ACCOUNT, lock=True)
                    if held is None:
                        raise CryptoUnavailable("another process is opening this device's account")
                    record, publishing = held, False
            account = vz.Account.from_pickle(record["pickle"], store.sealer.pickle_key)
        device = cls(store, client, account)
        if publishing and not record["published"]:
            keys = {
                "device_keys": device._device_keys(),
                "one_time_keys": device._one_time_keys(account.max_number_of_one_time_keys // 2),
                "fallback_keys": device._fallback_key(),
            }
            await device._publish(keys)
        return device

    def _signed(self, value: dict[str, Any]) -> dict[str, Any]:
        signature = self.account.sign(canonical(value)).to_base64()
        return {
            **value,
            "signatures": {self.user_id: {f"ed25519:{self.device_id}": signature}},
        }

    def _device_keys(self) -> dict[str, Any]:
        return self._signed(
            {
                "user_id": self.user_id,
                "device_id": self.device_id,
                "algorithms": [OLM, MEGOLM],
                "keys": {
                    f"curve25519:{self.device_id}": self.identity_key,
                    f"ed25519:{self.device_id}": self.signing_key,
                },
            }
        )

    def _one_time_keys(self, count: int) -> dict[str, Any]:
        """`count` keys the homeserver has not been offered, signed. Keys the account already holds
        unpublished are offered again rather than generated over: `_publish` stores the account
        before it uploads so a refused upload is tried again with the same keys, and generating on
        top of them would churn the pool past the maximum every time a homeserver refuses."""
        self.account.generate_one_time_keys(max(count - len(self.account.one_time_keys), 0))
        return {
            f"{SIGNED_KEY}:{key_id}": self._signed({"key": key.to_base64()})
            for key_id, key in self.account.one_time_keys.items()
        }

    def _fallback_key(self) -> dict[str, Any]:
        self.account.generate_fallback_key()
        return {
            f"{SIGNED_KEY}:{key_id}": self._signed({"key": key.to_base64(), "fallback": True})
            for key_id, key in self.account.fallback_key.items()
        }

    async def _publish(self, keys: Mapping[str, Any]) -> None:
        """Upload what the account generated, then record it published. The account is stored
        first, so a failed upload is tried again with the same keys rather than lost."""
        await self._save_account(published=False)
        await self.client.upload_keys(keys)
        self.account.mark_keys_as_published()
        await self._save_account(published=True)

    async def _save_account(self, *, published: bool = True, rows: Rows | None = None) -> None:
        record = {"pickle": self.account.pickle(self._pickle_key), "published": published}
        if rows is not None:
            await rows.put(ACCOUNT, value=record)
            return
        async with self.store.rows() as fresh:
            await fresh.put(ACCOUNT, value=record)

    async def receive(self, batch: Mapping[str, Any]) -> None:
        """Everything a sync batch tells the device: whose devices changed, the to-device events
        addressed to it, and how many one-time keys the homeserver has left to hand out."""
        lists = batch.get("device_lists")
        if isinstance(lists, Mapping):
            changed = [
                u for k in ("changed", "left") for u in lists.get(k) or () if isinstance(u, str)
            ]
            await self._outdate(changed)
        section = batch.get("to_device")
        events = section.get("events", []) if isinstance(section, Mapping) else []
        events = [e for e in events if isinstance(e, Mapping)]
        senders = {e["sender"] for e in events if isinstance(e.get("sender"), str)}
        await self.refresh(senders)
        for event in events:
            try:
                await self._to_device(event)
            except (MatrixError, StoreLocked):
                raise
            except Exception as error:
                warn("matrix.to_device_skipped", error_class=type(error).__name__)
        await self._replenish(
            batch.get("device_one_time_keys_count"),
            batch.get("device_unused_fallback_key_types"),
        )

    async def _replenish(self, counts: Any, fallback_types: Any) -> None:
        keys: dict[str, Any] = {}
        if isinstance(counts, Mapping):
            target = self.account.max_number_of_one_time_keys // 2
            held = counts.get(SIGNED_KEY, 0)
            if isinstance(held, int) and held < target:
                keys["one_time_keys"] = self._one_time_keys(target - held)
        if isinstance(fallback_types, list) and SIGNED_KEY not in fallback_types:
            keys["fallback_keys"] = self._fallback_key()
        if keys:
            await self._publish(keys)

    async def _outdate(self, users: Iterable[str]) -> None:
        async with self.store.rows() as rows:
            for user in users:
                record = await rows.get(DEVICES, user, lock=True)
                if record is not None and not record.get("outdated"):
                    await rows.put(DEVICES, user, value={**record, "outdated": True})

    async def refresh(self, users: Iterable[str]) -> None:
        """Ask the homeserver for the devices of every user whose list the store lacks or knows to
        be outdated, and pin each new device's keys."""
        async with self.store.rows() as rows:
            stale = []
            for user in sorted(set(users)):
                record = await rows.get(DEVICES, user)
                if record is None or record.get("outdated"):
                    stale.append(user)
        if not stale:
            return
        listed = await self.client.query_keys(stale)
        async with self.store.rows() as rows:
            for user in stale:
                record = await rows.get(DEVICES, user, lock=True) or {"pins": {}}
                current = []
                devices = listed.get(user)
                for device_id, keys in (devices if isinstance(devices, Mapping) else {}).items():
                    shown = verified_device(user, device_id, keys)
                    if shown is None:
                        continue
                    pinned = record["pins"].setdefault(device_id, shown)
                    if pinned != shown:
                        warn(
                            "matrix.device_keys_changed",
                            device_id=device_id,
                            verified=device_id in record.get("verified", {}),
                        )
                        continue
                    current.append(device_id)
                await rows.put(
                    DEVICES,
                    user,
                    value={
                        "pins": record["pins"],
                        "current": current,
                        "outdated": False,
                        "verified": record.get("verified", {}),
                    },
                )

    async def trusted(self, rows: Rows, users: Iterable[str]) -> dict[tuple[str, str], dict]:
        """The pinned keys of every current device of these users, this device aside."""
        found: dict[tuple[str, str], dict] = {}
        for user in sorted(set(users)):
            record = await rows.get(DEVICES, user)
            if record is None:
                continue
            for device_id in record.get("current", ()):
                if (user, device_id) != (self.user_id, self.device_id):
                    found[(user, device_id)] = record["pins"][device_id]
        return found

    async def verified(self, user: str) -> dict[str, str]:
        """The Ed25519 key each of this user's devices confirmed over SAS, by device id. A device
        absent from it is pinned and no more, which every device is until a member verifies one."""
        async with self.store.rows() as rows:
            record = await rows.get(DEVICES, user)
        return dict(record.get("verified", {})) if record is not None else {}

    async def _sender_device(self, rows: Rows, user: str, curve: str) -> tuple[str, dict] | None:
        for (_, device_id), pinned in (await self.trusted(rows, [user])).items():
            if pinned["curve25519"] == curve:
                return device_id, pinned
        return None

    async def _to_device(self, event: Mapping[str, Any]) -> None:
        kind = event.get("type")
        content = event.get("content")
        sender = event.get("sender")
        if not isinstance(kind, str) or not isinstance(content, Mapping):
            return
        if not isinstance(sender, str):
            return
        if kind.startswith(f"{VERIFICATION}."):
            await self._in_the_clear(sender, kind, content)
            return
        if kind != ENCRYPTED_TYPE or content.get("algorithm") != OLM:
            return
        sender_key = content.get("sender_key")
        ours = (content.get("ciphertext") or {}).get(self.identity_key)
        if not isinstance(sender_key, str) or not ours:
            return
        message = vz.AnyOlmMessage.from_parts(ours["type"], decode(ours["body"]))
        async with self.store.rows() as rows:
            payload = json.loads(await self._olm_decrypt(rows, sender_key, message))
            found = await self._sender_device(rows, sender, sender_key)
            if (
                found is None
                or payload.get("sender") != sender
                or payload.get("recipient") != self.user_id
                or (payload.get("recipient_keys") or {}).get("ed25519") != self.signing_key
                or (payload.get("keys") or {}).get("ed25519") != found[1]["ed25519"]
            ):
                warn("matrix.to_device_untrusted", kind=str(payload.get("type")))
                return
            device_id, device = found
            kind = payload.get("type")
            inner = payload.get("content")
            if not isinstance(kind, str) or not isinstance(inner, Mapping):
                return
            if kind == ROOM_KEY:
                await self._room_key(rows, sender, device, inner, forwarded=False)
            elif kind == FORWARDED_ROOM_KEY:
                if inner.get("sender_key") == sender_key and (
                    inner.get("sender_claimed_ed25519_key") == device["ed25519"]
                ):
                    await self._room_key(rows, sender, device, inner, forwarded=True)
            elif kind.startswith(f"{VERIFICATION}."):
                await self._verification(rows, sender, device_id, device, kind, inner)

    async def _in_the_clear(self, sender: str, kind: str, content: Mapping[str, Any]) -> None:
        """A verification event no Olm session carried. The homeserver stamps the sender of an
        unencrypted to-device event, so it names a user and not a device: it can never be the
        evidence that one device is verified, and the exchange itself is read only off Olm, where
        the sending device proves its identity key. The opening request names no key and its one
        effect is the `.ready` sent back, so the clients that send it in the clear are answered; a
        `.start` in the clear is ended where the member's client can show why, and every later
        event in the clear names no device to end it with."""
        if kind != REQUEST:
            warn("matrix.verification_in_the_clear", kind=kind)
        if kind not in (REQUEST, START):
            return
        transaction = content.get("transaction_id")
        device_id = content.get("from_device")
        if not isinstance(transaction, str) or not isinstance(device_id, str):
            return
        async with self.store.rows() as rows:
            if kind == REQUEST:
                await self._ready(rows, sender, device_id, transaction, content)
            else:
                await self._cancel(rows, sender, device_id, transaction, "m.invalid_message")

    async def _verification(
        self,
        rows: Rows,
        sender: str,
        device_id: str,
        pinned: dict,
        kind: str,
        content: Mapping[str, Any],
    ) -> None:
        """One event of a SAS exchange, from the device Olm proved sent it. The bot is always the
        answering end: a member's client requests, starts, sends its key and sends its MAC, and each
        of those is answered here."""
        transaction = content.get("transaction_id")
        if not isinstance(transaction, str):
            return
        if kind == REQUEST:
            await self._ready(rows, sender, device_id, transaction, content)
        elif kind == START:
            await self._accept(rows, sender, device_id, transaction, content)
        elif kind == KEY:
            await self._agree(rows, sender, device_id, transaction, content)
        elif kind == MAC:
            await self._confirm(rows, sender, device_id, pinned, transaction, content)
        elif kind in (DONE, CANCEL):
            self._drop(sender, transaction)
            if kind == CANCEL:
                log(
                    "matrix.verification_cancelled",
                    device_id=device_id,
                    code=str(content.get("code")),
                )

    async def _ready(
        self, rows: Rows, user: str, device_id: str, transaction: str, content: Mapping[str, Any]
    ) -> None:
        """Answer a request with `.ready`, naming SAS as the one method. The bot offers no method
        it cannot finish alone, and it starts nothing: a `.ready` says which end the member's client
        is to start from."""
        methods = content.get("methods")
        if content.get("from_device") != device_id or not isinstance(methods, list):
            return
        if SAS not in methods:
            await self._cancel(rows, user, device_id, transaction, "m.unknown_method")
            return
        await self._to_peer(
            rows,
            user,
            device_id,
            READY,
            {"transaction_id": transaction, "from_device": self.device_id, "methods": [SAS]},
        )

    async def _accept(
        self, rows: Rows, user: str, device_id: str, transaction: str, content: Mapping[str, Any]
    ) -> None:
        """Answer a `.start` with `.accept`, committing to an ephemeral key before the other end's
        key is known. The commitment is the hash of that key and the `.start` as it arrived, so a
        member's client can tell the key was chosen ahead of its own."""
        if (
            content.get("method") != SAS
            or content.get("from_device") != device_id
            or SAS_AGREEMENT not in (content.get("key_agreement_protocols") or ())
            or SAS_HASH not in (content.get("hashes") or ())
            or SAS_MAC not in (content.get("message_authentication_codes") or ())
        ):
            await self._cancel(rows, user, device_id, transaction, "m.unknown_method")
            return
        offered = content.get("short_authentication_string") or ()
        strings = [string for string in SAS_STRINGS if string in offered]
        if not strings:
            await self._cancel(rows, user, device_id, transaction, "m.unknown_method")
            return
        sas = vz.Sas()
        public = sas.public_key.to_base64()
        self._hold(user, transaction, Verifying(device_id, public, sas))
        digest = hashlib.sha256(public.encode() + canonical(dict(content))).digest()
        await self._to_peer(
            rows,
            user,
            device_id,
            ACCEPT,
            {
                "transaction_id": transaction,
                "method": SAS,
                "key_agreement_protocol": SAS_AGREEMENT,
                "hash": SAS_HASH,
                "message_authentication_code": SAS_MAC,
                "short_authentication_string": strings,
                "commitment": encode(digest),
            },
        )

    async def _agree(
        self, rows: Rows, user: str, device_id: str, transaction: str, content: Mapping[str, Any]
    ) -> None:
        """Agree the shared secret from the other end's ephemeral key, and send this end's."""
        exchange = self._held(user, transaction)
        public = content.get("key")
        if exchange is None or exchange.peer_device != device_id or not isinstance(public, str):
            await self._cancel(rows, user, device_id, transaction, "m.unknown_transaction")
            return
        exchange.established = exchange.sas.diffie_hellman(
            vz.Curve25519PublicKey.from_base64(public)
        )
        await self._to_peer(
            rows,
            user,
            device_id,
            KEY,
            {"transaction_id": transaction, "key": exchange.public},
        )

    async def _confirm(
        self,
        rows: Rows,
        user: str,
        device_id: str,
        pinned: dict,
        transaction: str,
        content: Mapping[str, Any],
    ) -> None:
        """Take the other end's MAC, answer with this end's, and record the device verified.

        The MAC the member's client sends covers the Ed25519 key it holds for its own device, under
        the secret this exchange agreed. It is checked against the key pinned here, so a MAC over
        any other key ends the exchange in `m.key_mismatch` and verifies nothing. A key id the MAC
        names beyond that device's own — a cross-signing key, say — is covered by the key-id MAC and
        checked no further, since nothing here signs or holds cross-signing keys.

        The bot has no screen and compares no emoji: what it answers is the member's own
        confirmation, arriving as a MAC their client sends only once they have confirmed. The
        answer is what their client checks the bot's device key against, which is the verification
        the member asked for."""
        exchange = self._held(user, transaction)
        macs = content.get("mac")
        listed = content.get("keys")
        if (
            exchange is None
            or exchange.established is None
            or exchange.peer_device != device_id
            or not isinstance(macs, Mapping)
            or not isinstance(listed, str)
        ):
            await self._cancel(rows, user, device_id, transaction, "m.unknown_transaction")
            return
        self._drop(user, transaction)
        theirs = _mac_info(user, device_id, self.user_id, self.device_id, transaction)
        key_id = f"ed25519:{device_id}"
        try:
            exchange.established.verify_mac(",".join(sorted(macs)), theirs + KEY_IDS, listed)
            exchange.established.verify_mac(pinned["ed25519"], theirs + key_id, macs[key_id])
        except (vz.SasException, KeyError, TypeError, ValueError):
            warn("matrix.verification_key_mismatch", device_id=device_id)
            await self._cancel(rows, user, device_id, transaction, "m.key_mismatch")
            return
        ours = _mac_info(self.user_id, self.device_id, user, device_id, transaction)
        mine = f"ed25519:{self.device_id}"
        await self._to_peer(
            rows,
            user,
            device_id,
            MAC,
            {
                "transaction_id": transaction,
                "keys": exchange.established.calculate_mac(mine, ours + KEY_IDS),
                "mac": {mine: exchange.established.calculate_mac(self.signing_key, ours + mine)},
            },
        )
        record = await rows.get(DEVICES, user, lock=True)
        if record is not None:
            verified = {**record.get("verified", {}), device_id: pinned["ed25519"]}
            await rows.put(DEVICES, user, value={**record, "verified": verified})
            log("matrix.device_verified", device_id=device_id)
        await self._to_peer(rows, user, device_id, DONE, {"transaction_id": transaction})

    async def _cancel(
        self, rows: Rows, user: str, device_id: str, transaction: str, code: str
    ) -> None:
        self._drop(user, transaction)
        await self._to_peer(
            rows,
            user,
            device_id,
            CANCEL,
            {"transaction_id": transaction, "code": code, "reason": CANCELLED[code]},
        )

    async def _to_peer(
        self, rows: Rows, user: str, device_id: str, kind: str, content: Mapping[str, Any]
    ) -> None:
        """One verification event, Olm-encrypted to the single device it answers. A device the
        store holds no pinned keys for is written nothing: an exchange is with a device, and one
        that is not pinned is one this bot cannot verify anything about."""
        pinned = (await self.trusted(rows, [user])).get((user, device_id))
        if pinned is None:
            log("matrix.verification_unknown_device", device_id=device_id)
            return
        await self._olm_send(rows, {(user, device_id): pinned}, kind, content)

    def _slot(self, user: str, transaction: str) -> tuple[str, str, str, str]:
        """One exchange's place in the table, workspace first: no workspace reads another's
        exchange, and `EXCHANGE_LIMIT` counts the entries under this workspace alone."""
        return (str(self.store.ctx.workspace_id), self.device_id, user, transaction)

    def _held(self, user: str, transaction: str) -> Verifying | None:
        """The exchange that transaction id names, unless it has aged past `EXCHANGE_SECONDS`. The
        age is read here rather than only swept when the next exchange starts, so a `.key` arriving
        hours after its `.start` is let go on a bot nobody else is verifying too."""
        slot = self._slot(user, transaction)
        exchange = _EXCHANGES.get(slot)
        if exchange is not None and exchange.aged(now_ms()):
            del _EXCHANGES[slot]
            return None
        return exchange

    def _drop(self, user: str, transaction: str) -> None:
        _EXCHANGES.pop(self._slot(user, transaction), None)

    def _hold(self, user: str, transaction: str, exchange: Verifying) -> None:
        """Hold one exchange, letting go of whatever timed out anywhere and of this workspace's
        oldest past the limit. A member who opens exchange after exchange crowds out their own
        workspace's and no other's."""
        now = now_ms()
        for slot in [held for held, kept in _EXCHANGES.items() if kept.aged(now)]:
            del _EXCHANGES[slot]
        slot = self._slot(user, transaction)
        ours = [held for held in _EXCHANGES if held[0] == slot[0]]
        while len(ours) >= EXCHANGE_LIMIT:
            oldest = min(ours, key=lambda held: _EXCHANGES[held].at)
            ours.remove(oldest)
            del _EXCHANGES[oldest]
        _EXCHANGES[slot] = exchange

    async def _olm_decrypt(self, rows: Rows, sender_key: str, message: vz.AnyOlmMessage) -> bytes:
        """Decrypt with the sender's session that takes the message, or open an inbound session
        from a pre-key message. The session's new state commits with whatever the plaintext
        carried."""
        record = await rows.get(OLM_SESSIONS, sender_key, lock=True) or {"sessions": []}
        sessions = [vz.Session.from_pickle(p, self._pickle_key) for p in record["sessions"]]
        pre_key = message.to_pre_key()
        for index, session in enumerate(sessions):
            if pre_key is not None and not session.session_matches(pre_key):
                continue
            try:
                plaintext = session.decrypt(message)
            except vz.OlmDecryptionException:
                continue
            sessions.insert(0, sessions.pop(index))
            await self._save_sessions(rows, sender_key, sessions)
            return plaintext
        if pre_key is None:
            raise vz.OlmDecryptionException("no Olm session takes this message")
        session, plaintext = self.account.create_inbound_session(
            vz.Curve25519PublicKey.from_base64(sender_key), pre_key
        )
        await self._save_sessions(rows, sender_key, [session, *sessions])
        await self._save_account(rows=rows)
        return plaintext

    async def _save_sessions(self, rows: Rows, peer: str, sessions: Sequence[vz.Session]) -> None:
        pickles = [s.pickle(self._pickle_key) for s in sessions[:OLM_SESSIONS_KEPT]]
        await rows.put(OLM_SESSIONS, peer, value={"sessions": pickles})

    async def _room_key(
        self, rows: Rows, sender: str, device: dict, content: Mapping[str, Any], *, forwarded: bool
    ) -> None:
        room_id = content.get("room_id")
        session_id = content.get("session_id")
        key = content.get("session_key")
        if content.get("algorithm") != MEGOLM or not all(
            isinstance(v, str) for v in (room_id, session_id, key)
        ):
            return
        if forwarded:
            session = vz.InboundGroupSession.import_session(vz.ExportedSessionKey(key))
        else:
            session = vz.InboundGroupSession(vz.SessionKey(key))
        if session.session_id != session_id:
            warn("matrix.room_key_mismatch")
            return
        held = await rows.get(INBOUND, room_id, session_id, lock=True)
        if held is not None and (
            held["sender"] != sender or held["first_index"] <= session.first_known_index
        ):
            return
        await rows.put(
            INBOUND,
            room_id,
            session_id,
            value={
                "pickle": session.pickle(self._pickle_key),
                "sender": sender,
                "sender_key": device["curve25519"],
                "first_index": session.first_known_index,
            },
        )

    async def decrypt(self, room_id: str, event: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """The plain event an `m.room.encrypted` timeline event carries, or None for one that can
        never be read here — another algorithm, a sender other than the session's, a payload
        naming another room. Raises `KeyMissing` while its session has not arrived."""
        content = event.get("content")
        if not isinstance(content, Mapping) or content.get("algorithm") != MEGOLM:
            return None
        session_id = content.get("session_id")
        ciphertext = content.get("ciphertext")
        if not isinstance(session_id, str) or not isinstance(ciphertext, str):
            return None
        async with self.store.rows() as rows:
            held = await rows.get(INBOUND, room_id, session_id)
        if held is None:
            raise KeyMissing(session_id)
        session = vz.InboundGroupSession.from_pickle(held["pickle"], self._pickle_key)
        try:
            decrypted = session.decrypt(vz.MegolmMessage.from_base64(ciphertext))
        except vz.MegolmDecryptionException:
            raise KeyMissing(session_id) from None
        if held["sender"] != event.get("sender"):
            warn("matrix.megolm_sender_mismatch")
            return None
        payload = json.loads(decrypted.plaintext)
        inner = payload.get("content")
        if payload.get("room_id") != room_id or not isinstance(inner, Mapping):
            return None
        inner = dict(inner)
        if "m.relates_to" in content:
            inner["m.relates_to"] = content["m.relates_to"]
        return {**event, "type": payload.get("type"), "content": inner}

    async def park(self, room_id: str, event: Mapping[str, Any]) -> None:
        """Hold an event whose key has not arrived, and ask the sender's devices for it once per
        session."""
        async with self.store.rows() as rows:
            record = await rows.get(PENDING, lock=True) or {"events": []}
            events = record["events"]
            session_id = event.get("content", {}).get("session_id")
            asked = any(
                p["event"].get("content", {}).get("session_id") == session_id for p in events
            )
            if any(p["event"].get("event_id") == event.get("event_id") for p in events):
                return
            events.append({"room_id": room_id, "event": dict(event), "at": now_ms()})
            await rows.put(PENDING, value={"events": events[-PENDING_LIMIT:]})
        if not asked:
            await self._request_key(room_id, event)

    async def _request_key(self, room_id: str, event: Mapping[str, Any]) -> None:
        content = event.get("content", {})
        sender = event.get("sender")
        if not isinstance(sender, str):
            return
        session_id = str(content.get("session_id"))
        request = {
            "action": "request",
            "body": {
                "algorithm": MEGOLM,
                "room_id": room_id,
                "sender_key": content.get("sender_key", ""),
                "session_id": session_id,
            },
            "request_id": hashlib.sha256(f"{room_id}|{session_id}".encode()).hexdigest()[:24],
            "requesting_device_id": self.device_id,
        }
        try:
            await self.client.send_to_device(KEY_REQUEST, uuid4().hex, {sender: {"*": request}})
        except MatrixError as error:
            log("matrix.key_request_failed", http_status=error.status)

    async def retry(self) -> list[tuple[str, Mapping[str, Any]]]:
        """The parked events whose keys have since arrived, oldest first. An event parked longer
        than `PENDING_SECONDS` is dropped with a warning; the rest stay parked.

        An event whose key arrives but whose plaintext will not read as an event is dropped here and
        now, named by error class. A failure to decrypt is one skipped event and never a stopped
        stream: a parked event that raised out of this loop would leave `since` where it was and the
        pending row unpruned, so one malformed payload would be retried on every sync forever and
        silence the bot for good."""
        async with self.store.rows() as rows:
            record = await rows.get(PENDING) or {"events": []}
        if not record["events"]:
            return []
        kept, found = [], []
        deadline = now_ms() - PENDING_SECONDS * 1000
        for parked in record["events"]:
            try:
                plain = await self.decrypt(parked["room_id"], parked["event"])
            except KeyMissing:
                if parked["at"] < deadline:
                    warn("matrix.undecryptable_dropped", waited_seconds=PENDING_SECONDS)
                else:
                    kept.append(parked)
                continue
            except StoreLocked:
                raise
            except Exception as error:
                warn("matrix.undecryptable", error_class=type(error).__name__)
                continue
            if plain is not None:
                found.append((parked["room_id"], plain))
        async with self.store.rows() as rows:
            await rows.put(PENDING, value={"events": kept})
        return found

    async def encrypt(
        self, room_id: str, event_type: str, content: Mapping[str, Any], settings: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Megolm-encrypt one event for the room, first sharing the room's session with every
        trusted device of every joined member that lacks it. The session rotates when the room's
        `m.room.encryption` settings say it has served long enough, or when a device it was shared
        with has left, so a departed member cannot read what follows."""
        members = await self.client.joined_members(room_id)
        await self.refresh(members)
        async with self.store.rows() as rows:
            targets = await self.trusted(rows, members)
            record = await rows.get(OUTBOUND, room_id, lock=True)
            session, shared = self._outbound(record, settings, targets)
            if session is None:
                session, shared = vz.GroupSession(), set()
                await self._room_key(
                    rows,
                    self.user_id,
                    {"curve25519": self.identity_key},
                    {
                        "algorithm": MEGOLM,
                        "room_id": room_id,
                        "session_id": session.session_id,
                        "session_key": session.session_key.to_base64(),
                    },
                    forwarded=False,
                )
                record = {"created": now_ms(), "count": 0}
            missing = {d: keys for d, keys in targets.items() if d not in shared}
            if missing:
                room_key = {
                    "algorithm": MEGOLM,
                    "room_id": room_id,
                    "session_id": session.session_id,
                    "session_key": session.session_key.to_base64(),
                }
                shared |= await self._olm_send(rows, missing, ROOM_KEY, room_key)
            ciphertext = session.encrypt(
                canonical({"type": event_type, "content": dict(content), "room_id": room_id})
            )
            await rows.put(
                OUTBOUND,
                room_id,
                value={
                    "pickle": session.pickle(self._pickle_key),
                    "created": record["created"],
                    "count": record["count"] + 1,
                    "shared": sorted(list(d) for d in shared),
                },
            )
        return {
            "algorithm": MEGOLM,
            "sender_key": self.identity_key,
            "ciphertext": ciphertext.to_base64(),
            "session_id": session.session_id,
            "device_id": self.device_id,
        }

    def _outbound(
        self,
        record: Mapping[str, Any] | None,
        settings: Mapping[str, Any],
        targets: Mapping[tuple[str, str], dict],
    ) -> tuple[vz.GroupSession | None, set[tuple[str, str]]]:
        if record is None:
            return None, set()
        shared = {(u, d) for u, d in record["shared"]}
        age_limit = settings.get("rotation_period_ms", ROTATION_MS)
        count_limit = settings.get("rotation_period_msgs", ROTATION_MSGS)
        if (
            record["count"] >= (count_limit if isinstance(count_limit, int) else ROTATION_MSGS)
            or now_ms() - record["created"]
            >= (age_limit if isinstance(age_limit, int) else ROTATION_MS)
            or shared - set(targets)
        ):
            return None, set()
        return vz.GroupSession.from_pickle(record["pickle"], self._pickle_key), shared

    async def _olm_send(
        self,
        rows: Rows,
        devices: Mapping[tuple[str, str], dict],
        kind: str,
        content: Mapping[str, Any],
    ) -> set[tuple[str, str]]:
        """Send one payload to each device over Olm, opening a session with a claimed one-time key
        where there is none, and return the devices it reached. A device that offers no signed key
        is left out: a room key is offered again on the next message, and a verification event
        ends that exchange in the member's client."""
        sessions: dict[tuple[str, str], list[vz.Session]] = {}
        unopened = []
        for device, keys in devices.items():
            record = await rows.get(OLM_SESSIONS, keys["curve25519"], lock=True)
            if record and record["sessions"]:
                sessions[device] = [
                    vz.Session.from_pickle(p, self._pickle_key) for p in record["sessions"]
                ]
            else:
                unopened.append(device)
        if unopened:
            wanted: dict[str, list[str]] = {}
            for user, device_id in unopened:
                wanted.setdefault(user, []).append(device_id)
            claimed = await self.client.claim_keys(wanted)
            for user, device_id in unopened:
                pinned = devices[(user, device_id)]
                offered = (claimed.get(user) or {}).get(device_id) or {}
                key = next(
                    (
                        _verified_key(user, device_id, pinned["ed25519"], value)
                        for key_id, value in offered.items()
                        if key_id.startswith(f"{SIGNED_KEY}:")
                    ),
                    None,
                )
                if key is None:
                    log("matrix.no_one_time_key", device_id=device_id)
                    continue
                sessions[(user, device_id)] = [
                    self.account.create_outbound_session(
                        vz.Curve25519PublicKey.from_base64(pinned["curve25519"]),
                        vz.Curve25519PublicKey.from_base64(key),
                    )
                ]
        messages: dict[str, dict[str, Any]] = {}
        for (user, device_id), held in sessions.items():
            pinned = devices[(user, device_id)]
            payload = {
                "type": kind,
                "content": dict(content),
                "sender": self.user_id,
                "sender_device": self.device_id,
                "keys": {"ed25519": self.signing_key},
                "recipient": user,
                "recipient_keys": {"ed25519": pinned["ed25519"]},
            }
            olm, body = held[0].encrypt(canonical(payload)).to_parts()
            messages.setdefault(user, {})[device_id] = {
                "algorithm": OLM,
                "sender_key": self.identity_key,
                "ciphertext": {pinned["curve25519"]: {"type": olm, "body": encode(body)}},
            }
            await self._save_sessions(rows, pinned["curve25519"], held)
        if messages:
            await self.client.send_to_device(ENCRYPTED_TYPE, uuid4().hex, messages)
        return set(sessions)


async def device_for(ctx: SurfaceContext, client: MatrixClient) -> Device | None:
    """The bot's device for this workspace, or None where it has none: the `matrix-e2ee` extra
    absent, an empty store-key slot, a token bound to no device, a slot holding a key the store was
    not sealed with, or a homeserver that refuses the device's keys."""
    if not INSTALLED:
        return None
    try:
        secret = await ctx.credential(STORE_KEY_SLOT)
    except CredentialSlotUnset:
        return None
    user_id, device_id = await client.identity()
    if device_id is None:
        warn("matrix.crypto_no_device")
        return None
    try:
        sealer = Sealer(secret)
    except ValueError:
        warn("matrix.crypto_store_key_short")
        return None
    try:
        return await Device.open(CryptoStore(ctx, sealer, user_id, device_id), client)
    except CryptoUnavailable:
        warn("matrix.crypto_device_contended", device_id=device_id)
    except StoreLocked:
        warn("matrix.crypto_store_locked", device_id=device_id)
    except MatrixError as error:
        if error.status == 429 or error.status >= 500 or error.unauthorized:
            raise
        warn("matrix.crypto_keys_refused", http_status=error.status, errcode=error.errcode)
    return None


async def inbound(
    device: Device | None,
    batch: Mapping[str, Any],
    earlier: Mapping[str, Sequence[Mapping[str, Any]]],
    admitting: bool,
) -> list[tuple[str, Mapping[str, Any]]]:
    """The batch's timeline as the surface hears it: encrypted events replaced by the plain events
    they carry, parked events whose keys have arrived ahead of them, and nothing still ciphertext.
    Without a device the timeline is heard as it came, and an encrypted event founds nothing.

    A batch that carried ciphertext to a bot with no device says so once, and says which of the two
    reasons it was: the libraries are not installed, or they are and the device has no keys — the
    `matrix_store_key` slot empty being the one cause of that which `device_for` passes over in
    silence. A batch carrying no ciphertext is read without a word, so a deploy that encrypts
    nothing and fills no slot logs nothing either."""
    events = list(timeline(batch, earlier))
    if device is None:
        if any(e.get("type") == ENCRYPTED_TYPE for _, e in events):
            if INSTALLED:
                warn("matrix.crypto_no_keys", slot=STORE_KEY_SLOT)
            else:
                warn("matrix.crypto_extra_missing", extra=EXTRA, install=INSTALL)
        return events
    await device.receive(batch)
    heard = await device.retry()
    for room_id, event in events:
        if event.get("type") != ENCRYPTED_TYPE:
            heard.append((room_id, event))
            continue
        try:
            plain = await device.decrypt(room_id, event)
        except KeyMissing:
            if admitting:
                await device.park(room_id, event)
            continue
        except StoreLocked:
            raise
        except Exception as error:
            warn("matrix.undecryptable", error_class=type(error).__name__)
            continue
        if plain is not None:
            heard.append((room_id, plain))
    return heard


async def outbound(
    ctx: SurfaceContext,
    client: MatrixClient,
    room_id: str,
    event_type: str,
    content: Mapping[str, Any],
    settings: Any = UNOBSERVED,
) -> tuple[str, Mapping[str, Any]]:
    """The event type and content an event goes out as: itself in a plain room, Megolm ciphertext
    in an encrypted one. The type the room would have seen is sealed inside the ciphertext, so a
    poll and a message reach an encrypted room as the same `m.room.encrypted` event.

    A caller that has already read the room's state passes it rather than letting this read it
    again. A file and the message naming it are two events, and two reads of a mutable remote value
    can disagree: a room that turns encryption on between them yields a sealed message pointing at
    bytes that were uploaded in the clear, which is the failure sealing the bytes exists to
    prevent."""
    if settings is UNOBSERVED:
        settings = await client.encryption(room_id)
    if settings is None:
        return event_type, content
    if settings.get("algorithm") != MEGOLM:
        raise CryptoUnavailable(f"the room is encrypted with {settings.get('algorithm')}")
    if not INSTALLED:
        raise ExtraMissing()
    device = await device_for(ctx, client)
    if device is None:
        raise CryptoUnavailable("the room is encrypted and the bot has no device keys")
    return ENCRYPTED_TYPE, await device.encrypt(room_id, event_type, content, settings)
