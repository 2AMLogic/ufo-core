from ufo.sdk.context import ExtensionContext

WORKSPACE_FACT_NAME = "sample_capability"
WORKSPACE_FACT_KEY = "workspace_fact:held"
WORKSPACE_FACT_LINE = "Sample: this workspace set up the sample extension's capability."


async def workspace_fact_held(ext: ExtensionContext) -> bool:
    """The sample's own store answers, so a test proves core ran this read at turn assembly by
    writing the key through the public store and finding the line in the assembled prompt. A
    workspace that never wrote it states nothing, which is the other half of the point."""
    return await ext.store.get(WORKSPACE_FACT_KEY) is True
