"""What a room shows while a turn runs: the member's message read, and the bot typing until the turn
ends.

None of this is delivery. The hub is lossy, so a frame that never arrives costs a typing indicator
and never a turn's answer, and every call here is best effort: a homeserver that refuses one is
logged by error class and the turn goes on. Typing carries the homeserver's own timeout and is
refreshed under it, so a tail that drops ends the indicator on the server's clock rather than leaving
a room typing forever; the terminal or parked frame ends it at once."""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterator

import httpx

from ufo.sdk.hub import LiveFrame, Parked, Terminal
from ufo.sdk.o11y import warn
from ufo_ext_matrix.client import MatrixClient, MatrixError

TYPING_TIMEOUT_MS = 30_000
TYPING_REFRESH_SECONDS = 20.0

Frames = AsyncIterator[tuple[str, LiveFrame]]


async def attend(
    client: MatrixClient,
    room_id: str,
    bot: str,
    event_id: str,
    frames: Frames,
    refresh: float = TYPING_REFRESH_SECONDS,
) -> None:
    """Show one room the turn its message opened: the message read, then typing refreshed under the
    homeserver's timeout until a terminal or parked frame ends the turn."""
    await receipt(client, room_id, event_id)
    ended = asyncio.Event()
    reader = asyncio.ensure_future(_read(frames, ended))
    try:
        while not ended.is_set():
            await typing(client, room_id, bot, True)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(ended.wait(), refresh)
    finally:
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader
        await typing(client, room_id, bot, False)


async def typing(client: MatrixClient, room_id: str, bot: str, active: bool) -> None:
    with _best_effort("matrix.typing_unsent"):
        await client.typing(room_id, bot, active, TYPING_TIMEOUT_MS)


async def receipt(client: MatrixClient, room_id: str, event_id: str) -> None:
    with _best_effort("matrix.receipt_unsent"):
        await client.read_receipt(room_id, event_id)


async def _read(frames: Frames, ended: asyncio.Event) -> None:
    """Read the turn's frames until it ends. Only that one fact reaches the room, so no frame is
    read for what it says; a stream that fails ends the indicator as a terminal frame does."""
    try:
        async for _cursor, frame in frames:
            if isinstance(frame, Terminal | Parked):
                break
    except Exception as error:
        warn("matrix.tail_ended", error_class=type(error).__name__)
    finally:
        ended.set()


@contextlib.contextmanager
def _best_effort(event: str) -> Iterator[None]:
    try:
        yield
    except MatrixError as error:
        warn(event, http_status=error.status, errcode=error.errcode)
    except httpx.HTTPError as error:
        warn(event, error_class=type(error).__name__)
