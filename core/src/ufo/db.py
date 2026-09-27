"""The tenancy boundary: module-private engines, workspace_tx as the only scoped session source.

One serve process serves many workspaces: a request/turn/job sets `current_workspace` at its
boundary, and every statement it runs filters on that workspace with its own `workspace_id`
predicate. On Postgres `workspace_tx` also pins the workspace as the transaction-local GUC
(`app.workspace_id`) a deploy's row-level security policy reads. Pooled-connection reuse never
carries a workspace across checkouts: the GUC is pinned with `set_config(..., is_local=true)` —
`SET LOCAL` — which Postgres clears at transaction end.

`owner_tx` is the one exception: the cross-workspace read the background sweeps enumerate through —
never a scoped read, and the caller re-binds each row under `with ws(...)`.

Engines pool connections per event loop: an asyncpg connection binds to the loop that created it,
and surfaces and DBOS workflows run on different loops in the same process, so each loop lazily
builds and keeps its own engine for the process's life (the pattern `S3BlobStore._client` uses).
"""

import asyncio
import os
import sqlite3
import time
import warnings
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID
from weakref import WeakKeyDictionary

import sqlalchemy as sa
from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

MIGRATIONS_DIR = Path(__file__).parent / "schema" / "migrations"
SQLITE_BUSY_TIMEOUT_MS = 5_000
STATEMENT_LOG_MAX_CHARS = 2_000
WORKSPACE_GUC = "app.workspace_id"
# Every pool is a ceiling one event loop can reach and the fleet's total has to fit.
POOL_SIZE = 5
MAX_OVERFLOW = 5
OWNER_POOL_SIZE = 2
OWNER_MAX_OVERFLOW = 3
POOL_TIMEOUT_SECONDS = 10
POOL_RECYCLE_SECONDS = 1800
CONNECT_TIMEOUT_SECONDS = 10
ASYNCPG_DRIVER = "asyncpg"


@dataclass(frozen=True)
class _Pool:
    """`application_name` is what attributes a connection to its pool in `pg_stat_activity`."""

    application_name: str
    size: int
    overflow: int
    engines: dict[tuple[asyncio.AbstractEventLoop, str], AsyncEngine] = field(default_factory=dict)


_APP = _Pool(application_name="ufo_app", size=POOL_SIZE, overflow=MAX_OVERFLOW)
_OWNER = _Pool(application_name="ufo_owner", size=OWNER_POOL_SIZE, overflow=OWNER_MAX_OVERFLOW)

_app_url: str | None = None
_owner_url: str | None = None
_disposing: dict[asyncio.AbstractEventLoop, set[asyncio.Task[None]]] = {}
_SQLITE_TRANSACTION_LOCKS: WeakKeyDictionary[AsyncEngine, asyncio.Lock] = WeakKeyDictionary()

current_workspace: ContextVar[UUID | None] = ContextVar("current_workspace", default=None)


def _build_engine(url: str, pool: _Pool) -> AsyncEngine:
    """asyncpg's own default dial timeout is 60 seconds, longer than any caller here will wait."""
    engine = create_async_engine(url, **_pool_kwargs(url, pool))
    if engine.dialect.name == "sqlite":
        sa.event.listen(engine.sync_engine, "connect", _sqlite_on_connect)
        sa.event.listen(engine.sync_engine, "begin", _sqlite_begin_immediate)
        sa.event.listen(engine.sync_engine, "commit", _sqlite_end_read_only)
        sa.event.listen(engine.sync_engine, "rollback", _sqlite_end_read_only)
    return engine


def _pool_kwargs(url: str, pool: _Pool) -> dict[str, Any]:
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite":
        return {
            "poolclass": AsyncAdaptedQueuePool,
            "pool_size": pool.size,
            "max_overflow": -1,
            "pool_timeout": POOL_TIMEOUT_SECONDS,
        }
    return {
        "pool_size": pool.size,
        "max_overflow": pool.overflow,
        "pool_timeout": POOL_TIMEOUT_SECONDS,
        "pool_recycle": POOL_RECYCLE_SECONDS,
        "pool_pre_ping": True,
        **_driver_kwargs(parsed.get_driver_name(), pool),
    }


