"""The session debugger: a read-only operator surface over a workspace's sessions — its
conversations across every surface, and each one's turns, steps, transcript, context boundaries,
sandbox files, and live tail — and, under fleet reach, an index of the deploy's workspaces and its
most recently active threads.

Authorization is entirely in `resolve_operator_workspace`, the surface's `identify`: the request's
bearer must verify against this deploy's `UFO_TOKEN_SECRET` and be granted by the deploy's operator
rule. The workspace it resolves — the grant's home, or under fleet reach the one `?ws=` names —
becomes the request's RLS binding and `SurfaceContext.workspace_id`, so every read below is
workspace-scoped by construction.

The page is a React app `make debugger` builds into `static/index.html`, served whole; everything
it renders comes from the JSON routes under `api/`, thin dumps of the `SurfaceContext` read views
plus an SSE tail of a live turn rendered raw for debugging. The links out — from a turn to its trace
and logs, from a conversation to the surface it lives on — are the `[debugger]` templates, filled
here. A session starts with `POST /surface/debug` carrying the bearer in its form body — never in a
URL, so no access log ever records it — which lands it as the httponly session cookie and redirects
to the app; `ufoctl debugger` makes that POST from this machine's CLI token."""

import re
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from string import Formatter
from urllib.parse import quote
from uuid import UUID

from ufo.sdk.http import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Request,
    Response,
    StreamingResponse,
)
from ufo.sdk.hub import (
    Absorbed,
    Activity,
    ArtifactsChanged,
    CostTick,
    Created,
    LiveFrame,
    Parked,
    Reply,
    Resumed,
    Sources,
    SubagentActivity,
    Terminal,
    TextDelta,
)
from ufo.sdk.operator import (
    FleetDirectory,
    FleetReachRequired,
    bind_operator_session,
    debugger_links,
    operator_grant,
)
from ufo.sdk.surfaces import SurfaceContext, SurfaceRoute

APP_FILE = Path(__file__).parent / "static" / "index.html"
APP_HTML = APP_FILE.read_text() if APP_FILE.is_file() else None
APP_MISSING = "The debugger page is not built. Run `make debugger` and restart `ufoctl serve`."
TRACEPARENT = re.compile(r"00-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}")
TRACE_WINDOW_PAD = timedelta(minutes=5)


async def app_page(ctx: SurfaceContext, request: Request) -> Response:
    if APP_HTML is None:
        return PlainTextResponse(APP_MISSING, status_code=500)
    return HTMLResponse(APP_HTML)


async def fleet(ctx: SurfaceContext, request: Request) -> Response:
    """The landing index of a fleet grant: the deploy's workspaces and its most recent threads,
    read across the fleet whatever `?ws=` scopes the request to. Any other grant answers 403."""
    try:
        listing = await FleetDirectory().read(operator_grant(request))
    except FleetReachRequired:
        return JSONResponse({"error": "this session reads one workspace"}, status_code=403)
    return JSONResponse(listing.model_dump(mode="json"))


async def workspace_meta(ctx: SurfaceContext, request: Request) -> Response:
    """The session's scope: the workspace, the grant's reach, and the workspace's installation on
    each surface `[debugger] conversation_urls` links into."""
    installations = {
        surface: await ctx.installation(surface) for surface in debugger_links().conversation_urls
    }
    return JSONResponse(
        {
            "workspace_id": str(ctx.workspace_id),
            "reach": operator_grant(request).reach,
            "installations": {
                surface: installation
                for surface, installation in installations.items()
                if installation is not None
            },
        }
    )


async def conversations(ctx: SurfaceContext, request: Request) -> Response:
    """The workspace's conversations, each carrying the link `[debugger] conversation_urls` gives
    its surface: the first of its templates filled from the workspace's installation there and the
    conversation's key, or None where no template fills."""
    listed = await ctx.list_conversations()
    templates = debugger_links().conversation_urls
    installations = {surface: await ctx.installation(surface) for surface in templates}
    rows = []
    for entry in listed:
        values = {"installation": installations.get(entry.surface), "address": entry.queue_key}
        links = (_filled(template, values) for template in templates.get(entry.surface, ()))
        rows.append(
            entry.model_dump(mode="json")
            | {"link": next((link for link in links if link is not None), None)}
        )
    return JSONResponse(rows)


