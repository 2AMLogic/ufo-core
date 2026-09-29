"""The record in the tables pulse owns, and the workspace files projected from it.

The property every one of these exists for is that **the record is one record**: the same rows from
every turn in the workspace, whatever carrier ran it and whatever directory it started in. That is
what a workspace-relative file could not be, and it is why no assertion here about a row depends on
a file having landed — the file is a copy, and a carrier that cannot take one is ordinary.

(This module used to be organised around #120's claim that a carrier-less fire recorded nothing.
That claim was withdrawn: a fire records, it just recorded somewhere else. The tests were right
about what to assert and wrong about why, which is worth saying once rather than leaving a retracted
premise standing as a module's authority.)

Skipped without `ufo`, whose distribution carries sqlalchemy; the file-shaped contract tests beside
this one still run on a checkout that has neither.
"""

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

pytest.importorskip("sqlalchemy", reason="install ufo from git to run the store tests")

from pulse_fakes import Files, Store, job_store, on_store, tool_context
from ufo_ext_pulse import jobs, record, tools
from ufo_ext_pulse.record import Coverage, Sighting, Story

NOW = datetime(2026, 9, 28, 13, 0, tzinfo=UTC)
SERIES = "data-infra"


def sighting(seen: str, slug: str, title: str = "Acme 1.0", url: str = "https://x/1") -> Sighting:
    return Sighting(seen=seen, slug=slug, title=title, url=url, source="releases")


# --- the record itself -------------------------------------------------------------------------


@on_store
async def test_an_unrecorded_series_reads_empty(store: Store) -> None:
    assert await record.read_sightings(store, SERIES) == []
    assert await record.read_covered(store, SERIES) == []


@on_store
async def test_a_sighting_is_read_back_whole(store: Store) -> None:
    await record.record_sightings(store, SERIES, [sighting("2026-09-21", "acme-1-0")], NOW)
    assert await record.read_sightings(store, SERIES) == [
        {
            "seen": "2026-09-21",
            "slug": "acme-1-0",
            "title": "Acme 1.0",
            "url": "https://x/1",
            "source": "releases",
        }
    ]


@on_store
async def test_the_same_sighting_recorded_twice_is_one_row(store: Store) -> None:
    """The property a file could not have: a replayed fire, a retried batch, or a gather that
    surfaced one lead twice lands one row. An append-only file would have held both and every
    count read off it would have been wrong by however many times the fire was retried."""
    for _ in range(3):
        await record.record_sightings(store, SERIES, [sighting("2026-09-21", "acme-1-0")], NOW)
    assert len(await record.read_sightings(store, SERIES)) == 1


@on_store
async def test_a_second_sighting_of_one_lead_on_a_later_day_is_a_second_row(store: Store) -> None:
    """Two rows, not an overwrite: the difference between them is the evidence a material new
    development needs."""
    await record.record_sightings(
        store, SERIES, [sighting("2026-09-21", "acme-1-0", "Acme 1.0 in beta")], NOW
    )
    await record.record_sightings(
        store, SERIES, [sighting("2026-09-23", "acme-1-0", "Acme 1.0 GA")], NOW
    )
    rows = await record.read_sightings(store, SERIES)
    assert [row["seen"] for row in rows] == ["2026-09-21", "2026-09-23"]
    assert rows[0]["title"] != rows[1]["title"]


@on_store
async def test_one_lead_at_two_addresses_on_one_day_is_two_rows(store: Store) -> None:
    """The url is in the key, so the pool keeps what `reconcile` exists to ask about rather than
    collapsing it into one row and answering the question itself."""
    await record.record_sightings(
        store,
        SERIES,
        [
            sighting("2026-09-21", "acme-1-0", url="https://x/blog"),
            sighting("2026-09-21", "acme-1-0", url="https://x/notes"),
        ],
        NOW,
    )
    assert len({row["url"] for row in await record.read_sightings(store, SERIES)}) == 2


@on_store
async def test_a_retitled_sighting_keeps_one_row_and_the_newer_title(store: Store) -> None:
    await record.record_sightings(store, SERIES, [sighting("2026-09-21", "acme-1-0", "first")], NOW)
    await record.record_sightings(
        store, SERIES, [sighting("2026-09-21", "acme-1-0", "second")], NOW
    )
    rows = await record.read_sightings(store, SERIES)
    assert [row["title"] for row in rows] == ["second"]


