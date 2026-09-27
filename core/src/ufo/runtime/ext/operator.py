"""The operator session every operator surface (the session debugger, the memory explorer) rides,
and the rule that decides whom it admits.

A request's credential is a signed member bearer: the token POSTed to open a session, then the
Authorization header, then the `ufo_debug` session cookie — never a query parameter, so the
long-lived credential stays out of URLs, access logs, and browser history. The deploy's one
`OperatorRule`, selected at boot by `[operator] rule`, turns the first credential it grants into an
`OperatorGrant`: the workspace the bearer claims as its home, and a reach of `own` (that workspace
alone) or `fleet` (any workspace `?ws=` names, resolved by the rule's own `select`). The built-in
`seated_admin` rule grants a seated admin of the claimed workspace reach `own`; a rule that grants
fleet reach is a first-party extension's (`operator_rules`), since only the deploy's owner can say
who operates all of it. One cookie serves every operator surface, so an operator authenticates once
and browses all of them.

`FleetDirectory` is what fleet reach picks from: the workspaces this deploy serves and the threads
that moved most recently across all of them. It lives in core, re-exported through
`ufo.sdk.operator`, because it reads core tables across workspaces through `owner_tx`, which no
extension reaches. Verification stays the caller's: `verified_claims` takes the token and resolves
`UFO_TOKEN_SECRET` itself, so this module never holds the signing key."""

import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import UUID

import sqlalchemy as sa
from pydantic import BaseModel, field_validator

from ufo.config import DEFAULT_OPERATOR_RULE, DebuggerConfig
from ufo.db import owner_tx, workspace_tx
from ufo.harness.auth.bearer import verified_claims
from ufo.runtime import seats
from ufo.runtime.workspace import ws
from ufo.schema import tables
from ufo.schema.records import SUBAGENT_SURFACE
from ufo.sdk.http import (
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Request,
    Response,
    set_session_cookie,
)

if TYPE_CHECKING:
    from ufo.runtime.ext.manifest import Manifest
    from ufo.runtime.ext.surface import SurfaceAuth, SurfaceContext

DEBUGGER_SURFACE = "debug"
OPERATOR_COOKIE = "ufo_debug"
TOKEN_FIELD = "token"
OPERATOR_PAGE_PATH = re.compile(r"/surface/[^/]+/?")
OPERATOR_GRANT_SCOPE = "ufo.operator_grant"
OWN_REACH_REFUSAL = "This session reads its own workspace only."

Reach = Literal["own", "fleet"]


@dataclass(frozen=True)
class OperatorGrant:
    """What one admitted credential reaches: `home` is the workspace its bearer claims; reach `own`
    reads that workspace alone, reach `fleet` any workspace `?ws=` selects."""

    home: UUID
    reach: Reach


class OperatorLookup(Protocol):
    """The reads a rule decides by. Core answers each, so a rule holds no connection of its own:
    whether an address is a seated admin of a workspace, the domain a workspace's first member is
    at, and the oldest workspace a domain addresses that way."""

    async def seated_admin(self, workspace_id: UUID, email: str) -> bool: ...

    async def workspace_domain(self, workspace_id: UUID) -> str | None: ...

    async def workspace_by_domain(self, domain: str) -> UUID | None: ...


class OperatorRule(Protocol):
    """Who operates this deploy. `admit` grants one verified bearer's claim, or refuses it with
    None. `select` resolves a `?ws=` target for a fleet grant; core never calls it under reach
    `own`. `operator_workspace` says whether a workspace is the operator's own, which gates
    renderings meant for the operator alone. `sign_in` is where a credential-less GET of an operator
    page is sent to mint a bearer, and what an operator API's 401 names, or None to answer the page
    401 and name none."""

    @property
    def sign_in(self) -> str | None: ...

    async def admit(self, claimed_ws: UUID, email: str) -> OperatorGrant | None: ...

    async def select(self, grant: OperatorGrant, target: str) -> UUID | None: ...

    async def operator_workspace(self, workspace_id: UUID) -> bool: ...


@dataclass(frozen=True)
class OperatorRuleSpec:
    """One operator rule a first-party extension registers: the name `[operator] rule` selects it
    by, and how boot builds it over core's `OperatorLookup`. An unselected rule is never built."""

    name: str
    build: Callable[[OperatorLookup], OperatorRule]


