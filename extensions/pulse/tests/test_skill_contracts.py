"""The rules `parse_skill_content` enforces, asserted without the runtime: a skill whose frontmatter
breaks one of these fails at boot, so it is worth catching in a test that needs nothing installed.

The description budget is a routing budget, not a style preference — an over-long description is what
makes a skill load on the wrong turn.
"""

import pytest
import yaml
from conftest import SKILL_NAMES, SKILLS_ROOT

DESCRIPTION_WORD_BUDGET = 50


def frontmatter(name: str) -> dict:
    raw = (SKILLS_ROOT / name / "SKILL.md").read_text()
    assert raw.startswith("---"), f"{name}: SKILL.md must open with ---"
    _, meta, _ = raw.split("---", 2)
    return yaml.safe_load(meta)


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_name_matches_its_directory(name: str) -> None:
    assert frontmatter(name)["name"] == name


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_description_routes(name: str) -> None:
    description = frontmatter(name)["description"]
    assert description.startswith("Load when")
    assert len(description.split()) <= DESCRIPTION_WORD_BUDGET


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_body_is_present(name: str) -> None:
    _, _, body = (SKILLS_ROOT / name / "SKILL.md").read_text().split("---", 2)
    assert body.strip()


def test_field_pulse_pulls_the_writer_and_both_contracts() -> None:
    """Wired, not remembered. A setup turn cannot reach its first edition without the skill that
    writes every edition, and neither skill can be loaded without the two contracts."""
    depends = frontmatter("field-pulse")["metadata"]["depends"]
    assert set(depends) == {"field-report", "brief-continuity", "coverage-honesty"}


def test_field_report_pulls_both_contracts() -> None:
    depends = frontmatter("field-report")["metadata"]["depends"]
    assert set(depends) == {"brief-continuity", "coverage-honesty"}


def test_the_handoff_pulls_nothing() -> None:
    """`depends` is the only pull, and the workflow this turn must not run is the one it hands over.
    A `field-pulse` pulled in here is a second turn able to run the setup, in the one agent whose
    armed gather would be owned by the wrong row."""
    assert "depends" not in frontmatter("pulse-handoff").get("metadata", {})


def test_the_handoff_routes_away_from_competitive_intel() -> None:
    """This is the description that reads the member's own sentence, so it is the one that has to
    say which nearby ask it is not: a list of named companies is `competitive-intel`, which watches
    names rather than a domain."""
    assert "competitive-intel" in frontmatter("pulse-handoff")["description"]


def test_the_handoff_routes_away_from_reading_an_edition() -> None:
    """An edition is answered where the member asked, because the record is keyed by workspace and
    series. A handoff that took the edition too would spawn a conversation to write what this one
    can write, and answer minutes later."""
    assert "field-report" in frontmatter("pulse-handoff")["description"]


def test_field_pulse_routes_the_chat_side_ask_to_the_handoff() -> None:
    """Both descriptions sit in every agent's index, and the member's sentence has to reach exactly
    one of them. The handoff names the phrasing; this one names the situation it answers in — a
    turn already handed a field — and says where the phrasing goes instead."""
    description = frontmatter("field-pulse")["description"]
    assert "pulse-handoff" in description
    assert "pulse agent" in description


def test_field_report_routes_away_from_setting_a_pulse_up() -> None:
    """Setting a pulse up and reading an edition of one are the nearby asks here, and routing to the
    wrong one either re-asks a member for sources they confirmed or skips the ask entirely."""
    assert "field-pulse" in frontmatter("field-report")["description"]


def test_the_report_reads_the_pool_by_tool() -> None:
    """The report's whole claim -- that it publishes without searching -- rests on it reading the
    pool a gather filled, so the file has to name the call rather than describe the store.

    It names the tool and not the script because an edition written on a fire with no client has no
    command tool to run a script with, and a script that cannot run reads as a series with no
    history: the report would then publish last week's stories as new."""
    body = (SKILLS_ROOT / "field-report" / "SKILL.md").read_text()
    assert "pulse_recall" in body
    assert "seen.py" not in body


def test_the_report_reads_the_coverage_store_by_script() -> None:
    """The report is the only skill that writes an edition, so it is the only one that writes a
    footer over several gathers. That footer is a count of recorded states, and a file that
    described the store rather than invoking it would be back to inferring from the pool.

    The window is the one read with no tool behind it: it is an aggregation a footer is written
    from rather than a decision, and a fourth read tool is a prompt-token cost on every turn of
    every agent that loads this pack. It reads the projection, which is rendered whole from the
    record, so what it aggregates is the record's rows and not one tree's half of them."""
    assert "coverage.py" in (SKILLS_ROOT / "field-report" / "SKILL.md").read_text()


