"""The coverage-state store, run against a temporary workspace.

A footer over several gathers is only honest if the days disagree on the record: these assert that
one source's three days aggregate as three days, that a window of one gather still reads as one run,
and that no read path writes — the moment aggregation writes anything it has started editing the
evidence it reports.
"""

import pytest

WEEK = ("2026-09-24", "2026-09-25", "2026-09-26")


def gather_of(coverage, gathered: str, **sources) -> None:
    """One gather's states: a source mapped to its items count, or to a not-read reason."""
    for source, answer in sources.items():
        slug = source.replace("_", "-")
        if isinstance(answer, int):
            state = "read" if answer else "read-empty"
            coverage.record("data-infra", gathered, slug, state, items=answer)
        else:
            coverage.record("data-infra", gathered, slug, "not-read", reason=answer)


def test_an_ungathered_series_reads_empty(coverage) -> None:
    assert coverage.read_rows("data-infra") == []
    assert coverage.window("data-infra") == []
    assert coverage.main(["window", "--series", "data-infra"]) == 0


def test_the_store_lands_in_the_workspace_beside_the_pool(coverage, tmp_path, monkeypatch) -> None:
    """Workspace-relative for the same reason the ledger and the pool are, and a `UFO_HOME`
    elsewhere moves nothing."""
    monkeypatch.setenv("UFO_HOME", str(tmp_path / "scratch" / "ufo"))
    path = coverage.record("data-infra", "2026-09-24", "releases", "read", items=11)
    assert str(path) == "pulse/data-infra.coverage.jsonl"
    assert (tmp_path / "pulse" / "data-infra.coverage.jsonl").is_file()


def test_the_store_is_a_third_file_beside_the_ledger_and_the_pool(
    coverage, seen, covered, tmp_path
) -> None:
    """Three stores, three questions: what was published, what was seen, what could be read."""
    coverage.record("data-infra", "2026-09-24", "releases", "read", items=11)
    assert (tmp_path / "pulse" / "data-infra.coverage.jsonl").is_file()
    assert not (tmp_path / "pulse" / "data-infra.seen.jsonl").exists()
    assert not (tmp_path / "pulse" / "data-infra.covered.jsonl").exists()


def test_a_row_carries_the_state_and_what_it_was(coverage) -> None:
    coverage.record("data-infra", "2026-09-24", "filings-index", "not-read", reason="rate-limited")
    assert coverage.read_rows("data-infra") == [
        {
            "gathered": "2026-09-24",
            "source": "filings-index",
            "state": "not-read",
            "items": 0,
            "reason": "rate-limited",
        }
    ]


def test_a_source_read_with_nothing_in_it_is_read_empty(coverage) -> None:
    """The distinction the whole skill rests on: zero items from a source that answered is an
    answer, and the store refuses to file it as the other kind of zero."""
    with pytest.raises(ValueError, match="read-empty"):
        coverage.record("data-infra", "2026-09-24", "forums", "read", items=0)
    coverage.record("data-infra", "2026-09-24", "forums", "read-empty")
    assert coverage.read_rows("data-infra")[0]["state"] == "read-empty"


def test_a_not_read_source_names_one_of_the_four(coverage) -> None:
    with pytest.raises(ValueError, match="unreachable, rate-limited"):
        coverage.record("data-infra", "2026-09-24", "filings-index", "not-read")
    with pytest.raises(ValueError, match="unreachable, rate-limited"):
        coverage.record("data-infra", "2026-09-24", "filings-index", "not-read", reason="slow")
    for reason in coverage.REASONS:
        coverage.record("data-infra", "2026-09-24", "filings-index", "not-read", reason=reason)
    assert len(coverage.read_rows("data-infra")) == 4


def test_an_answering_source_carries_no_reason(coverage) -> None:
    with pytest.raises(ValueError, match="carries no reason"):
        coverage.record("data-infra", "2026-09-24", "releases", "read", 3, "rate-limited")


def test_a_state_outside_the_three_is_refused(coverage) -> None:
    with pytest.raises(ValueError, match="state must be one of"):
        coverage.record("data-infra", "2026-09-24", "releases", "partial", items=3)


def test_a_gather_date_is_a_date_and_a_source_is_a_slug(coverage) -> None:
    with pytest.raises(ValueError, match="gathered must be"):
        coverage.record("data-infra", "Thursday", "releases", "read", items=3)
    with pytest.raises(ValueError, match="source must be"):
        coverage.record("data-infra", "2026-09-24", "The Filings Index", "read", items=3)


