"""Where the bot's device keeps its secrets: its Olm account, its Olm and Megolm sessions, the
device keys it pinned on first use, and the encrypted events waiting for a key. They live in
`matrix_ext_crypto`, a table this extension's migration owns, never in workspace files, a scoped
store, or the sandbox.

Every value is sealed with AES-256-GCM under a key derived from the `matrix_store_key` slot, with
the row's own address as associated data, so a value copied into another row does not open. A row's
name is an HMAC of what it names, so the table does not list the rooms, people, or sessions the bot
holds keys for. Only the bot's own user and device id are written in the clear: they are what a
store is for, and a token bound to another device finds no rows and starts its own.

The sealing itself is `cryptography`, which the `matrix-e2ee` extra installs. The table and its
addresses are declared without it, so this module imports wherever the extension does; a `Sealer`
is what the extra is needed for, and it raises `ExtraMissing` rather than be built without one."""

import hashlib
import hmac
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncConnection

from ufo_ext_matrix.e2ee import ExtraMissing
from ufo_ext_matrix.since import Transactional

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    SEALING = True
except ImportError:
    SEALING = False

MIN_SECRET_CHARS = 32
NONCE_BYTES = 12
KEY_BYTES = 32

CRYPTO_TABLE = sa.Table(
    "matrix_ext_crypto",
    sa.MetaData(),
    sa.Column(
        "workspace_id",
        sa.Uuid(),
        sa.ForeignKey("workspace.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("user_id", sa.Text(), primary_key=True),
    sa.Column("device_id", sa.Text(), primary_key=True),
    sa.Column("kind", sa.Text(), primary_key=True),
    sa.Column("name", sa.Text(), primary_key=True),
    sa.Column("value", sa.LargeBinary(), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)


def _insert_ignoring(dialect: str) -> Any:
    """An insert that leaves an existing row alone, in the two dialects ufo runs on."""
    if dialect == "postgresql":
        return postgresql.insert(CRYPTO_TABLE).on_conflict_do_nothing()
    if dialect == "sqlite":
        return sqlite.insert(CRYPTO_TABLE).on_conflict_do_nothing()
    raise NotImplementedError(f"the crypto store has no insert for {dialect}")


class StoreLocked(Exception):
    """A row did not open under the slot's key: the slot now holds a different secret than the one
    the store was sealed with."""


def _derive(secret: bytes, purpose: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_BYTES,
        salt=None,
        info=b"ufo-ext-matrix crypto store " + purpose,
    ).derive(secret)


class Sealer:
    """The three keys one slot secret yields: one seals values, one names rows, and one pickles
    vodozemac objects before they are sealed."""

    def __init__(self, secret: str) -> None:
        if not SEALING:
            raise ExtraMissing()
        if len(secret) < MIN_SECRET_CHARS:
            raise ValueError(f"the store key is shorter than {MIN_SECRET_CHARS} characters")
        material = secret.encode()
        self._aead = AESGCM(_derive(material, b"seal"))
        self._names = _derive(material, b"names")
        self.pickle_key = _derive(material, b"pickle")

    def __repr__(self) -> str:
        return "Sealer()"

    def name(self, parts: tuple[str, ...]) -> str:
        """A row's name: empty for a kind the device holds one of, such as its account, so a store
        opened under the wrong key finds that row and fails to open it rather than finding
        nothing and starting over."""
        if not parts:
            return ""
        joined = "\x1f".join(parts).encode()
        return hmac.new(self._names, joined, hashlib.sha256).hexdigest()

    def seal(self, address: bytes, value: Any) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        plaintext = json.dumps(value, separators=(",", ":")).encode()
        return nonce + self._aead.encrypt(nonce, plaintext, address)

    def open(self, address: bytes, blob: bytes) -> Any:
        try:
            plaintext = self._aead.decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], address)
        except InvalidTag:
            raise StoreLocked("a crypto store row did not open under the store key") from None
        return json.loads(plaintext)


@dataclass(frozen=True)
class CryptoStore:
    """One device's rows in one workspace."""

    ctx: Transactional
    sealer: Sealer
    user_id: str
    device_id: str

    @asynccontextmanager
    async def rows(self) -> AsyncIterator["Rows"]:
        """One transaction over the device's rows: everything read and written inside it commits
        together, or not at all."""
        async with self.ctx.transaction() as connection:
            yield Rows(self, connection)


@dataclass(frozen=True)
class Rows:
    store: CryptoStore
    connection: AsyncConnection

    async def get(self, kind: str, *key: str, lock: bool = False) -> Any:
        """The value at `(kind, key)`, or None. `lock` holds the row until the transaction ends,
        where the database can, so two writers never ratchet the same session from one state."""
        name = self.store.sealer.name(key)
        query = sa.select(CRYPTO_TABLE.c.value).where(self._row(kind, name))
        if lock:
            query = query.with_for_update()
        blob = (await self.connection.execute(query)).scalar_one_or_none()
        return None if blob is None else self.store.sealer.open(self._address(kind, name), blob)

    async def create(self, kind: str, *key: str, value: Any) -> bool:
        """Write the row only if no row is there, and say whether this call is the one that wrote
        it. The table's own primary key arbitrates, so of two callers minting at once exactly one
        wins: a `get` under `FOR UPDATE` locks nothing when the row does not exist yet, and `put`
        would have overwritten the winner rather than conflicting with it."""
        name = self.store.sealer.name(key)
        statement = _insert_ignoring(self.connection.dialect.name).values(
            workspace_id=self.store.ctx.workspace_id,
            user_id=self.store.user_id,
            device_id=self.store.device_id,
            kind=kind,
            name=name,
            value=self.store.sealer.seal(self._address(kind, name), value),
            updated_at=sa.func.now(),
        )
        return (await self.connection.execute(statement)).rowcount == 1

    async def put(self, kind: str, *key: str, value: Any) -> None:
        name = self.store.sealer.name(key)
        sealed = self.store.sealer.seal(self._address(kind, name), value)
        updated = await self.connection.execute(
            sa.update(CRYPTO_TABLE)
            .where(self._row(kind, name))
            .values(value=sealed, updated_at=sa.func.now())
        )
        if updated.rowcount == 0:
            await self.connection.execute(
                sa.insert(CRYPTO_TABLE).values(
                    workspace_id=self.store.ctx.workspace_id,
                    user_id=self.store.user_id,
                    device_id=self.store.device_id,
                    kind=kind,
                    name=name,
                    value=sealed,
                    updated_at=sa.func.now(),
                )
            )

    def _row(self, kind: str, name: str) -> sa.ColumnElement[bool]:
        store = self.store
        return sa.and_(
            CRYPTO_TABLE.c.workspace_id == store.ctx.workspace_id,
            CRYPTO_TABLE.c.user_id == store.user_id,
            CRYPTO_TABLE.c.device_id == store.device_id,
            CRYPTO_TABLE.c.kind == kind,
            CRYPTO_TABLE.c.name == name,
        )

    def _address(self, kind: str, name: str) -> bytes:
        store = self.store
        parts = (str(store.ctx.workspace_id), store.user_id, store.device_id, kind, name)
        return "\x1f".join(parts).encode()