def _driver_kwargs(driver: str, pool: _Pool) -> dict[str, Any]:
    """psycopg rejects `timeout` and `ufoctl proxy`/`ingress` dial psycopg. Plans cached past
    `ufo-migrate` DDL: asyncpg `InvalidCachedStatementError`, psycopg `FeatureNotSupported`."""
    if driver == ASYNCPG_DRIVER:
        return {
            "connect_args": {
                "timeout": CONNECT_TIMEOUT_SECONDS,
                "server_settings": {"application_name": pool.application_name},
                "prepared_statement_cache_size": 0,
            }
        }
    return {
        "connect_args": {
            "connect_timeout": CONNECT_TIMEOUT_SECONDS,
            "application_name": pool.application_name,
            "prepare_threshold": None,
        }
    }


def _engine_for(url: str, pool: _Pool) -> AsyncEngine:
    """`dispose_loop_engines` disposes; this sweep only bounds the registry. Keys are snapshotted
    because the serve, DBOS, and heartbeat loops insert from different threads."""
    key = (asyncio.get_running_loop(), url)
    for stale in [held for held in list(pool.engines) if held[0].is_closed()]:
        pool.engines.pop(stale, None)
    engine = pool.engines.get(key)
    if engine is None:
        engine = pool.engines[key] = _build_engine(url, pool)
    return engine


def init_db(url: str) -> None:
    global _app_url
    if _app_url is not None:
        raise RuntimeError("db already initialized")
    _app_url = url


def init_owner_db(url: str) -> None:
    """The RLS-bypassing owner-role URL `owner_tx` enumerates through (`UFO_OWNER_DSN`, the same
    secret the shared proxy opens). Its role owns the tables and is never FORCEd RLS, so it reads
    across every workspace — the one cross-tenant path. `serve` always sets it; `ufoctl ingress`,
    `ufoctl proxy`, and one-shot verbs set no owner URL, so `owner_tx` falls to the app pool and
    that URL's own role scopes it.

    The driver is normalized here rather than by each caller. A secret store hands this DSN out in
    libpq form (`postgresql://`), which SQLAlchemy resolves to the sync psycopg2 dialect — a banned
    import that is not installed — so an engine built from it raises before any query runs. Doing it
    where the URL is registered leaves no caller holding the unusable form."""
    global _owner_url
    if _owner_url is not None:
        raise RuntimeError("owner db already initialized")
    _owner_url = url.replace("postgresql://", "postgresql+asyncpg://", 1)


async def verify_db_reachable() -> None:
    """Fail loud at boot on a database this process cannot reach. Engines build per loop on first
    touch, so nothing dials until the first request — and `ufo-sandbox-proxy` and `ufo-ingress` are
    TCP-probed rather than `/healthz`-probed (`hosted.yaml.tpl`), so a bound socket in front of an
    unreachable database passes readiness and then fails every request behind it.

    Awaited on the caller's loop rather than driven on a private one. `init_db`'s callers include
    `async def` composition roots, and a blocking dial there stalls every surface that loop carries:
    the proxy registers its SIGTERM handler before it opens the database, so a stalled loop is also
    a loop that cannot be shut down. The engine this dials with is disposed here and never published
    to a registry, so no pooled connection outlives the check."""
    opened = [(url, pool) for url, pool in ((_app_url, _APP), (_owner_url, _OWNER)) if url]
    if not opened:
        raise RuntimeError("db not initialized (init_db runs in the composition root)")
    for url, pool in opened:
        engine = _build_engine(url, pool)
        try:
            async with engine.connect():
                pass
        finally:
            await engine.dispose()


