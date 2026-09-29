"""The projection is the file the scripts wrote, byte for byte.

`record.py` restates the shapes the skill scripts own — the slug pattern, the three date patterns,
and coverage's three states with their four reasons — because a skill script is materialised
standalone in a sandbox and cannot import the extension package. Restating is not the risk;
restating *and then drifting* is, because the drift would be silent: a store that accepted a slug
the script rejects writes a line the script cannot read back, and the member's file and the agent's
record would disagree about what the series holds.

So these read the scripts' own source and behaviour rather than trusting the duplication. They are
the reason `record.py` may say "the row shapes are the file's, unchanged" in its docstring.

Skipped without sqlalchemy, which is `record.py`'s only import beyond the standard library.
"""

import json

import pytest

pytest.importorskip("sqlalchemy", reason="install ufo from git to run the projection tests")

from ufo_ext_pulse import record

ROW = {
    "seen": "2026-09-21",
    "slug": "acme-1-0",
    "title": "Acme 1.0",
    "url": "https://x/1",
    "source": "releases",
}
STORY = {"edition": "2026-09-28", "slug": "acme-1-0", "title": "Acme 1.0", "url": "https://x/1"}
STATE = {
    "gathered": "2026-09-24",
    "source": "filings-index",
    "state": "not-read",
    "items": 0,
    "reason": "rate-limited",
}


def test_the_pool_patterns_are_the_scripts_own(seen) -> None:
    assert record.SLUG.pattern == seen.SLUG.pattern
    assert record.DATE.pattern == seen.SEEN_DATE.pattern
    assert record.POOL_DIR == seen.POOL_DIR
    assert record.DEFAULT_WITHIN_DAYS == seen.DEFAULT_WITHIN_DAYS


def test_the_ledger_patterns_are_the_scripts_own(covered) -> None:
    assert record.DATE.pattern == covered.EDITION.pattern
    assert record.POOL_DIR == covered.LEDGER_DIR
    assert record.DEFAULT_EDITIONS == covered.DEFAULT_EDITIONS


def test_the_coverage_patterns_and_vocabulary_are_the_scripts_own(coverage) -> None:
    """The states and the reasons are a vocabulary rather than a format, and a store that accepted
    a fourth state would write a line the window aggregates into no column at all."""
    assert record.DATE.pattern == coverage.GATHERED.pattern
    assert record.POOL_DIR == coverage.COVERAGE_DIR
    assert record.COVERAGE_STATES == coverage.STATES
    assert record.COVERAGE_REASONS == coverage.REASONS


@pytest.mark.parametrize(
    "row",
    [
        ("2026-09-24", "releases", "read", 11, ""),
        ("2026-09-24", "forums", "read-empty", 0, ""),
        ("2026-09-24", "filings-index", "not-read", 0, "rate-limited"),
        ("Thursday", "releases", "read", 11, ""),
        ("2026-09-24", "The Filings Index", "read", 11, ""),
        ("2026-09-24", "releases", "partial", 11, ""),
        ("2026-09-24", "releases", "read", 0, ""),
        ("2026-09-24", "forums", "read-empty", 3, ""),
        ("2026-09-24", "releases", "read", 11, "rate-limited"),
        ("2026-09-24", "filings-index", "not-read", 0, ""),
        ("2026-09-24", "filings-index", "not-read", 0, "slow"),
        ("2026-09-24", "filings-index", "not-read", 3, "unreachable"),
    ],
)
def test_the_two_validators_agree_on_every_row(coverage, row: tuple) -> None:
    """The duplication checked rather than trusted, in the direction that matters: a row the script
    writes and the store refuses cannot be imported, and a row the store takes and the script
    rejects is a line the member's own reader stops at."""
    gathered, source, state, items, reason = row

    def refused(call) -> bool:
        try:
            call()
        except ValueError:
            return True
        return False

    by_script = refused(
        lambda: coverage.record("data-infra", gathered, source, state, items, reason)
    )
    by_store = refused(
        lambda: record.check_coverage(gathered, record.Coverage(source, state, items, reason))
    )
    assert by_script == by_store


def test_a_projected_sighting_line_is_the_line_the_script_appends(seen) -> None:
    """Not a comparison of formats — the script writes a real file here, and the projection of the
    same row has to be that file's bytes."""
    path = seen.record(
        "data-infra", ROW["seen"], ROW["slug"], ROW["title"], ROW["url"], ROW["source"]
    )
    assert record.seen_lines([ROW]) == path.read_text()


