from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from openfeature.provider.in_memory_provider import InMemoryFlag, InMemoryProvider

from core.tests.skills.test_skills import _write_nested_child, _write_skill
from core.tests.test_flagged_tools import ACTIONS, FLAG, FLAGGED, PLAIN, _Input
from ufo.flags import SERVED_FALSE, SERVED_TRUE, init_flags
from ufo.harness.models.interface import Message, ToolResultBlock, ToolUseBlock
from ufo.host.assemble import HostEnvironment, _skills_with_document, flags_reading_off
from ufo.host.environment import EnvironmentDocument
from ufo.host.ext.loader import skill_registry
from ufo.runtime.engine import _loaded_skill_closures
from ufo.runtime.ext.manifest import Manifest, SkillSpec, SubagentProfile
from ufo.runtime.queue import AssembleRequest
from ufo.runtime.skills.runtime import LoadedSkills, SkillRegistry, loaded_context
from ufo.runtime.subagents import SubagentRegistry
from ufo.runtime.turns.audience import SHARED_AUDIENCE
from ufo.runtime.workspace import ws
from ufo.schema.records import Agent, Turn


@pytest.fixture
def registry(tmp_path: Path) -> SkillRegistry:
    gated = _write_skill(tmp_path, "gated", "Gated workflow", "Gated instructions")
    _write_nested_child(gated, "child", "Nested workflow", "Child instructions")
    plain = _write_skill(tmp_path, "plain", "Plain workflow", "Plain instructions")
    return skill_registry(
        (Manifest(name="example", version="1", skills=(SkillSpec(gated, FLAG), SkillSpec(plain))),)
    )


@pytest.fixture
def request_for_skills(registry: SkillRegistry) -> AssembleRequest:
    return AssembleRequest(
        turn=Turn(
            id=uuid4(),
            workspace_id=uuid4(),
            conversation_id=uuid4(),
            agent_id=uuid4(),
            seq=1,
            status="running",
            inbound="Hello",
            created_at=datetime.now(UTC),
        ),
        agent=Agent(prompt="Agent", model="model", use_workspace_skills=False),
        audience=SHARED_AUDIENCE,
        profile=None,
        model="model",
        knowledge_cutoff="2026-01",
        preload_names=(),
        skills=registry,
        subagents=SubagentRegistry(()),
        subagent_grants={},
        member_block=False,
        environment=None,
        context_window="",
    )


def test_loader_carries_the_flag_to_the_skill_and_its_children(registry: SkillRegistry) -> None:
    assert registry.named("gated").flag == FLAG
    assert registry.named("gated/child").flag == FLAG
    assert registry.named("plain").flag is None


@pytest.mark.parametrize("value", (SERVED_FALSE, SERVED_TRUE, None))
@pytest.mark.parametrize("subagent", (False, True))
async def test_assembly_withholds_flagged_skills_from_index_and_load(
    db: None, request_for_skills: AssembleRequest, value: str | None, subagent: bool
) -> None:
    if subagent:
        request_for_skills = replace(
            request_for_skills,
            profile=SubagentProfile(
                name="example",
                prompt="Instructions",
                tool_names=(),
                input_model=_Input,
                output_model=_Input,
            ),
            preload_names=("gated", "plain"),
        )
    init_flags(
        InMemoryProvider(
            {}
            if value is None
            else {FLAG: InMemoryFlag(default_variant="set", variants={"set": value})}
        )
    )
    try:
        with ws(request_for_skills.turn.workspace_id):
            assembled = await HostEnvironment(manifests=(), credentials=None).assemble(
                request_for_skills
            )
        assert "plain" in dict(assembled.skills.index())
        assert (
            "Plain instructions" if subagent else "Plain workflow"
        ) in assembled.system_prompt.content
        for name in ("gated", "gated/child"):
            if value == SERVED_TRUE:
                loaded = await assembled.skills.materialize(assembled.skills.closure(name))
                assert loaded[0].skill == request_for_skills.skills.named(name)
            else:
                assert name in frozenset(assembled.skills.by_name)
                assert name not in assembled.skills.known_names()
                assert name not in {card.name for card in assembled.skills.all_cards()}
                with pytest.raises(ValueError, match="unknown skill"):
                    assembled.skills.closure(name)
        gated_text = "Gated instructions" if subagent else "Gated workflow"
        assert (gated_text in assembled.system_prompt.content) is (value == SERVED_TRUE)
        if subagent:
            assert {entry.skill.name for entry in assembled.preload} == (
                {"gated", "plain"} if value == SERVED_TRUE else {"plain"}
            )
    finally:
        init_flags(InMemoryProvider({}))


