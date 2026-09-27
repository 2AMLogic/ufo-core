from dataclasses import dataclass, field

from ufo.sdk.context import BoundaryInputs, BoundaryOutcome, ContextBoundary, ContextRemaining
from ufo.sdk.models import Message
from ufo.sdk.skills import LoadedSkills

CONTEXT_STRATEGY = "sample_boundary"
CONTEXT_BOUNDARY_HANDOFF_CHARS = 4_000
CONTEXT_BOUNDARY_CHECKLIST_CHARS = 2_000
CONTEXT_WINDOW_PROMPT = (
    "<context_window>\nThis deploy holds your whole window: nothing is summarized and nothing is "
    "reset. Read where the window stands with get_context_remaining.\n</context_window>"
)


@dataclass(frozen=True)
class SampleContextBoundary:
    """A trivial ContextBoundary the probe registers through the `context_boundaries` Manifest
    point: it holds every window whole and never crosses, so a deploy that selects
    `sample_boundary` runs the extension's strategy instead of either core one. A real object
    consumed through the protocol, so a test drives it as the loop does; rollover and compaction
    keep their own boundary proofs."""

    window_tokens: int
    loaded_skills: LoadedSkills = field(default_factory=LoadedSkills)

    def remaining(self, messages: tuple[Message, ...] | None = None) -> ContextRemaining:
        return ContextRemaining(
            used_tokens=0,
            rollover_at_tokens=self.window_tokens,
            tokens_until_rollover=self.window_tokens,
            hard_limit_tokens=self.window_tokens,
            tokens_until_hard_limit=self.window_tokens,
        )

    def handoff_cap(self) -> int:
        return CONTEXT_BOUNDARY_HANDOFF_CHARS

    def checklist_cap(self) -> int:
        return CONTEXT_BOUNDARY_CHECKLIST_CHARS

    async def maybe_cross(
        self,
        messages: tuple[Message, ...],
        force: bool = False,
        active_requests: tuple[str, ...] = (),
        final: bool = False,
    ) -> BoundaryOutcome:
        return BoundaryOutcome(messages=messages, crossed=False)


def build_sample_boundary(inputs: BoundaryInputs) -> ContextBoundary:
    return SampleContextBoundary(window_tokens=inputs.serving.spec.context_window)