@dataclass(frozen=True)
class OperatorSeats:
    """Core's `OperatorLookup`: the reads of one workspace run under that workspace's own binding,
    and the domain read across workspaces under the owner role, which returns an id and nothing
    else."""

    async def seated_admin(self, workspace_id: UUID, email: str) -> bool:
        with ws(workspace_id):
            async with workspace_tx() as connection:
                member_id = await seats.member_by_email(connection, workspace_id, email)
                return member_id is not None and await seats.member_is_admin(
                    connection, workspace_id, member_id
                )

    async def workspace_domain(self, workspace_id: UUID) -> str | None:
        with ws(workspace_id):
            async with workspace_tx() as connection:
                return await seats.workspace_domain(connection, workspace_id)

    async def workspace_by_domain(self, domain: str) -> UUID | None:
        async with owner_tx() as connection:
            return await seats.workspace_by_domain(connection, domain)


@dataclass(frozen=True)
class SeatedAdminRule:
    """The built-in rule: a seated admin of the bearer's own workspace operates that workspace and
    no other, no workspace is the operator's own, and there is no sign-in page to send anyone to."""

    lookup: OperatorLookup
    sign_in: str | None = None

    async def admit(self, claimed_ws: UUID, email: str) -> OperatorGrant | None:
        if not await self.lookup.seated_admin(claimed_ws, email):
            return None
        return OperatorGrant(home=claimed_ws, reach="own")

    async def select(self, grant: OperatorGrant, target: str) -> UUID | None:
        raise RuntimeError("seated_admin grants no fleet reach to select a workspace with")

    async def operator_workspace(self, workspace_id: UUID) -> bool:
        return False


SEATED_ADMIN = OperatorRuleSpec(name=DEFAULT_OPERATOR_RULE, build=SeatedAdminRule)


def select_operator_rule(name: str, manifests: Sequence["Manifest"]) -> OperatorRule:
    """The deploy's one operator rule: the spec registered under `name` among the built-in
    `seated_admin` and the active manifests' `operator_rules`, built over core's lookup. A name two
    specs share and a name nobody registers both fail loud; every other registered rule stays
    inert."""
    specs = (SEATED_ADMIN, *(spec for manifest in manifests for spec in manifest.operator_rules))
    registered = Counter(spec.name for spec in specs)
    shared = sorted(rule for rule, count in registered.items() if count > 1)
    if shared:
        raise ValueError(f"operator rule {shared[0]!r} is registered more than once")
    chosen = next((spec for spec in specs if spec.name == name), None)
    if chosen is None:
        raise ValueError(
            f"[operator] rule {name!r} names no registered rule; registered: "
            + ", ".join(sorted(registered))
        )
    return chosen.build(OperatorSeats())


@dataclass(frozen=True)
class OperatorSetup:
    """The operator configuration boot resolved: the deploy's one rule, and where the session
    debugger links out."""

    rule: OperatorRule
    links: DebuggerConfig


_installed: OperatorSetup | None = None


def install_operator(setup: OperatorSetup | None) -> None:
    """The process's operator setup, installed once at serve boot before any request or turn runs.
    Every operator surface's resolver and session bind, and both contexts' `is_operator_workspace`,
    read it rather than threading one deploy-fixed rule through every context a turn, job, hook, and
    surface builds. A test installs the rule it exercises and clears it with None."""
    global _installed
    _installed = setup


def installed_operator() -> OperatorSetup:
    if _installed is None:
        raise RuntimeError("no operator rule is installed; serve installs one at boot")
    return _installed


def debugger_links() -> DebuggerConfig:
    """Where the session debugger links out, as `[debugger]` configures it."""
    return installed_operator().links


async def resolve_operator_workspace(
    request: Request, _auth: "SurfaceAuth"
) -> UUID | Response | None:
    """The workspace an operator request is scoped to, or None to reject. The POSTed form token,
    then the Authorization bearer, then the session cookie are tried in turn and the first the rule
    grants is admitted, so a cookie whose bearer expired or whose admin is gone never shadows the
    fresh token posted to replace it. The grant rides the request for `operator_grant`. Without
    `?ws=` the grant's home is the scope; under fleet reach `?ws=` is the rule's to select, and
    under reach `own` anything but the home workspace's id answers 403 — a domain included, since
    only a fleet rule resolves one.

    A GET of the surface page carrying no credential the rule grants redirects to the rule's
    `sign_in` when it has one, so a link into an operator surface leads to the page that mints a
    bearer rather than dead-ending on `unauthorized`. Any other route answers 401 with the rule's
    `sign_in` (null without one) as JSON, so a page whose session lapsed says where to sign in."""
    rule = installed_operator().rule
    scheme, _, header_token = request.headers.get("authorization", "").partition(" ")
    posted = (await request.form()).get(TOKEN_FIELD, "") if request.method == "POST" else ""
    grant = None
    for candidate in (
        posted if isinstance(posted, str) else "",
        header_token if scheme.lower() == "bearer" else "",
        request.cookies.get(OPERATOR_COOKIE, ""),
    ):
        grant = await _granted(rule, candidate.strip())
        if grant is not None:
            break
    if grant is None:
        if not OPERATOR_PAGE_PATH.fullmatch(request.url.path):
            return JSONResponse({"sign_in": rule.sign_in}, status_code=401)
        if rule.sign_in is not None and request.method == "GET":
            return RedirectResponse(rule.sign_in, status_code=303)
        return None
    request.scope[OPERATOR_GRANT_SCOPE] = grant
    target = request.query_params.get("ws", "").strip()
    if not target:
        return grant.home
    if grant.reach == "fleet":
        return await rule.select(grant, target)
    try:
        named: UUID | None = UUID(target)
    except ValueError:
        named = None
    if named != grant.home:
        return PlainTextResponse(OWN_REACH_REFUSAL, status_code=403)
    return grant.home


