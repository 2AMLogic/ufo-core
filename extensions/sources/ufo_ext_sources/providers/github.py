"""The GitHub connector — repositories, issues, comments, users, and their siblings synced into
recallable pages.

GitHub paginates uniformly: every list endpoint returns records as a bare JSON array and ships an
RFC 5988 `Link: rel=next` header until the last page (`?per_page=100`). Auth is the
two required GitHub headers (the v3 media type and the API version) layered on whichever client the
base built from the resolved `Credential`.

The catalog is a tree three deep: `organizations` is the root at `/user/orgs`, `repositories`,
`teams` and `users` hang under an org, and the other 21 streams hang under a repository. A stream's
partitions are its parent's landed pages, so the repo catalog is walked once by the `repositories`
row and the 21 repo-scoped streams fan out over what it landed rather than re-deriving it, and the
owner and name its path reads off the repository are part of every child page's address — a key
unique only inside one repo (a branch or tag `name`, a commit `sha`, a starrer's user id) addresses
one page per repo instead of landing every repo's `main` on one. The edge carries the repository's
`full_name` onto each record as `repo_full_name`, and an organization's `login` as `org_login`, and
`render` scopes the primary key to it (`acme/ufo/main`), so a page reads and is titled as every
page already landed does — the provider's own `number`, `login`, `commit.sha` and `url` carry the
unscoped values. The two catalog rows are full listings and snapshots: a repository that stops
qualifying — archived, forked, or gone from the grant — is tombstoned on the next pass and leaves
the partition set of the 21 streams under it, and an organization the grant lists but cannot read
into fails the `repositories` run rather than emptying that organization's repositories. Each repo
is a partition of the SDK's `PartitionWalk`, which owns the cursor map and resume state; this
connector only produces one repo's bounded pages per stream `Ordering`.
`issues`/`comments`/`review_comments` are `ascending`: a `sort=updated&direction=asc&since` walk
whose running watermark is a sound resume point, and whose first pass starts at the row's floor,
the one bound these three endpoints take server-side.
`commits`/`events`/`issue_events`/`pull_requests` are `newest_first`: a first backfill walks the
repo newest-first as a descending `{high, until}` window (`commits` bounds it server-side with
`?until`, the others client-side since their API takes no time filter), so a capped
run resumes downward without the position drift that
would lose records prepended between slices; steady-state stops early once a page sits strictly
below the repo watermark (a tying page re-yields, so a tied-but-new record lands and the repeats
dedup downstream). Every other repo-scoped stream is `none` — checkpointed at
the repo boundary only, and re-walked whole on every completed pass.

`pull_requests` is read over GraphQL, alone among the streams. A reviewer is woken by a pull
request's checks, and REST publishes them nowhere a pull request read reaches: the timeline of
`metalcraftai/ufo` 3560 answered 19 entries over five event types and not one check-run or status
event, beside the 44 check runs its head commit carried, so a walk over `/issues/{n}/timeline` sees
a green branch go red and reports nothing. One GraphQL read by number answers the rollup, the
reviews, the unresolved threads, the files and the timeline with the pull request, and `databaseId`
is its key, so every page settles on the address its REST record already had. The answer's own
`rateLimit` ends a walk that cannot pay for its next read, because GitHub serves the refusal as a
200 carrying no records.

Every GraphQL list takes a `first` or `last` of at most 100, and GitHub prices a query by the items
it could return, so no read answers a whole pull request: the field set clips each list — 30 check
contexts, 5 reviews, 10 threads, 20 files — for the 1 point it prices, and `_pull_tails` pages each
clipped list to its end, `PULL_REQUEST_TAIL_PAGE_SIZE` at a time, 1 point a page. The by-number
read is the one read that builds a body. The repository walk reads an index — `databaseId`,
`number`, `updatedAt`, 100 a page for 1 point, newest first — and reads whole every pull request
the index lists inside the walk's bound. A walk that landed the clipped list page itself wrote a
second body for every watched pull request past a clip, and the two bodies moved the page's
revision on every pass, waking the watch on a pull request nothing had touched for hours.

**Prices, measured against `metalcraftai/ufo` (3,560 pull requests, 2026-09-14):** an index page
of 100 costs 1 point and 8 KB; one pull request by number costs 1 point and 19 KB in 0.8 s; a
clipped list's next page costs 1. A `newest_first` steady state reads the first index page of every
repository each pass to see whether anything sits above the watermark, then one read per pull
request that moved — 1 point a repository a pass and 1 a moved pull request, against the hourly
5,000. A first walk of a repository pays 1 point per pull request it holds, bounded by the budget's
own refusal and resumed downward. `PULL_REQUEST_PASS_INTERVAL_SECONDS` spaces the passes.

A check run flips without moving the pull request's `updatedAt`, so the newest-first index stops
above an old pull request and never lists it again. Each pass therefore reads a second index first:
every open pull request's rollup state and context count, 100 a page for 2 points, checkpointed on
the partition as `{number: "<state>/<contexts>"}`. A number whose entry moved is read whole by
number and lands reporting no watermark span, so every open pull request's page carries current
checks whether or not anything watches it. A pull request that closes leaves the open index and its
entry with it. A read that meets the budget with pages of a clipped list still to fetch lands
nothing for that pull request, since a page short of its files would read as files removed.
Mergeability is not read: GitHub answers `mergeable: UNKNOWN` for every merged pull request and
for every open one while a push to its base reruns the merge test, then the settled value a tick
later, so the field turned one push to main into two revisions of every watched pull request's page.

`workflow_runs` is the one unordered stream
that carries a floor: Actions runs outnumber every other collection a busy repo publishes, so
the stream declares a zero-day backfill window and each repo slice sends the pinned floor as the
Actions API's `created=>=` range. A `none` stream re-walks whole every pass, so that bound governs
every pass, not just the first. `check_runs` hangs under the pull request whose head they ran on:
GitHub publishes a commit's check runs under a ref and nowhere else, and a pull request carries the
ref, so the edge reads it off the parent and asks the collection at the path GitHub publishes it at.
A check-run id is unique across the account, so the stream keys `global` and a page is addressed by
its id alone. It is syncable and not canonical: one read per pull request per pass is noisy against
the hourly budget, and the rollup on the pull request's own page already says whether its checks
passed — so it joins no connection by declaration and the rows that hold it are retired. A commit's
combined status has no stream at all: those contexts are on that same rollup, where they are the
pull request's summary of its checks rather than a second set of pages under a key a fork would
collide on.
GitHub surfaces no delete signal for the streams under a repository, so the sync runner's row-level
cursor skips already-seen rows. A grant that can't enumerate orgs at all (`/user/orgs` refused with
a 403) has no root to hang the tree off, so that row raises `StreamSkipped` and records a skip, not
a failure; every stream below it enumerates no partition and spends no request. The write path is
intentionally absent — the source seam only reads."""

