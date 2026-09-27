from ufo.sdk.context import ExtensionContext
from ufo_ext_sample.sources import SOURCE_BACKEND, SOURCE_TOPIC, SampleSourceConfig

ONBOARDING_NAME = "sample_setup"
ONBOARDING_KEY = "onboarding:done"


async def setup(ctx: ExtensionContext) -> None:
    await ctx.store.put(ONBOARDING_KEY, {"onboarded": True})
    await ctx.register_source(
        SOURCE_BACKEND,
        SampleSourceConfig(topic=SOURCE_TOPIC),
        connection_id=await ctx.register_connection(SOURCE_BACKEND),
    )