@on_store
async def test_a_blank_url_is_refused_and_writes_nothing(store: Store) -> None:
    """Blank is as unreconcilable as missing, and it is also part of the key — every whitespace-only
    sighting of a lead would otherwise collapse into one row."""
    with pytest.raises(ValueError):
        await record.record_sightings(
            store, SERIES, [Sighting("2026-09-21", "acme-1-0", "Acme", "   ", "")], NOW
        )
    assert await record.read_sightings(store, SERIES) == []


@on_store
async def test_a_bad_date_or_slug_is_refused_and_writes_nothing(store: Store) -> None:
    with pytest.raises(ValueError):
        await record.record_sightings(store, SERIES, [sighting("21 Sep 2026", "acme-1-0")], NOW)
    with pytest.raises(ValueError):
        await record.record_sightings(store, SERIES, [sighting("2026-09-21", "Acme 1.0")], NOW)
    assert await record.read_sightings(store, SERIES) == []


@on_store
async def test_a_whole_batch_is_refused_when_one_row_is_bad(store: Store) -> None:
    """A gather never lands half of itself: one bad row and the batch writes nothing.

    Two things hold this, and only the outcome is asserted because this cannot tell them apart.
    `record_sightings` checks every row before opening a transaction, and the transaction would
    roll back a partial write anyway — moving the check inside the write loop leaves this test
    green, which is measured (M2) rather than assumed. Stating it here so the next reader does not
    take the test as evidence that the ordering is what does the work.
    """
    with pytest.raises(ValueError):
        await record.record_sightings(
            store,
            SERIES,
            [sighting("2026-09-21", "acme-1-0"), sighting("2026-09-21", "Bad Slug")],
            NOW,
        )
    assert await record.read_sightings(store, SERIES) == []


@on_store
async def test_a_series_name_cannot_escape(store: Store) -> None:
    with pytest.raises(ValueError):
        await record.read_sightings(store, "../escape")
    with pytest.raises(ValueError):
        record.seen_projection("../escape")


@on_store
async def test_one_series_never_reads_another(store: Store) -> None:
    await record.record_sightings(store, SERIES, [sighting("2026-09-21", "acme-1-0")], NOW)
    await record.record_sightings(store, "open-silicon", [sighting("2026-09-21", "beta-2")], NOW)
    assert [row["slug"] for row in await record.read_sightings(store, SERIES)] == ["acme-1-0"]


@on_store
async def test_one_workspace_never_reads_another(store: Store) -> None:
    """The scoping is in the predicate. Two workspaces sharing one database and one series name see
    only their own rows — the case a store keyed by series alone would have got wrong silently."""
    other = Store(engine=store.engine, workspace_id=uuid4())
    await record.record_sightings(store, SERIES, [sighting("2026-09-21", "acme-1-0")], NOW)
    await record.record_sightings(other, SERIES, [sighting("2026-09-21", "beta-2")], NOW)
    assert [row["slug"] for row in await record.read_sightings(store, SERIES)] == ["acme-1-0"]
    assert [row["slug"] for row in await record.read_sightings(other, SERIES)] == ["beta-2"]


@on_store
async def test_reading_the_pool_never_changes_it(store: Store) -> None:
    """Collection is additive. The moment a read writes, staleness has become the expiry the
    design rules out."""
    await record.record_sightings(store, SERIES, [sighting("2026-08-01", "acme-1-0")], NOW)
    rows = await record.read_sightings(store, SERIES)
    record.fresh(rows, 14, date(2026, 9, 28))
    record.last_seen(rows)
    assert await record.read_sightings(store, SERIES) == rows


# --- the bug this replaced ------------------------------------------------------------------------