import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal

import httpx

from ufo.sdk.authproxy import Credential
from ufo.sdk.o11y import log
from ufo.sdk.sources import (
    REPO_BACKFILL_WINDOW_DAYS,
    Ordering,
    ParentEdge,
    Partition,
    PartitionBound,
    PartitionSkipped,
    ProviderRateLimited,
    RestConnector,
    Run,
    StreamFault,
    StreamPage,
    StreamSkipped,
    StreamSpec,
    WalkPage,
    dict_or_empty,
    fanned_out,
    get_path,
    list_or_empty,
    records_at,
)
from ufo_ext_sources.watermark import text_checkpoint

PAGE_SIZE = 100
PULL_REQUEST_INDEX_PAGE_SIZE = 100
PULL_REQUEST_PASS_INTERVAL_SECONDS = 300
"""How long the catalog walk of `pull_requests` waits between passes. GitHub priced one index page
of `PULL_REQUEST_INDEX_PAGE_SIZE` at 1 of the hourly 5,000 GraphQL points and one pull request by
number at 1, measured against `metalcraftai/ufo` (3,560 pull requests, 2026-09-14). A
`newest_first` steady state reads the first index page of every repository each pass to see whether
anything sits above the watermark, so a quiet walk costs `repositories` points a pass and a moved
pull request 1 more: at every 60 s tick a 100-repository connection would spend 6,000 an hour on
index pages alone. At 300 s it spends 1,200 and leaves 3,800 for the pull requests that moved and
the watched reads, 23 of which cost a read a tick each. A watched partition ignores this and is read
every tick, which is the whole reason a member sets a watch."""
REPO_STREAM_FETCH_BUDGET = 12
"""Repositories one run of a canonical REST stream under a repository may fetch. GitHub's REST pool
is 5,000 requests an hour per token or installation — 83 a minute, shared by every row of the
connection and apart from GraphQL's points (`/rate_limit` reports `core` and `graphql`
separately). A quiet `?since=` stream spends one request per repository per tick, so the six
canonical REST streams under a repository spend `6 x 12` = 72 a minute and the catalog rows above
them — one request per organization and one for the root — fit in what is left. A pass over more
repositories than the budget resumes on the next tick, so a large catalog sweeps slower and never
over the pool. A stream that registers no row spends nothing and carries none."""
PULL_REQUEST_FETCH_BUDGET = 20
"""Repositories one `pull_requests` run reads. GraphQL's own 5,000 points an hour are 83 a minute
and an index page prices 1, so 20 pages spend 20 and leave 63 for the 1-point reads of the watched
pull requests, which go first, and of the pull requests the index lists above the watermark;
`PULL_REQUEST_PASS_INTERVAL_SECONDS` bounds the smaller catalog that completes a pass inside one
tick."""
GRAPHQL_PATH = "/graphql"
COMMENT_BACKFILL_WINDOW_DAYS = 7
ISSUE_BACKFILL_WINDOW_DAYS = 365
_REPO_LIST_PARAMS = {"per_page": PAGE_SIZE, "type": "all", "sort": "pushed", "direction": "desc"}
_USERS_ENRICH_CONCURRENCY = 8
_GITHUB_ACCEPT = "application/vnd.github+json, application/vnd.github.star+json"
_GITHUB_API_VERSION = "2022-11-28"
_UNTIL_STREAMS = frozenset({"commits"})
_CREATED_FLOOR_STREAMS = frozenset({"workflow_runs"})
WORKFLOW_RUNS_BACKFILL_WINDOW_DAYS = 0
_PARTITION_SKIP_STATUS = frozenset({404, 409, 410})
REPO_PARTITION_FIELD = "repo_full_name"
ORG_PARTITION_FIELD = "org_login"
_CARRIED: dict[str, dict[str, str]] = {
    "repositories": {REPO_PARTITION_FIELD: "full_name"},
    "organizations": {ORG_PARTITION_FIELD: "login"},
}


GRAPHQL_ERROR_CHARS = 200
GRAPHQL_PARTITION_REFUSALS = frozenset({"NOT_FOUND", "FORBIDDEN", "SAML_PROTECTED"})
PULL_REQUEST_MAX_PAGES = 200
PULL_REQUEST_TAIL_PAGE_SIZE = 100
PULL_REQUEST_TAIL_MAX_PAGES = 50
GRAPHQL_BUDGET_MIN_WAIT_SECONDS = 1.0
_PAGE_INFO = "pageInfo { hasNextPage endCursor }"
_CONTEXT_NODE = """... on CheckRun { name conclusion detailsUrl }
    ... on StatusContext { context state targetUrl }"""
_CONTEXT_COUNTS = (
    "totalCount checkRunCountsByState { state count } statusContextCountsByState { state count }"
)
_THREAD_NODE = "isResolved comments(first: 1) { nodes { path body author { login } } }"
_FILE_NODE = "path additions deletions"
PULL_REQUEST_FIELDS = f"""
  databaseId number title state isDraft reviewDecision updatedAt createdAt
  totalCommentsCount author {{ login }} headRefName headRefOid baseRefName url
  repository {{ owner {{ login }} name }}
  commits(last: 1) {{ totalCount nodes {{ commit {{ statusCheckRollup {{ state
    contexts(first: 30) {{ {_CONTEXT_COUNTS} nodes {{ {_CONTEXT_NODE} }} {_PAGE_INFO} }}
  }} }} }} }}
  reviews(last: 5) {{ totalCount nodes {{ author {{ login }} state submittedAt }} }}
  reviewThreads(first: 10) {{ totalCount nodes {{ {_THREAD_NODE} }} {_PAGE_INFO} }}
  files(first: 20) {{ totalCount nodes {{ {_FILE_NODE} }} {_PAGE_INFO} }}
  timelineItems(last: 10, itemTypes: [HEAD_REF_FORCE_PUSHED_EVENT, READY_FOR_REVIEW_EVENT,
    CONVERT_TO_DRAFT_EVENT, MERGED_EVENT, CLOSED_EVENT, REOPENED_EVENT, REVIEW_REQUESTED_EVENT])
    {{ nodes {{ __typename ... on Node {{ id }} }} }}
"""
_RATE_LIMIT_FIELDS = "rateLimit { cost remaining resetAt }"


