from ufo.sdk.manifest import ConversationSlotContext, WorkspaceChanges

CONVERSATION_SLOT = "sample_changes"


async def conversation_slot_summary(_ctx: ConversationSlotContext) -> None:
    return None


async def conversation_slot_read(_ctx: ConversationSlotContext) -> WorkspaceChanges:
    return WorkspaceChanges(changes=(), truncated=False)