@on_store
async def test_the_record_does_not_depend_on_where_the_turn_started(
    store: Store, tmp_path, monkeypatch
) -> None:
    """The regression test for the fault that motivated all of this.

    A brief's carriers do not share a working directory: a conversation bound to a terminal runs in
    that terminal's own directory, one that is not runs under `workspace_root/<conversation_id>`.
    The record has to be the same record from both. Reading it from two different directories is how
    that is asserted, because that is exactly the difference the old store could not survive.
    """
    here, there = tmp_path / "deploy-home", tmp_path / "sandbox-root"
    here.mkdir()
    there.mkdir()

    monkeypatch.chdir(here)
    await record.record_sightings(store, SERIES, [sighting("2026-09-21", "from-cli")], NOW)
    monkeypatch.chdir(there)
    await record.record_sightings(store, SERIES, [sighting("2026-09-22", "from-fire")], NOW)

    for where in (here, there):
        monkeypatch.chdir(where)
        assert {row["slug"] for row in await record.read_sightings(store, SERIES)} == {
            "from-cli",
            "from-fire",
        }


def test_the_file_store_alone_would_have_split_under_the_same_move(seen, tmp_path, monkeypatch):
    """The other half of the comparison, against the real script.

    Two writes to one series from two directories leave two files that each look complete. This is
    not a hypothetical: the demo deploy carried two `agent-runtimes.covered.jsonl` files whose
    2026-09-28 editions had six rows each and no story in common.
    """
    here, there = tmp_path / "deploy-home", tmp_path / "sandbox-root"
    here.mkdir()
    there.mkdir()

    monkeypatch.chdir(here)
    seen.record(SERIES, "2026-09-21", "from-cli", "From the CLI", "https://x/1", "")
    assert [row["slug"] for row in seen.read_rows(SERIES)] == ["from-cli"]

    monkeypatch.chdir(there)
    seen.record(SERIES, "2026-09-22", "from-fire", "From the fire", "https://x/2", "")
    # The whole fault in one line: the second carrier sees a series with no history, and the
    # no-repeat rule it enforces is a rule over half the record.
    assert [row["slug"] for row in seen.read_rows(SERIES)] == ["from-fire"]
    assert (here / "pulse" / f"{SERIES}.seen.jsonl").is_file()
    assert (there / "pulse" / f"{SERIES}.seen.jsonl").is_file()


# --- what is live --------------------------------------------------------------------------------


def test_freshness_keys_on_the_most_recent_sighting() -> None:
    """A lead first seen thirty days ago and seen again this morning is live, not old."""
    rows = [
        {"seen": "2026-08-20", "slug": "acme-1-0"},
        {"seen": "2026-09-28", "slug": "acme-1-0"},
        {"seen": "2026-08-20", "slug": "quiet-one"},
    ]
    assert record.fresh(rows, 14, date(2026, 9, 28)) == [("acme-1-0", "2026-09-28")]


def test_a_stale_lead_is_kept_and_simply_not_offered() -> None:
    """Age is a reason not to pursue a lead, never a reason to forget it."""
    rows = [{"seen": "2026-01-01", "slug": "quiet-one"}]
    assert record.fresh(rows, 14, date(2026, 9, 28)) == []
    assert record.last_seen(rows) == {"quiet-one": "2026-01-01"}


def test_recent_rows_counts_editions_not_days() -> None:
    """Five editions is five briefs the reader has seen, whether they fell over one week or three."""
    rows = [
        {"edition": "2026-01-01", "slug": "old"},
        {"edition": "2026-09-01", "slug": "mid"},
        {"edition": "2026-09-28", "slug": "new"},
    ]
    assert [row["slug"] for row in record.recent_rows(rows, 2)] == ["mid", "new"]
    assert [row["slug"] for row in record.recent_rows(rows, 5)] == ["old", "mid", "new"]


# --- the covered ledger --------------------------------------------------------------------------


@on_store
async def test_one_edition_carries_a_story_once(store: Store) -> None:
    for _ in range(3):
        await record.record_covered(
            store, SERIES, "2026-09-28", [Story("acme-1-0", "Acme 1.0", "https://x/1")], NOW
        )
    assert len(await record.read_covered(store, SERIES)) == 1


@on_store
async def test_a_story_carried_again_is_a_row_in_each_edition(store: Store) -> None:
    """The material-new-development exception, as the ledger has always recorded it."""
    await record.record_covered(store, SERIES, "2026-09-21", [Story("acme-1-0", "beta")], NOW)
    await record.record_covered(store, SERIES, "2026-09-28", [Story("acme-1-0", "GA")], NOW)
    rows = await record.read_covered(store, SERIES)
    assert [row["edition"] for row in rows] == ["2026-09-21", "2026-09-28"]


