"""End-to-end encryption against a fake homeserver, with real Olm and Megolm on both ends: an
encrypted direct room round trips, a restart keeps its device and its sessions, an event that
arrives before its key is heard once the key lands and never as ciphertext, a post reaches every
member device and no departed one, a device that changes its keys is not believed, a member's client
verifies the bot's device over SAS, and the store opens only under its slot's key."""

import asyncio
import logging
from uuid import uuid4

import pytest

pytest.importorskip("ufo", reason="install ufo from git to run the crypto tests")
pytest.importorskip("vodozemac", reason="install the matrix-e2ee extra to run the crypto tests")
pytest.importorskip("cryptography", reason="install the matrix-e2ee extra to run the crypto tests")

import sqlalchemy as sa
import vodozemac as vz
from crypto_fakes import (
    BOT_DEVICE,
    MEGOLM,
    READY,
    STORE_KEY,
    E2EHomeserver,
    Peer,
    Verifier,
)
from matrix_fakes import (
    ALICE,
    BOB,
    BOT,
    DIRECT,
    HOMESERVER,
    ROOM,
    TOKEN,
    Listener,
    Workspace,
    batch,
    on_loop,
)
from ufo_ext_matrix import crypto
from ufo_ext_matrix.client import MatrixClient
from ufo_ext_matrix.crypto import STORE_KEY_SLOT, device_for
from ufo_ext_matrix.crypto_store import (
    CRYPTO_TABLE,
    CryptoStore,
    Rows,
    Sealer,
    StoreLocked,
)
from ufo_ext_matrix.events import (
    RoomFile,
    txn_id,
)
from ufo_ext_matrix.since import read_since
from ufo_ext_matrix.surface import (
    HOMESERVER_SLOT,
    TOKEN_SLOT,
    Installation,
    MatrixSurface,
)

from ufo.sdk.surfaces import (
    MidTurnReply,
    SharedArtifact,
    SurfaceDeliveryError,
    TerminalFrame,
    Writeback,
)

ENCRYPTED = {"algorithm": MEGOLM}


def rig(server: E2EHomeserver, workspace: Workspace) -> Installation:
    """A fresh surface and installation, as a restarted process builds them."""
    surface = MatrixSurface(transport=server.transport, bots="")
    return Installation(surface, Listener(workspace), BOT)  # type: ignore[arg-type]


async def primed(server: E2EHomeserver, workspace: Workspace) -> Installation:
    """An installation past its first sync, which creates and publishes the bot's device."""
    workspace.credentials[STORE_KEY_SLOT] = STORE_KEY
    server.members.setdefault(ROOM, [BOT, ALICE, BOB])
    server.members.setdefault(DIRECT, [BOT, ALICE])
    server.syncs.setdefault(None, batch("s1"))
    installation = rig(server, workspace)
    assert await installation.step() == 0.0
    return installation


async def post(server: E2EHomeserver, workspace: Workspace, room_id: str, text: str) -> dict:
    wb = Writeback(
        turn_id=uuid4(),
        conversation_id=uuid4(),
        agent_id=uuid4(),
        queue_key=room_id,
        terminal=TerminalFrame(status="done", text=text),
        artifacts=(),
    )
    surface = MatrixSurface(transport=server.transport, bots="")
    await surface.post(workspace, wb)  # type: ignore[arg-type]
    return server.sent[txn_id(wb.turn_id)]


def ciphertexts(*events: dict) -> list[str]:
    return [e["content"]["ciphertext"] for e in events]


@on_loop
async def test_an_encrypted_direct_room_round_trips(workspace: Workspace) -> None:
    server = E2EHomeserver()
    server.encrypted[DIRECT] = ENCRYPTED
    installation = await primed(server, workspace)
    assert server.device_uploads == 1
    alice = Peer(server, ALICE, "ALICEPHONE")
    alice.share(DIRECT)
    said = alice.say(DIRECT, "$d1", "what's on today")
    server.syncs["s1"] = batch("s2", {DIRECT: [said]})
    await installation.step()

    [admitted] = workspace.admitted
    assert admitted["key"] == "$d1"
    assert "what's on today" in admitted["body"]
    assert ciphertexts(said)[0] not in admitted["body"]
    assert admitted["speaker"] == workspace.members["alice@example.org"]

    sent = await post(server, workspace, DIRECT, "Standup at ten.")
    assert sent["type"] == "m.room.encrypted"
    assert "body" not in sent and "Standup" not in str(sent)
    [room_key] = alice.read_inbox()
    assert room_key["type"] == "m.room_key" and room_key["sender"] == BOT
    payload = alice.decrypt(sent)
    assert payload["room_id"] == DIRECT
    assert payload["content"]["msgtype"] == "m.text"
    assert payload["content"]["body"] == "Standup at ten."
    assert payload["content"]["formatted_body"] == "<p>Standup at ten.</p>"