def test_a_projected_ledger_line_is_the_line_the_script_appends(covered) -> None:
    path = covered.record(
        "data-infra", STORY["edition"], STORY["slug"], STORY["title"], STORY["url"]
    )
    assert record.covered_lines([STORY]) == path.read_text()


def test_a_projected_coverage_line_is_the_line_the_script_appends(coverage) -> None:
    path = coverage.record(
        "data-infra",
        STATE["gathered"],
        STATE["source"],
        STATE["state"],
        STATE["items"],
        STATE["reason"],
    )
    assert record.coverage_lines([STATE]) == path.read_text()


def test_the_script_reads_a_whole_coverage_projection_back_unchanged(coverage) -> None:
    """The footer's own reader over the store's writer: `window` has to aggregate what the
    projection renders, or a multi-gather footer counts days the record does not hold."""
    rows = [
        dict(STATE, gathered="2026-09-24"),
        dict(STATE, gathered="2026-09-25", source="releases", state="read", items=4, reason=""),
        dict(STATE, gathered="2026-09-25"),
    ]
    path = coverage.coverage_path("data-infra")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record.coverage_lines(rows))
    assert coverage.read_rows("data-infra") == rows
    assert coverage.gathers("data-infra") == ["2026-09-24", "2026-09-25"]
    filings = next(a for a in coverage.window("data-infra") if a["source"] == "filings-index")
    assert filings["states"]["not-read"] == 2


def test_the_script_reads_a_whole_projection_back_unchanged(seen, tmp_path) -> None:
    """The member's reader over the agent's writer: whatever the store renders, `seen.py` parses to
    the rows the store holds, so `fresh`, `stale` and `history` answer the same on either side."""
    rows = [
        dict(ROW, seen="2026-09-21"),
        dict(ROW, seen="2026-09-23", title="Acme 1.0 GA"),
        dict(ROW, seen="2026-09-23", slug="beta-2", url="https://x/2"),
    ]
    path = seen.pool_path("data-infra")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record.seen_lines(rows))
    assert seen.read_rows("data-infra") == rows


def test_the_script_reads_a_whole_ledger_projection_back_unchanged(covered) -> None:
    rows = [dict(STORY, edition="2026-09-21"), dict(STORY, edition="2026-09-28")]
    path = covered.ledger_path("data-infra")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record.covered_lines(rows))
    assert covered.read_rows("data-infra") == rows


def test_the_projection_paths_are_the_paths_the_scripts_resolve(seen, covered, coverage) -> None:
    assert record.seen_projection("data-infra") == str(seen.pool_path("data-infra"))
    assert record.covered_projection("data-infra") == str(covered.ledger_path("data-infra"))
    assert record.coverage_projection("data-infra") == str(coverage.coverage_path("data-infra"))


def test_a_projected_line_carries_no_field_the_script_would_drop(seen) -> None:
    """A field the store added and the file did not would survive a projection and vanish on the
    next one the script wrote, which is the quiet way two stores of one fact come apart."""
    line = json.loads(record.seen_lines([ROW]))
    seen.record("data-infra", ROW["seen"], ROW["slug"], ROW["title"], ROW["url"], ROW["source"])
    assert line.keys() == seen.read_rows("data-infra")[0].keys()


def test_no_script_offers_a_way_to_write_from_a_shell(seen, covered, coverage) -> None:
    """The measured reason this matters, not a style preference.

    A live fire on 2026-09-28 was told by `brief-continuity` to call `pulse_record_edition`, had the
    tool available, and appended its two published stories to the workspace file instead. The file
    is rendered whole from the record, so both rows were pending erasure: the edition would have
    read as never published and the next one would have carried it again.

    Prose alone did not move the model off the script, so the script no longer offers the move. All
    three files keep a `record` function as the definition of the row shape the projection must
    match, and none exposes it as a subcommand — a shell reaches only what `main` parses.
    """
    for module in (seen, covered, coverage):
        with pytest.raises(SystemExit) as refused:
            module.main(["record", "--series", "data-infra", "--slug", "acme-1-0"])
        assert refused.value.code != 0


def test_the_read_subcommands_all_still_work(seen, covered, coverage) -> None:
    """Removing the write must not have removed the reads with it."""
    assert seen.main(["history", "--series", "data-infra", "--slug", "acme-1-0"]) == 0
    assert seen.main(["fresh", "--series", "data-infra"]) == 0
    assert seen.main(["reconcile", "--series", "data-infra"]) == 0
    assert covered.main(["recent", "--series", "data-infra"]) == 0
    assert covered.main(["check", "--series", "data-infra", "--slug", "acme-1-0"]) == 0
    assert coverage.main(["window", "--series", "data-infra"]) == 0
