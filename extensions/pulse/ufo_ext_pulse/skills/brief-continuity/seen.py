"""The sightings pool: what a gather has seen, separately from what an edition published.

One JSON Lines file per series at `pulse/<series>.seen.jsonl` in the conversation workspace,
append-only, one row per sighting. `record` appends a sighting; `history` reads every sighting of one
lead; `fresh` lists the leads worth pursuing now; `stale` answers whether one lead has gone quiet.

Two stores, two questions. The covered ledger answers *what has been published* and governs what an
edition may repeat. This answers *what has been seen* and governs what is worth chasing. Merging them
would lose both answers: a lead seen four times and never published is not a repeat, and a story
published once is not a lead.

Rows sharing a slug are that lead's history, exactly as they are in the covered ledger. A story seen
on Tuesday and again on Wednesday in a changed state is two rows rather than an overwrite, and the
difference between them is the evidence a material new development needs.

Nothing is ever removed. Collection is additive: a lead that goes quiet for three weeks and then
moves is still here with its whole history attached, which a pool that expired it would have
discarded at exactly the moment it became interesting. Age is a reason not to pursue a lead, never a
reason to forget it — so staleness is asked at read time, here, rather than enforced by deletion.

Staleness keys on the *most recent* sighting. A lead first seen thirty days ago and seen again this
morning is live, not old; asking how long ago it first appeared answers a different question than
whether it is still moving.

The path is workspace-relative for the same reason the covered ledger's is: only a command's argv is
rewritten to the carrier's workspace directory, never a path inside a script.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _jsonl_pool import SLUG, append_row, dated_path  # noqa: E402
from _jsonl_pool import read_rows as _read_rows  # noqa: E402

SEEN_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
POOL_DIR = "pulse"
DEFAULT_WITHIN_DAYS = 14


def pool_path(series: str) -> Path:
    return dated_path(POOL_DIR, series, "seen.jsonl")


def read_rows(series: str) -> list[dict]:
    return _read_rows(pool_path(series))


def record(series: str, seen: str, slug: str, title: str, url: str, source: str) -> Path:
    """The shape of one sighting line.

