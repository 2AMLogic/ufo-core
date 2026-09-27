from dataclasses import dataclass

EMBED_BACKEND = "sample_embed"
SAMPLE_EMBED_VECTOR = (1.0, 0.0, 0.0)


@dataclass(frozen=True)
class SampleEmbed:
    """A canned EmbedClient the probe registers through the `embeds` Manifest point: `embed` returns
    one fixed vector per text. A real client consumed through the protocol, so a test drives core's
    embed selection exactly as core does; the OpenAI backend keeps its own proof."""

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        return tuple(SAMPLE_EMBED_VECTOR for _ in texts)