def test_the_handoff_spawns_the_agent_and_not_a_profile() -> None:
    """`agent:pulse` resolves against the workspace's agent rows whatever else a deploy registered.
    The bare name is ambiguous once a `pulse` agent row exists — true from the first activation on —
    and the runtime refuses it loudly, naming `profile:pulse` and `agent:pulse` in the error, rather
    than silently handing the setup to this turn's own agent."""
    body = (SKILLS_ROOT / "pulse-handoff" / "SKILL.md").read_text()
    assert 'target="agent:pulse"' in body


def test_the_handoff_carries_the_clock_the_spawned_turn_cannot_read() -> None:
    """A spawned turn's inbound is the bare payload — no opening line, no `<context>` header — so
    the business and the member's own clock are handed over or they are absent. The gather fires
    early in the member's morning, and a setup with no clock has only the deploy's."""
    body = (SKILLS_ROOT / "pulse-handoff" / "SKILL.md").read_text()
    for key in ("request", "business", "local_time"):
        assert key in body


def test_the_setup_reads_its_clock_from_the_payload() -> None:
    """The one field the setup cannot recover for itself. It runs as a spawn, so the `<context>`
    header a member's own turn carries its clock in is not in front of it, and the deploy's time is
    the only other answer available — which arms the gather in the member's evening."""
    body = (SKILLS_ROOT / "field-pulse" / "SKILL.md").read_text()
    assert "local_time" in body


def scheduled_manifest() -> dict:
    """The one YAML block in field-pulse: the task a setup turn applies."""
    body = (SKILLS_ROOT / "field-pulse" / "SKILL.md").read_text()
    block = body.split("```yaml", 1)[1].split("```", 1)[0]
    return yaml.safe_load(block)


def test_the_scheduled_task_gathers_and_nothing_else() -> None:
    """The split is only real if the armed row cannot publish. A fire that ranked, wrote a file or
    recorded the ledger would be the fused run again, and its ledger rows would make every story in
    it ineligible for the edition the member did ask for."""
    manifest = scheduled_manifest()
    assert manifest["name"].endswith("-gather")
    prompt = manifest["spec"]["prompt"]
    assert "pulse_record_sightings" in prompt
    assert "research-report" not in prompt
    assert "covered.py" not in prompt
    assert "run_now" not in manifest["spec"]


def test_the_scheduled_task_records_through_a_tool_and_not_a_script() -> None:
    """The armed row is the prompt least likely to run where a member's own session does, since
    nothing binds a scheduled fire to the terminal a series was set up from. So it is the one that
    must not reach for a script: a row telling the fire to run `seen.py` writes a second ledger for
    the series in whichever tree that fire started in, which is the fault this whole store replaced
    — measured on the demo deploy as two `agent-runtimes` ledgers whose most recent edition shared
    no story at all.

    It asserts the tool and the *absence* of the script, not a phrase, because the reason belongs in
    prose that can be rewritten. The earlier version of this test pinned the phrase "no client
    attached" — which described a mechanism that turned out not to exist."""
    prompt = scheduled_manifest()["spec"]["prompt"]
    assert "pulse_record_sightings" in prompt
    assert "seen.py" not in prompt
    assert "covered.py" not in prompt
    assert "coverage.py" not in prompt


def test_the_scheduled_gather_records_each_source_state() -> None:
    """A state is only writable while the read is in front of the run, and the gather is the run
    that holds it. An armed row that recorded sightings and not states leaves every edition over
    its window to read a failure out of the pool's silence, which is the ambiguity the store ends.

    Through the tool, for the reason above it: a fire's own tree is not the tree the edition that
    counts these states is written from."""
    assert "pulse_record_coverage" in scheduled_manifest()["spec"]["prompt"]


def test_the_daily_gather_is_bounded() -> None:
    """`task-scheduling` bounds a task that fires daily or more often with an `expires_at`, and an
    unbounded one keeps paying for search after the member has stopped reading it."""
    spec = scheduled_manifest()["spec"]
    assert spec["schedule"].split() == ["0", "12", "*", "*", "*"]
    assert "expires_at" in spec


def test_continuity_ships_its_ledger_script() -> None:
    assert (SKILLS_ROOT / "brief-continuity" / "covered.py").is_file()


def test_continuity_ships_its_pool_script() -> None:
    """SKILL.md invokes seen.py by path, exactly as it does covered.py. The argument for a script
    over a prose rule -- that staleness is a lookup rather than a recollection -- rests entirely on
    the script being there to look up."""
    assert (SKILLS_ROOT / "brief-continuity" / "seen.py").is_file()


def test_continuity_ships_its_coverage_script() -> None:
    """The multi-day footer `coverage-honesty` specifies is an aggregation over gathers, so the
    store holding each gather's source states has to ship where SKILL.md says to run it."""
    assert (SKILLS_ROOT / "brief-continuity" / "coverage.py").is_file()


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_every_skill_closes_with_traps(name: str) -> None:
    """House shape: the failure modes are listed where a reader looks for them."""
    assert "## Traps" in (SKILLS_ROOT / name / "SKILL.md").read_text()
