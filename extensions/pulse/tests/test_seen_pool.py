"""The sightings pool, run against a temporary workspace.

The pool's whole value is that it is additive: these assert that reading it — including asking
whether a lead is stale — never changes it, because the moment staleness writes anything it has
become the expiry the design rules out.
"""

from datetime import date
from pathlib import Path

import pytest


def test_an_unseen_series_reads_empty(seen) -> None:
    assert seen.read_rows("data-infra") == []
    assert seen.main(["history", "--series", "data-infra", "--slug", "acme-1-0"]) == 0


def test_a_series_name_cannot_escape_its_directory(seen) -> None:
    with pytest.raises(ValueError):
        seen.pool_path("../escape")


def test_the_pool_lands_in_the_workspace_beside_the_ledger(seen, tmp_path, monkeypatch) -> None:
    """Workspace-relative for the same reason the covered ledger is, and a `UFO_HOME` elsewhere
    moves nothing."""
    monkeypatch.setenv("UFO_HOME", str(tmp_path / "scratch" / "ufo"))
    path = seen.record(
        "data-infra", "2026-09-21", "acme-1-0", "Acme 1.0", "https://x/1", "releases"
    )
    assert path == Path("pulse/data-infra.seen.jsonl")
    assert (tmp_path / "pulse" / "data-infra.seen.jsonl").is_file()


def test_two_sightings_of_one_lead_are_two_rows(seen) -> None:
    """Not an overwrite: the difference between the rows is what evidences a development."""
    seen.record(
        "data-infra", "2026-09-21", "acme-1-0", "Acme 1.0 in beta", "https://x/r", "releases"
    )
    seen.record("data-infra", "2026-09-23", "acme-1-0", "Acme 1.0 GA", "https://x/r", "releases")
    rows = seen.history("data-infra", "acme-1-0")
    assert [row["seen"] for row in rows] == ["2026-09-21", "2026-09-23"]
    assert rows[0]["title"] != rows[1]["title"]


def test_staleness_keys_on_the_most_recent_sighting(seen) -> None:
    """A lead first seen long ago but seen again this morning is live, not old."""
    seen.record(
        "data-infra", "2026-08-01", "acme-1-0", "Acme 1.0 rumoured", "https://x/f", "forums"
    )
    seen.record("data-infra", "2026-09-27", "acme-1-0", "Acme 1.0 GA", "https://x/r", "releases")
    assert (
        seen.main(
            ["stale", "--series", "data-infra", "--slug", "acme-1-0", "--today", "2026-09-28"]
        )
        == 0
    )


def test_a_lead_that_went_quiet_is_stale(seen) -> None:
    seen.record(
        "data-infra", "2026-08-01", "old-news", "Something from August", "https://x/f", "forums"
    )
    assert (
        seen.main(
            ["stale", "--series", "data-infra", "--slug", "old-news", "--today", "2026-09-28"]
        )
        == 1
    )


def test_a_never_seen_lead_is_not_live(seen) -> None:
    assert (
        seen.main(["stale", "--series", "data-infra", "--slug", "nothing", "--today", "2026-09-28"])
        == 1
    )


def test_a_stale_lead_is_still_in_the_pool_with_its_history(seen) -> None:
    """The point of additive collection: going quiet loses a lead no data at all, so when it moves
    again its whole history is still attached."""
    seen.record(
        "data-infra", "2026-08-01", "old-news", "Something from August", "https://x/f", "forums"
    )
    seen.main(["stale", "--series", "data-infra", "--slug", "old-news", "--today", "2026-09-28"])
    assert len(seen.history("data-infra", "old-news")) == 1

    seen.record(
        "data-infra", "2026-09-28", "old-news", "It moved after all", "https://x/r", "releases"
    )
    assert len(seen.history("data-infra", "old-news")) == 2
    assert (
        seen.main(
            ["stale", "--series", "data-infra", "--slug", "old-news", "--today", "2026-09-28"]
        )
        == 0
    )


def test_reading_never_writes(seen, tmp_path) -> None:
    """The constraint that keeps this a pool rather than an expiry: no read path — stale, fresh or
    history — may write, mark or delete a row. Asserted on the file's bytes, so any future write
    dressed up as a read fails here."""
    seen.record(
        "data-infra", "2026-08-01", "old-news", "Something from August", "https://x/f", "forums"
    )
    seen.record(
        "data-infra", "2026-09-27", "live-news", "Something recent", "https://x/r", "releases"
    )
    path = tmp_path / "pulse" / "data-infra.seen.jsonl"
    before = path.read_bytes()

    seen.main(["fresh", "--series", "data-infra", "--today", "2026-09-28"])
    seen.main(["stale", "--series", "data-infra", "--slug", "old-news", "--today", "2026-09-28"])
    seen.main(["stale", "--series", "data-infra", "--slug", "live-news", "--today", "2026-09-28"])
    seen.main(["history", "--series", "data-infra", "--slug", "old-news"])
    seen.last_seen("data-infra")
    seen.fresh("data-infra", 14, date(2026, 9, 28))

    assert path.read_bytes() == before


