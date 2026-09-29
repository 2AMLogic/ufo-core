"""A member links a Matrix ID the workspace's domain does not vouch for, by proving they hold it.

The member names the MXID in chat, on any surface; `matrix_link_account` records a claim holding a
one-time code, and the member sends that code to the bot in a direct room from the MXID they named.
The bot never writes first to anyone. The proving message links the MXID to the member who asked and
founds no turn; a replay of it is recognised by its event id and founds none either.

Both tables are this extension's own. Core's address reservation serves addressed surfaces alone,
and Matrix is installation-routed, so the claim lives here; the link itself is core's
`link_member_id`. Core has no call that removes a link, so an admin's unlink is a row here that
makes the MXID nobody until it is proved again."""

import hashlib
import hmac
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncConnection

from ufo.sdk.surfaces import SurfaceContext
from ufo.sdk.tools import TextContent, ToolContext, ToolResult
from ufo_ext_matrix.events import SURFACE, RoomMessage
from ufo_ext_matrix.since import Transactional

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_CHARS = 8
CLAIM_MINUTES = 15
CODE_ATTEMPTS = 5
HASH_ROUNDS = 20_000
CODE_SHAPE = re.compile(r"\s*([A-Za-z0-9]{4})[-\s]?([A-Za-z0-9]{4})\s*")
MXID_SHAPE = re.compile(r"@[^:\s]+:[^\s]+")

LINKED_LINE = "Linked. This Matrix ID now speaks for you in this workspace."
WRONG_LINE = "That code does not match. {left} tries remain."
SPENT_LINE = "Too many wrong codes. Ask the agent to link this Matrix ID again for a new one."
EXPIRED_LINE = "That code expired. Ask the agent to link this Matrix ID again for a new one."
HELD_LINE = "This Matrix ID stays linked to another member of the workspace."
GONE_LINE = "The member who asked for this code is no longer in the workspace."

_metadata = sa.MetaData()


def _workspace() -> sa.ForeignKey:
    return sa.ForeignKey("workspace.id", ondelete="CASCADE")


CLAIM_TABLE = sa.Table(
    "matrix_ext_claim",
    _metadata,
    sa.Column("workspace_id", sa.Uuid(), _workspace(), primary_key=True),
    sa.Column("mxid", sa.Text(), primary_key=True),
    sa.Column("member_id", sa.Uuid(), nullable=False),
    sa.Column("code_hash", sa.LargeBinary(), nullable=False),
    sa.Column("attempts", sa.Integer(), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
)

LINK_TABLE = sa.Table(
    "matrix_ext_link",
    _metadata,
    sa.Column("workspace_id", sa.Uuid(), _workspace(), primary_key=True),
    sa.Column("mxid", sa.Text(), primary_key=True),
    sa.Column("member_id", sa.Uuid(), nullable=True),
    sa.Column("proved_by", sa.Text(), nullable=True),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)


class LinkInput(BaseModel):
    mxid: str = Field(description="The member's own Matrix ID, like @name:matrix.org.")


class UnlinkInput(BaseModel):
    mxid: str = Field(description="The Matrix ID to unlink, like @name:matrix.org.")


def is_mxid(value: str) -> bool:
    return MXID_SHAPE.fullmatch(value) is not None


def mint_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_CHARS))


def shown(code: str) -> str:
    half = CODE_CHARS // 2
    return f"{code[:half]}-{code[half:]}"


def code_in(body: str) -> str | None:
    """The code a message carries, normalised, when the whole message is one — `abcd-efgh`,
    `ABCD EFGH` and `ABCDEFGH` read alike. Anything longer is words, not a code."""
    found = CODE_SHAPE.fullmatch(body)
    return None if found is None else (found[1] + found[2]).upper()


def code_hash(workspace_id: UUID, mxid: str, code: str) -> bytes:
    """A slow digest salted by the claim's own key, so the stored hash is no use for guessing."""
    salt = f"{workspace_id}:{mxid}".encode()
    return hashlib.pbkdf2_hmac("sha256", code.encode(), salt, HASH_ROUNDS)