def _filled(template: str, values: Mapping[str, str | None]) -> str | None:
    filled: list[str] = []
    for literal, field, _, _ in Formatter().parse(template):
        filled.append(literal)
        if field is None:
            continue
        name, _, part = field.partition("[")
        value = values[name]
        if value is not None and part:
            pieces = value.split(":")
            index = int(part.removesuffix("]"))
            value = pieces[index] if index < len(pieces) else None
        if value is None:
            return None
        filled.append(quote(value, safe=""))
    return "".join(filled)


async def conversation_turns(ctx: SurfaceContext, request: Request) -> Response:
    conversation_id = _uuid_param(request, "conversation_id")
    if conversation_id is None:
        return JSONResponse({"error": "no such conversation"}, status_code=404)
    turns = await ctx.list_turns(conversation_id)
    return JSONResponse([turn.model_dump(mode="json") for turn in turns])


async def conversation_transcript(ctx: SurfaceContext, request: Request) -> Response:
    conversation_id = _uuid_param(request, "conversation_id")
    if conversation_id is None:
        return JSONResponse({"error": "no such conversation"}, status_code=404)
    transcript = await ctx.read_transcript(conversation_id)
    if transcript is None:
        return JSONResponse({"error": "no transcript"}, status_code=404)
    return JSONResponse(transcript.model_dump(mode="json"))


async def conversation_rollovers(ctx: SurfaceContext, request: Request) -> Response:
    """The indices of the conversation's context boundaries, ascending — rollovers and compactions
    alike, each readable through `rollover_record`."""
    conversation_id = _uuid_param(request, "conversation_id")
    if conversation_id is None:
        return JSONResponse({"error": "no such conversation"}, status_code=404)
    return JSONResponse(list(await ctx.list_rollovers(conversation_id)))


async def rollover_record(ctx: SurfaceContext, request: Request) -> Response:
    """One context boundary read back whole: the window it closed, the window that replaced it, and
    the recovery record the fresh window opened with — a compaction's summary reads as that
    record's handoff."""
    conversation_id = _uuid_param(request, "conversation_id")
    index = request.path_params["index"]
    if conversation_id is None or not index.isdigit():
        return JSONResponse({"error": "no such rollover"}, status_code=404)
    record = await ctx.read_rollover(conversation_id, int(index))
    if record is None:
        return JSONResponse({"error": "no such rollover"}, status_code=404)
    return JSONResponse(
        {
            "index": record.index,
            "before": [message.model_dump(mode="json") for message in record.before],
            "after": [message.model_dump(mode="json") for message in record.after],
            "recovery": record.recovery.model_dump(mode="json"),
        }
    )


async def workspace_files(ctx: SurfaceContext, request: Request) -> Response:
    conversation_id = _uuid_param(request, "conversation_id")
    if conversation_id is None:
        return JSONResponse({"error": "no such conversation"}, status_code=404)
    files = await ctx.list_workspace_files(conversation_id)
    return JSONResponse([entry.model_dump(mode="json") for entry in files])


async def workspace_file(ctx: SurfaceContext, request: Request) -> Response:
    conversation_id = _uuid_param(request, "conversation_id")
    if conversation_id is None:
        return JSONResponse({"error": "no such file"}, status_code=404)
    try:
        stream = await ctx.read_workspace_file(conversation_id, request.path_params["path"])
    except ValueError:
        return JSONResponse({"error": "no such file"}, status_code=404)
    if stream is None:
        return JSONResponse({"error": "no such file"}, status_code=404)
    return StreamingResponse(stream, media_type="application/octet-stream")


async def turn(ctx: SurfaceContext, request: Request) -> Response:
    """One turn in depth, carrying the links `[debugger] turn_urls` gives it, in their configured
    order: each filled from the turn's `traceparent`, its conversation, and its window padded by
    `TRACE_WINDOW_PAD` either side, and left out where the turn lacks a value its template names."""
    turn_id = _uuid_param(request, "turn_id")
    if turn_id is None:
        return JSONResponse({"error": "no such turn"}, status_code=404)
    detail = await ctx.turn_detail(turn_id)
    if detail is None:
        return JSONResponse({"error": "no such turn"}, status_code=404)
    traced = TRACEPARENT.fullmatch(detail.turn.traceparent or "")
    start = detail.turn.created_at - TRACE_WINDOW_PAD
    end = (detail.turn.updated_at or datetime.now(UTC)) + TRACE_WINDOW_PAD
    values = {
        "trace_id": None if traced is None else traced.group(1),
        "conversation_id": str(detail.turn.conversation_id),
        "start_ms": str(int(start.timestamp() * 1000)),
        "end_ms": str(int(end.timestamp() * 1000)),
    }
    return JSONResponse(
        detail.model_dump(mode="json")
        | {
            "links": [
                {"label": label, "url": link}
                for label, template in debugger_links().turn_urls.items()
                if (link := _filled(template, values)) is not None
            ]
        }
    )