@pytest.mark.parametrize("unread", [0, 1, 2, 3])
def test_one_source_over_three_gathers_counts_its_unread_days(coverage, unread: int) -> None:
    """The window a report covers, at every count of failure inside it: none, some, all."""
    for index, gathered in enumerate(WEEK):
        if index < unread:
            gather_of(coverage, gathered, filings_index="unreachable")
        else:
            gather_of(coverage, gathered, filings_index=2)

    aggregate = coverage.window("data-infra", WEEK[0], WEEK[-1])[0]
    assert aggregate["source"] == "filings-index"
    assert aggregate["gathers"] == 3
    assert aggregate["states"]["not-read"] == unread
    assert aggregate["states"]["read"] == 3 - unread
    assert aggregate["unrecorded"] == 0
    assert aggregate["items"] == 2 * (3 - unread)


@pytest.mark.parametrize(
    ("unread", "line"),
    [
        (0, "read on all 3 gathers (6 items)"),
        (1, "read on 2 of 3 gathers (4 items); not read on 1 of 3 gathers — unreachable"),
        (2, "read on 1 of 3 gathers (2 items); not read on 2 of 3 gathers — unreachable"),
        (3, "not read on all 3 gathers — unreachable"),
    ],
)
def test_the_window_says_which_of_the_gathers_failed(coverage, unread: int, line: str) -> None:
    """Unreachable on two of three and unreachable throughout are different claims about the field,
    and the line the footer is written from has to hold the difference."""
    for index, gathered in enumerate(WEEK):
        if index < unread:
            gather_of(coverage, gathered, filings_index="unreachable")
        else:
            gather_of(coverage, gathered, filings_index=2)
    assert coverage.state_line(coverage.window("data-infra", WEEK[0], WEEK[-1])[0]) == line


@pytest.mark.parametrize(
    ("answers", "line"),
    [
        ((1, "unreachable", "unreachable"), "read on 1 of 3 gathers (1 item)"),
        ((1, 1, "unreachable"), "read on 2 of 3 gathers (2 items)"),
        ((0, "unreachable", "unreachable"), "read on 1 of 3 gathers, nothing in it"),
        ((0, 0, "unreachable"), "read on 2 of 3 gathers, nothing in them"),
    ],
)
def test_the_line_agrees_in_number_with_the_count_in_front_of_it(
    coverage, answers: tuple, line: str
) -> None:
    """One item is an ordinary result and one gather is one gather: the singular and the plural are
    written from the same counts, so the table holds both and they cannot part company."""
    for gathered, answer in zip(WEEK, answers, strict=False):
        gather_of(coverage, gathered, filings_index=answer)
    unread = sum(isinstance(answer, str) for answer in answers)
    aggregate = coverage.window("data-infra", WEEK[0], WEEK[-1])[0]
    assert coverage.state_line(aggregate) == (
        f"{line}; not read on {unread} of 3 gathers — unreachable"
    )


def test_a_window_of_one_gather_reads_as_one_run(coverage) -> None:
    """The single-run wording stays the default where the window is a single gather: a report of one
    day has no count of days to state, and a source that returned one item says one item."""
    gather_of(coverage, WEEK[0], releases=11, papers=1, forums=0, filings_index="rate-limited")
    lines = {
        aggregate["source"]: coverage.state_line(aggregate)
        for aggregate in coverage.window("data-infra", WEEK[0], WEEK[0])
    }
    assert lines == {
        "releases": "read (11 items)",
        "papers": "read (1 item)",
        "forums": "read, nothing in it",
        "filings-index": "not read — rate-limited",
    }


def test_a_source_empty_every_gather_is_read_every_gather(coverage) -> None:
    for gathered in WEEK:
        gather_of(coverage, gathered, forums=0)
    aggregate = coverage.window("data-infra", WEEK[0], WEEK[-1])[0]
    assert aggregate["states"]["read-empty"] == 3
    assert coverage.state_line(aggregate) == "read on all 3 gathers, nothing in them"


def test_two_failures_of_one_source_name_both(coverage) -> None:
    """A source that rate-limited twice and then expired is not a source with one problem."""
    gather_of(coverage, WEEK[0], analytics="rate-limited")
    gather_of(coverage, WEEK[1], analytics="rate-limited")
    gather_of(coverage, WEEK[2], analytics="unauthorized")
    aggregate = coverage.window("data-infra", WEEK[0], WEEK[-1])[0]
    assert aggregate["reasons"] == {"rate-limited": 2, "unauthorized": 1}
    assert (
        coverage.state_line(aggregate)
        == "not read on all 3 gathers — rate-limited (2), unauthorized (1)"
    )


