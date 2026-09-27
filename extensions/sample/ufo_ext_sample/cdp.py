import asyncio
from dataclasses import dataclass
from pathlib import Path

from ufo.sdk.browser import CdpEndpoint, CdpLease, FileBytes
from ufo.sdk.sandbox import Sandbox

CDP_PROVIDER = "sample_cdp"
SAMPLE_CDP_URL = "wss://sample.test/cdp"
SAMPLE_DOWNLOAD_DIR = "/tmp/ufo-sample-downloads"


@dataclass(frozen=True)
class SampleCdpLease:
    """The canned per-turn lease the sample's cdp provider mints: `endpoint` returns a fixed
    `CdpEndpoint`, `token` the durable reattach handle (the fixed URL), `place_file` answers the
    path unchanged as a sandbox-local browser does, `aclose` is a no-op — a real object exercised
    through the `CdpLease` protocol the browser engine drives, never a mock."""

    async def endpoint(self) -> CdpEndpoint:
        return CdpEndpoint(url=SAMPLE_CDP_URL)

    async def token(self) -> str:
        return SAMPLE_CDP_URL

    async def place_file(self, path: str, read: FileBytes) -> str:
        return path

    async def download_dir(self) -> str:
        return SAMPLE_DOWNLOAD_DIR

    async def fetch_download(self, guid: str) -> bytes:
        return await asyncio.to_thread((Path(SAMPLE_DOWNLOAD_DIR) / guid).read_bytes)

    async def aclose(self) -> None:
        return None


@dataclass(frozen=True)
class SampleCdpProvider:
    """A trivial CdpProvider the probe registers through the `cdp_providers` Manifest point: `lease`
    mints a canned SampleCdpLease, `reattach` reconnects to the same fixed endpoint. A real object
    consumed through the protocol, so a test drives it as core selects and leases it; the BUA engine
    keeps its own live-CDP proof."""

    async def lease(self, sandbox: Sandbox | None = None) -> CdpLease:
        return SampleCdpLease()

    async def reattach(self, token: str, sandbox: Sandbox | None = None) -> CdpLease:
        return SampleCdpLease()