@on_store
async def test_a_bad_edition_date_is_refused_and_writes_nothing(store: Store) -> None:
    with pytest.raises(ValueError):
        await record.record_covered(store, SERIES, "28 Sep", [Story("acme-1-0", "Acme")], NOW)
    assert await record.read_covered(store, SERIES) == []


# --- the coverage state ---------------------------------------------------------------------------


@on_store
async def test_an_ungathered_series_has_no_coverage(store: Store) -> None:
    assert await record.read_coverage(store, SERIES) == []


@on_store
async def test_a_source_state_is_read_back_whole(store: Store) -> None:
    await record.record_coverage(
        store, SERIES, "2026-09-24", [Coverage("releases", record.READ, 11)], NOW
    )
    assert await record.read_coverage(store, SERIES) == [
        {
            "gathered": "2026-09-24",
            "source": "releases",
            "state": "read",
            "items": 11,
            "reason": "",
        }
    ]


@on_store
async def test_the_same_state_recorded_twice_is_one_row(store: Store) -> None:
    """A replayed fire lands one row. The file this projects to appends, so a gather recorded twice
    would read as a gather with two answers from one source."""
    for _ in range(3):
        await record.record_coverage(
            store, SERIES, "2026-09-24", [Coverage("releases", record.READ, 11)], NOW
        )
    assert len(await record.read_coverage(store, SERIES)) == 1


@on_store
async def test_a_retry_inside_one_gather_is_that_gathers_answer(store: Store) -> None:
    """A source that rate-limited the first attempt and answered the second was read that day. The
    file holds both rows and resolves them on read; this resolves them on write, and both surfaces
    take the later one."""
    await record.record_coverage(
        store,
        SERIES,
        "2026-09-24",
        [Coverage("filings-index", record.NOT_READ, reason="rate-limited")],
        NOW,
    )
    await record.record_coverage(
        store, SERIES, "2026-09-24", [Coverage("filings-index", record.READ, 6)], NOW
    )
    assert await record.read_coverage(store, SERIES) == [
        {
            "gathered": "2026-09-24",
            "source": "filings-index",
            "state": "read",
            "items": 6,
            "reason": "",
        }
    ]


@on_store
async def test_one_source_over_three_gathers_is_three_rows(store: Store) -> None:
    """The distinction the store exists for: unread on one day and unread on all three are
    different claims about the field, and only a row per gather can tell them apart."""
    for day in ("2026-09-24", "2026-09-25", "2026-09-26"):
        await record.record_coverage(
            store,
            SERIES,
            day,
            [Coverage("filings-index", record.NOT_READ, reason="unreachable")],
            NOW,
        )
    rows = await record.read_coverage(store, SERIES)
    assert [row["gathered"] for row in rows] == ["2026-09-24", "2026-09-25", "2026-09-26"]


@on_store
async def test_a_read_source_with_nothing_in_it_is_refused_as_read(store: Store) -> None:
    """Zero items from a source that answered is `read-empty`, which is an answer. Filing it as the
    other kind of zero is the one confusion `coverage-honesty` exists to prevent."""
    with pytest.raises(ValueError, match="read-empty"):
        await record.record_coverage(
            store, SERIES, "2026-09-24", [Coverage("forums", record.READ, 0)], NOW
        )
    assert await record.read_coverage(store, SERIES) == []


@on_store
async def test_a_not_read_source_names_one_of_the_four(store: Store) -> None:
    with pytest.raises(ValueError, match="unreachable, rate-limited"):
        await record.record_coverage(
            store, SERIES, "2026-09-24", [Coverage("filings-index", record.NOT_READ)], NOW
        )
    with pytest.raises(ValueError, match="unreachable, rate-limited"):
        await record.record_coverage(
            store,
            SERIES,
            "2026-09-24",
            [Coverage("filings-index", record.NOT_READ, reason="slow")],
            NOW,
        )
    assert await record.read_coverage(store, SERIES) == []