@on_loop
async def test_the_bots_own_encrypted_echo_is_heard_as_its_own(workspace: Workspace) -> None:
    server = E2EHomeserver()
    server.encrypted[DIRECT] = ENCRYPTED
    installation = await primed(server, workspace)
    Peer(server, ALICE, "ALICEPHONE")
    sent = await post(server, workspace, DIRECT, f"{BOT} replying")
    echo = {"type": "m.room.encrypted", "event_id": sent["event_id"], "sender": BOT}
    echo["content"] = {k: v for k, v in sent.items() if k not in ("room", "event_id", "type")}
    server.syncs["s1"] = batch("s2", {DIRECT: [echo]})
    await installation.step()
    assert workspace.admitted == []
    assert [h.line.own for h in installation.heard[DIRECT]] == [True]
    assert not any(e["type"] == "m.room_key_request" for _, e in server.to_device_sent)


@on_loop
async def test_a_restart_keeps_the_device_and_its_sessions(workspace: Workspace) -> None:
    server = E2EHomeserver()
    server.encrypted[ROOM] = ENCRYPTED
    await primed(server, workspace)
    alice = Peer(server, ALICE, "ALICEPHONE")
    alice.share(ROOM)
    server.syncs["s1"] = batch("s2")
    await rig(server, workspace).step()
    before = alice.say(ROOM, "$m1", f"{BOT} sent before the restart")

    restarted = rig(server, workspace)
    server.syncs["s2"] = batch("s3", {ROOM: [before]})
    await restarted.step()
    assert server.device_uploads == 1
    assert [a["key"] for a in workspace.admitted] == ["$m1"]

    alice.outbound.pop(ROOM)
    alice.share(ROOM)
    after = alice.say(ROOM, "$m2", f"{BOT} sent on a new session")
    server.syncs["s3"] = batch("s4", {ROOM: [after]})
    await rig(server, workspace).step()
    assert [a["key"] for a in workspace.admitted] == ["$m1", "$m2"]


@on_loop
async def test_an_event_before_its_key_is_heard_once_the_key_lands(
    workspace: Workspace,
) -> None:
    server = E2EHomeserver()
    server.encrypted[ROOM] = ENCRYPTED
    installation = await primed(server, workspace)
    alice = Peer(server, ALICE, "ALICEPHONE")
    early = alice.say(ROOM, "$m1", f"{BOT} are you there")
    server.syncs["s1"] = batch("s2", {ROOM: [early]})
    await installation.step()
    assert workspace.admitted == []
    [request] = alice.read_inbox()
    assert request["type"] == "m.room_key_request"
    assert request["content"]["body"]["session_id"] == early["content"]["session_id"]

    alice.forward(ROOM)
    server.syncs["s2"] = batch("s3")
    server.syncs["s3"] = batch("s4", {ROOM: [early]})
    await installation.step()
    await installation.step()
    [admitted] = workspace.admitted
    assert admitted["key"] == "$m1"
    assert "are you there" in admitted["body"]
    assert ciphertexts(early)[0] not in admitted["body"]
    assert len(alice.read_inbox()) == 0