def test_fresh_lists_only_leads_inside_the_window(seen) -> None:
    seen.record("data-infra", "2026-08-01", "old-news", "August", "https://x/f", "forums")
    seen.record("data-infra", "2026-09-27", "live-news", "Recent", "https://x/r", "releases")
    live = seen.fresh("data-infra", 14, date(2026, 9, 28))
    assert [slug for slug, _ in live] == ["live-news"]


def test_the_pool_is_a_different_file_from_the_ledger(seen, covered, tmp_path) -> None:
    """Two stores, two questions. A lead seen four times and never published is not a repeat, and
    the covered ledger must not learn about it."""
    seen.record("data-infra", "2026-09-27", "acme-1-0", "Acme 1.0", "https://x/r", "releases")
    assert (tmp_path / "pulse" / "data-infra.seen.jsonl").is_file()
    assert not (tmp_path / "pulse" / "data-infra.covered.jsonl").exists()
    assert covered.main(["check", "--series", "data-infra", "--slug", "acme-1-0"]) == 0


def test_fresh_separates_a_max_length_slug_from_its_title(seen, capsys) -> None:
    """A slug exactly as wide as the column pad left no gap at all, which only shows with a real
    slug — every fixture here was short enough to hide it."""
    wide = "kicad-ai-assistant-plugin-gemini-fix-release"  # 44 chars, the pad width
    seen.record(
        "data-infra", "2026-09-27", wide, "A title that must not abut", "https://x/g", "github"
    )
    seen.main(["fresh", "--series", "data-infra", "--today", "2026-09-28"])
    line = capsys.readouterr().out.strip()
    assert f"{wide}  " in line, line


def test_a_sighting_without_a_url_is_refused(seen) -> None:
    """The url is the best external key a row carries — better than a title, which drifts between
    sightings, and a source, which cannot separate two stories from one publisher. A row without one
    is the row a reconciler cannot repair automatically, which is why record refuses it."""
    with pytest.raises(ValueError, match="url is required"):
        seen.record("data-infra", "2026-09-27", "no-url", "No url", "", "releases")


def test_a_whitespace_only_url_is_refused_too(seen) -> None:
    """A blank url at least announces itself; " " looks like a value and reconciles no better."""
    with pytest.raises(ValueError, match="url is required"):
        seen.record("data-infra", "2026-09-27", "blank-url", "Blank", "   ", "releases")


def test_a_padded_url_is_stored_stripped(seen) -> None:
    """Checking `strip()` and storing the raw string would defeat the reason the url is required:
    two sightings of one story that differ only by surrounding whitespace would not reconcile."""
    seen.record("data-infra", "2026-09-27", "padded", "Padded", "  https://x/1  ", "releases")
    assert seen.history("data-infra", "padded")[0]["url"] == "https://x/1"


def test_one_url_under_two_slugs_cannot_be_told_apart(seen) -> None:
    """The trap `brief-continuity` already names — giving a carried story a new slug — seen from
    the pool's side. The ledger's ineligibility rule keys on the slug, so a story renamed on its
    second outing is republished and nothing says so."""
    seen.record("data-infra", "2026-09-20", "acme-1-0-shipped", "Acme 1.0", "https://acme/1", "rel")
    seen.record(
        "data-infra", "2026-09-25", "big-week-for-acme", "Big week", "https://acme/1", "rel"
    )
    split, merged = seen.unreconciled("data-infra")
    assert split == [("https://acme/1", ["acme-1-0-shipped", "big-week-for-acme"])]
    assert merged == []
    assert seen.main(["reconcile", "--series", "data-infra"]) == 1


def test_one_slug_over_two_urls_is_two_stories_under_one_lead(seen) -> None:
    """The other direction, and the quieter one: two stories filed under one slug hide the second
    behind the first's history, so `history` reads as one lead moving rather than two leads."""
    seen.record("data-infra", "2026-09-20", "acme-ships", "First", "https://acme/1", "rel")
    seen.record("data-infra", "2026-09-25", "acme-ships", "Second", "https://acme/2", "rel")
    split, merged = seen.unreconciled("data-infra")
    assert split == []
    assert merged == [("acme-ships", ["https://acme/1", "https://acme/2"])]
    assert seen.main(["reconcile", "--series", "data-infra"]) == 1