@on_store
async def test_a_whole_gather_is_refused_when_one_state_is_bad(store: Store) -> None:
    """A gather never lands half of its states: a window missing one source reads as a source
    nobody thought about, which is the claim the footer may not make."""
    with pytest.raises(ValueError):
        await record.record_coverage(
            store,
            SERIES,
            "2026-09-24",
            [Coverage("releases", record.READ, 4), Coverage("The Filings Index", record.READ, 1)],
            NOW,
        )
    assert await record.read_coverage(store, SERIES) == []


@on_store
async def test_a_bad_gather_date_is_refused_and_writes_nothing(store: Store) -> None:
    with pytest.raises(ValueError, match="gathered must be"):
        await record.record_coverage(
            store, SERIES, "Thursday", [Coverage("releases", record.READ, 4)], NOW
        )
    assert await record.read_coverage(store, SERIES) == []


@on_store
async def test_one_workspace_never_reads_anothers_coverage(store: Store) -> None:
    other = Store(engine=store.engine, workspace_id=uuid4())
    await record.record_coverage(
        store, SERIES, "2026-09-24", [Coverage("releases", record.READ, 4)], NOW
    )
    await record.record_coverage(
        other, SERIES, "2026-09-24", [Coverage("papers", record.READ, 1)], NOW
    )
    assert [row["source"] for row in await record.read_coverage(store, SERIES)] == ["releases"]
    assert [row["source"] for row in await record.read_coverage(other, SERIES)] == ["papers"]


@on_store
async def test_the_coverage_state_does_not_depend_on_where_the_turn_started(
    store: Store, tmp_path, monkeypatch
) -> None:
    """The regression test for this store's own half of the fault.

    A gather's states and the edition that counts them are written by different conversations, and
    those do not share a working directory. A footer built from the tree the reporting turn happened
    to start in is a claim about part of the window, which is the claim `coverage-honesty` refuses.
    """
    here, there = tmp_path / "deploy-home", tmp_path / "sandbox-root"
    here.mkdir()
    there.mkdir()

    monkeypatch.chdir(there)
    await record.record_coverage(
        store, SERIES, "2026-09-24", [Coverage("releases", record.READ, 4)], NOW
    )
    monkeypatch.chdir(here)
    assert [row["source"] for row in await record.read_coverage(store, SERIES)] == ["releases"]


@on_store
async def test_a_tool_records_a_whole_gathers_states(store: Store) -> None:
    result = await tools.record_coverage(
        tool_context(store),
        tools.RecordCoverageInput(
            series=SERIES,
            gathered="2026-09-24",
            sources=[
                tools.CoverageInput(source="releases", state="read", items=11),
                tools.CoverageInput(source="forums", state="read-empty"),
                tools.CoverageInput(
                    source="filings-index", state="not-read", reason="rate-limited"
                ),
            ],
        ),
    )
    assert not result.is_error
    assert len(await record.read_coverage(store, SERIES)) == 3


@on_store
async def test_a_gather_with_no_sources_is_refused_rather_than_reported_as_a_write(
    store: Store,
) -> None:
    """A gather that reached none of them records not-read rows naming why. Recording nothing is
    the silence an edition cannot tell from a day nobody gathered."""
    result = await tools.record_coverage(
        tool_context(store),
        tools.RecordCoverageInput(series=SERIES, gathered="2026-09-24", sources=[]),
    )
    assert result.is_error
    assert await record.read_coverage(store, SERIES) == []


@on_store
async def test_a_coverage_write_leaves_its_series_due_for_projection(store: Store) -> None:
    conversation = uuid4()
    await tools.record_coverage(
        tool_context(store, conversation),
        tools.RecordCoverageInput(
            series=SERIES,
            gathered="2026-09-24",
            sources=[tools.CoverageInput(source="releases", state="read", items=11)],
        ),
    )
    due = await record.due_series(store)
    assert [(series, where) for series, where, _ in due] == [(SERIES, conversation)]


# --- the projection ------------------------------------------------------------------------------