def test_a_source_missing_from_a_gather_is_unrecorded_rather_than_read(coverage) -> None:
    """A source nobody set a state for on Wednesday is not a source read on Wednesday — the footer
    says so rather than counting the silence as an answer."""
    gather_of(coverage, WEEK[0], releases=4, papers=2)
    gather_of(coverage, WEEK[1], releases=3, papers=1)
    gather_of(coverage, WEEK[2], releases=5)

    aggregates = coverage.window("data-infra", WEEK[0], WEEK[-1])
    papers = next(a for a in aggregates if a["source"] == "papers")
    assert papers["unrecorded"] == 1
    assert coverage.state_line(papers) == (
        "read on 2 of 3 gathers (3 items); no state recorded on 1 of 3 gathers"
    )


def test_a_retry_inside_one_gather_is_that_gathers_answer(coverage) -> None:
    """Append-only, and the read resolves the pair: a source that rate-limited the first attempt and
    answered the second was read that day, and both rows stay in the file."""
    gather_of(coverage, WEEK[0], filings_index="rate-limited")
    gather_of(coverage, WEEK[0], filings_index=6)
    assert len(coverage.read_rows("data-infra")) == 2

    aggregate = coverage.window("data-infra", WEEK[0], WEEK[0])[0]
    assert aggregate["gathers"] == 1
    assert aggregate["states"] == {"read": 1, "read-empty": 0, "not-read": 0}
    assert coverage.state_line(aggregate) == "read (6 items)"


def test_the_window_bounds_which_gathers_count(coverage) -> None:
    """A report covers the gathers between its dates, so an earlier failure is the earlier report's
    to state and does not follow this one."""
    gather_of(coverage, "2026-09-20", filings_index="unreachable")
    for gathered in WEEK:
        gather_of(coverage, gathered, filings_index=2)

    assert coverage.gathers("data-infra", WEEK[0], WEEK[-1]) == list(WEEK)
    assert coverage.gathers("data-infra") == ["2026-09-20", *WEEK]
    assert coverage.window("data-infra", WEEK[0], WEEK[-1])[0]["states"]["not-read"] == 0
    assert coverage.window("data-infra")[0]["states"]["not-read"] == 1


def test_a_gather_that_recorded_nothing_is_not_a_gather(coverage) -> None:
    """The denominator counts the gathers that reported, not the days the window spans: two of
    three is a claim about gathers, and a silent day was never one."""
    gather_of(coverage, WEEK[0], releases=4)
    gather_of(coverage, WEEK[2], releases=4)
    assert coverage.window("data-infra", WEEK[0], WEEK[-1])[0]["gathers"] == 2


def test_the_window_prints_a_line_per_source(coverage, capsys) -> None:
    for gathered in WEEK:
        gather_of(coverage, gathered, releases=4)
    gather_of(coverage, WEEK[0], filings_index="rate-limited")
    gather_of(coverage, WEEK[1], filings_index="rate-limited")
    gather_of(coverage, WEEK[2], filings_index=1)

    assert coverage.main(["window", "--series", "data-infra", "--until", WEEK[-1]]) == 0
    out = capsys.readouterr().out
    assert f"{WEEK[0]}..{WEEK[-1]}  3 gathers" in out
    assert "releases                  read on all 3 gathers (12 items)" in out
    assert "not read on 2 of 3 gathers — rate-limited" in out


def test_reading_never_writes(coverage, tmp_path) -> None:
    """The constraint that keeps this a record rather than a running total: no read path — window,
    gathers or the line a footer is written from — may write, mark or amend a row. Asserted on the
    file's bytes, so any future write dressed up as a read fails here."""
    for gathered in WEEK:
        gather_of(coverage, gathered, releases=4, filings_index="unreachable")
    path = tmp_path / "pulse" / "data-infra.coverage.jsonl"
    before = path.read_bytes()

    coverage.main(["window", "--series", "data-infra", "--until", WEEK[-1]])
    coverage.main(["window", "--series", "data-infra", "--since", WEEK[0], "--until", WEEK[0]])
    coverage.gathers("data-infra")
    for aggregate in coverage.window("data-infra", WEEK[0], WEEK[-1]):
        coverage.state_line(aggregate)

    assert path.read_bytes() == before
