"""The ufo extension's manifest: one live surface on the core seam, the terminal wire the `ufo`
shell client renders. No credential slots — the member's bearer is verified against the env
`UFO_TOKEN_SECRET`, not a workspace slot — and no config knob: installed means mounted, like web.
It declares the held stream twice over — per channel, and per conversation the member joins by
id — beside the read projection that lists those conversations, and no writeback delivery: it
admits without writeback and tails the hub in the same routes."""

from functools import partial

from ufo.sdk.manifest import Manifest
from ufo.sdk.surfaces import SurfaceRoute, SurfaceSpec
from ufo_ext_ufo.surface import (
    SURFACE_UFO,
    channel,
    conversation,
    conversations,
    op_body,
    resolve_workspace,
    served_client,
    store_environment,
    store_environment_file,
    system_skills,
    workspace_file,
    workspace_listing,
    workspace_upload,
)

NAME = "ufo"
VERSION = "0.1.0"


def manifest() -> Manifest:
    served = served_client()
    routes = (
        SurfaceRoute(method="POST", path="environment/document", handler=store_environment),
        SurfaceRoute(method="POST", path="environment/file", handler=store_environment_file),
        SurfaceRoute(method="GET", path="conversations", handler=conversations),
        SurfaceRoute(
            method="POST",
            path="conversation/{conversation_id}",
            handler=partial(conversation, served),
        ),
        SurfaceRoute(method="POST", path="{channel}", handler=partial(channel, served)),
        SurfaceRoute(method="GET", path="{channel}/op/{op_id}", handler=op_body),
        SurfaceRoute(method="GET", path="{channel}/skills", handler=system_skills),
        SurfaceRoute(method="GET", path="{channel}/files", handler=workspace_listing),
        SurfaceRoute(method="GET", path="{channel}/file/{path:path}", handler=workspace_file),
        SurfaceRoute(method="PUT", path="{channel}/file/{path:path}", handler=workspace_upload),
    )
    return Manifest(
        name=NAME,
        version=VERSION,
        surfaces=(SurfaceSpec(name=SURFACE_UFO, routes=routes, identify=resolve_workspace),),
    )