@on_store
async def test_a_tool_writes_no_file_and_the_row_lands_anyway(store: Store) -> None:
    """`ExtensionContext.files` is None in every tool handler — `turn_tools` never wires the
    sandbox seam — so a tool that tried to project would be dead code. The record is what a write
    is for, and it does not depend on a file having landed."""
    assert store.files is None
    result = await tools.record_sightings(
        tool_context(store),
        tools.RecordSightingsInput(
            series=SERIES,
            sightings=[
                tools.SightingInput(
                    seen="2026-09-21", slug="acme-1-0", title="Acme 1.0", url="https://x/1"
                )
            ],
        ),
    )
    assert not result.is_error
    assert len(await record.read_sightings(store, SERIES)) == 1


@on_store
async def test_a_write_leaves_its_series_due_for_projection(store: Store) -> None:
    """The stamp is what couples a committed write to the file that follows it."""
    conversation = uuid4()
    await tools.record_sightings(
        tool_context(store, conversation),
        tools.RecordSightingsInput(
            series=SERIES,
            sightings=[
                tools.SightingInput(
                    seen="2026-09-21", slug="acme-1-0", title="Acme 1.0", url="https://x/1"
                )
            ],
        ),
    )
    due = await record.due_series(store)
    assert [(series, where) for series, where, _ in due] == [(SERIES, conversation)]


@on_store
async def test_a_shared_sandbox_is_where_the_projection_is_bound(store: Store) -> None:
    """Core opens a sandbox against `sandbox_conversation_id or conversation_id`, so a turn sharing
    another conversation's sandbox has to record that one — binding its own would land the file in
    a tree the agent never reads."""
    own, shared = uuid4(), uuid4()
    await tools.record_sightings(
        tool_context(store, own, sandbox_conversation_id=shared),
        tools.RecordSightingsInput(
            series=SERIES,
            sightings=[
                tools.SightingInput(
                    seen="2026-09-21", slug="acme-1-0", title="Acme 1.0", url="https://x/1"
                )
            ],
        ),
    )
    assert [where for _, where, _ in await record.due_series(store)] == [shared]


@on_store
async def test_a_series_name_with_a_trailing_newline_is_refused(store: Store) -> None:
    """`.match` with a `$` anchor accepts one, and the name becomes a filename with a newline in
    it. Every validator here full-matches instead."""
    with pytest.raises(ValueError):
        await record.read_sightings(store, "open-silicon\n")


def test_a_date_written_in_non_ascii_digits_is_refused() -> None:
    """`\\d` matches every Unicode decimal digit, so an Arabic-Indic date validated, stored, and
    then sorted as text beside ASCII ones — which is how a lead reads as the newest thing in the
    pool forever."""
    assert record.DATE.fullmatch("٢٠٢٦-٠٩-٢١") is None
    assert record.DATE.fullmatch("2026-09-21") is not None


@on_store
async def test_the_job_writes_every_file_and_clears_the_series(store: Store) -> None:
    conversation = uuid4()
    await record.record_sightings(
        store, SERIES, [sighting("2026-09-21", "acme-1-0")], NOW, conversation
    )
    await record.record_coverage(
        store, SERIES, "2026-09-21", [Coverage("releases", record.READ, 4)], NOW, conversation
    )
    await jobs.project(job_store(store))
    assert set(store.files.written) == {
        "pulse/data-infra.seen.jsonl",
        "pulse/data-infra.covered.jsonl",
        "pulse/data-infra.coverage.jsonl",
    }
    assert '"slug": "acme-1-0"' in store.files.text("pulse/data-infra.seen.jsonl")
    assert '"source": "releases"' in store.files.text("pulse/data-infra.coverage.jsonl")
    assert await record.due_series(store) == []


@on_store
async def test_the_job_skips_a_series_nothing_has_touched(store: Store) -> None:
    """A projection opens a sandbox rather than reusing a live one, so a job that rewrote every
    series each tick would start a container per conversation per tick for no change."""
    await record.record_sightings(store, SERIES, [sighting("2026-09-21", "acme-1-0")], NOW, uuid4())
    await jobs.project(job_store(store))
    store.files.written.clear()
    await jobs.project(store)
    assert store.files.written == {}