async def dispose_db() -> None:
    """Teardown — a CLI verb's `finally`, a test's fixture. The urls clear first, before anything
    can fail, so a teardown that cannot finish never leaves the next `init_db` refusing, and no
    caller sees an exception: seventeen of them are a bare `finally: await dispose_db()`, where a
    raise would replace whatever drove teardown.

    A connection can only be closed by the loop that opened it. This loop's engines are disposed
    here and awaited. Another loop's are handed to that loop and *not* awaited: waiting on a loop
    this one does not drive is a wait with no end. An entry whose loop is already closed is
    unreachable by anything and only its key goes. Each entry leaves the registry before its
    handoff, so the foreign loop is never runnable with an engine both registered and disposing."""
    global _app_url, _owner_url
    loop = asyncio.get_running_loop()
    _app_url = None
    _owner_url = None
    for pool in (_APP, _OWNER):
        for held in [key for key in list(pool.engines) if key[0] is loop]:
            engine = pool.engines.pop(held, None)
            if engine is not None:
                await engine.dispose()
        for held in list(pool.engines):
            engine = pool.engines.pop(held, None)
            if engine is not None and not held[0].is_closed():
                _hand_off(held[0], engine)


def _hand_off(loop: asyncio.AbstractEventLoop, engine: AsyncEngine) -> None:
    """`call_soon_threadsafe` raises exactly when `loop` closed after the caller looked."""
    try:
        loop.call_soon_threadsafe(_dispose_on_this_loop, engine)
    except RuntimeError:
        return


def _dispose_on_this_loop(engine: AsyncEngine) -> None:
    """`_disposing` keeps the task from being collected before it finishes; awaiting a task from
    another loop raises, so tasks are held per loop."""
    loop = asyncio.get_running_loop()
    for stopped in list(_disposing):
        if stopped.is_closed():
            _disposing.pop(stopped, None)
    pending = _disposing.setdefault(loop, set())
    task = asyncio.ensure_future(engine.dispose())
    pending.add(task)

    def finished(done: asyncio.Task[None]) -> None:
        pending.discard(done)
        if not pending:
            _disposing.pop(loop, None)

    task.add_done_callback(finished)


async def dispose_loop_engines() -> None:
    """Dispose and drop the running loop's engines, keeping the urls initialized. The steps `serve`
    drives on throwaway `asyncio.run` loops call this before their loop closes, so no pooled
    connection is abandoned to a dead loop; the persistent loops keep theirs for the process's
    life."""
    loop = asyncio.get_running_loop()
    for pool in (_APP, _OWNER):
        for held in [key for key in list(pool.engines) if key[0] is loop]:
            engine = pool.engines.pop(held, None)
            if engine is not None:
                await engine.dispose()


def _stopping() -> bool:
    """A cancellation delivered while the driver holds the greenlet surfaces from SQLAlchemy as a
    database error; `cancelling` still counts it until an `uncancel`."""
    task = asyncio.current_task()
    return task is not None and bool(task.cancelling())


async def _await_opening(
    opening: asyncio.Future[AsyncConnection],
) -> tuple[AsyncConnection, asyncio.CancelledError | None]:
    cancelled: asyncio.CancelledError | None = None
    while not opening.done():
        try:
            await asyncio.shield(opening)
        except asyncio.CancelledError as cancel:
            cancelled = cancel
        except Exception:
            break
    return opening.result(), cancelled


async def _await_close(close: asyncio.Future[bool | None]) -> asyncio.CancelledError | None:
    cancelled: asyncio.CancelledError | None = None
    while not close.done():
        try:
            await asyncio.shield(close)
        except asyncio.CancelledError as cancel:
            cancelled = cancel
    close.result()
    return cancelled