**This function is not how a row is recorded.** `pulse_record_sightings` and `pulse_record_edition`
write the record; this file is a projection rendered whole from it, so a row appended here is erased
by the next projection rather than kept. It survives as the definition of the row shape the
projection must match — `tests/test_projection_shape.py` asserts the two are byte-identical — and
there is deliberately no `record` subcommand, so nothing reachable from a shell can write here.
    """
    if not SEEN_DATE.fullmatch(seen):
        raise ValueError(f"seen must be YYYY-MM-DD, got {seen!r}")
    if not SLUG.fullmatch(slug):
        raise ValueError(f"slug must be a lowercase hyphenated slug, got {slug!r}")
    # The url is the best key a row carries for reconciling two sightings of one story later --
    # better than title, which legitimately drifts between sightings, and than source, which is too
    # coarse to tell two stories from one publisher apart. It is not the only external field, and a
    # row without one is not beyond manual repair; it is simply the one a reconciler cannot do
    # automatically. So it is required, and required to be more than whitespace: " " and an empty
    # string are equally unreconcilable, and the blank one at least announces itself.
    url = url.strip()
    if not url:
        raise ValueError("url is required: a sighting with no url cannot be reconciled later")
    path = pool_path(series)
    row = {"seen": seen, "slug": slug, "title": title, "url": url, "source": source}
    append_row(path, row)
    return path


def history(series: str, slug: str) -> list[dict]:
    """Every sighting of one lead, oldest first — how it moved, not merely that it exists."""
    return [row for row in read_rows(series) if row["slug"] == slug]


def last_seen(series: str) -> dict[str, str]:
    """The most recent sighting date per lead. The only date staleness is allowed to ask about."""
    latest: dict[str, str] = {}
    for row in read_rows(series):
        slug, seen = row["slug"], row["seen"]
        if slug not in latest or seen > latest[slug]:
            latest[slug] = seen
    return latest


def fresh(series: str, within_days: int, today: date) -> list[tuple[str, str]]:
    """Leads whose most recent sighting is inside the window, most recently seen first."""
    cutoff = (today - timedelta(days=within_days)).isoformat()
    live = [(slug, seen) for slug, seen in last_seen(series).items() if seen >= cutoff]
    return sorted(live, key=lambda pair: pair[1], reverse=True)


def unreconciled(series: str) -> tuple[list[tuple[str, list[str]]], list[tuple[str, list[str]]]]:
    """Rows the pool cannot tell apart, by url then by slug. It reports; it does not diagnose.

    A slug is a judgement made inside one run, and two runs looking at one story need not reach the
    same judgement. Nothing in `record` can catch that — the slug is the caller's to choose and only
    its shape is checked. The url is chosen outside the run, which makes it the better key and not a
    sound one.

    **One url under several slugs has two causes and this cannot separate them.** Either a story was
    renamed between sightings, which is the trap the skill already names — or the url names a page
    rather than a story, a blog index or a repository root, and the slugs are different stories that
    happened to be found at one address. Both look identical here. Reporting the first alone would
    be a verdict the data does not carry, so both are named and neither is chosen.

    One slug over several urls is the quieter direction: two stories filed as one lead, the second
    hidden behind the first's history.

    What it stays silent about is not thereby clean. A story syndicated at three addresses is three
    urls and reads as three leads, and no comparison of urls will say otherwise.

    Urls are stripped as they are read, and a row whose url is blank once stripped is skipped. This
    reader is the one consumer that cannot trust the writer: rows predate the guard that requires a
    url, and the guard that preceded it tested `url.strip()` while storing `url` raw. So a padded
    legacy row and a clean one naming one story would read as two urls, and a whitespace-only url
    would group with every other whitespace-only url — both reported as findings, with a diagnosis
    attached to each, on exactly the old rows this exists to audit."""
    by_url: dict[str, list[str]] = {}
    by_slug: dict[str, list[str]] = {}
    for row in read_rows(series):
        raw = row.get("url")
        url = raw.strip() if isinstance(raw, str) else ""
        if not url:
            continue
        slug = row["slug"]
        by_url.setdefault(url, [])
        if slug not in by_url[url]:
            by_url[url].append(slug)
        by_slug.setdefault(slug, [])
        if url not in by_slug[slug]:
            by_slug[slug].append(url)
    split = sorted((url, slugs) for url, slugs in by_url.items() if len(slugs) > 1)
    merged = sorted((slug, urls) for slug, urls in by_slug.items() if len(urls) > 1)
    return split, merged


def _title_of(series: str, slug: str) -> str:
    rows = history(series, slug)
    return rows[-1]["title"] if rows else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_reconcile = sub.add_parser("reconcile", help="rows the pool cannot tell apart")
    p_reconcile.add_argument("--series", required=True)

    p_history = sub.add_parser("history", help="every sighting of one lead, oldest first")
    p_history.add_argument("--series", required=True)
    p_history.add_argument("--slug", required=True)

    p_fresh = sub.add_parser("fresh", help="leads worth pursuing now")
    p_fresh.add_argument("--series", required=True)
    p_fresh.add_argument("--within-days", type=int, default=DEFAULT_WITHIN_DAYS)
    p_fresh.add_argument("--today", default=None)

    p_stale = sub.add_parser("stale", help="whether one lead has gone quiet")
    p_stale.add_argument("--series", required=True)
    p_stale.add_argument("--slug", required=True)
    p_stale.add_argument("--within-days", type=int, default=DEFAULT_WITHIN_DAYS)
    p_stale.add_argument("--today", default=None)

    args = parser.parse_args(argv)
    today = date.fromisoformat(args.today) if getattr(args, "today", None) else date.today()

    if args.command == "history":
        rows = history(args.series, args.slug)
        if not rows:
            print("never seen")
            return 0
        for row in rows:
            source = f"  [{row['source']}]" if row.get("source") else ""
            print(f"{row['seen']}  {row['title']}{source}")
        return 0

    if args.command == "reconcile":
        shared_url, shared_slug = unreconciled(args.series)
        for url, slugs in shared_url:
            print(f"one url under {len(slugs)} slugs: {url}")
            for slug in slugs:
                print(f"    {slug}")
            print("    either a renamed story, or a url naming a page rather than a story")
        for slug, urls in shared_slug:
            print(f"one slug over {len(urls)} urls: {slug}")
            for url in urls:
                print(f"    {url}")
            print("    two stories under one lead, the second hidden behind the first's history")
        if shared_url or shared_slug:
            print("these rows cannot be told apart — read them before trusting the history")
            return 1
        print("no rows share a url under different slugs, or a slug across different urls")
        return 0

    if args.command == "fresh":
        live = fresh(args.series, args.within_days, today)
        if not live:
            print("no leads seen in the window")
            return 0
        for slug, seen in live:
            # Two spaces minimum: a slug exactly as long as the pad width would otherwise
            # butt straight against its title, and real slugs reach it.
            print(f"{seen}  {slug:<44}  {_title_of(args.series, slug)}")
        return 0

    seen = last_seen(args.series).get(args.slug)
    if seen is None:
        print("never seen")
        return 1
    cutoff = (today - timedelta(days=args.within_days)).isoformat()
    if seen < cutoff:
        print(f"last seen {seen} — stale, do not pursue without a reason")
        return 1
    print(f"last seen {seen} — live")
    return 0


if __name__ == "__main__":
    sys.exit(main())