def test_a_pool_that_agrees_with_itself_reports_nothing(seen) -> None:
    """Repeated sightings of one lead at one url are the ordinary case and are not drift — the
    pool is additive, so a lead seen five times is five rows and one identity."""
    for day in ("2026-09-20", "2026-09-21", "2026-09-22"):
        seen.record("data-infra", day, "acme-1-0", "Acme 1.0", "https://acme/1", "rel")
    assert seen.unreconciled("data-infra") == ([], [])
    assert seen.main(["reconcile", "--series", "data-infra"]) == 0


def test_reconcile_never_writes(seen, tmp_path) -> None:
    """A check that repairs what it finds is the deletion this pool rules out. It reports."""
    seen.record("data-infra", "2026-09-20", "acme-1-0-shipped", "Acme", "https://acme/1", "rel")
    seen.record("data-infra", "2026-09-25", "big-week-for-acme", "Acme", "https://acme/1", "rel")
    pool = tmp_path / "pulse" / "data-infra.seen.jsonl"
    before = pool.read_bytes()
    seen.unreconciled("data-infra")
    seen.main(["reconcile", "--series", "data-infra"])
    assert pool.read_bytes() == before


def test_an_index_url_reads_the_same_as_a_renamed_story(seen) -> None:
    """Two genuinely different stories found at one blog index share a url and differ in slug —
    which is indistinguishable from one story renamed between sightings.

    Both are real. In the first real gather, one of three rows was `https://www.kicad.org/blog/` and
    another a repository root, so the coarse-url case is the ordinary one rather than the exotic
    one. A report that called this drift would be confidently wrong about the commoner cause, so it
    names both and chooses neither."""
    index = "https://www.kicad.org/blog/"
    seen.record("data-infra", "2026-09-20", "kicad-8-0-release", "KiCad 8.0", index, "blog")
    seen.record("data-infra", "2026-09-25", "kicad-9-0-roadmap", "Roadmap", index, "blog")
    shared_url, shared_slug = seen.unreconciled("data-infra")
    assert shared_url == [(index, ["kicad-8-0-release", "kicad-9-0-roadmap"])]
    assert shared_slug == []
    assert seen.main(["reconcile", "--series", "data-infra"]) == 1


def test_the_report_names_both_causes_and_picks_neither(seen, capsys) -> None:
    """The exit code says these rows cannot be told apart. It does not say which of the two
    reasons applies, because the pool does not know."""
    index = "https://www.kicad.org/blog/"
    seen.record("data-infra", "2026-09-20", "kicad-8-0-release", "KiCad 8.0", index, "blog")
    seen.record("data-infra", "2026-09-25", "kicad-9-0-roadmap", "Roadmap", index, "blog")
    seen.main(["reconcile", "--series", "data-infra"])
    printed = capsys.readouterr().out
    assert "renamed story" in printed
    assert "naming a page rather than a story" in printed
    assert "cannot be told apart" in printed


def test_a_padded_legacy_url_is_the_url_it_names(seen, tmp_path) -> None:
    """Rows predate the required url, and the guard before it tested `url.strip()` while storing
    `url` raw. So one story can sit in the pool twice, once padded and once clean, and a reader
    that compares raw strings calls that two stories under one lead — a wrong diagnosis, not an
    unproven one, on exactly the rows this audits."""
    pool = tmp_path / "pulse" / "legacy.seen.jsonl"
    pool.parent.mkdir(parents=True, exist_ok=True)
    pool.write_text(
        '{"seen": "2026-09-20", "slug": "acme-1-0", "title": "A", "url": "  https://acme/1  ", "source": ""}\n'
        '{"seen": "2026-09-25", "slug": "acme-1-0", "title": "A", "url": "https://acme/1", "source": ""}\n'
    )
    assert seen.unreconciled("legacy") == ([], [])
    assert seen.main(["reconcile", "--series", "legacy"]) == 0


def test_whitespace_only_legacy_urls_do_not_group(seen, tmp_path) -> None:
    """A url of spaces is not a url. Truthy as a string, it survived the empty check and gathered
    every other whitespace-only row under one address that names nothing.

    Both rows carry the same blank so they would collide without the strip — an earlier draft used
    different runs of spaces, which are different strings, so the test passed whether or not the
    reader normalised."""
    pool = tmp_path / "pulse" / "legacy.seen.jsonl"
    pool.parent.mkdir(parents=True, exist_ok=True)
    pool.write_text(
        '{"seen": "2026-09-20", "slug": "ws-one", "title": "A", "url": " ", "source": ""}\n'
        '{"seen": "2026-09-21", "slug": "ws-two", "title": "B", "url": " ", "source": ""}\n'
    )
    assert seen.unreconciled("legacy") == ([], [])
    assert seen.main(["reconcile", "--series", "legacy"]) == 0
