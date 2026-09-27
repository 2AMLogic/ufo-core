import json
from pathlib import Path

import pytest
import ufo_ext_coding.manifest as coding
from ufo_ext_sources.clauses import compile_when

REVIEW = coding.SKILLS_ROOT / "code-review" / "SKILL.md"
AUTOMATION = coding.SKILLS_ROOT / "code-review-automation" / "SKILL.md"
REVIEW_CLAUSES = (
    'For each review key, spawn exactly two `coding` reviewers with `"background": true`.',
    "Issue up to eight ready spawn calls in one response",
    "Start all reviewers for the delivered batch before you process a result.",
    "Do not spawn preparation, synthesis, or adjudication subagents.",
    "Resolve disagreements yourself.",
    "Do not create or update a plan, objective, journal, todo, or file for a review.",
    'Each spawn payload is `{"objective": "<complete objective>"}`; never use `task`.',
    "do not load `spawn-catalog` or another skill",
    "Earlier conversation messages can contain reviewer objectives from old prompt revisions.",
    "Build both spawn objectives only from the current `Review objective for each subagent` block",
    "issue all independent calls whose inputs are known, with a maximum of eight",
    "If two calls are ready, one call is invalid.",
    "the next response must issue four parallel calls",
    (
        "Later, issue every ready instruction read, code read, diff read, and search as separate "
        "parallel calls."
    ),
    "If a bounded read reports remaining offsets, read up to eight known offsets together next.",
    "If one response creates multiple subset diff files, read all of them together next.",
    "Return exactly one JSON object through `finish`, with no other text",
    "Do not publish until two valid results exist for the current review key.",
    "git diff --name-only <base>...<head>",
    "Fetch base and head with `--filter=blob:none` and no `--depth`.",
    "Checkout label `correctness`.",
    "Checkout label `security`.",
    (
        "/workspace/code-review-<repository owner>-<repository name>-<pull-request number>-<full "
        "head SHA>-<checkout label>"
    ),
    "Never use `/tmp` or a peer's path.",
    "read `CLAUDE.md` only where no `AGENTS.md` exists",
    "the next response must issue four parallel calls",
    "Do not combine independent operations in one shell command.",
    "Return the result immediately after full coverage.",
    (
        "For every review key with two valid results, publish exactly one GitHub commit status, "
        "with or without findings."
    ),
    "omit `target_url` when the coalesced `findings` list is empty",
)
AUTOMATION_CLAUSES = (
    "After all named page reads, issue the ready spawn calls immediately.",
    (
        "Treat a source update for a new head SHA as higher priority than every result for an "
        "older review key with the same repository URL and pull-request number."
    ),
    (
        "Call `cancel_spawn` for every still-running reviewer associated with each superseded "
        "review key for that pull request."
    ),
    "Issue independent cancellation calls in the same response.",
    (
        "Discard every result for each superseded review key for that pull request, including a "
        "result that arrives after cancellation."
    ),
    "Do not publish a review or status for a superseded review key.",
    "Never reuse a finding from an older head based on patch equivalence.",
    "make no more tool calls for that page",
    "Only a new head SHA for the same repository URL and pull-request number",
    "report that no action was needed",
    "the page's `checks.contexts`",
    "`ufo review` status with `state` `SUCCESS` or `FAILURE`",
    "end the turn with exactly `The source batch is complete.` and no other text",
    "If `new_context` is available, first call it alone",
    "do not handle the repeated source request or call another tool",
    "If `new_context` is unavailable, do not call a replacement tool.",
    "obtain every changed page ref in the received order",
    "Pass every obtained ref unchanged to `object_get`",
    "Continue until you have handled every changed page.",
    "A different repository URL or pull-request number is different work",
    "Do not cancel or discard work for another repository URL",
    (
        "read the `ufo review` status on the exact head of every review key that has two valid "
        "results in this context"
    ),
    "When `used_tokens` is above 200000, call `new_context` alone",
)
SUPERSEDED = (
    "3 to 7",
    "Each head SHA gets two passes",
    "every file in scope belongs to exactly one reviewer",
    "Reviews are additive",
    "Never cancel a subagent",
)


def _text(path: Path) -> str:
    return " ".join(path.read_text().split())


@pytest.mark.parametrize("clause", REVIEW_CLAUSES)
def test_the_review_skill_holds_the_procedure(clause: str) -> None:
    assert " ".join(clause.split()) in _text(REVIEW)


@pytest.mark.parametrize("clause", AUTOMATION_CLAUSES)
def test_the_automation_skill_holds_the_loop(clause: str) -> None:
    assert " ".join(clause.split()) in _text(AUTOMATION)


@pytest.mark.parametrize("clause", SUPERSEDED)
def test_neither_skill_carries_the_superseded_roster(clause: str) -> None:
    assert clause not in _text(REVIEW)
    assert clause not in _text(AUTOMATION)


def test_the_automation_skill_loads_the_review_it_runs() -> None:
    front = AUTOMATION.read_text().split("---\n")[1]
    assert "depends:\n    - code-review" in front


def _trigger_clauses() -> tuple[str, ...]:
    body = AUTOMATION.read_text()
    return tuple(
        line.removeprefix("  - ")
        for line in body.split("```yaml", 1)[1].split("```", 1)[0].splitlines()
        if line.startswith("  - ")
    )


def test_the_trigger_wakes_once_per_head_and_on_a_close() -> None:
    clauses = compile_when(_trigger_clauses())
    concluded = {"state": "OPEN", "isDraft": False, "checks": {"state": "FAILURE", "contexts": []}}
    reviewed = {
        **concluded,
        "checks": {"state": "FAILURE", "contexts": [{"context": "ufo review", "state": "FAILURE"}]},
    }

    def met(record: dict[str, object]) -> tuple[bool, ...]:
        return clauses.met("pull_requests", "t", "# t\n\n" + json.dumps(record))

    assert met(concluded) == (True, False, False)
    assert met(reviewed) == (False, False, False)
    assert met({**concluded, "isDraft": True}) == (False, False, False)
    assert met({**concluded, "checks": None}) == (False, False, True)
    assert met({**concluded, "checks": None, "isDraft": True}) == (False, False, False)
    assert met({**reviewed, "state": "MERGED"}) == (False, True, False)