def proof_txn(event_id: str) -> str:
    """The transaction the answer to one proving message is sent under, so a replay lands once."""
    return "ufo-proof-" + hashlib.sha256(event_id.encode()).hexdigest()[:32]


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


async def _upsert(
    connection: AsyncConnection,
    table: sa.Table,
    key: dict[str, Any],
    values: dict[str, Any],
) -> None:
    where = sa.and_(*(table.c[name] == value for name, value in key.items()))
    updated = await connection.execute(sa.update(table).where(where).values(**values))
    if updated.rowcount == 0:
        await connection.execute(sa.insert(table).values(**key, **values))


def _key(ctx: Transactional, mxid: str) -> dict[str, Any]:
    return {"workspace_id": ctx.workspace_id, "mxid": mxid}


def _where(table: sa.Table, ctx: Transactional, mxid: str) -> sa.ColumnElement[bool]:
    return sa.and_(table.c.workspace_id == ctx.workspace_id, table.c.mxid == mxid)


async def unlinked(ctx: Transactional, mxid: str) -> bool:
    """Whether an admin unlinked this MXID and nobody has proved it since."""
    async with ctx.transaction() as connection:
        row = (
            await connection.execute(
                sa.select(LINK_TABLE.c.member_id).where(_where(LINK_TABLE, ctx, mxid))
            )
        ).one_or_none()
    return row is not None and row.member_id is None


@dataclass(frozen=True)
class Proof:
    """What a proving message came to: the line the bot answers with, and the member the MXID is
    now linked to when it was."""

    reply: str
    member_id: UUID | None = None


