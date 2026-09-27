"""Apply agent specifications through the shared object mutation boundary."""

import yaml

from ufo.runtime.kinds.agents import AGENT_KIND, AGENT_OBJECT, AgentSpec
from ufo.runtime.objects import BoundKind, ObjectApplyInput, ObjectVerbs
from ufo.runtime.tools.context import ToolContext, ToolResult


async def apply_agent(
    ctx: ToolContext,
    name: str,
    spec: AgentSpec,
    *,
    create_only: bool = True,
) -> ToolResult:
    """Apply an agent with normal ownership checks, duplicate protection, and change journal."""
    verbs = ObjectVerbs({AGENT_KIND: BoundKind(AGENT_OBJECT, extension=None, context=None)})
    return await verbs.apply(
        ctx,
        ObjectApplyInput(
            manifest=yaml.safe_dump(
                {
                    "kind": AGENT_KIND,
                    "name": name,
                    "spec": spec.model_dump(mode="json", exclude_unset=True),
                }
            ),
            create_only=create_only,
        ),
    )
