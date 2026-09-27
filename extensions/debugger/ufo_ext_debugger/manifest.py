"""The debugger extension's manifest: one live surface on the core seam, reachable only by a
bearer the deploy's operator rule grants."""

from ufo.sdk.manifest import Manifest
from ufo.sdk.operator import DEBUGGER_SURFACE, resolve_operator_workspace
from ufo.sdk.surfaces import SurfaceSpec
from ufo_ext_debugger.surface import ROUTES

NAME = "debugger"
VERSION = "0.1.0"


def manifest() -> Manifest:
    return Manifest(
        name=NAME,
        version=VERSION,
        surfaces=(
            SurfaceSpec(name=DEBUGGER_SURFACE, routes=ROUTES, identify=resolve_operator_workspace),
        ),
    )
