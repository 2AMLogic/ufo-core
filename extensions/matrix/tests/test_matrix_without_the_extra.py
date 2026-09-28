"""What a deploy without the `matrix-e2ee` extra meets, held in the lane where the extra is present.

`crypto.INSTALLED` is the one flag the absence sets, so switching it off here exercises the whole
degradation path without needing the libraries gone from the environment — which is what
`test_matrix_crypto.py` cannot do, since it is the module that disappears exactly when the extra
does. The lane installed without the extra runs this file too, where the flag is already off.

The bot with the extra installed and the `matrix_store_key` slot empty has no device keys either, and
reaches the same code by another road. It is here beside its neighbour because the two are told apart
by the line they log and by nothing else: `matrix.crypto_extra_missing` names a deploy to fix,
`matrix.crypto_no_keys` names a slot to fill."""

import logging
from uuid import uuid4

import pytest

pytest.importorskip("ufo", reason="install ufo from git to run the degradation tests")

import sqlalchemy as sa  # noqa: E402
from matrix_fakes import (  # noqa: E402
    ALICE,
    BOT,
    HOMESERVER,
    ROOM,
    TOKEN,
    Homeserver,
    Listener,
    Workspace,
    batch,
    mention,
    on_loop,
)
from ufo.sdk.surfaces import SurfaceDeliveryError, TerminalFrame, Writeback  # noqa: E402
from ufo_ext_matrix import crypto, crypto_store  # noqa: E402
from ufo_ext_matrix.client import MatrixClient  # noqa: E402
from ufo_ext_matrix.crypto import STORE_KEY_SLOT, device_for, outbound  # noqa: E402
from ufo_ext_matrix.crypto_store import CRYPTO_TABLE, Sealer  # noqa: E402
from ufo_ext_matrix.e2ee import EXTRA, INSTALL, ExtraMissing  # noqa: E402
from ufo_ext_matrix.events import MESSAGE_TYPE, TEXT_MSGTYPE  # noqa: E402
from ufo_ext_matrix.messages import message_content  # noqa: E402
from ufo_ext_matrix.surface import Installation, MatrixSurface  # noqa: E402

MEGOLM = "m.megolm.v1.aes-sha2"
ENCRYPTED = {"algorithm": MEGOLM}
STORE_KEY = "a-store-key-of-at-least-thirty-two-characters"


def ciphertext(event_id: str) -> dict[str, object]:
    """One `m.room.encrypted` event as a member's client sends it. A bot with no device keys must
    not open it, so what is sealed inside does not matter."""
    return {
        "type": "m.room.encrypted",
        "event_id": event_id,
        "sender": ALICE,
        "content": {"algorithm": MEGOLM, "ciphertext": "AwgAEnB", "session_id": "sess"},
    }


def writeback(text: str) -> Writeback:
    return Writeback(
        turn_id=uuid4(),
        conversation_id=uuid4(),
        agent_id=uuid4(),
        queue_key=ROOM,
        terminal=TerminalFrame(status="done", text=text),
        artifacts=(),
        speaker_member_id=uuid4(),
    )


async def primed(server: Homeserver, workspace: Workspace) -> Installation:
    """An installation past its first sync, in a room the homeserver reports as encrypted."""
    server.members = {ROOM: [BOT, ALICE]}
    server.encrypted[ROOM] = ENCRYPTED
    server.syncs.setdefault(None, batch("s1"))
    surface = MatrixSurface(transport=server.transport, environ={})
    installation = Installation(surface, Listener(workspace), BOT)  # type: ignore[arg-type]
    assert await installation.step() == 0.0
    return installation


async def crypto_rows(workspace: Workspace) -> int:
    """How many rows the crypto store holds, which is how a parked event would show."""
    async with workspace.transaction() as connection:
        return len((await connection.execute(sa.select(CRYPTO_TABLE.c.kind))).all())


def named(caplog: pytest.LogCaptureFixture, event: str) -> list[dict[str, object]]:
    """The structured fields of every record `warn` wrote under `event`, which `caplog.text` does
    not carry: `ufo.sdk.o11y` hands them to logging as a record attribute rather than a message."""
    return [
        record.ufo  # type: ignore[attr-defined]
        for record in caplog.records
        if record.getMessage() == event
    ]