def _tail_query(selection: str) -> str:
    return f"""
query($owner: String!, $name: String!, $number: Int!, $after: String) {{
  {_RATE_LIMIT_FIELDS}
  repository(owner: $owner, name: $name) {{
    pullRequest(number: $number) {{ {selection} }}
  }}
}}
"""


def _rollup_contexts(node: dict[str, Any]) -> dict[str, Any] | None:
    commits = list_or_empty(dict_or_empty(node.get("commits")).get("nodes"))
    return get_path(commits[0], "commit.statusCheckRollup.contexts") if commits else None


@dataclass(frozen=True)
class _Tail:
    name: str
    query: str
    connection: Callable[[dict[str, Any]], dict[str, Any] | None]
    landed: Callable[[dict[str, Any]], list[dict[str, Any]]]


PULL_REQUEST_TAILS: tuple[_Tail, ...] = (
    _Tail(
        name="reviewThreads",
        query=_tail_query(
            f"reviewThreads(first: {PULL_REQUEST_TAIL_PAGE_SIZE}, after: $after) "
            f"{{ nodes {{ {_THREAD_NODE} }} {_PAGE_INFO} }}"
        ),
        connection=lambda node: node.get("reviewThreads"),
        landed=lambda record: record["reviewThreads"],
    ),
    _Tail(
        name="files",
        query=_tail_query(
            f"files(first: {PULL_REQUEST_TAIL_PAGE_SIZE}, after: $after) "
            f"{{ nodes {{ {_FILE_NODE} }} {_PAGE_INFO} }}"
        ),
        connection=lambda node: node.get("files"),
        landed=lambda record: record["files"],
    ),
    _Tail(
        name="contexts",
        query=_tail_query(
            "commits(last: 1) { nodes { commit { statusCheckRollup { "
            f"contexts(first: {PULL_REQUEST_TAIL_PAGE_SIZE}, after: $after) "
            f"{{ nodes {{ {_CONTEXT_NODE} }} {_PAGE_INFO} }} }} }} }} }}"
        ),
        connection=_rollup_contexts,
        landed=lambda record: record["checks"]["contexts"],
    ),
)
PULL_REQUEST_INDEX_QUERY = f"""
query($owner: String!, $name: String!, $cursor: String) {{
  {_RATE_LIMIT_FIELDS}
  repository(owner: $owner, name: $name) {{
    pullRequests(first: {PULL_REQUEST_INDEX_PAGE_SIZE},
      orderBy: {{field: UPDATED_AT, direction: DESC}}, after: $cursor) {{
      nodes {{ databaseId number updatedAt }}
      pageInfo {{ hasNextPage endCursor }}
    }}
  }}
}}
"""
OPEN_CHECK_INDEX_QUERY = f"""
query($owner: String!, $name: String!, $cursor: String) {{
  {_RATE_LIMIT_FIELDS}
  repository(owner: $owner, name: $name) {{
    pullRequests(states: OPEN, first: {PULL_REQUEST_INDEX_PAGE_SIZE},
      orderBy: {{field: UPDATED_AT, direction: DESC}}, after: $cursor) {{
      nodes {{ number commits(last: 1) {{ nodes {{ commit {{ statusCheckRollup {{
        state contexts(first: 1) {{ totalCount }}
      }} }} }} }} }}
      pageInfo {{ hasNextPage endCursor }}
    }}
  }}
}}
"""
OPEN_CHECK_INDEX_MAX_PAGES = 20
"""Pages of the open-pull-request check index one partition reads. GitHub prices a page of 100 at
2 points for the nested context connection, so the bound is 40 points against a repository holding
2,000 open pull requests at once. A repository with more keeps syncing: the index truncates at the
bound and the newest-first walk, which this only saves reads on, carries the rest."""
PULL_REQUEST_QUERY = f"""
query($owner: String!, $name: String!, $number: Int!) {{
  {_RATE_LIMIT_FIELDS}
  repository(owner: $owner, name: $name) {{
    pullRequest(number: $number) {{ {PULL_REQUEST_FIELDS} }}
  }}
}}
"""
_OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})"
_REPO = r"[A-Za-z0-9_.-]{1,100}"
_RESOURCE_URLS = (
    re.compile(
        rf"^https://(?:www\.)?github\.com/(?P<owner>{_OWNER})/(?P<repo>{_REPO})"
        r"/(?P<path>pull|issues)/(?P<number>[0-9]{1,10})(?=$|[/#?])"
    ),
    re.compile(
        rf"^https://api\.github\.com/repos/(?P<owner>{_OWNER})/(?P<repo>{_REPO})"
        r"/(?P<path>pulls|issues)/(?P<number>[0-9]{1,10})(?=$|[/#?])"
    ),
)