@asynccontextmanager
async def _opened(engine: AsyncEngine, path: str) -> AsyncIterator[AsyncConnection]:
    """A pool at its ceiling raises `sqlalchemy.exc.TimeoutError`, named like a lost dial's, so
    saturation carries its own name. `emit_metric` is imported here to break the `o11y` cycle."""
    from ufo.harness.o11y import emit_histogram, emit_metric

    lock = None
    if engine.dialect.name == "sqlite" and not engine.get_execution_options().get("ufo_read_only"):
        lock = _SQLITE_TRANSACTION_LOCKS.get(engine)
        if lock is None:
            lock = _SQLITE_TRANSACTION_LOCKS[engine] = asyncio.Lock()
    locked = False
    started = time.monotonic()
    try:
        try:
            if lock is not None:
                await lock.acquire()
                locked = True
            stack = AsyncExitStack()
            opening = asyncio.ensure_future(stack.enter_async_context(engine.begin()))
            try:
                connection, cancelled = await _await_opening(opening)
            except Exception as error:
                if isinstance(error, sa.exc.TimeoutError):
                    emit_metric("db_pool_exhausted_total", path=path)
                emit_metric("db_tx_unavailable_total", path=path, error_class=type(error).__name__)
                if _stopping():
                    raise asyncio.CancelledError from error
                raise
        finally:
            elapsed = round((time.monotonic() - started) * 1000)
            emit_histogram("db_tx_acquire_ms", elapsed, path=path)
        caught: BaseException | None = cancelled
        if caught is None:
            try:
                yield connection
            except BaseException as error:
                caught = error
        close = asyncio.ensure_future(
            stack.__aexit__(type(caught), caught, caught.__traceback__)
            if caught is not None
            else stack.__aexit__(None, None, None)
        )
        cancelled = await _await_close(close)
        if cancelled is not None:
            raise cancelled
        if caught is not None:
            if _stopping() and not isinstance(caught, asyncio.CancelledError):
                raise asyncio.CancelledError from caught
            raise caught
    finally:
        if locked and lock is not None:
            lock.release()


@asynccontextmanager
async def workspace_tx(
    *, snapshot: bool = False, read_only: bool = False
) -> AsyncIterator[AsyncConnection]:
    """`snapshot=True` reads every statement of the transaction from one snapshot, for a caller
    assembling several reads into one picture of a row's state: under the default read committed
    each statement takes its own, so a concurrent commit can land between two of them and be half
    visible. The level is chosen at BEGIN because the workspace GUC below is a query, and Postgres
    refuses SET TRANSACTION after one. Read-only SQLite snapshots use a deferred transaction,
    so WAL readers do not hold the writer slot. Other SQLite transactions acquire that slot
    before reading."""
    if _app_url is None:
        raise RuntimeError("db not initialized (init_db runs in the composition root)")
    engine = _engine_for(_app_url, _APP)
    if snapshot and engine.dialect.name == "postgresql":
        engine = engine.execution_options(isolation_level="REPEATABLE READ")
    if read_only:
        engine = engine.execution_options(ufo_read_only=True)
    async with _opened(engine, "workspace") as connection:
        if read_only and connection.dialect.name == "postgresql":
            await connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        workspace_id = current_workspace.get()
        if workspace_id is not None and connection.dialect.name == "postgresql":
            await connection.execute(
                sa.text("select set_config(:guc, :ws, true)"),
                {"guc": WORKSPACE_GUC, "ws": str(workspace_id)},
            )
        yield connection


def failed_statement(error: BaseException) -> dict[str, str]:
    """The driver's own account of a refused statement, as log fields: the SQL it refused and the
    SQLSTATE it answered with. Empty for anything that is not a database error.

    A `ProgrammingError` says only that the statement was refused — an unknown column or table, a
    parameter the query cannot bind — and which one it was is in the statement and the code. A
    failure log carrying neither names no defect at all: the schema the process met has moved on
    by the time anyone reads the line, so both fields are taken here, where the driver holds them.

    The bound parameters stay out, and so does the message the database returned beside them. The
    statement text is this repo's own SQL; the values bound into it are a workspace's rows, and a
    driver message quotes them back — `formatted_stack` withholds a message for the same reason."""
    if not isinstance(error, sa.exc.DBAPIError):
        return {}
    fields = {}
    if error.statement:
        fields["statement"] = error.statement[:STATEMENT_LOG_MAX_CHARS]
    sqlstate = getattr(error.orig, "sqlstate", None) or getattr(error.orig, "pgcode", None)
    if isinstance(sqlstate, str):
        fields["sqlstate"] = sqlstate
    return fields