def operator_grant(request: Request) -> OperatorGrant:
    """The grant `resolve_operator_workspace` admitted this request under — how an operator surface
    tells a session of its own workspace from a fleet one."""
    match request.scope.get(OPERATOR_GRANT_SCOPE):
        case OperatorGrant() as grant:
            return grant
        case _:
            raise RuntimeError("this request was not admitted by resolve_operator_workspace")


async def bind_operator_session(ctx: "SurfaceContext", request: Request) -> Response:
    """Open a session: land the POSTed bearer as the httponly session cookie and redirect into the
    page. The token crosses only in the form body — never a URL — so access logs and browser
    history hold no credential. It binds only when the rule grants it this request's workspace (its
    home, or any under fleet reach), so a POST the resolver admitted on another credential never
    overwrites a working session with a token that reaches nothing here. The cookie is `lax`, not
    `strict`, because arrival is a cross-site navigation (a sign-in page or the `ufoctl debugger`
    handoff posts here) and the redirected GET must already carry it."""
    posted = (await request.form()).get(TOKEN_FIELD, "")
    if not isinstance(posted, str) or not posted.strip():
        return JSONResponse({"error": "token form field is required"}, status_code=400)
    grant = await _granted(installed_operator().rule, posted.strip())
    if grant is None or (grant.reach != "fleet" and grant.home != ctx.workspace_id):
        return PlainTextResponse("unauthorized", status_code=401)
    response = RedirectResponse(str(request.url), status_code=303)
    set_session_cookie(
        response,
        OPERATOR_COOKIE,
        posted.strip(),
        samesite="lax",
        secure=ctx.cookie_secure,
    )
    return response


async def _granted(rule: OperatorRule, token: str) -> OperatorGrant | None:
    claims = verified_claims(token) if token else None
    if claims is None:
        return None
    claimed_workspace, email = claims
    try:
        workspace_id = UUID(claimed_workspace)
    except ValueError:
        return None
    return await rule.admit(workspace_id, email)


class FleetReachRequired(PermissionError):
    """A fleet read asked under a grant whose reach is one workspace."""


FLEET_THREAD_LIMIT = 50


class FleetWorkspace(BaseModel):
    """One workspace as the operator's index lists it: the domain that addresses it, how much is
    in it, and when it last did anything."""

    workspace_id: UUID
    domain: str | None
    members: int
    conversations: int
    last_turn_at: datetime | None

    @field_validator("last_turn_at")
    @classmethod
    def _aware_utc(cls, value: datetime | None) -> datetime | None:
        return value if value is None or value.tzinfo is not None else value.replace(tzinfo=UTC)