def _stream(
    name: str,
    *,
    parent: str | None = None,
    path: str | None = None,
    source_object: str | None = None,
    primary_key: str = "id",
    cursor_field: str | None = None,
    created_at_field: str | None = "created_at",
    updated_at_field: str | None = "updated_at",
    ordering: Ordering = Ordering.none,
    canonical: bool = False,
    backfill_window_days: int | None = None,
    indexed: bool = True,
    key_scope: Literal["local", "global"] = "local",
    pass_interval_seconds: int | None = None,
    delete_missing: bool = False,
    fetch_budget: int | None = None,
) -> StreamSpec:
    if (parent is None) != (path is None):
        raise ValueError(f"github: stream {name!r} names a parent without a path, or the reverse")
    if fetch_budget is None and canonical and parent == "repositories":
        fetch_budget = REPO_STREAM_FETCH_BUDGET
    return StreamSpec(
        name=name,
        source_object=source_object or name,
        primary_key=primary_key,
        cursor_field=cursor_field,
        created_at_field=created_at_field,
        updated_at_field=updated_at_field,
        ordering=ordering,
        canonical=canonical,
        backfill_window_days=backfill_window_days,
        indexed=indexed,
        key_scope=key_scope,
        pass_interval_seconds=pass_interval_seconds,
        delete_missing=delete_missing,
        fetch_budget=fetch_budget,
        parents=(
            ()
            if parent is None or path is None
            else (ParentEdge(stream=parent, path=path, carry=_CARRIED.get(parent, {})),)
        ),
    )


ALL_STREAMS: list[StreamSpec] = [
    _stream("organizations", cursor_field=None, delete_missing=True),
    _stream(
        "repositories",
        parent="organizations",
        path="/orgs/{login}/repos",
        cursor_field=None,
        canonical=True,
        delete_missing=True,
    ),
    _stream("teams", parent="organizations", path="/orgs/{login}/teams", cursor_field=None),
    _stream("users", parent="organizations", path="/orgs/{login}/members", cursor_field=None),
    _stream(
        "issues",
        parent="repositories",
        path="/repos/{owner.login}/{name}/issues",
        cursor_field="updated_at",
        ordering=Ordering.ascending,
        backfill_window_days=ISSUE_BACKFILL_WINDOW_DAYS,
        canonical=True,
    ),
    _stream(
        "issue_milestones",
        parent="repositories",
        path="/repos/{owner.login}/{name}/milestones",
        cursor_field="updated_at",
    ),
    _stream(
        "comments",
        parent="repositories",
        path="/repos/{owner.login}/{name}/issues/comments",
        cursor_field="updated_at",
        ordering=Ordering.ascending,
        backfill_window_days=COMMENT_BACKFILL_WINDOW_DAYS,
        canonical=True,
    ),
    _stream(
        "assignees",
        parent="repositories",
        path="/repos/{owner.login}/{name}/assignees",
        cursor_field=None,
    ),
    _stream(
        "branches",
        parent="repositories",
        path="/repos/{owner.login}/{name}/branches",
        primary_key="name",
        cursor_field=None,
    ),
    _stream(
        "check_runs",
        parent="pull_requests",
        path="/repos/{repository.owner.login}/{repository.name}/commits/{headRefOid}/check-runs",
        cursor_field=None,
        created_at_field="started_at",
        indexed=False,
        key_scope="global",
    ),
    _stream(
        "collaborators",
        parent="repositories",
        path="/repos/{owner.login}/{name}/collaborators",
        cursor_field=None,
    ),
    _stream(
        "commit_comments",
        parent="repositories",
        path="/repos/{owner.login}/{name}/comments",
        cursor_field="updated_at",
        canonical=True,
    ),
    _stream(
        "commits",
        parent="repositories",
        path="/repos/{owner.login}/{name}/commits",
        primary_key="sha",
        cursor_field="commit.committer.date",
        created_at_field="commit.committer.date",
        ordering=Ordering.newest_first,
        backfill_window_days=REPO_BACKFILL_WINDOW_DAYS,
    ),
    _stream(
        "contributor_activity",
        parent="repositories",
        path="/repos/{owner.login}/{name}/stats/contributors",
        primary_key="author.id",
        cursor_field=None,
        indexed=False,
    ),
    _stream(
        "deployments",
        parent="repositories",
        path="/repos/{owner.login}/{name}/deployments",
        cursor_field="updated_at",
    ),
    _stream(
        "events",
        parent="repositories",
        path="/repos/{owner.login}/{name}/events",
        cursor_field="created_at",
        ordering=Ordering.newest_first,
        backfill_window_days=REPO_BACKFILL_WINDOW_DAYS,
    ),
    _stream(
        "issue_events",
        parent="repositories",
        path="/repos/{owner.login}/{name}/issues/events",
        cursor_field="created_at",
        ordering=Ordering.newest_first,
        backfill_window_days=REPO_BACKFILL_WINDOW_DAYS,
    ),
    _stream(
        "issue_labels",
        parent="repositories",
        path="/repos/{owner.login}/{name}/labels",
        cursor_field=None,
    ),
    _stream(
        "projects",
        parent="repositories",
        path="/repos/{owner.login}/{name}/projects",
        cursor_field="updated_at",
    ),
    _stream(
        "pull_requests",
        parent="repositories",
        path="/repos/{owner.login}/{name}/pulls",
        primary_key="databaseId",
        cursor_field="updatedAt",
        created_at_field="createdAt",
        updated_at_field="updatedAt",
        ordering=Ordering.newest_first,
        backfill_window_days=REPO_BACKFILL_WINDOW_DAYS,
        canonical=True,
        pass_interval_seconds=PULL_REQUEST_PASS_INTERVAL_SECONDS,
        fetch_budget=PULL_REQUEST_FETCH_BUDGET,
    ),
    _stream(
        "releases",
        parent="repositories",
        path="/repos/{owner.login}/{name}/releases",
        cursor_field="created_at",
        canonical=True,
    ),
    _stream(
        "review_comments",
        parent="repositories",
        path="/repos/{owner.login}/{name}/pulls/comments",
        cursor_field="updated_at",
        ordering=Ordering.ascending,
        backfill_window_days=COMMENT_BACKFILL_WINDOW_DAYS,
        canonical=True,
    ),
    _stream(
        "stargazers",
        parent="repositories",
        path="/repos/{owner.login}/{name}/stargazers",
        cursor_field="starred_at",
        created_at_field="starred_at",
        indexed=False,
    ),
    _stream(
        "tags",
        parent="repositories",
        path="/repos/{owner.login}/{name}/tags",
        primary_key="name",
        cursor_field=None,
    ),
    _stream(
        "workflow_runs",
        parent="repositories",
        path="/repos/{owner.login}/{name}/actions/runs",
        cursor_field="updated_at",
        backfill_window_days=WORKFLOW_RUNS_BACKFILL_WINDOW_DAYS,
        indexed=False,
    ),
    _stream(
        "workflows",
        parent="repositories",
        path="/repos/{owner.login}/{name}/actions/workflows",
        cursor_field="updated_at",
        canonical=True,
    ),
]