@on_store
async def test_a_write_landing_during_a_projection_stays_due(store: Store) -> None:
    """The mark is the `updated_at` the run *read*, never `now`.

    The interleaved write happens inside `Files.write`, so this exercises `jobs.project` itself
    rather than `mark_projected` in isolation — an earlier version asserted the same property
    against the helper directly and stayed green when the job was mutated to stamp `now()`,
    which is the one mutation that survived the first pass.
    """
    conversation = uuid4()
    early = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    late = datetime(2026, 9, 28, 14, 0, tzinfo=UTC)
    await record.record_sightings(
        store, SERIES, [sighting("2026-09-21", "acme-1-0")], early, conversation
    )

    async def landing_mid_render(conversation_id, rel, content):
        await record.record_sightings(
            store, SERIES, [sighting("2026-09-22", "beta-2", url="https://x/2")], late, conversation
        )

    store.files = Files(on_write=landing_mid_render)
    await jobs.project(store)
    assert [series for series, _, _ in await record.due_series(store)] == [SERIES]


@on_store
async def test_a_write_stamped_before_the_last_projection_is_still_due(store: Store) -> None:
    """The regression test for a series that went permanently unprojectable.

    Dueness was `projected_at < updated_at`, both wall-clock. A write whose `now` preceded an
    already-recorded projection — clock skew, a replayed job, two writers disagreeing about now —
    read as older than the file. Its rows were in the record, the series was not due, and nothing
    brought the projection back. Measured before the fix: two rows recorded, `due_series` empty,
    the file holding neither. A counter cannot go backwards, so this asks for the worst case the
    clock allowed and expects the series still due.
    """
    conversation = uuid4()
    late = datetime(2026, 9, 28, 14, 0, tzinfo=UTC)
    early = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    await record.record_sightings(
        store, SERIES, [sighting("2026-09-21", "acme-1-0")], late, conversation
    )
    await jobs.project(job_store(store))
    await record.record_sightings(
        store, SERIES, [sighting("2026-09-22", "beta-2", url="https://x/2")], early, conversation
    )
    assert [series for series, _, _ in await record.due_series(store)] == [SERIES]
    store.files = Files()
    await jobs.project(store)
    assert store.files.text("pulse/data-infra.seen.jsonl").count("\n") == 2


@on_store
async def test_a_slow_run_cannot_drag_the_mark_back_past_a_newer_projection(store: Store) -> None:
    """Two projections overlapping: the later one finishes first and marks a higher revision, and
    the straggler must not undo it. Without the guard the series reads as due forever, and every
    tick re-renders a file that was already current."""
    conversation = uuid4()
    await record.record_sightings(store, SERIES, [sighting("2026-09-21", "a-1")], NOW, conversation)
    stale = (await record.due_series(store))[0][2]
    await record.record_sightings(
        store, SERIES, [sighting("2026-09-22", "b-2", url="https://x/2")], NOW, conversation
    )
    current = (await record.due_series(store))[0][2]
    assert current > stale

    await record.mark_projected(store, SERIES, current)
    await record.mark_projected(store, SERIES, stale)
    assert await record.due_series(store) == []


@on_store
async def test_a_series_worked_on_elsewhere_moves_its_files_with_it(store: Store) -> None:
    """The conversation is carried on every write rather than set once, so a brief a member moves
    to a new conversation gets its files there rather than in the one they left. Documented in the
    README, so it is asserted here rather than left to the reader to believe."""
    first, second = uuid4(), uuid4()
    await record.record_sightings(store, SERIES, [sighting("2026-09-21", "a-1")], NOW, first)
    await jobs.project(job_store(store))
    await record.record_sightings(
        store, SERIES, [sighting("2026-09-22", "b-2", url="https://x/2")], NOW, second
    )
    assert [where for _, where, _ in await record.due_series(store)] == [second]


@on_store
async def test_a_write_that_does_not_know_its_conversation_leaves_the_binding(store: Store) -> None:
    """The other half of the same rule: an unbound write advances the record without clearing
    where the files go."""
    bound = uuid4()
    await record.record_sightings(store, SERIES, [sighting("2026-09-21", "a-1")], NOW, bound)
    await record.record_sightings(
        store, SERIES, [sighting("2026-09-22", "b-2", url="https://x/2")], NOW, None
    )
    assert [where for _, where, _ in await record.due_series(store)] == [bound]


