from collections.abc import AsyncIterator
from uuid import UUID

from pydantic import BaseModel

from ufo.sdk.audience import conversation_audience
from ufo.sdk.http import JSONResponse, Request, Response, StreamingResponse
from ufo.sdk.surfaces import (
    NOTHING_DELIVERED,
    NothingDelivered,
    SurfaceAuth,
    SurfaceContext,
    Writeback,
    writeback_says_nothing,
)
from ufo_ext_sample.routes import resolve_workspace

SURFACE_NAME = "sample-surface"
SURFACE_LIVE_NAME = "sample-live"
SURFACE_INBOX_REL = "sample-inbox/note.txt"
SURFACE_DELIVERED_PREFIX = "sample-delivered"
SURFACE_POST_REF = "sample-posted-ref"
SURFACE_LIVE_PATH = "live"
SURFACE_LIVE_STREAM_PATH = "live/{turn_id}/stream"
SURFACE_PEER = "sample_peer"
SURFACE_SPEND_WINDOW_SECONDS = 3600


class SurfaceIngestInput(BaseModel):
    external_id: str
    email: str | None = None
    message: str
    inbound_text: str | None = None
    title: str | None = None


async def _one_chunk(data: bytes) -> AsyncIterator[bytes]:
    yield data


SURFACE_MODEL_PATH = "model"


async def surface_served_model(ctx: SurfaceContext, request: Request) -> Response:
    """Report the metered model this surface's routes were handed. A route may generate what it
    answers rather than only project stored rows, and what it generates on is the deploy's, wired
    per surface at boot — so the seam is proved here, by a real route reading it, rather than by
    core asserting against itself. A deploy that wired none answers null, which is the case a route
    must still serve a page for."""
    return JSONResponse({"model": None if ctx.model is None else ctx.model.model})


async def surface_ingest(ctx: SurfaceContext, request: Request) -> Response:
    """Exercise the whole surface seam: resolve (and link) a member identity, get-or-create the
    conversation, optionally name it and stream an inbound file into the workspace, then admit a
    turn — all read back by the conformance test through the durable rows core writes here. The
    admission's `opened_run` rides the response so the test can drive a redelivery of one external
    id and see the seam report the second one as joining the run the first opened."""
    args = SurfaceIngestInput.model_validate_json(await request.body())
    member_id = await ctx.linked_member(args.external_id)
    if member_id is None and args.email is not None:
        member_id = await ctx.link_member(args.external_id, args.email)
    conversation_id = await ctx.conversation_for(args.external_id, conversation_audience(member_id))
    if args.title is not None:
        await ctx.name_conversation(conversation_id, args.title)
    if args.inbound_text is not None:
        await ctx.write_workspace_file(
            conversation_id, SURFACE_INBOX_REL, _one_chunk(args.inbound_text.encode())
        )
    admitted = await ctx.admit(
        conversation_id,
        args.message,
        idempotency_key=args.external_id,
        speaker_member_id=member_id,
    )
    return JSONResponse(
        {
            "turn_id": str(admitted.turn_id),
            "conversation_id": str(conversation_id),
            "opened_run": admitted.opened_run,
        }
    )


async def surface_post(ctx: SurfaceContext, writeback: Writeback) -> str | NothingDelivered:
    if writeback_says_nothing(writeback):
        return NOTHING_DELIVERED
    return SURFACE_POST_REF


async def surface_attach(ctx: SurfaceContext, writeback: Writeback, reply_ref: str) -> None:
    """Stream each shared file out of the blob store and back into a delivered key, so the test
    reads the round-tripped bytes through the blob store — the streaming get is exercised here."""
    for artifact in writeback.artifacts:
        delivered_key = f"{SURFACE_DELIVERED_PREFIX}/{writeback.turn_id}/{artifact.filename}"
        await ctx.blob.put_stream(delivered_key, ctx.blob.get_stream(artifact.blob_key))


async def surface_live_admit(ctx: SurfaceContext, request: Request) -> Response:
    """Exercise the seam's LIVE mode on its own surface: adopt a member from a peer surface's
    identity, get-or-create the conversation, admit (a live surface declares no `post`, so
    admission registers nothing for the poller and the member tails the hub), then read back the
    turn's owner and the workspace spend rollup. The conformance test asserts no writeback row
    exists for this turn — the live/durable contrast against `surface_ingest` on the durable
    surface."""
    args = SurfaceIngestInput.model_validate_json(await request.body())
    member_id = await ctx.linked_member(args.external_id)
    if member_id is None:
        member_id = await ctx.adopt_identity(SURFACE_PEER, args.external_id)
    conversation_id = await ctx.conversation_for(args.external_id, conversation_audience(member_id))
    turn_id = (await ctx.admit(conversation_id, args.message, speaker_member_id=member_id)).turn_id
    owner = await ctx.turn_owner(turn_id)
    report = await ctx.spend_rollup(SURFACE_SPEND_WINDOW_SECONDS)
    return JSONResponse(
        {
            "turn_id": str(turn_id),
            "conversation_id": str(conversation_id),
            "owner": "" if owner is None else str(owner),
            "spend_total_micro_usd": report.total_micro_usd,
        }
    )


async def surface_live_stream(ctx: SurfaceContext, request: Request) -> Response:
    """Drive the seam's `tail` capability: stream the turn's live frames off the hub as newline-
    delimited JSON, ending on its durable terminal-or-parked state."""
    turn_id = UUID(request.path_params["turn_id"])
    return StreamingResponse(_surface_frames(ctx, turn_id), media_type="application/x-ndjson")


async def _surface_frames(ctx: SurfaceContext, turn_id: UUID) -> AsyncIterator[bytes]:
    async with ctx.tail(turn_id) as frames:
        async for _cursor, frame in frames:
            yield frame.model_dump_json().encode() + b"\n"


async def resolve_surface_workspace(request: Request, _auth: SurfaceAuth) -> UUID | None:
    """The async `SurfaceSpec.identify`, resolving the same bearer claim as the route resolver."""
    return resolve_workspace(request)