@on_loop
async def test_a_batch_carrying_ciphertext_names_the_extra_once_and_is_heard_otherwise_whole(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(crypto, "INSTALLED", False)
    workspace.credentials[STORE_KEY_SLOT] = STORE_KEY
    server = Homeserver()
    installation = await primed(server, workspace)
    server.syncs["s1"] = batch(
        "s2",
        {
            ROOM: [
                ciphertext("$c1"),
                mention("$m1", ALICE, f"{BOT} are you there"),
                ciphertext("$c2"),
            ]
        },
    )
    with caplog.at_level(logging.DEBUG):
        assert await installation.step() == 0.0
    assert named(caplog, "matrix.crypto_extra_missing") == [{"extra": EXTRA, "install": INSTALL}]
    assert [admitted["key"] for admitted in workspace.admitted] == ["$m1"]
    assert await crypto_rows(workspace) == 0


@on_loop
async def test_a_store_key_slot_left_empty_names_the_slot_rather_than_the_extra(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The neighbouring silence: the libraries are installed and the slot is empty, so `device_for`
    returns no device without a word of its own, and the batch that met ciphertext is what says so."""
    monkeypatch.setattr(crypto, "INSTALLED", True)
    assert STORE_KEY_SLOT not in workspace.credentials
    server = Homeserver()
    installation = await primed(server, workspace)
    server.syncs["s1"] = batch(
        "s2", {ROOM: [ciphertext("$c1"), mention("$m1", ALICE, f"{BOT} hi")]}
    )
    with caplog.at_level(logging.DEBUG):
        assert await installation.step() == 0.0
    assert named(caplog, "matrix.crypto_no_keys") == [{"slot": STORE_KEY_SLOT}]
    assert named(caplog, "matrix.crypto_extra_missing") == []
    assert [admitted["key"] for admitted in workspace.admitted] == ["$m1"]


@on_loop
async def test_a_batch_with_no_ciphertext_says_nothing_at_all(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A deploy that encrypts nothing and fills no slot is not misconfigured, so neither line
    appears: the warning is about a room's words going unheard, not about the flag."""
    monkeypatch.setattr(crypto, "INSTALLED", False)
    server = Homeserver()
    installation = await primed(server, workspace)
    server.syncs["s1"] = batch("s2", {ROOM: [mention("$m1", ALICE, f"{BOT} hi")]})
    with caplog.at_level(logging.DEBUG):
        assert await installation.step() == 0.0
    assert named(caplog, "matrix.crypto_extra_missing") == []
    assert named(caplog, "matrix.crypto_no_keys") == []


@on_loop
async def test_a_post_into_an_encrypted_room_refuses_rather_than_going_out_in_the_clear(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(crypto, "INSTALLED", False)
    server = Homeserver()
    server.encrypted[ROOM] = ENCRYPTED
    surface = MatrixSurface(transport=server.transport, environ={})
    with pytest.raises(SurfaceDeliveryError):
        await surface.post(workspace, writeback("ready"))  # type: ignore[arg-type]
    assert server.sent == {}


@on_loop
async def test_the_refusal_is_one_sentence_naming_the_extra_and_the_command(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(crypto, "INSTALLED", False)
    server = Homeserver()
    server.encrypted[ROOM] = ENCRYPTED
    async with MatrixClient(HOMESERVER, TOKEN, transport=server.transport) as client:
        with pytest.raises(ExtraMissing) as raised:
            await outbound(
                workspace,  # type: ignore[arg-type]
                client,
                ROOM,
                MESSAGE_TYPE,
                message_content("ready", TEXT_MSGTYPE),
            )
    sentence = str(raised.value)
    assert sentence.splitlines() == [sentence]
    assert EXTRA in sentence and INSTALL in sentence


@on_loop
async def test_a_plain_room_is_served_without_the_extra_as_with_it(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(crypto, "INSTALLED", False)
    server = Homeserver()
    surface = MatrixSurface(transport=server.transport, environ={})
    assert await surface.post(workspace, writeback("ready"))  # type: ignore[arg-type]
    assert [(sent["type"], sent["body"]) for sent in server.sent.values()] == [
        (MESSAGE_TYPE, "ready")
    ]


@on_loop
async def test_the_bot_opens_no_device_without_the_extra(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(crypto, "INSTALLED", False)
    workspace.credentials[STORE_KEY_SLOT] = STORE_KEY
    server = Homeserver()
    async with MatrixClient(HOMESERVER, TOKEN, transport=server.transport) as client:
        assert await device_for(workspace, client) is None  # type: ignore[arg-type]


def test_a_sealer_names_the_extra_it_wants(monkeypatch: pytest.MonkeyPatch) -> None:
    """The store is declared without `cryptography` so the module imports; a `Sealer` is the one
    thing the library is needed for, and it says which extra installs it."""
    monkeypatch.setattr(crypto_store, "SEALING", False)
    with pytest.raises(ExtraMissing) as raised:
        Sealer(STORE_KEY)
    assert EXTRA in str(raised.value)