@on_loop
async def test_an_event_whose_key_never_comes_is_dropped(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    server = E2EHomeserver()
    server.encrypted[ROOM] = ENCRYPTED
    installation = await primed(server, workspace)
    alice = Peer(server, ALICE, "ALICEPHONE")
    server.syncs["s1"] = batch("s2", {ROOM: [alice.say(ROOM, "$m1", f"{BOT} hello?")]})
    await installation.step()
    later = crypto.now_ms() + (crypto.PENDING_SECONDS + 1) * 1000
    monkeypatch.setattr(crypto, "now_ms", lambda: later)
    server.syncs["s2"] = batch("s3")
    with caplog.at_level(logging.DEBUG):
        await installation.step()
    assert "matrix.undecryptable_dropped" in caplog.text
    alice.share(ROOM)
    server.syncs["s3"] = batch("s4")
    await installation.step()
    assert workspace.admitted == []


@on_loop
async def test_a_post_reaches_every_member_device_and_no_departed_one(
    workspace: Workspace,
) -> None:
    server = E2EHomeserver()
    server.encrypted[ROOM] = ENCRYPTED
    await primed(server, workspace)
    phone = Peer(server, ALICE, "ALICEPHONE")
    laptop = Peer(server, ALICE, "ALICELAPTOP")
    bob = Peer(server, BOB, "BOBPHONE")
    peers = (phone, laptop, bob)

    first = await post(server, workspace, ROOM, "First.")
    for peer in peers:
        assert [p["type"] for p in peer.read_inbox()] == ["m.room_key"]
        assert peer.decrypt(first)["content"]["body"] == "First."

    second = await post(server, workspace, ROOM, "Second.")
    assert second["session_id"] == first["session_id"]
    for peer in peers:
        assert peer.read_inbox() == []
        assert peer.decrypt(second)["content"]["body"] == "Second."

    server.members[ROOM] = [BOT, ALICE]
    third = await post(server, workspace, ROOM, "Third.")
    assert third["session_id"] != first["session_id"]
    assert bob.read_inbox() == []
    assert third["session_id"] not in bob.inbound
    for peer in (phone, laptop):
        assert [p["type"] for p in peer.read_inbox()] == ["m.room_key"]
        assert peer.decrypt(third)["content"]["body"] == "Third."


@on_loop
async def test_a_device_that_changes_its_keys_is_not_believed(workspace: Workspace) -> None:
    """Trust on first use: the keys a device id first shows are pinned. A device id that later
    shows other keys is sent no room key, and a room key it sends is not taken."""
    server = E2EHomeserver()
    server.encrypted[ROOM] = ENCRYPTED
    installation = await primed(server, workspace)
    phone = Peer(server, ALICE, "ALICEPHONE")
    await post(server, workspace, ROOM, "Pinned.")
    phone.read_inbox()

    impostor = Peer(server, ALICE, "ALICEPHONE")
    server.members[ROOM] = [BOT, ALICE]
    server.syncs["s1"] = {**batch("s2"), "device_lists": {"changed": [ALICE]}}
    await installation.step()
    await post(server, workspace, ROOM, "After the change.")
    assert impostor.read_inbox() == []

    impostor.share(ROOM)
    forged = impostor.say(ROOM, "$f1", f"{BOT} trust me")
    server.syncs["s2"] = batch("s3", {ROOM: [forged]})
    await installation.step()
    assert workspace.admitted == []


async def confirmed(server: E2EHomeserver, workspace: Workspace, user: str) -> dict[str, str]:
    """The devices of `user` the bot holds verified, as the bot's own device answers it."""
    async with MatrixClient(
        workspace.credentials[HOMESERVER_SLOT],
        workspace.credentials[TOKEN_SLOT],
        transport=server.transport,
    ) as client:
        device = await device_for(workspace, client)  # type: ignore[arg-type]
        assert device is not None
        return await device.verified(user)


async def exchange(installation: Installation, verifier: Verifier, rounds: int = 5) -> None:
    """The exchange to its end: one round is one sync of the bot's and the client's answer."""
    for _ in range(rounds):
        await installation.step()
        verifier.answer()


@on_loop
async def test_a_member_verifies_the_bots_device_over_sas(workspace: Workspace) -> None:
    """The member's client starts, and the bot answers every event of the exchange — no command, no
    keyword, nothing but to-device protocol. The client's MAC over the bot's device key is the
    verification the member asked for, and the bot records which device confirmed it."""
    server = E2EHomeserver()
    server.encrypted[DIRECT] = ENCRYPTED
    installation = await primed(server, workspace)
    alice = Peer(server, ALICE, "ALICEPHONE")
    assert await confirmed(server, workspace, ALICE) == {}

    verifier = Verifier(alice)
    verifier.request()
    await exchange(installation, verifier)

    assert verifier.verified
    assert verifier.done
    assert verifier.cancelled is None
    assert verifier.strings == ["emoji", "decimal"]
    assert await confirmed(server, workspace, ALICE) == {"ALICEPHONE": alice.ed}
    assert workspace.admitted == []


@on_loop
async def test_a_mac_over_a_key_this_device_does_not_hold_ends_in_a_cancel(
    workspace: Workspace,
) -> None:
    """A MAC that covers any key but the pinned one ends the exchange in `m.key_mismatch`, and
    nothing is recorded verified. A false verified is the one outcome worse than none."""
    server = E2EHomeserver()
    server.encrypted[DIRECT] = ENCRYPTED
    installation = await primed(server, workspace)
    alice = Peer(server, ALICE, "ALICEPHONE")
    verifier = Verifier(alice, honest=False)
    verifier.request()
    await exchange(installation, verifier)

    assert verifier.cancelled == "m.key_mismatch"
    assert not verifier.verified
    assert not verifier.done
    assert await confirmed(server, workspace, ALICE) == {}


def in_flight(workspace: Workspace) -> list[crypto.Verifying]:
    """The exchanges the table holds for this workspace, which is the scope its bounds count in."""
    held = crypto._EXCHANGES.items()
    return [kept for slot, kept in held if slot[0] == str(workspace.workspace_id)]


@on_loop
async def test_an_exchange_older_than_the_age_bound_is_not_answered(
    workspace: Workspace,
) -> None:
    """The age bound holds on a bot nobody else is verifying, where no later exchange starts to
    sweep this one. A `.key` for an exchange `EXCHANGE_SECONDS` old — the age the bound counts as
    let go — is answered with `m.unknown_transaction` and never with this end's key."""
    server = E2EHomeserver()
    server.encrypted[DIRECT] = ENCRYPTED
    installation = await primed(server, workspace)
    alice = Peer(server, ALICE, "ALICEPHONE")
    verifier = Verifier(alice)
    verifier.request()
    await exchange(installation, verifier, rounds=2)

    [held] = in_flight(workspace)
    held.at -= crypto.EXCHANGE_SECONDS * 1000
    await exchange(installation, verifier, rounds=1)

    assert verifier.cancelled == "m.unknown_transaction"
    assert not verifier.verified
    assert not verifier.done
    assert in_flight(workspace) == []
    assert await confirmed(server, workspace, ALICE) == {}


async def opened(server: E2EHomeserver, workspace: Workspace) -> crypto.Device:
    """The bot's device for this workspace, opened straight from the store: the exchange table hangs
    off the device, so its bounds want nothing a sync brings."""
    workspace.credentials[STORE_KEY_SLOT] = STORE_KEY
    async with MatrixClient(
        workspace.credentials[HOMESERVER_SLOT],
        workspace.credentials[TOKEN_SLOT],
        transport=server.transport,
    ) as client:
        device = await device_for(workspace, client)  # type: ignore[arg-type]
    assert device is not None
    return device


@on_loop
async def test_one_workspaces_exchanges_outlive_anothers_filling_the_table(
    workspace: Workspace,
) -> None:
    """`EXCHANGE_LIMIT` counts one workspace's exchanges. A member opening exchange after exchange
    lets go of their own workspace's oldest and of no other workspace's, so a bot serving several
    workspaces has no member who can end another's verification."""
    theirs = Workspace(engine=workspace.engine)
    ours = await opened(E2EHomeserver(), workspace)
    neighbour = await opened(E2EHomeserver(), theirs)
    at = crypto.now_ms()
    neighbour._hold(ALICE, "t-theirs", crypto.Verifying("ALICEPHONE", "", vz.Sas(), at=at))
    for count in range(crypto.EXCHANGE_LIMIT + 1):
        opening = crypto.Verifying("ALICEPHONE", "", vz.Sas(), at=at + 1 + count)
        ours._hold(ALICE, f"t-{count}", opening)

    assert neighbour._held(ALICE, "t-theirs") is not None
    assert len(in_flight(workspace)) == crypto.EXCHANGE_LIMIT
    assert ours._held(ALICE, "t-0") is None
    assert ours._held(ALICE, "t-1") is not None
    assert ours._held(ALICE, f"t-{crypto.EXCHANGE_LIMIT}") is not None


@on_loop
async def test_a_member_who_never_verifies_is_served_exactly_as_before(
    workspace: Workspace,
) -> None:
    """Trust on first use is what decides who is sent a room key. A device that never verifies is
    pinned, hears what the bot posts, is heard back, and is recorded verified nowhere."""
    server = E2EHomeserver()
    server.encrypted[DIRECT] = ENCRYPTED
    installation = await primed(server, workspace)
    alice = Peer(server, ALICE, "ALICEPHONE")
    alice.share(DIRECT)
    server.syncs["s1"] = batch("s2", {DIRECT: [alice.say(DIRECT, "$d1", "what's on today")]})
    await installation.step()
    assert [a["key"] for a in workspace.admitted] == ["$d1"]

    sent = await post(server, workspace, DIRECT, "Standup at ten.")
    assert [p["type"] for p in alice.read_inbox()] == ["m.room_key"]
    assert alice.decrypt(sent)["content"]["body"] == "Standup at ten."
    assert await confirmed(server, workspace, ALICE) == {}


@on_loop
async def test_a_request_in_the_clear_opens_an_exchange_that_runs_over_olm(
    workspace: Workspace,
) -> None:
    """Clients send the opening request with no Olm around it. It names no key and its one effect is
    the `.ready` that comes back, so it is answered — and the exchange it opens finishes as any
    other, every event of it Olm-encrypted."""
    server = E2EHomeserver()
    server.encrypted[DIRECT] = ENCRYPTED
    installation = await primed(server, workspace)
    alice = Peer(server, ALICE, "ALICEPHONE")
    verifier = Verifier(alice)
    verifier.request(clear=True)
    await installation.step()
    assert verifier.answer() == [READY]
    await exchange(installation, verifier, rounds=4)

    assert verifier.verified and verifier.done
    assert await confirmed(server, workspace, ALICE) == {"ALICEPHONE": alice.ed}


@on_loop
async def test_a_start_in_the_clear_is_refused(
    workspace: Workspace, caplog: pytest.LogCaptureFixture
) -> None:
    """An unencrypted to-device event names a user and not a device, so it can never be the
    evidence that one device is verified. A `.start` in the clear is ended where the member's client
    can show why, rather than left to hang."""
    server = E2EHomeserver()
    server.encrypted[DIRECT] = ENCRYPTED
    installation = await primed(server, workspace)
    alice = Peer(server, ALICE, "ALICEPHONE")
    verifier = Verifier(alice)
    verifier.begin(clear=True)
    with caplog.at_level(logging.DEBUG):
        await installation.step()
    verifier.answer()

    assert "matrix.verification_in_the_clear" in caplog.text
    assert verifier.cancelled == "m.invalid_message"
    assert not verifier.verified
    assert await confirmed(server, workspace, ALICE) == {}


@on_loop
async def test_a_post_into_an_encrypted_room_without_a_store_key_is_refused(
    workspace: Workspace,
) -> None:
    server = E2EHomeserver()
    server.encrypted[ROOM] = ENCRYPTED
    with pytest.raises(SurfaceDeliveryError, match="no device keys"):
        await post(server, workspace, ROOM, "Should not leak.")
    assert server.sent == {}


@on_loop
async def test_the_store_is_unreadable_without_the_slots_key(workspace: Workspace) -> None:
    server = E2EHomeserver()
    server.encrypted[ROOM] = ENCRYPTED
    await primed(server, workspace)
    Peer(server, ALICE, "ALICEPHONE")
    await post(server, workspace, ROOM, "Sealed.")

    async with workspace.engine.begin() as connection:
        rows = (await connection.execute(sa.select(CRYPTO_TABLE))).all()
    assert {row.kind for row in rows} >= {"account", "devices", "olm", "inbound", "outbound"}
    assert {(row.user_id, row.device_id) for row in rows} == {(BOT, BOT_DEVICE)}
    for row in rows:
        assert ROOM not in row.name and ALICE not in row.name
        for secret in (ROOM.encode(), ALICE.encode(), b"pickle", b"session_key", b"Sealed"):
            assert secret not in row.value

    other = CryptoStore(
        workspace, Sealer("another-key-of-at-least-thirty-two-chars"), BOT, BOT_DEVICE
    )
    stolen = CryptoStore(workspace, Sealer(STORE_KEY), BOT, BOT_DEVICE)
    async with stolen.rows() as opened:
        assert await opened.get("account") is not None
    with pytest.raises(StoreLocked):
        async with other.rows() as locked:
            await locked.get("account")

    workspace.credentials[STORE_KEY_SLOT] = "another-key-of-at-least-thirty-two-chars"
    async with MatrixClient(
        workspace.credentials[HOMESERVER_SLOT],
        workspace.credentials[TOKEN_SLOT],
        transport=server.transport,
    ) as client:
        assert await device_for(workspace, client) is None  # type: ignore[arg-type]
    assert server.device_uploads == 1


async def pending(workspace: Workspace) -> list[dict]:
    store = CryptoStore(workspace, Sealer(STORE_KEY), BOT, BOT_DEVICE)  # type: ignore[arg-type]
    async with store.rows() as rows:
        return (await rows.get("pending") or {"events": []})["events"]


async def stored_account(workspace: Workspace) -> str:
    """The Curve25519 key of the account the store holds for the bot's device."""
    store = CryptoStore(workspace, Sealer(STORE_KEY), BOT, BOT_DEVICE)  # type: ignore[arg-type]
    async with store.rows() as rows:
        held = await rows.get("account")
    return vz.Account.from_pickle(
        held["pickle"], store.sealer.pickle_key
    ).curve25519_key.to_base64()


@on_loop
async def test_two_processes_opening_one_device_agree_on_one_account(
    workspace: Workspace,
) -> None:
    """`Installation.deliver` and the writeback path each open the device, so two of them meeting a
    first encrypted event together is an ordinary interleaving, not a contrivance. Both leave with
    one account, and the homeserver is offered one set of device keys."""
    server = E2EHomeserver()
    server.encrypted[DIRECT] = ENCRYPTED
    workspace.credentials[STORE_KEY_SLOT] = STORE_KEY
    async with MatrixClient(
        workspace.credentials[HOMESERVER_SLOT],
        workspace.credentials[TOKEN_SLOT],
        transport=server.transport,
    ) as client:
        opened = await asyncio.gather(
            device_for(workspace, client),  # type: ignore[arg-type]
            device_for(workspace, client),  # type: ignore[arg-type]
        )
    keys = {device.identity_key for device in opened if device is not None}
    assert [device is not None for device in opened] == [True, True]
    assert len(keys) == 1
    assert server.device_uploads == 1
    published = server.devices[BOT][BOT_DEVICE]["keys"][f"curve25519:{BOT_DEVICE}"]
    assert published == keys.pop() == await stored_account(workspace)


@on_loop
async def test_a_mint_that_loses_reads_the_winners_account(workspace: Workspace) -> None:
    """The interleaving the primary key arbitrates, held still: a caller whose read saw no account
    mints one, loses the insert, and leaves with the stored account rather than publishing its own
    keys over the winner's. Two accounts for one device id is #16's named failure — the published
    and the stored keys diverge and the bot silently stops decrypting, with no log to say why."""
    server = E2EHomeserver()
    server.encrypted[DIRECT] = ENCRYPTED
    await primed(server, workspace)
    winner = await stored_account(workspace)
    honest = Rows.get
    lied: list[bool] = []

    async def blind(self: Rows, kind: str, *key: str, lock: bool = False) -> object:
        """The stale read: the account row is there and this caller's first look does not see it."""
        if kind == "account" and not lied:
            lied.append(True)
            return None
        return await honest(self, kind, *key, lock=lock)

    Rows.get = blind  # type: ignore[method-assign]
    try:
        async with MatrixClient(
            workspace.credentials[HOMESERVER_SLOT],
            workspace.credentials[TOKEN_SLOT],
            transport=server.transport,
        ) as client:
            late = await device_for(workspace, client)  # type: ignore[arg-type]
    finally:
        Rows.get = honest  # type: ignore[method-assign]

    assert late is not None
    assert late.identity_key == winner
    assert await stored_account(workspace) == winner
    assert server.device_uploads == 1
    assert server.devices[BOT][BOT_DEVICE]["keys"][f"curve25519:{BOT_DEVICE}"] == winner


@on_loop
async def test_a_parked_event_that_will_not_decrypt_is_dropped_rather_than_retried(
    workspace: Workspace,
) -> None:
    """A parked event whose key arrives but whose plaintext is no Matrix event is dropped, named by
    error class, and the stream carries on past it. Raising out of the retry loop would leave the
    position where it was and the pending row unpruned, so one malformed payload would be retried on
    every sync for good and silence that bot."""
    server = E2EHomeserver()
    server.encrypted[ROOM] = ENCRYPTED
    installation = await primed(server, workspace)
    alice = Peer(server, ALICE, "ALICEPHONE")
    server.syncs["s1"] = batch("s2", {ROOM: [alice.babble(ROOM, "$m1")]})
    assert await installation.step() == 0.0
    assert workspace.admitted == []
    assert await read_since(workspace, BOT) == "s2"  # type: ignore[arg-type]
    assert len(await pending(workspace)) == 1

    alice.forward(ROOM)
    good = alice.say(ROOM, "$m2", f"{BOT} and again")
    server.syncs["s2"] = batch("s3", {ROOM: [good]})
    assert await installation.step() == 0.0
    assert [a["key"] for a in workspace.admitted] == ["$m2"]
    assert await read_since(workspace, BOT) == "s3"  # type: ignore[arg-type]
    assert await pending(workspace) == []


@on_loop
async def test_every_delivery_handler_encrypts_what_it_sends(workspace: Workspace) -> None:
    """A room's words leave by three handlers — the terminal reply, a shared file, and a mid-turn
    reply — and an encrypted room takes ciphertext from all three. A handler that sent its own
    message rather than going through the surface's one seam would put a filename, a subject or a
    mid-turn line on the server's timeline in the clear."""
    server = E2EHomeserver()
    server.encrypted[DIRECT] = ENCRYPTED
    await primed(server, workspace)
    alice = Peer(server, ALICE, "ALICEPHONE")
    surface = MatrixSurface(transport=server.transport, bots="")

    chart = SharedArtifact(
        id=uuid4(),
        blob_key="blob-chart",
        filename="chart.png",
        media_type="image/png",
        size_bytes=4,
        subject="Last week",
        role="file",
    )
    workspace.blob.objects = {chart.blob_key: b"\x89PNG"}
    wb = Writeback(
        turn_id=uuid4(),
        conversation_id=uuid4(),
        agent_id=uuid4(),
        queue_key=DIRECT,
        terminal=TerminalFrame(status="done", text="Standup at ten."),
        artifacts=(chart,),
    )
    reply_ref = await surface.post(workspace, wb)  # type: ignore[arg-type]
    await surface.attach(workspace, wb, str(reply_ref))  # type: ignore[arg-type]
    said = MidTurnReply(
        id=uuid4(),
        turn_id=wb.turn_id,
        conversation_id=wb.conversation_id,
        agent_id=wb.agent_id,
        queue_key=DIRECT,
        message_ref=None,
        text="Halfway through.",
    )
    await surface.speak(workspace, said)  # type: ignore[arg-type]

    assert len(server.sent) == 3
    for event in server.sent.values():
        assert event["type"] == "m.room.encrypted"
        assert event["algorithm"] == MEGOLM
    for leak in ("Standup at ten.", "Halfway through.", "chart.png", "Last week", "mxc://"):
        assert leak not in str(server.sent), leak

    assert [event["type"] for event in alice.read_inbox()] == ["m.room_key"]
    bodies = [alice.decrypt(event)["content"] for event in server.sent.values()]
    said_bodies = [body.get("body", "") for body in bodies]
    for expected in ("Standup at ten.", "Halfway through.", "Last week"):
        assert any(expected in body for body in said_bodies), expected
    [picture] = [body for body in bodies if body["msgtype"] == "m.image"]
    assert picture["filename"] == "chart.png"
    assert "url" not in picture
    assert picture["file"]["url"].startswith("mxc://")
    assert picture["file"]["key"]["alg"] == crypto.FILE_ALGORITHM
    assert picture["info"]["mimetype"] == "image/png"

    [(_, stored)] = server.uploaded
    assert stored != b"\x89PNG"
    assert crypto.open_file(picture["file"], stored) == b"\x89PNG"


def test_a_short_store_key_is_refused() -> None:
    with pytest.raises(ValueError, match="shorter"):
        Sealer("short")


def test_a_row_sealed_for_one_address_does_not_open_at_another() -> None:
    sealer = Sealer(STORE_KEY)
    blob = sealer.seal(b"one", {"pickle": "x"})
    assert sealer.open(b"one", blob) == {"pickle": "x"}
    with pytest.raises(StoreLocked):
        sealer.open(b"two", blob)


def test_device_keys_must_be_self_signed() -> None:
    account = vz.Account()
    keys = {
        "user_id": ALICE,
        "device_id": "D",
        "algorithms": [MEGOLM],
        "keys": {
            "curve25519:D": account.curve25519_key.to_base64(),
            "ed25519:D": account.ed25519_key.to_base64(),
        },
    }
    signature = account.sign(crypto.canonical(keys)).to_base64()
    signed = {**keys, "signatures": {ALICE: {"ed25519:D": signature}}}
    assert crypto.verified_device(ALICE, "D", signed) == {
        "curve25519": account.curve25519_key.to_base64(),
        "ed25519": account.ed25519_key.to_base64(),
    }
    assert crypto.verified_device(BOB, "D", signed) is None
    tampered = {
        **signed,
        "keys": {**keys["keys"], "curve25519:D": vz.Account().curve25519_key.to_base64()},
    }
    assert crypto.verified_device(ALICE, "D", tampered) is None


def test_a_sealed_file_round_trips() -> None:
    """The bytes a room gets back are the bytes that went in, and nothing about the plaintext is
    left in the event: the `mxc://` names ciphertext, and the key that opens it travels inside the
    Megolm payload rather than beside it."""
    data = b"the quarterly numbers, and a picture of a cat" * 40
    ciphertext, sealed = crypto.seal_file(data)
    assert ciphertext != data
    assert data not in ciphertext
    assert sealed["v"] == crypto.FILE_VERSION
    assert sealed["key"]["alg"] == crypto.FILE_ALGORITHM
    assert sealed["key"]["kty"] == "oct"
    assert crypto.open_file(sealed, ciphertext) == data


def test_each_file_is_sealed_under_its_own_key_and_counter() -> None:
    """AES-CTR reuses a keystream whenever the key and counter repeat, and two files sealed under
    one pair are readable from their XOR without either key. So the pair is fresh per file, and the
    same bytes sealed twice share no ciphertext."""
    data = b"identical content"
    first, one = crypto.seal_file(data)
    second, two = crypto.seal_file(data)
    assert one["key"]["k"] != two["key"]["k"]
    assert one["iv"] != two["iv"]
    assert first != second


def test_the_counter_half_of_the_iv_starts_at_zero() -> None:
    """The low half of the IV is the block counter and the high half is the nonce.

    The literals are written out rather than taken from the constants under test: sliced by its own
    `FILE_COUNTER_BYTES`, this holds for any value of it, and a four-byte counter is both off-spec
    and a 32 GiB ceiling before it carries into the nonce."""
    assert crypto.FILE_IV_BYTES == 16
    assert crypto.FILE_COUNTER_BYTES == 8
    _, sealed = crypto.seal_file(b"x")
    iv = crypto.decode(sealed["iv"])
    assert len(iv) == 16
    assert iv[8:] == bytes(8)
    assert iv[:8] != bytes(8)


def test_a_hash_that_does_not_match_is_never_decrypted(monkeypatch: pytest.MonkeyPatch) -> None:
    """What the media repository answers with is not yet what the sender sealed.

    The cipher is replaced with a failure, so this asserts the ordering rather than the raise: a
    test that only catches `FileHashMismatch` passes just as well when the decryption happens first
    and the check happens after, which is the arrangement the bold claim rules out."""
    ciphertext, sealed = crypto.seal_file(b"the original file")
    tampered = bytes([ciphertext[0] ^ 0xFF]) + ciphertext[1:]
    monkeypatch.setattr(
        crypto,
        "_stream",
        lambda *_: pytest.fail("the ciphertext reached the cipher before the hash was checked"),
    )
    for sealed_file, body in (
        (sealed, tampered),
        ({**sealed, "hashes": {}}, ciphertext),
        ({**sealed, "hashes": None}, ciphertext),
        ({**sealed, "hashes": "deadbeef"}, ciphertext),
        ({**sealed, "key": {**sealed["key"], "alg": "A128CTR"}}, ciphertext),
        ({**sealed, "key": {**sealed["key"], "k": None}}, ciphertext),
        ({k: v for k, v in sealed.items() if k != "iv"}, ciphertext),
    ):
        with pytest.raises(crypto.FileHashMismatch):
            crypto.open_file(sealed_file, body)


def test_a_malformed_file_is_a_dropped_file_and_never_an_unhandled_error() -> None:
    """Every field is a hostile sender's to choose, the whole object included.

    It arrives as an event's `file`, so `{"msgtype": "m.image", "file": "gotcha"}` reaches this with
    a string. A reader drops a file by catching `FileHashMismatch`, so a shape that escapes as
    `AttributeError` or `KeyError` turns a dropped attachment into an unhandled exception in
    whoever is reading the room."""
    ciphertext, sealed = crypto.seal_file(b"a file")
    for broken in (
        "gotcha",
        None,
        42,
        [],
        {},
        {**sealed, "hashes": None},
        {**sealed, "hashes": "deadbeef"},
        {**sealed, "hashes": {"sha256": "not base64 !!"}},
        {**sealed, "key": None},
        {**sealed, "key": {**sealed["key"], "k": None}},
        {**sealed, "key": {**sealed["key"], "k": "c2hvcnQ="}},
        {**sealed, "iv": crypto.encode(b"12345678")},
        {k: v for k, v in sealed.items() if k != "iv"},
    ):
        with pytest.raises(crypto.FileHashMismatch):
            crypto.open_file(broken, ciphertext)


def test_a_file_naming_another_algorithm_is_refused() -> None:
    """The algorithm is the sender's claim about what it sealed with. A file naming something else
    is not opened under `A256CTR` on the assumption it meant that."""
    ciphertext, sealed = crypto.seal_file(b"a file")
    other = {**sealed, "key": {**sealed["key"], "alg": "A128CTR"}}
    with pytest.raises(crypto.FileHashMismatch):
        crypto.open_file(other, ciphertext)


@on_loop
async def test_a_shared_file_costs_one_reading_of_the_rooms_encryption(
    workspace: Workspace,
) -> None:
    """The bytes and the message naming them are decided by one observation, not two.

    Reading a mutable remote value twice can disagree. A room that turned encryption on between the
    upload and the send would seal the message over bytes already in the clear — a timeline that
    looks encrypted above a media repository that is not, which is the failure sealing the bytes
    exists to prevent."""
    server = E2EHomeserver()
    server.encrypted[DIRECT] = ENCRYPTED
    await primed(server, workspace)
    surface = MatrixSurface(transport=server.transport, bots="")
    chart = SharedArtifact(
        id=uuid4(),
        blob_key="blob-chart",
        filename="chart.png",
        media_type="image/png",
        size_bytes=4,
        subject=None,
        role="file",
    )
    workspace.blob.objects = {chart.blob_key: b"\x89PNG"}
    wb = Writeback(
        turn_id=uuid4(),
        conversation_id=uuid4(),
        agent_id=uuid4(),
        queue_key=DIRECT,
        terminal=TerminalFrame(status="done", text="Here."),
        artifacts=(chart,),
    )
    reply_ref = await surface.post(workspace, wb)  # type: ignore[arg-type]
    before = _encryption_reads(server)
    await surface.attach(workspace, wb, str(reply_ref))  # type: ignore[arg-type]
    assert _encryption_reads(server) - before == 1


def _encryption_reads(server: E2EHomeserver) -> int:
    return sum("m.room.encryption" in str(request.url) for request in server.requests)


@on_loop
async def test_a_sealed_member_file_is_opened_after_its_hash(workspace: Workspace) -> None:
    """The bytes a member sealed come back as the bytes they sealed, and the media repository
    holds ciphertext throughout."""
    server = E2EHomeserver()
    installation = await primed(server, workspace)
    plaintext = b"the quarterly numbers"
    ciphertext, sealed = crypto.seal_file(plaintext)
    async with MatrixClient(HOMESERVER, TOKEN, transport=server.transport) as client:
        uri = await client.upload("q3.pdf", "application/octet-stream", ciphertext)
        shared = RoomFile(
            room_id=DIRECT,
            event_id="$f",
            sender=ALICE,
            filename="q3.pdf",
            media_type="application/pdf",
            size_bytes=len(plaintext),
            sealed={**sealed, "url": uri},
        )
        assert await installation.fetched(client, shared) == plaintext
    [(_, stored)] = server.uploaded
    assert stored == ciphertext != plaintext


@on_loop
async def test_a_tampered_sealed_file_is_dropped_not_raised(workspace: Workspace) -> None:
    """The media repository is the homeserver's, so what it answers with is a claim. A body that
    does not match the file's own sha256 costs that file and never reaches the cipher — and never
    stops the room being read."""
    server = E2EHomeserver()
    installation = await primed(server, workspace)
    ciphertext, sealed = crypto.seal_file(b"the original")
    tampered = bytes([ciphertext[0] ^ 0xFF]) + ciphertext[1:]
    async with MatrixClient(HOMESERVER, TOKEN, transport=server.transport) as client:
        uri = await client.upload("q3.pdf", "application/octet-stream", tampered)
        shared = RoomFile(
            room_id=DIRECT,
            event_id="$f",
            sender=ALICE,
            filename="q3.pdf",
            media_type="application/pdf",
            size_bytes=len(ciphertext),
            sealed={**sealed, "url": uri},
        )
        assert await installation.fetched(client, shared) is None