@on_store
async def test_a_failing_conversation_stays_due_and_does_not_strand_the_next_series(
    store: Store,
) -> None:
    """The rows are already committed when the job runs, so a sandbox that will not open costs a
    stale file and never a lost row — and one series' fault is not the next one's."""
    await record.record_sightings(store, SERIES, [sighting("2026-09-21", "acme-1-0")], NOW, uuid4())
    await record.record_sightings(
        store, "open-silicon", [sighting("2026-09-21", "beta-2")], NOW, uuid4()
    )
    store.files = Files(breaks_for={"pulse/data-infra.seen.jsonl"})
    await jobs.project(store)
    assert [series for series, _, _ in await record.due_series(store)] == [SERIES]
    assert "pulse/open-silicon.seen.jsonl" in store.files.written


@on_store
async def test_a_projection_after_unwatched_fires_catches_the_whole_file_up(store: Store) -> None:
    """Rendered whole rather than appended, so there is no path by which the file and the tables can
    disagree about a row — three fires nobody projected are three lines the next projection carries.
    """
    conversation = uuid4()
    for day in ("2026-09-21", "2026-09-22", "2026-09-23"):
        await record.record_sightings(
            store, SERIES, [sighting(day, f"acme-{day[-2:]}")], NOW, conversation
        )
    await jobs.project(job_store(store))
    assert store.files.text("pulse/data-infra.seen.jsonl").count("\n") == 3


@on_store
async def test_the_job_without_the_file_seam_leaves_the_series_due(store: Store) -> None:
    """A context with no `files` is a deploy wired differently, not an empty series. Marking it
    projected would strand the file forever."""
    await record.record_sightings(store, SERIES, [sighting("2026-09-21", "acme-1-0")], NOW, uuid4())
    await jobs.project(store)
    assert [series for series, _, _ in await record.due_series(store)] == [SERIES]


@on_store
async def test_an_empty_batch_is_refused_rather_than_reported_as_a_write(store: Store) -> None:
    result = await tools.record_sightings(
        tool_context(store), tools.RecordSightingsInput(series=SERIES, sightings=[])
    )
    assert result.is_error


# --- recall ---------------------------------------------------------------------------------------


@on_store
async def test_recall_names_the_live_leads_and_what_the_editions_spent(store: Store) -> None:
    today = datetime.now(UTC).date().isoformat()
    await record.record_sightings(store, SERIES, [sighting(today, "acme-1-0")], NOW)
    await record.record_covered(store, SERIES, today, [Story("beta-2", "Beta 2")], NOW)
    said = (
        (await tools.recall(tool_context(store), tools.RecallInput(series=SERIES))).content[0].text
    )
    assert "acme-1-0" in said
    assert "beta-2" in said


@on_store
async def test_recall_of_one_slug_answers_with_its_whole_history(store: Store) -> None:
    await record.record_sightings(store, SERIES, [sighting("2026-09-21", "acme-1-0", "beta")], NOW)
    await record.record_sightings(store, SERIES, [sighting("2026-09-23", "acme-1-0", "GA")], NOW)
    await record.record_covered(store, SERIES, "2026-09-23", [Story("acme-1-0", "GA")], NOW)
    said = (
        (await tools.recall(tool_context(store), tools.RecallInput(series=SERIES, slug="acme-1-0")))
        .content[0]
        .text
    )
    assert "beta" in said and "GA" in said and "carried 2026-09-23" in said


@on_store
async def test_recall_of_an_unknown_slug_says_so_rather_than_answering_emptily(
    store: Store,
) -> None:
    said = (
        (await tools.recall(tool_context(store), tools.RecallInput(series=SERIES, slug="nobody")))
        .content[0]
        .text
    )
    assert "never seen and never carried" in said


@on_store
async def test_recall_never_changes_the_record(store: Store) -> None:
    await record.record_sightings(store, SERIES, [sighting("2026-09-21", "acme-1-0")], NOW)
    before = await record.read_sightings(store, SERIES)
    await tools.recall(tool_context(store), tools.RecallInput(series=SERIES))
    await tools.recall(tool_context(store), tools.RecallInput(series=SERIES, slug="acme-1-0"))
    assert await record.read_sightings(store, SERIES) == before