@asynccontextmanager
async def owner_tx() -> AsyncIterator[AsyncConnection]:
    """The one cross-workspace read path: a transaction that pins NO workspace GUC, so it enumerates
    every workspace this deploy serves. The background sweeps find their work across workspaces
    through it, then re-scope each unit under `with ws(row.workspace_id)`. With an owner URL set
    (`serve`) it bypasses RLS through the owner role; without one it falls to the app pool — the
    same engine `workspace_tx` resolves, never a second pool for one URL — and that URL's own role
    scopes it. It threads no workspace and sets no GUC, so nothing it yields is a tenant boundary:
    never read a row's contents through it beyond the identifiers needed to re-bind that row's own
    workspace."""
    url, pool = (_app_url, _APP) if _owner_url is None else (_owner_url, _OWNER)
    if url is None:
        raise RuntimeError("db not initialized (init_db runs in the composition root)")
    async with _opened(_engine_for(url, pool), "owner") as connection:
        yield connection


def apply_migrations(url: str, pack: str | None = None) -> None:
    """Alembic owns the schema; runs off the loop (CLI startup, test fixtures). Core's version
    location and every active extension's are layered into one run, so `upgrade heads` brings the
    deploy to core's head plus each pinned extension's — one head per owner, each extension ordered
    after core by the `depends_on` its base declares. With `pack` set the active set narrows to that
    pack's bundle, so only its extensions' tables are created. The loader import is local to break
    the db↔loader↔context cycle (the loader reaches core through the same context that binds to this
    module).

    Two files claiming one revision id are one graph node, and a location with two heads is a fork
    that would stamp both and wedge every migrate after the fork is linearized — so the graph is
    validated before any DDL runs."""
    from ufo.host.ext.loader import migration_locations

    config = AlembicConfig()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    locations = (str(MIGRATIONS_DIR / "versions"), *migration_locations(pack))
    config.set_main_option("version_locations", os.pathsep.join(locations))
    config.set_main_option("path_separator", "os")
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    scripts = ScriptDirectory.from_config(config)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", UserWarning)
            heads = scripts.get_heads()
    except UserWarning as error:
        raise RuntimeError("a duplicate migration revision collapses into one node") from error
    heads_by_location: dict[Path, list[str]] = {}
    for head in heads:
        heads_by_location.setdefault(Path(scripts.get_revision(head).path).parent, []).append(head)
    for location, revisions in heads_by_location.items():
        if len(revisions) > 1:
            raise RuntimeError(
                f"migration graph forked: {location} has heads {sorted(revisions)}; "
                "repoint down_revision at the branch head"
            )
    command.upgrade(config, "heads")
    if url.startswith("sqlite"):
        _seal_sqlite_journal(url)


def _seal_sqlite_journal(url: str) -> None:
    """Alembic's own engine never runs `_sqlite_on_connect`, and a later journal-mode conversion
    needs an exclusive lock it cannot wait out (`database is locked`), so it converts here once."""
    database = make_url(url).database
    if database is None:
        raise RuntimeError(f"sqlite url names no file: {url}")
    with sqlite3.connect(database) as connection:
        connection.execute("pragma journal_mode=wal")
    connection.close()


def core_migration_head() -> str:
    """Core's single head — the revision a new core migration chains onto. Core's version location
    alone, so an extension's branch head is never what comes back."""
    config = AlembicConfig()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    head = ScriptDirectory.from_config(config).get_current_head()
    if head is None:
        raise RuntimeError("core's migration graph has no head")
    return head


def _sqlite_on_connect(dbapi_connection: Any, _connection_record: Any) -> None:
    dbapi_connection.isolation_level = None
    cursor = dbapi_connection.cursor()
    cursor.execute("pragma journal_mode=wal")
    cursor.execute("pragma foreign_keys=on")
    cursor.execute(f"pragma busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    cursor.close()


def _sqlite_begin_immediate(connection: sa.Connection) -> None:
    """Claim the single writer slot up front: lock-upgrade deadlocks become queueing."""
    if connection.get_execution_options().get("ufo_read_only"):
        connection.exec_driver_sql("pragma query_only=on")
        connection.exec_driver_sql("begin")
    else:
        connection.exec_driver_sql("begin immediate")


def _sqlite_end_read_only(connection: sa.Connection) -> None:
    if connection.get_execution_options().get("ufo_read_only"):
        connection.exec_driver_sql("pragma query_only=off")
