"""The covered ledger, run against a temporary workspace: these assert the file the next fire
actually reads, which is the whole point of keeping the ledger on disk rather than in memory."""

import json
from pathlib import Path

import pytest


def test_an_unwritten_series_reads_empty(covered) -> None:
    assert covered.recent_rows("data-infra", 5) == []


def test_the_ledger_lands_in_the_workspace(covered, tmp_path, monkeypatch) -> None:
    """Workspace-relative, so the member can open it and no sandbox refuses the write: a `UFO_HOME`
    pointing elsewhere — a scratch dir outside every writable root, on the local carrier — moves
    nothing."""
    monkeypatch.setenv("UFO_HOME", str(tmp_path / "scratch" / "ufo"))
    path = covered.record("data-infra", "2026-09-21", "acme-1-0", "Acme 1.0", "")
    assert path == Path("pulse/data-infra.covered.jsonl")
    assert (tmp_path / "pulse" / "data-infra.covered.jsonl").is_file()


def test_a_recorded_story_is_ineligible(covered) -> None:
    covered.record("data-infra", "2026-09-21", "acme-1-0", "Acme 1.0 shipped", "https://x")
    assert covered.main(["check", "--series", "data-infra", "--slug", "acme-1-0"]) == 1


def test_an_unrecorded_story_is_eligible(covered) -> None:
    assert covered.main(["check", "--series", "data-infra", "--slug", "fresh"]) == 0


def test_the_span_is_counted_in_editions_not_days(covered) -> None:
    """Five editions is five briefs the reader saw, whatever cadence produced them."""
    for day in range(20, 27):
        covered.record("data-infra", f"2026-09-{day}", f"s-{day}", "t", "")
    editions = sorted({row["edition"] for row in covered.recent_rows("data-infra", 5)})
    assert editions == [f"2026-09-{day}" for day in range(22, 27)]


def test_a_story_older_than_the_span_becomes_eligible_again(covered) -> None:
    """Old coverage has left the reader's head, so it may be carried as new."""
    covered.record("data-infra", "2026-09-01", "acme-1-0", "Acme 1.0", "")
    for day in range(20, 25):
        covered.record("data-infra", f"2026-09-{day}", f"s-{day}", "t", "")
    assert covered.main(["check", "--series", "data-infra", "--slug", "acme-1-0"]) == 0


def test_a_carried_story_keeps_one_slug(covered) -> None:
    """The exception reuses the slug, so the rows sharing it are the story's history."""
    covered.record("data-infra", "2026-09-21", "acme-1-0", "announced", "")
    covered.record("data-infra", "2026-09-25", "acme-1-0", "shipped", "")
    rows = [r for r in covered.recent_rows("data-infra", 5) if r["slug"] == "acme-1-0"]
    assert len(rows) == 2


def test_the_ledger_is_append_only_jsonl(covered) -> None:
    covered.record("data-infra", "2026-09-21", "acme-1-0", "Acme 1.0", "https://x")
    covered.record("data-infra", "2026-09-23", "beta-raise", "Beta raised", "")
    rows = [json.loads(line) for line in covered.ledger_path("data-infra").read_text().splitlines()]
    assert [row["slug"] for row in rows] == ["acme-1-0", "beta-raise"]


def test_two_series_do_not_share_a_ledger(covered) -> None:
    covered.record("data-infra", "2026-09-21", "acme-1-0", "Acme 1.0", "")
    assert covered.recent_rows("robotics", 5) == []


@pytest.mark.parametrize(
    ("edition", "slug"),
    [("21-09-2026", "ok-slug"), ("2026-09-21", "Not A Slug"), ("2026-09-21", "trailing-")],
)
def test_malformed_identity_is_refused(covered, edition: str, slug: str) -> None:
    with pytest.raises(ValueError):
        covered.record("data-infra", edition, slug, "t", "")


def test_a_series_name_cannot_escape_its_directory(covered) -> None:
    with pytest.raises(ValueError):
        covered.ledger_path("../escape")