@dataclass
class Linking:
    """Claims and proofs over the extension's tables. `clock` is the seam a test moves time by."""

    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    async def live_claim(self, ctx: Transactional, mxid: str) -> bool:
        """Whether a member is waiting on a code from this MXID — the one reason the bot joins a
        room a non-member invites it to."""
        async with ctx.transaction() as connection:
            row = (
                await connection.execute(
                    sa.select(CLAIM_TABLE.c.expires_at).where(_where(CLAIM_TABLE, ctx, mxid))
                )
            ).one_or_none()
        return row is not None and _aware(row.expires_at) > self.clock()

    async def prove(
        self,
        ctx: SurfaceContext,
        message: RoomMessage,
        resolve: Callable[[str], Awaitable[UUID | None]],
    ) -> Proof | None:
        """Read one code-shaped direct message. None is a message that proves nothing — its sender
        already speaks for a member, or no claim names it — and goes on as any other line does."""
        mxid = message.sender
        async with ctx.transaction() as connection:
            link = (
                await connection.execute(
                    sa.select(LINK_TABLE.c.proved_by).where(_where(LINK_TABLE, ctx, mxid))
                )
            ).one_or_none()
        if link is not None and link.proved_by == message.event_id:
            return Proof(LINKED_LINE)
        if await resolve(mxid) is not None:
            return None
        async with ctx.transaction() as connection:
            claim = (
                await connection.execute(
                    sa.select(CLAIM_TABLE).where(_where(CLAIM_TABLE, ctx, mxid))
                )
            ).one_or_none()
            if claim is None:
                return None
            if _aware(claim.expires_at) <= self.clock():
                await connection.execute(
                    sa.delete(CLAIM_TABLE).where(_where(CLAIM_TABLE, ctx, mxid))
                )
                return Proof(EXPIRED_LINE)
            code = code_in(message.body) or ""
            if not hmac.compare_digest(code_hash(ctx.workspace_id, mxid, code), claim.code_hash):
                left = CODE_ATTEMPTS - claim.attempts - 1
                if left <= 0:
                    await connection.execute(
                        sa.delete(CLAIM_TABLE).where(_where(CLAIM_TABLE, ctx, mxid))
                    )
                    return Proof(SPENT_LINE)
                await connection.execute(
                    sa.update(CLAIM_TABLE)
                    .where(_where(CLAIM_TABLE, ctx, mxid))
                    .values(attempts=CLAIM_TABLE.c.attempts + 1)
                )
                return Proof(WRONG_LINE.format(left=left))
            await connection.execute(sa.delete(CLAIM_TABLE).where(_where(CLAIM_TABLE, ctx, mxid)))
        member_id = claim.member_id
        if await ctx.link_member_id(mxid, member_id) is None:
            return Proof(GONE_LINE)
        if await ctx.linked_member(mxid) != member_id:
            return Proof(HELD_LINE)
        async with ctx.transaction() as connection:
            await _upsert(
                connection,
                LINK_TABLE,
                _key(ctx, mxid),
                {
                    "member_id": member_id,
                    "proved_by": message.event_id,
                    "updated_at": self.clock(),
                },
            )
        return Proof(LINKED_LINE, member_id)

    async def link_account(self, ctx: ToolContext, args: LinkInput) -> ToolResult:
        """Record the speaking member's claim on an MXID and hand back the code that proves it."""
        ext = ctx.ext
        if ext is None:
            raise RuntimeError("matrix_link_account dispatched without its ExtensionContext")
        member_id = ctx.require_speaker("linking a Matrix ID")
        mxid = args.mxid.strip()
        if not is_mxid(mxid):
            return _said(
                f"{mxid!r} is not a Matrix ID; one reads like @name:matrix.org.",
                error=True,
            )
        bot = await ext.installations.installation(SURFACE)
        if bot is None:
            return _said("Matrix is not connected in this workspace yet.", error=True)
        if mxid == bot:
            return _said(f"{bot} is the workspace's own bot.", error=True)
        held = {
            m for m, x in (await ext.installations.linked_members(SURFACE)).items() if x == mxid
        }
        if held - {member_id}:
            return _said(f"{mxid} is linked to another member of the workspace.", error=True)
        if held and not await unlinked(ext, mxid):
            return _said(f"{mxid} is already linked to you.")
        code = mint_code()
        async with ext.transaction() as connection:
            await _upsert(
                connection,
                CLAIM_TABLE,
                _key(ext, mxid),
                {
                    "member_id": member_id,
                    "code_hash": code_hash(ext.workspace_id, mxid, code),
                    "attempts": 0,
                    "expires_at": self.clock() + timedelta(minutes=CLAIM_MINUTES),
                },
            )
        return _said(
            f"From {mxid}, send {shown(code)} to {bot} in a direct room with only the two of you, "
            f"within {CLAIM_MINUTES} minutes. With no such room yet, invite {bot} to a new one; "
            f"while the code is live it accepts an invitation from {mxid}. The code works once, "
            f"and {CODE_ATTEMPTS} wrong tries void it."
        )

    async def unlink_account(self, ctx: ToolContext, args: UnlinkInput) -> ToolResult:
        """An admin cuts an MXID off: it speaks for nobody until someone proves it again."""
        ext = ctx.ext
        if ext is None:
            raise RuntimeError("matrix_unlink_account dispatched without its ExtensionContext")
        if not await ctx.require_speaking_admin("unlinking a Matrix ID"):
            return _said("Only a workspace admin can unlink a Matrix ID.", error=True)
        mxid = args.mxid.strip()
        if not is_mxid(mxid):
            return _said(
                f"{mxid!r} is not a Matrix ID; one reads like @name:matrix.org.",
                error=True,
            )
        async with ext.transaction() as connection:
            await connection.execute(sa.delete(CLAIM_TABLE).where(_where(CLAIM_TABLE, ext, mxid)))
            await _upsert(
                connection,
                LINK_TABLE,
                _key(ext, mxid),
                {"member_id": None, "proved_by": None, "updated_at": self.clock()},
            )
        return _said(f"Unlinked {mxid}. It speaks for nobody here until it is proved again.")


def _said(text: str, *, error: bool = False) -> ToolResult:
    return ToolResult(content=(TextContent(text=text),), is_error=error)
