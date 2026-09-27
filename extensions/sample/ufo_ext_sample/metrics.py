import time
from collections.abc import Awaitable, Callable

from ufo.sdk.o11y import emit_histogram, emit_metric, emit_up_down_metric

CALL_METRIC = "sample_call_total"
CALL_LATENCY_METRIC = "sample_call_ms"
CALL_ACTIVE_METRIC = "sample_call_active"
CALL_DIMENSION = "call"


async def measured[T](call: str, work: Callable[[], Awaitable[T]]) -> T:
    emit_up_down_metric(CALL_ACTIVE_METRIC, 1, call=call)
    started = time.monotonic()
    try:
        return await work()
    finally:
        emit_up_down_metric(CALL_ACTIVE_METRIC, -1, call=call)
        emit_histogram(CALL_LATENCY_METRIC, int((time.monotonic() - started) * 1000), call=call)
        emit_metric(CALL_METRIC, call=call)
