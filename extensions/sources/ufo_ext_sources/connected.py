"""A connection's feeds are created with the connection: the registrar the `connection_recorded`
hook fires, and the job that retries a creation which did not land.

A connection is one account's authority and a feed is one `source` row per syncing stream of its
connector, so connecting an account is the whole of what a member does to sync it. Canonical
streams are the provider's core collections — the ones a connector marks as the objects it exists
to carry, one to nine per provider — and a stream one of those hangs under syncs with them, because
its landed records are the partitions the canonical stream fans over. So the feed is what the
account is for, plus what reaching it costs, rather than every list its API publishes. Which agents
read what it syncs is the grant's answer at read time, so this asks nothing about grants and nothing
about disclosure: the connection carries both.

A provider no broker grants is connected the same way from the other end: a member fills its
credential slots, and the job mints the workspace's own connection to it — no account handle, and
nobody's to keep private — so the one act of adding the keys starts the feed, and the one act of
clearing them ends it: the next tick removes that connection, and its streams, their pages and its
grants go with it. Which slots those are is the connector's own declaration, one per header where a
provider authenticates with several keys, and a pair half filled is neither act. A provider already
holding a connection keeps it, whoever made it.

The job is the retry path for everything else, never the producer: a hook that raised, or a process
that died between the connection and its rows, leaves streams uncreated, and the next tick creates
exactly those. It is also how a per-tenant provider's rows arrive — a connector that declares no
host of its own dials the connection's `base_url`, and a connection that carries none registers
nothing until the member names it, which the next tick reads. A connector that dials no host at all
reads through broker tool executions instead, so its empty `base_url` is its whole address and its
rows land with the connection like a fixed-host provider's.

Nothing marks a connection done, because the rows are the record in both directions: a stream a
later connector release marks canonical reaches accounts that already sync, and a stream a release
stops marking canonical leaves them. Each stream's first sync reaches back as far as
the connection's `backfill_days` asks, and where it asks nothing, as far as the stream declares.
Raising that window re-pins the rows it now reaches further back and refetches them; lowering it
leaves them where they are, because the pages between the two floors would otherwise be stranded —
never re-walked, never tombstoned."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from ufo.sdk.context import ExtensionContext, SourceRecord
from ufo.sdk.grants import ConnectionRecorded, FeedConnection, feed_connections
from ufo.sdk.manifest import HookContext, HookOutcome
from ufo.sdk.sources import ConnectorSourceConfig, StreamSpec, syncing_streams
from ufo_ext_sources.registry import CONNECTORS, direct_slots


async def on_connection_recorded(ctx: HookContext) -> HookOutcome:
    """Give the connection the member just made its feeds, before the callback answers them."""
    match ctx.payload:
        case ConnectionRecorded(connection_id=connection_id):
            await ConnectedSources(ext=ctx.ext).register(connection_id=connection_id)
        case _:
            raise RuntimeError("sources hook fired on a non-connection_recorded payload")
    return None


async def retry_connected_sources(ctx: ExtensionContext) -> None:
    """Create the rows a connect-time creation did not. See the module docstring."""
    await ConnectedSources(ext=ctx).register()


def backfill_days(connection: FeedConnection, stream: StreamSpec) -> int | None:
    """How far back one row's first sync reaches: the connection's window where it names one, else
    the window the stream declares. A stream declaring none reads its whole history and takes no
    cutoff at all, which is what None on the row means."""
    if stream.backfill_window_days is None:
        return None
    if connection.backfill_days is None:
        return stream.backfill_window_days
    return connection.backfill_days


@dataclass(frozen=True)
class ConnectedSources:
    """Register the syncing streams of a connection — one connection for the hook that fires as it
    lands, every one of them for the job that retries. See the module docstring."""

    ext: ExtensionContext

    async def register(self, connection_id: UUID | None = None) -> None:
        if connection_id is None:
            await self._settle_keyed_connections()
        live = await self.ext.sources()
        for connection in await feed_connections():
            if connection_id is not None and connection.id != connection_id:
                continue
            connector_cls = CONNECTORS.get(connection.provider)
            if connector_cls is None:
                continue
            if connector_cls.dials_host and not (connector_cls.base_url or connection.base_url):
                continue
            streams = {stream.name: stream for stream in connector_cls().streams()}
            await self._retire(connection, streams, live)
            await self._create(connection, streams, live)
            await self._rewindow(connection, streams, live)

    async def _settle_keyed_connections(self) -> None:
        connections = await feed_connections()
        stored = await self.ext.credentials.stored_slots()
        keyed: set[str] = set()
        keyless: set[str] = set()
        for provider, connector in CONNECTORS.items():
            slots = direct_slots(connector)
            filled = sum(slot in stored for slot in slots)
            if filled == len(slots):
                keyed.add(provider)
            elif filled == 0:
                keyless.add(provider)
        for connection in connections:
            if (
                connection.owner_member_id is None
                and connection.account_id == ""
                and connection.provider in keyless
            ):
                await self.ext.remove_connection(connection.id)
        connected = {connection.provider for connection in connections}
        for provider in sorted((CONNECTORS.keys() - connected) & keyed):
            await self.ext.register_connection(provider)

    async def _retire(
        self,
        connection: FeedConnection,
        streams: dict[str, StreamSpec],
        live: tuple[SourceRecord, ...],
    ) -> None:
        """Removed rather than parked: an inert row would be found by the next registration of that
        stream and read as a feed the member asked for."""
        syncing = syncing_streams(list(streams.values()))
        for record in live:
            if record.connection_id != connection.id or record.backend != connection.provider:
                continue
            stream = record.config.get("stream")
            if not isinstance(stream, str) or stream in syncing:
                continue
            await self.ext.remove_source(record.id)

    async def _create(
        self,
        connection: FeedConnection,
        streams: dict[str, StreamSpec],
        live: tuple[SourceRecord, ...],
    ) -> None:
        held = {record.config["stream"] for record in live if record.connection_id == connection.id}
        syncing = syncing_streams(list(streams.values()))
        registered_at = datetime.now(UTC)
        for stream in streams.values():
            if stream.name not in syncing or stream.name in held:
                continue
            days = backfill_days(connection, stream)
            config = ConnectorSourceConfig(
                stream=stream.name,
                backfill_days=days,
                backfill_after=None if days is None else registered_at - timedelta(days=days),
            )
            await self.ext.register_source(connection.provider, config, connection_id=connection.id)

    async def _rewindow(
        self,
        connection: FeedConnection,
        streams: dict[str, StreamSpec],
        live: tuple[SourceRecord, ...],
    ) -> None:
        """The floor is measured from each row's registration (pin plus pinned days); measured from
        `now`, widening an old connection would pin a later floor."""
        configs: dict[UUID, ConnectorSourceConfig] = {}
        for record in live:
            if record.connection_id != connection.id:
                continue
            config = ConnectorSourceConfig.model_validate(record.config)
            stream = streams.get(config.stream)
            if stream is None or config.backfill_after is None:
                continue
            pinned = (
                stream.backfill_window_days
                if config.backfill_days is None
                else config.backfill_days
            )
            if pinned is None:
                raise RuntimeError(f"source {record.id} is pinned to a window it does not name")
            days = backfill_days(connection, stream)
            anchor = config.backfill_after + timedelta(days=pinned)
            pin = None if days is None else anchor - timedelta(days=days)
            if pin is not None and pin >= config.backfill_after:
                continue
            configs[record.id] = ConnectorSourceConfig(
                stream=config.stream, backfill_days=days, backfill_after=pin
            )
        await self.ext.rewindow_sources(configs, refetch=frozenset(configs))
