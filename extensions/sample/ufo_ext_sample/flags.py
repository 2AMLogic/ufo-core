from collections.abc import Mapping

from openfeature.provider.in_memory_provider import InMemoryFlag, InMemoryProvider

from ufo.sdk.flags import SERVED_FALSE, SERVED_TRUE
from ufo.sdk.manifest import FlagSpec

FLAG_BACKEND = "sample_flags"
FLAG_ON = "sample-flag-on"
FLAG_OFF = "sample-flag-off"
PROBE_FLAG = "sample-probe-flag"
PROBE_FLAG_WHAT = "The probe states one declared flag for the operator verb to reconcile."
ON_VARIANT = "on"
OFF_VARIANT = "off"


def build_flag_provider(
    _cache_ttl_seconds: float, _declared: Mapping[str, FlagSpec]
) -> InMemoryProvider:
    """The OpenFeature provider the probe registers through the `flag_providers` Manifest point:
    `sample-flag-on` resolves true and `sample-flag-off` false, so a flagged path is driven both
    ways through the real SDK. Its variations are the strings a flag service holds, which is what
    `flag_enabled` reads — a backend answering JSON booleans is one no deploy can have. It answers
    from memory, so the deploy's cache window has nothing to hold; the Flagship backend keeps the
    HTTP proof."""
    variants = {ON_VARIANT: SERVED_TRUE, OFF_VARIANT: SERVED_FALSE}
    return InMemoryProvider(
        {
            FLAG_ON: InMemoryFlag(default_variant=ON_VARIANT, variants=variants),
            FLAG_OFF: InMemoryFlag(default_variant=OFF_VARIANT, variants=variants),
        }
    )