class FleetThread(BaseModel):
    """One recently active conversation, carrying the workspace it belongs to so a click can
    re-scope and open it in the same step."""

    workspace_id: UUID
    domain: str | None
    conversation_id: UUID
    surface: str
    queue_key: str
    title: str | None
    turn_count: int
    last_turn_at: datetime

    @field_validator("last_turn_at")
    @classmethod
    def _aware_utc(cls, value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class FleetListing(BaseModel):
    workspaces: tuple[FleetWorkspace, ...]
    threads: tuple[FleetThread, ...]


@dataclass(frozen=True)
class FleetDirectory:
    """The operator's index of the deploy: every workspace it serves, newest activity first, and
    the most recently active threads across all of them. It reads across every workspace, so only a
    fleet grant opens it: under reach `own` it raises before reading anything.

    Crossing workspaces means `owner_tx`, whose contract admits identifiers and nothing else, so
    the two passes below split on exactly that line: the owner pass reads workspace and
    conversation ids plus the activity timestamps that order them, and every word an operator
    reads — the domain, the surface, the queue key, the title — comes from the scoped pass, re-bound
    under `with ws(...)` and read through RLS like any other workspace read. The scoped pass costs
    one transaction per workspace, which is right for a deploy holding tens of them and wrong for
    one holding thousands.

    Only rooted turns count. A subagent runs in a conversation of its own, so a busy workspace's
    fan-out otherwise fills the index and hides every other workspace's threads behind it —
    measured on the live fleet, 35 of 50 slots. `parent_turn_id is null` separates the two exactly
    (every subagent turn carries a parent, no member-facing turn does), and it is the filter the
    owner pass can apply, holding only identifiers; the conversation count beside it drops the same
    runs by the surface the portal's own listing excludes them by."""

    threads: int = FLEET_THREAD_LIMIT

    async def read(self, grant: OperatorGrant) -> FleetListing:
        if grant.reach != "fleet":
            raise FleetReachRequired(f"reach {grant.reach!r} reads one workspace, not the fleet")
        activity, recent = await self._enumerate()
        wanted: dict[UUID, list[UUID]] = {}
        for row in recent:
            wanted.setdefault(row.workspace_id, []).append(row.conversation_id)
        listed: list[FleetWorkspace] = []
        opened: dict[UUID, sa.Row[Any]] = {}
        for row in activity:
            with ws(row.id):
                workspace, conversations = await self._scoped(
                    row.id, row.last_turn_at, wanted.get(row.id, [])
                )
            listed.append(workspace)
            opened.update(conversations)
        named = {workspace.workspace_id: workspace.domain for workspace in listed}
        return FleetListing(
            workspaces=tuple(listed),
            threads=tuple(
                FleetThread(
                    workspace_id=row.workspace_id,
                    domain=named.get(row.workspace_id),
                    conversation_id=row.conversation_id,
                    surface=opened[row.conversation_id].surface,
                    queue_key=opened[row.conversation_id].queue_key,
                    title=opened[row.conversation_id].title,
                    turn_count=row.turn_count,
                    last_turn_at=row.last_turn_at,
                )
                for row in recent
                if row.conversation_id in opened
            ),
        )

    async def _enumerate(self) -> tuple[Sequence[sa.Row[Any]], Sequence[sa.Row[Any]]]:
        per_workspace = (
            sa.select(
                tables.turn.c.workspace_id,
                sa.func.max(tables.turn.c.updated_at).label("last_turn_at"),
            )
            .where(tables.turn.c.parent_turn_id.is_(None))
            .group_by(tables.turn.c.workspace_id)
            .subquery()
        )
        workspaces = (
            sa.select(tables.workspace.c.id, per_workspace.c.last_turn_at)
            .select_from(
                tables.workspace.outerjoin(
                    per_workspace, per_workspace.c.workspace_id == tables.workspace.c.id
                )
            )
            .order_by(per_workspace.c.last_turn_at.desc().nulls_last(), tables.workspace.c.id)
        )
        recent = (
            sa.select(
                tables.turn.c.workspace_id,
                tables.turn.c.conversation_id,
                sa.func.count().label("turn_count"),
                sa.func.max(tables.turn.c.updated_at).label("last_turn_at"),
            )
            .where(tables.turn.c.parent_turn_id.is_(None))
            .group_by(tables.turn.c.workspace_id, tables.turn.c.conversation_id)
            .order_by(sa.func.max(tables.turn.c.updated_at).desc())
            .limit(self.threads)
        )
        async with owner_tx() as connection:
            return (
                (await connection.execute(workspaces)).all(),
                (await connection.execute(recent)).all(),
            )

    async def _scoped(
        self, workspace_id: UUID, last_turn_at: datetime | None, conversation_ids: Sequence[UUID]
    ) -> tuple[FleetWorkspace, dict[UUID, sa.Row[Any]]]:
        """RLS pins the whole transaction to this workspace, as under the surface's `?ws=`."""
        async with workspace_tx() as connection:
            domain = await seats.workspace_domain(connection, workspace_id)
            members = (
                await connection.execute(
                    sa.select(sa.func.count())
                    .select_from(tables.member)
                    .where(tables.member.c.workspace_id == workspace_id)
                )
            ).scalar_one()
            conversations = (
                await connection.execute(
                    sa.select(sa.func.count())
                    .select_from(tables.conversation)
                    .where(
                        tables.conversation.c.workspace_id == workspace_id,
                        tables.conversation.c.surface != SUBAGENT_SURFACE,
                    )
                )
            ).scalar_one()
            opened = (
                (
                    await connection.execute(
                        sa.select(
                            tables.conversation.c.id,
                            tables.conversation.c.surface,
                            tables.conversation.c.queue_key,
                            tables.conversation.c.title,
                        ).where(tables.conversation.c.id.in_(conversation_ids))
                    )
                ).all()
                if conversation_ids
                else ()
            )
        return (
            FleetWorkspace(
                workspace_id=workspace_id,
                domain=domain,
                members=members,
                conversations=conversations,
                last_turn_at=last_turn_at,
            ),
            {row.id: row for row in opened},
        )
