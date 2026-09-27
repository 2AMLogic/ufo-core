import sqlalchemy as sa
from pydantic import BaseModel

from ufo.sdk.tools import TextContent, ToolContext, ToolResult
from ufo_ext_sample.metrics import measured

TOOL_NAME = "sample_echo"
NOTE_TOOL_NAME = "sample_note"
TOOL_KEY = "tool:echo"
NOTE_TABLE = sa.Table(
    "sample_ext_note",
    sa.MetaData(),
    sa.Column(
        "workspace_id",
        sa.Uuid(),
        sa.ForeignKey("workspace.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("note", sa.Text(), nullable=False),
    sa.Column("member_id", sa.Uuid(), sa.ForeignKey("member.id", ondelete="SET NULL")),
    sa.Column("noted_at", sa.DateTime(timezone=True)),
)


class EchoInput(BaseModel):
    message: str


class NoteInput(BaseModel):
    text: str


async def echo(ctx: ToolContext, args: EchoInput) -> ToolResult:
    ext = ctx.ext
    if ext is None:
        raise RuntimeError("sample tool dispatched without its ExtensionContext")
    await measured(TOOL_NAME, lambda: ext.store.put(TOOL_KEY, args.model_dump()))
    return ToolResult(content=(TextContent(text=args.message),))


async def note(ctx: ToolContext, args: NoteInput) -> ToolResult:
    """Write a note into `sample_ext_note` — the table the sample's own migration creates — and read
    it back through the extension's workspace-scoped transaction. The migration seam end to end: an
    extension owns a table and its capability reads and writes it, scoped to this workspace. The
    first note records who wrote it and when, which the sample's census contributions count."""
    if ctx.ext is None:
        raise RuntimeError("sample note tool dispatched without its ExtensionContext")
    workspace_id = ctx.ext.store.workspace_id
    async with ctx.ext.transaction() as connection:
        updated = await connection.execute(
            sa.update(NOTE_TABLE)
            .values(note=args.text)
            .where(NOTE_TABLE.c.workspace_id == workspace_id)
        )
        if updated.rowcount == 0:
            await connection.execute(
                sa.insert(NOTE_TABLE).values(
                    workspace_id=workspace_id,
                    note=args.text,
                    member_id=ctx.speaker_member_id,
                    noted_at=sa.func.now(),
                )
            )
        stored = (
            await connection.execute(
                sa.select(NOTE_TABLE.c.note).where(NOTE_TABLE.c.workspace_id == workspace_id)
            )
        ).one()
    return ToolResult(content=(TextContent(text=stored.note),))