async def turn_steps(ctx: SurfaceContext, request: Request) -> Response:
    turn_id = _uuid_param(request, "turn_id")
    if turn_id is None:
        return JSONResponse({"error": "no such turn"}, status_code=404)
    steps = await ctx.turn_steps(turn_id)
    if steps is None:
        return JSONResponse({"error": "no such turn"}, status_code=404)
    return JSONResponse([step.model_dump(mode="json") for step in steps])


async def stream(ctx: SurfaceContext, request: Request) -> Response:
    turn_id = _uuid_param(request, "turn_id")
    if turn_id is None or await ctx.turn_detail(turn_id) is None:
        return JSONResponse({"error": "no such turn"}, status_code=404)
    since = request.headers.get("last-event-id", "")
    return StreamingResponse(_events(ctx, turn_id, since), media_type="text/event-stream")


async def _events(ctx: SurfaceContext, turn_id: UUID, since: str) -> AsyncIterator[bytes]:
    async with ctx.tail(turn_id, since) as frames:
        async for cursor, frame in frames:
            yield _sse(cursor, frame)


def _sse(cursor: str, frame: LiveFrame) -> bytes:
    head = f"id: {cursor}\n".encode() if cursor else b""
    match frame:
        case Terminal():
            kind, payload = b"terminal", frame.frame.model_dump_json()
        case Parked():
            kind, payload = b"parked", frame.model_dump_json()
        case CostTick():
            kind, payload = b"cost", frame.model_dump_json()
        case Activity():
            kind, payload = b"activity", frame.model_dump_json()
        case ArtifactsChanged():
            kind, payload = b"artifacts_changed", frame.model_dump_json()
        case Created():
            kind, payload = b"created", frame.model_dump_json()
        case SubagentActivity():
            kind, payload = b"subagent_activity", frame.model_dump_json()
        case Absorbed():
            kind, payload = b"absorbed", frame.model_dump_json()
        case Resumed():
            kind, payload = b"resumed", frame.model_dump_json()
        case Reply():
            kind, payload = b"reply", frame.model_dump_json()
        case Sources():
            kind, payload = b"sources", frame.model_dump_json()
        case TextDelta():
            kind, payload = b"text", frame.model_dump_json()
        case _:
            raise ValueError(f"unmapped live frame {type(frame).__name__}")
    return head + b"event: " + kind + b"\ndata: " + payload.encode() + b"\n\n"


def _uuid_param(request: Request, name: str) -> UUID | None:
    try:
        return UUID(request.path_params[name])
    except ValueError:
        return None


ROUTES = (
    SurfaceRoute(method="GET", path="", handler=app_page),
    SurfaceRoute(method="POST", path="", handler=bind_operator_session),
    SurfaceRoute(method="GET", path="api/fleet", handler=fleet),
    SurfaceRoute(method="GET", path="api/workspace", handler=workspace_meta),
    SurfaceRoute(method="GET", path="api/conversations", handler=conversations),
    SurfaceRoute(
        method="GET", path="api/conversations/{conversation_id}/turns", handler=conversation_turns
    ),
    SurfaceRoute(
        method="GET",
        path="api/conversations/{conversation_id}/transcript",
        handler=conversation_transcript,
    ),
    SurfaceRoute(
        method="GET",
        path="api/conversations/{conversation_id}/rollovers",
        handler=conversation_rollovers,
    ),
    SurfaceRoute(
        method="GET",
        path="api/conversations/{conversation_id}/rollovers/{index}",
        handler=rollover_record,
    ),
    SurfaceRoute(
        method="GET", path="api/conversations/{conversation_id}/files", handler=workspace_files
    ),
    SurfaceRoute(
        method="GET",
        path="api/conversations/{conversation_id}/files/{path:path}",
        handler=workspace_file,
    ),
    SurfaceRoute(method="GET", path="api/turns/{turn_id}", handler=turn),
    SurfaceRoute(method="GET", path="api/turns/{turn_id}/steps", handler=turn_steps),
    SurfaceRoute(method="GET", path="api/turns/{turn_id}/stream", handler=stream),
)
