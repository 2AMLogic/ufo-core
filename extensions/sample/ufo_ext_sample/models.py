from collections.abc import AsyncIterator
from dataclasses import dataclass

from ufo.sdk.models import ModelEvent, ModelPrice, ModelRequest, ModelStreamStart, TextDelta, Usage

MODEL_PROVIDER_NAME = "sample_models"
SAMPLE_MODEL = "sample-model-x1"
SAMPLE_MODEL_REPLY = "sample model backend reply"
SAMPLE_MODEL_PRICE = ModelPrice(2_000_000, 4_000_000, 0, 0, 0)


@dataclass(frozen=True)
class SampleModelClient:
    """The canned backend the sample's model provider builds: `complete` streams one text delta and
    a fixed Usage, so the registry seam — core selecting a manifest-contributed model client and
    pricing its id against the contributed rate — is exercised by a real client, never a mock."""

    model: str

    async def complete(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        yield ModelStreamStart()
        yield TextDelta(text=SAMPLE_MODEL_REPLY)
        yield Usage(input_tokens=1, output_tokens=1)