ORGANIZATIONS_PATH = "/user/orgs"

_RECORD_PATHS: dict[str, str] = {
    "check_runs": "check_runs",
    "workflow_runs": "workflow_runs",
    "workflows": "workflows",
}


class GitHubConnector(RestConnector):
    name = "github"
    base_url = "https://api.github.com"
    streams_list = ALL_STREAMS
    checkpoint = staticmethod(text_checkpoint)

    def _make_client(self, base_url: str, credential: Credential) -> httpx.AsyncClient:
        client = super()._make_client(base_url, credential)
        client.headers["Accept"] = _GITHUB_ACCEPT
        client.headers["X-GitHub-Api-Version"] = _GITHUB_API_VERSION
        return client

    def flatten(self, record: dict[str, Any], stream: StreamSpec) -> dict[str, Any]:
        """Lift a starred-at envelope's user onto the record it wraps."""
        if stream.name != "stargazers":
            return record
        user = record.get("user")
        return {**user, **record} if isinstance(user, dict) else record

    def render(self, record: dict[str, Any], stream: StreamSpec) -> tuple[str, str]:
        """The record with its primary key scoped to the partition its edge carried onto it
        (`acme/ufo/main`), which is how every page already landed reads and is titled: a key unique
        only inside one repository names the repository in the body, while the address the adapter
        composes from the scope keeps the key as GitHub spelled it. A dotted key (`author.id`) is
        left as the provider wrote it. A pull request's update cursor is page metadata, not page
        content."""
        content = (
            {key: value for key, value in record.items() if key != stream.updated_at_field}
            if stream.name == "pull_requests"
            else record
        )
        field = next((name for edge in stream.parents for name in edge.carry), None)
        if field is None:
            return super().render(content, stream)
        partition = content.get(field)
        if not isinstance(partition, str) or not partition:
            raise RuntimeError(
                f"github: stream {stream.name!r} fans out over {field!r} but a record carries no "
                "such value"
            )
        if stream.primary_key not in content:
            return super().render(content, stream)
        scoped = {**content, stream.primary_key: f"{partition}/{content[stream.primary_key]}"}
        return super().render(scoped, stream)

    async def paginate(
        self, client: httpx.AsyncClient, stream: StreamSpec, run: Run
    ) -> AsyncIterator[list[dict[str, Any]] | StreamPage]:
        if stream.name == "organizations":
            async for records in self._organization_pages(client):
                yield records
            return
        if not stream.parents:
            raise NotImplementedError(f"github: stream {stream.name!r} has no paginate dispatch")
        floor = (
            None
            if run.backfill_after is None
            else run.backfill_after.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        pages = (
            partial(self._pull_pages, client)
            if stream.name == "pull_requests"
            else partial(self._partition_pages, client, stream, floor)
        )
        async for page in fanned_out(stream, run, pages, floor):
            yield page

    async def _pull_pages(
        self,
        client: httpx.AsyncClient,
        partition: Partition,
        bound: PartitionBound,
    ) -> AsyncIterator[WalkPage]:
        """A check result does not move a pull request's `updatedAt`, and GraphQL takes no time
        filter. The check index costs 2 points a hundred."""
        repo = partition.scope or ""
        variables = _repo_variables(repo)
        checks, spent = await self._open_checks(client, partition, variables)
        stored = _stored_checks(bound.checkpoint)
        owed = list(_moved_checks(stored, checks, read=bound.checkpoint is not None))
        moved: list[dict[str, Any]] = []
        for index, number in enumerate(owed):
            try:
                record, spent = await self._pull_whole(
                    client, partition, variables | {"number": number}, spent
                )
            except ProviderRateLimited:
                yield WalkPage(
                    records=moved,
                    checkpoint=_checkpoint(stored, checks, unread=owed[index:]),
                )
                raise
            if record is not None:
                moved.append(record)
        yield WalkPage(records=moved, checkpoint=_checkpoint(stored, checks, unread=()))
        if spent is not None:
            raise ProviderRateLimited(spent)
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(PULL_REQUEST_MAX_PAGES):
            data, spent = await self._graphql(
                client,
                PULL_REQUEST_INDEX_QUERY,
                variables | {"cursor": cursor},
                partition=partition,
            )
            connection = get_path(data, "repository.pullRequests")
            if connection is None:
                raise PartitionSkipped(f"github: {partition.ref} refused")
            info = dict_or_empty(connection.get("pageInfo"))
            cursor = info.get("endCursor") if info.get("hasNextPage") else None
            if cursor is not None and cursor in seen:
                raise StreamFault(f"github: {partition.ref} repeated cursor {cursor!r}")
            listed = list_or_empty(connection.get("nodes"))
            landed: list[dict[str, Any]] = []
            whole: list[dict[str, Any]] = []
            for entry in listed:
                updated_at = entry["updatedAt"]
                # Every edge is inclusive: a tie is re-read and the driver's digest-skip
                # absorbs the repeat.
                if (
                    (bound.after is not None and updated_at < bound.after)
                    or (bound.before is not None and updated_at > bound.before)
                    or (bound.since is not None and updated_at < bound.since)
                ):
                    continue
                try:
                    record, spent = await self._pull_whole(
                        client, partition, variables | {"number": entry["number"]}, spent
                    )
                except ProviderRateLimited:
                    if landed:
                        yield WalkPage(
                            records=landed,
                            high=max(entry["updatedAt"] for entry in listed),
                            low=whole[-1]["updatedAt"],
                        )
                    raise
                if record is None:
                    raise StreamFault(
                        f"github: {partition.ref} listed pull request {entry['number']} and "
                        "answered none"
                    )
                landed.append(record)
                whole.append(entry)
            yield WalkPage(
                records=landed,
                high=max((entry["updatedAt"] for entry in listed), default=None),
                low=min((entry["updatedAt"] for entry in listed), default=None),
            )
            if cursor is None:
                return
            seen.add(cursor)
            if spent is not None:
                raise ProviderRateLimited(spent)
        raise StreamFault(
            f"github: {partition.ref} paged past {PULL_REQUEST_MAX_PAGES} pages of pull requests"
        )

    async def _open_checks(
        self,
        client: httpx.AsyncClient,
        partition: Partition,
        variables: dict[str, Any],
    ) -> tuple[dict[str, str], float | None]:
        """A rollup sits at `PENDING` through a rerun that adds contexts, so the count rides beside
        the state."""
        checks: dict[str, str] = {}
        cursor: str | None = None
        spent: float | None = None
        for _ in range(OPEN_CHECK_INDEX_MAX_PAGES):
            data, spent = await self._graphql(
                client, OPEN_CHECK_INDEX_QUERY, variables | {"cursor": cursor}, partition=partition
            )
            connection = get_path(data, "repository.pullRequests")
            if connection is None:
                raise PartitionSkipped(f"github: {partition.ref} refused")
            for node in list_or_empty(connection.get("nodes")):
                checks[str(node["number"])] = _check_entry(node)
            info = dict_or_empty(connection.get("pageInfo"))
            cursor = info.get("endCursor") if info.get("hasNextPage") else None
            if cursor is None:
                break
            if spent is not None:
                raise ProviderRateLimited(spent)
        return checks, spent

    async def _pull_whole(
        self,
        client: httpx.AsyncClient,
        partition: Partition,
        variables: dict[str, Any],
        spent: float | None,
    ) -> tuple[dict[str, Any] | None, float | None]:
        if spent is not None:
            raise ProviderRateLimited(spent)
        data, spent = await self._graphql(
            client, PULL_REQUEST_QUERY, variables, partition=partition
        )
        node = get_path(data, "repository.pullRequest")
        if node is None:
            return None, spent
        record = _pull_record(node)
        return record, await self._pull_tails(client, partition, variables, node, record, spent)

    async def _pull_tails(
        self,
        client: httpx.AsyncClient,
        partition: Partition,
        variables: dict[str, Any],
        node: dict[str, Any],
        record: dict[str, Any],
        spent: float | None,
    ) -> float | None:
        """A page missing files or checks would read as files removed and checks gone."""
        for tail in PULL_REQUEST_TAILS:
            cursor = _next_page(tail.connection(node))
            for _ in range(PULL_REQUEST_TAIL_MAX_PAGES):
                if cursor is None:
                    break
                if spent is not None:
                    raise ProviderRateLimited(spent)
                data, spent = await self._graphql(
                    client, tail.query, variables | {"after": cursor}, partition=partition
                )
                answered = dict_or_empty(get_path(data, "repository.pullRequest"))
                connection = tail.connection(answered)
                if connection is None:
                    raise StreamFault(
                        f"github: {partition.ref} answered no {tail.name} page after {cursor!r}"
                    )
                tail.landed(record).extend(
                    _unwrapped(item) for item in list_or_empty(connection.get("nodes"))
                )
                cursor = _next_page(connection)
            else:
                raise StreamFault(
                    f"github: {partition.ref} paged past {PULL_REQUEST_TAIL_MAX_PAGES} pages of "
                    f"{tail.name}"
                )
        return spent

    async def _graphql(
        self,
        client: httpx.AsyncClient,
        query: str,
        variables: dict[str, Any],
        *,
        partition: Partition,
    ) -> tuple[dict[str, Any], float | None]:
        """GraphQL answers a refusal with HTTP 200 and an `errors` array."""
        body = await self._post(client, GRAPHQL_PATH, json={"query": query, "variables": variables})
        errors = list_or_empty(body.get("errors"))
        if errors:
            reason = str(errors[0].get("message", ""))[:GRAPHQL_ERROR_CHARS]
            if {str(error.get("type", "")) for error in errors} <= GRAPHQL_PARTITION_REFUSALS:
                raise PartitionSkipped(f"github: {partition.ref} refused: {reason}")
            raise StreamFault(f"github: graphql refused {partition.ref}: {reason}")
        data = dict_or_empty(body.get("data"))
        rate_limit = dict_or_empty(data.get("rateLimit"))
        log(
            "source_sync.graphql_rate_limit",
            stream="pull_requests",
            cost=str(rate_limit.get("cost", "")),
            remaining=str(rate_limit.get("remaining", "")),
            reset_at=str(rate_limit.get("resetAt", "")),
        )
        return data, _budget_spent(rate_limit)

    async def _organization_pages(
        self, client: httpx.AsyncClient
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """GitHub refuses a grant lacking org scope, or one a policy gates, here with a 403."""
        try:
            async for page in self._paginate_link_header(
                client, ORGANIZATIONS_PATH, params={"per_page": PAGE_SIZE}
            ):
                yield page
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 403:
                raise StreamSkipped(
                    "github: org enumeration refused (403); the grant is missing org scope"
                ) from error
            raise

    async def _partition_pages(
        self,
        client: httpx.AsyncClient,
        stream: StreamSpec,
        floor: str | None,
        partition: Partition,
        bound: PartitionBound,
    ) -> AsyncIterator[WalkPage]:
        """`events` and `issue_events` expose no time filter. GitHub answers an exhausted rate limit
        with a 403, so a 403 fails the run."""
        params = _partition_params(stream, floor, bound)
        semaphore = asyncio.Semaphore(_USERS_ENRICH_CONCURRENCY) if stream.name == "users" else None
        try:
            async for page in self._paginate_link_header(
                client,
                partition.path,
                params=dict(params),
                record_path=_RECORD_PATHS.get(stream.name),
            ):
                if semaphore is not None:
                    page = await self._enrich_users(client, page, semaphore=semaphore)
                page = _kept(page, stream)
                if not page:
                    continue
                landed = page
                if (
                    stream.ordering is Ordering.newest_first
                    and stream.name not in _UNTIL_STREAMS
                    and (bound.before or bound.since)
                ):
                    before, since = bound.before, bound.since
                    field = stream.cursor_field
                    landed = [
                        record
                        for record in page
                        if field
                        and isinstance(record.get(field), str)
                        and (before is None or record[field] <= before)
                        and (since is None or record[field] >= since)
                    ]
                high, _ = _cursor_bounds(landed, stream.cursor_field)
                _, low = _cursor_bounds(page, stream.cursor_field)
                yield WalkPage(records=landed, high=high, low=low)
        except httpx.HTTPStatusError as error:
            if error.response.status_code in _PARTITION_SKIP_STATUS:
                raise PartitionSkipped(f"github: {partition.ref} refused") from error
            raise

    async def _enrich_users(
        self, client: httpx.AsyncClient, page: list[dict[str, Any]], *, semaphore: asyncio.Semaphore
    ) -> list[dict[str, Any]]:
        """Replace each simple-user member (`login` + `id`) with the public-user record from
        `GET /users/{login}`, which carries `name`/`email` when public. On 404 keep the member."""

        async def one(member: dict[str, Any]) -> dict[str, Any]:
            login = member.get("login")
            if not isinstance(login, str) or not login:
                return member
            async with semaphore:
                try:
                    response = await self._get_raw(client, f"/users/{login}")
                except httpx.HTTPStatusError as error:
                    if error.response.status_code == 404:
                        return member
                    raise
                body = response.json() if response.content else None
                return body if isinstance(body, dict) else member

        return list(await asyncio.gather(*[one(member) for member in page]))

    async def _paginate_link_header(
        self,
        client: httpx.AsyncClient,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        record_path: str | None = None,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """GitHub's stats endpoints answer 202 with an empty body while computing."""
        async for page in self._get_link_header_pages(
            client,
            path,
            params=params,
            page_size=PAGE_SIZE,
            parse_records=partial(_parse_records, record_path=record_path),
        ):
            yield page


def _check_entry(node: dict[str, Any]) -> str:
    commits = list_or_empty(dict_or_empty(node.get("commits")).get("nodes"))
    rollup = dict_or_empty(get_path(commits[0], "commit.statusCheckRollup") if commits else None)
    state = rollup.get("state")
    count = dict_or_empty(rollup.get("contexts")).get("totalCount")
    return f"{state if isinstance(state, str) else ''}/{count if isinstance(count, int) else 0}"


def _stored_checks(stored: str | None) -> dict[str, str]:
    """The check map this partition's last pass left. Anything but a map of strings reads as empty,
    which costs a pass of re-reads and never a wrong answer."""
    if stored is None:
        return {}
    try:
        parsed = json.loads(stored)
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {key: value for key, value in parsed.items() if isinstance(value, str)}


def _moved_checks(
    stored: Mapping[str, str], checks: Mapping[str, str], *, read: bool
) -> tuple[int, ...]:
    if not read:
        return ()
    return tuple(
        sorted(int(number) for number, entry in checks.items() if stored.get(number) != entry)
    )


def _checkpoint(
    stored: Mapping[str, str], checks: Mapping[str, str], *, unread: Sequence[int]
) -> str:
    """A check does not move `updatedAt`, so storing an unread number's new entry would lose its
    check result for good."""
    kept = dict(checks)
    for number in unread:
        prior = stored.get(str(number))
        if prior is None:
            kept.pop(str(number), None)
        else:
            kept[str(number)] = prior
    return json.dumps(kept, sort_keys=True)


def _repo_variables(repo: str) -> dict[str, Any]:
    owner, _, name = repo.partition("/")
    return {"owner": owner, "name": name}


def _pull_record(node: dict[str, Any]) -> dict[str, Any]:
    """GraphQL publishes the check rollup only under the last commit."""
    record = _unwrapped(node)
    commits = record.pop("commits")
    record["checks"] = get_path(commits[0], "commit.statusCheckRollup") if commits else None
    return record


_CONNECTION_META = frozenset({"nodes", "totalCount", "pageInfo"})


def _unwrapped(value: Any) -> Any:
    match value:
        case dict():
            flat: dict[str, Any] = {}
            for key, item in value.items():
                match item:
                    case {"nodes": list() as nodes}:
                        flat[key] = [_unwrapped(node) for node in nodes]
                        if "totalCount" in item:
                            flat[f"{key}Count"] = item["totalCount"]
                        flat |= {
                            name: _unwrapped(extra)
                            for name, extra in item.items()
                            if name not in _CONNECTION_META
                        }
                    case _:
                        flat[key] = _unwrapped(item)
            return flat
        case list():
            return [_unwrapped(item) for item in value]
        case _:
            return value


def _next_page(connection: dict[str, Any] | None) -> str | None:
    info = dict_or_empty(dict_or_empty(connection).get("pageInfo"))
    return info.get("endCursor") if info.get("hasNextPage") else None


def _budget_spent(rate_limit: dict[str, Any]) -> float | None:
    """The seconds until GitHub's hourly points refill, when what is left will not pay for another
    read of the size just made, and None while the budget holds."""
    cost = rate_limit.get("cost")
    remaining = rate_limit.get("remaining")
    reset = rate_limit.get("resetAt")
    if not isinstance(cost, int) or not isinstance(remaining, int) or remaining > cost:
        return None
    refill = datetime.strptime(str(reset), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return max((refill - datetime.now(UTC)).total_seconds(), GRAPHQL_BUDGET_MIN_WAIT_SECONDS)


def _partition_params(
    stream: StreamSpec, floor: str | None, bound: PartitionBound
) -> dict[str, Any]:
    """One partition request's query, carrying the walk's resume `bound` in GitHub's own terms. See
    `_partition_pages`."""
    params: dict[str, Any] = {"per_page": PAGE_SIZE}
    if stream.name == "issues":
        params["state"] = "all"
    if stream.name == "repositories":
        params |= _REPO_LIST_PARAMS
    if stream.ordering is Ordering.ascending:
        params |= {"sort": "updated", "direction": "asc"}
        if bound.after:
            params["since"] = bound.after
    elif stream.name in _UNTIL_STREAMS:
        if bound.before:
            params["until"] = bound.before
        if bound.since:
            params["since"] = bound.since
    elif stream.name in _CREATED_FLOOR_STREAMS and floor:
        params["created"] = f">={floor}"
    return params


def _kept(page: list[dict[str, Any]], stream: StreamSpec) -> list[dict[str, Any]]:
    """GitHub's `/issues` returns pull requests too."""
    match stream.name:
        case "issues":
            return [record for record in page if "pull_request" not in record]
        case "repositories":
            return [
                record for record in page if not record.get("archived") and not record.get("fork")
            ]
        case _:
            return page


def _parse_records(response: httpx.Response, record_path: str | None) -> list[dict[str, Any]]:
    if not response.content:
        return []
    return records_at(response.json(), record_path)


def _cursor_bounds(
    page: list[dict[str, Any]], cursor_field: str | None
) -> tuple[str | None, str | None]:
    """The highest and lowest `cursor_field` values on a page, for the walk's watermark/window
    tracking. None when the stream carries no cursor field."""
    if not cursor_field:
        return None, None
    values = [value for record in page if isinstance(value := get_path(record, cursor_field), str)]
    if not values:
        return None, None
    return max(values), min(values)


def resource_url(url: str) -> str | None:
    """The canonical link of the pull request or issue `url` names —
    `https://github.com/<owner>/<repo>/pull/<n>` or `.../issues/<n>` — or None: a repository, a
    commit, or a listing is not a resource a trigger narrows to. The repository is case-folded
    because GitHub answers one repository under every spelling of its name, and the API form, a
    sub-page and a fragment all name the same resource, so one pull request is one watch however it
    was linked."""
    for pattern in _RESOURCE_URLS:
        match = pattern.match(url.strip())
        if match is None:
            continue
        repo = f"{match.group('owner')}/{match.group('repo')}".lower()
        path = "issues" if match.group("path") == "issues" else "pull"
        return f"https://github.com/{repo}/{path}/{match.group('number')}"
    return None


_PULL_CLOSED = "contains(['CLOSED', 'MERGED'], page.state)"
_PULL_CONCLUDED = "contains(['SUCCESS', 'FAILURE', 'ERROR'], page.checks.state)"
_ISSUE_CLOSED = "page.state == 'closed'"
_RUN_CONCLUDED = "page.status == 'completed' && contains(['success', 'failure'], page.conclusion)"
"""What a terminal state is, per stream, in the words that stream's own records carry.
`pull_requests` is read over GraphQL, whose enums are upper case; `issues` and `workflow_runs` are
REST, whose `state` and `status` are lower case. One spelling for both would match neither."""

WAKE_CLAUSES: tuple[str, ...] = (
    "stream == 'comments'",
    "stream == 'review_comments'",
    f"stream == 'pull_requests' && {_PULL_CLOSED}",
    f"stream == 'pull_requests' && {_PULL_CONCLUDED}",
    f"stream == 'issues' && {_ISSUE_CLOSED}",
    f"stream == 'workflow_runs' && {_RUN_CONCLUDED}",
)
"""What a trigger on a GitHub connection's whole feed wakes on when it names no clauses of its own:
a comment, a review comment, a pull request's checks concluding, and a pull request, an issue or an
Actions run reaching a terminal state. Everything else moves a page and reports nothing — a title
edit, a label, a draft flip, a review requested, a run still queued.

A pull request carries its checks on its own page, as the rollup GitHub publishes under the head
commit and nowhere a `workflow_runs` row reaches, so a conclusive rollup is how a watch hears that
the branch went red."""


def resource_identity(resource: str) -> str:
    """What makes two canonical links one resource: GitHub numbers a repository's pull requests and
    its issues in one sequence, so `pull/7` and `issues/7` are the one thing under two spellings and
    a page of links naming both earns one offer."""
    match = _RESOURCE_URLS[0].match(resource)
    if match is None:
        return resource
    return f"github.com/{match.group('owner')}/{match.group('repo')}/{match.group('number')}"


def resource_repo(url: str) -> str | None:
    """The repository a link names, spelled as the link spells it. The canonical form case-folds it
    so that two spellings are one trigger, but GitHub answers its own stored case in every URL a
    record carries, so a clause built from the folded form matches nothing of a repository anybody
    capitalized. A link copied out of GitHub carries that stored case, which is why the clauses take
    the link's spelling and the identity keeps the folded one."""
    for pattern in _RESOURCE_URLS:
        match = pattern.match(url.strip())
        if match is not None:
            return f"{match.group('owner')}/{match.group('repo')}"
    return None


def resource_clauses(resource: str, repo: str) -> tuple[str, ...]:
    """The clauses that wake a conversation about one pull request or issue, one per event. `repo`
    is the repository as the landed catalog spells it, because GitHub answers its own stored case in
    every URL a record carries while the canonical link is case-folded — comparing the folded form
    would match no record of a repository anybody capitalized.

    Every clause names the stream it selects. That is what lets a trigger narrowed to streams keep
    the clauses of those streams alone — without it a watch on `[issues]` and one on
    `[pull_requests]` reduce to the same list and stop being two triggers — and what lets each
    clause read the field and the spelling its own stream's records carry: `pull_requests` comes
    from GraphQL with the web link in `url` and upper-case enums, while `issues` comes from REST
    with the API link in `url`, the web link in `html_url`, and a lower-case `state`."""
    match = _RESOURCE_URLS[0].match(resource)
    if match is None:
        return ()
    number = match.group("number")
    page = f"https://github.com/{repo}/{match.group('path')}/{number}"
    issue_api = f"https://api.github.com/repos/{repo}/issues/{number}"
    pull_api = f"https://api.github.com/repos/{repo}/pulls/{number}"
    commented = f"stream == 'comments' && page.issue_url == '{issue_api}'"
    if match.group("path") == "issues":
        return (f"stream == 'issues' && page.html_url == '{page}' && {_ISSUE_CLOSED}", commented)
    return (
        f"stream == 'pull_requests' && page.url == '{page}' && {_PULL_CLOSED}",
        f"stream == 'pull_requests' && page.url == '{page}' && {_PULL_CONCLUDED}",
        commented,
        f"stream == 'review_comments' && page.pull_request_url == '{pull_api}'",
        f"stream == 'workflow_runs' && contains(page.pull_requests[].url || `[]`, '{pull_api}') "
        f"&& {_RUN_CONCLUDED}",
    )
