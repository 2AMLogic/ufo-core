"""What the matrix extension declares: one durable surface, the two credential slots its bot needs,
the setup action that binds that bot to a workspace, the skill that walks an admin through it, and
the two tools that link and unlink a member's MXID.

A Matrix room is a conversation and the people in it are members. The bot user's `/sync` stream is
the surface's listener, and each terminal turn is one message back into its room — rich text under a
relation to the message it answers — sent under a transaction id derived from the turn so a retried
delivery lands once. The turn's shared files follow that reply as messages of their own, and words
the turn marks for delivery before it ends reach the room as they are marked. The deploy names the bots
its listener runs in `UFO_MATRIX_BOTS`; each workspace holds its own bot's homeserver and token, and
`matrix_connect` binds the bot the token belongs to. A member whose MXID the workspace's domain does
not vouch for links it with `matrix_link_account` and a code sent from it; an admin undoes a link
with `matrix_unlink_account`.

Setup happens in chat and nowhere else. `matrix_connect` is an instance action bound to the `matrix`
surface, so it is offered on that one surface row — `action:surface:matrix_connect` — rather than as a
tool a turn holds everywhere, and `matrix-setup` is the skill that carries the order of the steps and
the four silences a misconfigured bot answers with.

That is why `requires` is empty while a sibling extension names a seam it cannot run without.
`requires` names one of four seams another extension serves — `cdp_providers`, `context_boundaries`,
`memory_search`, `search_providers` — and matrix consumes none of them; a credential slot is not a
seam and cannot be spelled as one. The "would a deploy refuse to boot without this" instinct still
has an answer, and it is `deploy_keys`, which matrix already carries for `MATRIX_BOTS`. The three
credential slots are a different thing again: per-workspace values, read per request, filled by
`matrix_connect` in chat.

In an encrypted room the bot's device decrypts what it is sent and encrypts what it posts; its keys
live in the extension's own table, sealed under the `matrix_store_key` slot. An edit and a redaction
found no turn."""

from pathlib import Path

from ufo.sdk.manifest import CredentialSlot, Manifest, SkillSpec
from ufo.sdk.objects import SURFACE_KIND
from ufo.sdk.surfaces import SurfaceSpec
from ufo.sdk.tools import ObjectBinding, ToolDef
from ufo_ext_matrix.crypto import STORE_KEY_SLOT
from ufo_ext_matrix.events import SURFACE
from ufo_ext_matrix.linking import CLAIM_MINUTES, LinkInput, UnlinkInput
from ufo_ext_matrix.surface import (
    HOMESERVER_SLOT,
    TOKEN_SLOT,
    ConnectInput,
    MatrixSurface,
    refuse_requests,
)

NAME = "matrix"
VERSION = "0.1.0"
CONNECT_TOOL = "matrix_connect"
LINK_TOOL = "matrix_link_account"
UNLINK_TOOL = "matrix_unlink_account"

SKILLS_ROOT = Path(__file__).parent / "skills"
SKILL_NAMES = ("matrix-setup",)


def manifest(surface: MatrixSurface | None = None) -> Manifest:
    matrix = surface or MatrixSurface()
    return Manifest(
        name=NAME,
        version=VERSION,
        surfaces=(
            SurfaceSpec(
                name=SURFACE,
                identify=refuse_requests,
                listen=matrix.listen,
                post=matrix.post,
                attach=matrix.attach,
                speak=matrix.speak,
            ),
        ),
        credentials=(
            CredentialSlot(
                name=HOMESERVER_SLOT,
                description="Base URL of the homeserver the Matrix bot user lives on.",
            ),
            CredentialSlot(
                name=TOKEN_SLOT,
                description="Access token of the Matrix bot user.",
            ),
            CredentialSlot(
                name=STORE_KEY_SLOT,
                description=(
                    "At least 32 random characters that seal the Matrix bot's end-to-end "
                    "encryption keys at rest. Empty, the bot neither reads nor posts in encrypted "
                    "rooms; changed, it loses the keys sealed under the old value."
                ),
            ),
        ),
        tools=(
            ToolDef(
                name=CONNECT_TOOL,
                description=(
                    "Connect this workspace's Matrix bot: check its access token with the "
                    "homeserver and bind the bot so its rooms reach this workspace."
                ),
                input_model=ConnectInput,
                handler=matrix.connect,
                side_effecting=True,
                bound=ObjectBinding(kind=SURFACE_KIND, binding="instance", name=SURFACE),
            ),
            ToolDef(
                name=LINK_TOOL,
                description=(
                    "Link the speaking member's own Matrix ID, one on any homeserver: returns a "
                    "one-time code the member sends the bot in a direct room from that ID within "
                    f"{CLAIM_MINUTES} minutes. Relay the code and the steps to the member exactly."
                ),
                input_model=LinkInput,
                handler=matrix.linking.link_account,
                side_effecting=True,
            ),
            ToolDef(
                name=UNLINK_TOOL,
                description=(
                    "Admin only: unlink a Matrix ID from whichever member it speaks for, so it "
                    "reaches the agent as nobody until it is linked again by code."
                ),
                input_model=UnlinkInput,
                handler=matrix.linking.unlink_account,
                side_effecting=True,
            ),
        ),
        skills=tuple(SkillSpec(path=SKILLS_ROOT / name) for name in SKILL_NAMES),
        deploy_keys=("MATRIX_BOTS",),
    )
