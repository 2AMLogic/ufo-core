"""Public structured logging and metrics for extensions.

A metric's name is one core declares or one an active extension declares in its manifest's
`metrics`; any other fails loud at the emit, so no extension mints a name and the fleet's metric
surface stays enumerable from core and the active manifests."""

from ufo.harness.o11y import emit_histogram as emit_histogram
from ufo.harness.o11y import emit_metric as emit_metric
from ufo.harness.o11y import emit_up_down_metric as emit_up_down_metric
from ufo.harness.o11y import log as log
from ufo.harness.o11y import log_error as log_error
from ufo.harness.o11y import turn_profile as turn_profile
from ufo.harness.o11y import warn as warn
