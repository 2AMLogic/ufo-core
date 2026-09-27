from dataclasses import dataclass
from uuid import UUID

from ufo.sdk.authproxy import Credential

AUTH_PROXY_BACKEND = "sample_auth_proxy"
AUTH_PROXY_BEARER = "sample"


@dataclass(frozen=True)
class SampleAuthProxy:
    """A trivial AuthProxy the probe registers through the `auth_proxies` Manifest point:
    `credential` returns a fixed bearer, exercising the seam core drives (select a
    manifest-contributed auth-proxy backend by name and thread it onto the sync runner). A real
    object consumed through the protocol, so a test drives it exactly as core does; the Composio and
    direct backends keep their own proofs."""

    async def credential(self, workspace_id: UUID, provider: str) -> Credential:
        return Credential(bearer=AUTH_PROXY_BEARER)