async def test_skill_flags_share_the_tool_and_action_flag_pass(registry: SkillRegistry) -> None:
    init_flags(
        InMemoryProvider(
            {FLAG: InMemoryFlag(default_variant="off", variants={"off": SERVED_FALSE})}
        )
    )
    try:
        with ws(uuid4()):
            withheld = await flags_reading_off((PLAIN, FLAGGED), ACTIONS, registry.by_name.values())
        assert withheld == {FLAG}
        assert registry.without_flags(withheld).named("plain") == registry.named("plain")
        assert registry.without_flags(withheld).known_names() == set(registry.by_name) - {
            "gated",
            "gated/child",
        }
    finally:
        init_flags(InMemoryProvider({}))


async def test_withheld_skill_names_stay_reserved_without_becoming_loadable(
    registry: SkillRegistry,
) -> None:
    async def no_member_skill(name: str) -> None:
        return None

    prior_refs = registry.closure("gated")
    filtered = registry.without_flags({FLAG})
    assert frozenset(filtered.by_name) == frozenset(registry.by_name)
    assert "gated" not in dict(filtered.index())
    assert "gated" not in {skill.name for skill in filtered.bundled_skills()}
    with pytest.raises(ValueError, match="unknown skill"):
        filtered.named("gated")
    with pytest.raises(ValueError, match="unknown skill"):
        await filtered.materialize(prior_refs)
    assert filtered.merged_with(()).known_names() == filtered.known_names()
    assert filtered.with_member((), no_member_skill).known_names() == filtered.known_names()


def test_environment_skill_replacement_keeps_its_flag(registry: SkillRegistry) -> None:
    text = registry.named("gated").raw_skill_md.replace(
        "Gated instructions", "Changed instructions"
    )
    changed = _skills_with_document(registry, EnvironmentDocument(skills={"gated": text}))
    assert changed.named("gated").instructions == "Changed instructions"
    assert changed.named("gated").flag == FLAG
    assert "gated" not in changed.without_flags({FLAG}).known_names()


@pytest.mark.parametrize("dependent_flag", (None, "different", FLAG))
def test_registry_rejects_a_dependency_with_a_different_flag(
    tmp_path: Path, dependent_flag: str | None
) -> None:
    dependency = _write_skill(tmp_path, "dependency", "Dependency", "Instructions")
    dependent = _write_skill(tmp_path, "dependent", "Dependent", "Instructions", ("dependency",))
    manifests = (
        Manifest(
            name="example",
            version="1",
            skills=(SkillSpec(dependency, FLAG), SkillSpec(dependent, dependent_flag)),
        ),
    )
    if dependent_flag == FLAG:
        assert len(skill_registry(manifests).closure("dependent")) == 2
    else:
        with pytest.raises(ValueError, match=r"dependent.*dependency.*different flag"):
            skill_registry(manifests)


async def test_next_turn_drops_a_previously_loaded_withheld_skill(
    db: None, request_for_skills: AssembleRequest
) -> None:
    host = HostEnvironment(manifests=(), credentials=None)
    tracker = LoadedSkills()
    try:
        init_flags(
            InMemoryProvider(
                {FLAG: InMemoryFlag(default_variant="on", variants={"on": SERVED_TRUE})}
            )
        )
        with ws(request_for_skills.turn.workspace_id):
            first = await host.assemble(request_for_skills)
        window: tuple[Message, ...] = ()
        for name in ("gated", "plain"):
            body = loaded_context(await first.skills.materialize(first.skills.closure(name)))
            window += (
                Message(
                    role="assistant",
                    content=(ToolUseBlock(id=name, name="load_skill", input={"name": name}),),
                ),
                Message(role="user", content=(ToolResultBlock(tool_use_id=name, content=body),)),
            )
        tracker.reseed(_loaded_skill_closures(window, first.skills))
        assert tracker.in_context == tracker.asked_for == {"gated", "plain"}
        init_flags(
            InMemoryProvider(
                {FLAG: InMemoryFlag(default_variant="off", variants={"off": SERVED_FALSE})}
            )
        )
        next_turn = request_for_skills.turn.model_copy(update={"id": uuid4(), "seq": 2})
        with ws(next_turn.workspace_id):
            second = await host.assemble(replace(request_for_skills, turn=next_turn))
        tracker.reseed(_loaded_skill_closures(window, second.skills))
        assert tracker.in_context == tracker.asked_for == {"plain"}
    finally:
        init_flags(InMemoryProvider({}))
