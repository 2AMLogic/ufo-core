from ufo.sdk.manifest import (
    Deny,
    HookContext,
    HookOutcome,
    ModifyInput,
    ModifyOutput,
    PageChangeBatch,
    PostCompact,
    PostToolUse,
    PostToolUseFailure,
    PreCompact,
    PreToolUse,
    Stop,
)
from ufo_ext_sample.objects import BlessInput, target_record

HOOK_POST_KEY = "hook:post"
HOOK_POST_FAILURE_KEY = "hook:post_failure"
HOOK_STOP_KEY = "hook:stop"
HOOK_PRE_COMPACT_KEY = "hook:pre_compact"
HOOK_POST_COMPACT_KEY = "hook:post_compact"
HOOK_PAGE_CHANGE_KEY = "hook:page_change"
HOOK_DENY_REASON = "the sample pre_tool_use hook refuses its sentinel tool"
HOOK_BLESS_PRE_KEY = "hook:bless_pre"
HOOK_BLESS_POST_KEY = "hook:bless_post"
HOOK_BLESS_FAILURE_KEY = "hook:bless_failure"
BLESS_FOLD_SUFFIX = " (folded)"
BLESS_REWRITE = "the bless output was replaced by the sample post hook"


async def bless_fold(ctx: HookContext) -> HookOutcome:
    match ctx.payload:
        case PreToolUse(call=call, target=target, tool_input=BlessInput() as args):
            await ctx.ext.store.put(
                HOOK_BLESS_PRE_KEY,
                {
                    "call": call,
                    "tool_name": ctx.payload.tool_name,
                    "target": target_record(target),
                },
            )
            return ModifyInput(
                tool_input=BlessInput(phrase=args.phrase + BLESS_FOLD_SUFFIX, fail=args.fail)
            )
    return None


async def bless_replace(ctx: HookContext) -> HookOutcome:
    match ctx.payload:
        case PostToolUse(call=call, output=output, target=target):
            await ctx.ext.store.put(
                HOOK_BLESS_POST_KEY,
                {"call": call, "output": output, "target": target_record(target)},
            )
            return ModifyOutput(output=BLESS_REWRITE)
    return None


async def bless_failure(ctx: HookContext) -> HookOutcome:
    match ctx.payload:
        case PostToolUseFailure(call=call, output=output):
            await ctx.ext.store.put(HOOK_BLESS_FAILURE_KEY, {"call": call, "output": output})
    return None


async def deny_echo(ctx: HookContext) -> HookOutcome:
    """A pre_tool_use gate matched to TOOL_NAME: it refuses that call, so the tool's handler never
    runs and never records TOOL_KEY. The probe that a Deny short-circuits before dispatch."""
    return Deny(reason=HOOK_DENY_REASON)


async def record_post(ctx: HookContext) -> HookOutcome:
    """A post_tool_use observer over every dispatched call that SUCCEEDED: it records the payload
    through the extension's own scoped store, so the test reads back through a public surface that
    the post payload arrived. The probe that a non-denied, non-erroring call reaches the post
    point — an errored call reaches post_tool_use_failure instead."""
    match ctx.payload:
        case PostToolUse(tool_name=tool_name):
            await ctx.ext.store.put(HOOK_POST_KEY, {"tool": tool_name})
    return None


async def record_post_failure(ctx: HookContext) -> HookOutcome:
    """A post_tool_use_failure observer: a dispatched call whose result was an error records here,
    never at post_tool_use. The probe that the success/failure split reaches distinct events."""
    match ctx.payload:
        case PostToolUseFailure(tool_name=tool_name):
            await ctx.ext.store.put(HOOK_POST_FAILURE_KEY, {"tool": tool_name})
    return None


async def record_stop(ctx: HookContext) -> HookOutcome:
    """A stop observer: records the final answer the turn is about to commit, so the test reads back
    that the turn-end event fired with its answer."""
    match ctx.payload:
        case Stop(answer=answer):
            await ctx.ext.store.put(HOOK_STOP_KEY, {"answer": answer})
    return None


async def record_pre_compact(ctx: HookContext) -> HookOutcome:
    """A pre_compact observer: records the reason and the token estimate before the reset."""
    match ctx.payload:
        case PreCompact(reason=reason, before_tokens=before_tokens):
            await ctx.ext.store.put(
                HOOK_PRE_COMPACT_KEY, {"reason": reason, "before_tokens": before_tokens}
            )
    return None


async def record_post_compact(ctx: HookContext) -> HookOutcome:
    """A post_compact observer: records the recovery record and the tokens bracketing the reset."""
    match ctx.payload:
        case PostCompact(record=record, before_tokens=before_tokens, after_tokens=after_tokens):
            await ctx.ext.store.put(
                HOOK_POST_COMPACT_KEY,
                {
                    "record": record,
                    "before_tokens": before_tokens,
                    "after_tokens": after_tokens,
                },
            )
    return None


# The page_change job name and cursor key carry this function's __name__, so it keeps its own.
async def _record_page_change(ctx: HookContext) -> HookOutcome:
    match ctx.payload:
        case PageChangeBatch(changes=changes):
            await ctx.ext.store.put(
                HOOK_PAGE_CHANGE_KEY,
                {
                    "page_ids": [str(change.page_id) for change in changes],
                    "model_wired": ctx.ext.model is not None,
                },
            )
    return None
