from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from uuid import UUID

from pydantic import BaseModel

from ufo.sdk.authproxy import Credential
from ufo.sdk.connectors import (
    BrokerFile,
    BrokerSearch,
    BrokerTool,
    OAuthAccount,
    StagedUpload,
    UnknownBrokerTool,
)
from ufo.sdk.sandbox import WORKSPACE_DIR
from ufo.sdk.tools import TextContent, ToolContext, ToolResult

CONNECTOR_PROVIDER = "sample_connector"
CONNECTOR_LABEL = "Sample Connector"
CONNECTOR_HOST = "api.connector.sample.test"
CONNECTOR_ACCOUNT = "sample-account-1"
CONNECTOR_AUTHORIZE_URL = "https://connect.sample.test/oauth"
CONNECTOR_EXECUTE_TOOL_NAME = "sample_connector_execute"
BROKER_TOOL_SLUG = "SAMPLE_LIST_WIDGETS"
BROKER_TOOL_DESCRIPTION = "List the sample provider's widgets."
BROKER_SEARCH_PLAN = "call SAMPLE_LIST_WIDGETS first"
BROKER_UPLOAD_PREFIX = "connector_upload"
BROKER_BEARER_PREFIX = "sample-broker-token:"
CONNECTOR_EXECUTE_KEY = "connector:executed"


@dataclass(frozen=True)
class SampleConnectorOAuth:
    """The stub connector's OAuth descriptor: a canned authorize URL and a canned account exchange
    stand in for a real provider's handoff, so the connect seam runs end to end without a live
    provider. `host` is the one the derived grant admits and meters at the egress proxy. The broker
    holds the account's token, so the exchange yields only the connected-account id — no secret."""

    provider: str = CONNECTOR_PROVIDER
    host: str = CONNECTOR_HOST

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        return f"{CONNECTOR_AUTHORIZE_URL}?state={state}&redirect_uri={redirect_uri}"

    async def exchange(
        self, code: str, redirect_uri: str, workspace_id: UUID, state: str
    ) -> OAuthAccount:
        return OAuthAccount(account_id=CONNECTOR_ACCOUNT)


@dataclass(frozen=True)
class SampleBroker:
    """The stub broker: a one-tool canned catalog, an execute that echoes its whole call back as
    the provider response, a workspace-backed file store staging uploads and projecting produced
    files, and a bearer credential naming the account — so a test asserting the dynamic connector
    tools, the file bridge, or feed-sync routing reads exactly what core dispatched through the
    seam, off public surfaces, with no live broker."""

    _minted_uploads: set[str] = field(default_factory=set)

    async def tools(self, workspace_id: UUID, provider: str, query: str) -> tuple[BrokerTool, ...]:
        return (BrokerTool(slug=BROKER_TOOL_SLUG, description=BROKER_TOOL_DESCRIPTION),)

    async def schema(self, workspace_id: UUID, provider: str, slug: str) -> BrokerTool:
        if slug != BROKER_TOOL_SLUG:
            raise UnknownBrokerTool(slug)
        return BrokerTool(
            slug=slug,
            description=BROKER_TOOL_DESCRIPTION,
            input_schema={"type": "object", "properties": {"limit": {"type": "integer"}}},
        )

    async def execute(
        self,
        workspace_id: UUID,
        provider: str,
        slug: str,
        arguments: Mapping[str, object],
        account_id: str,
        idempotency_key: str | None,
    ) -> dict[str, object]:
        if slug != BROKER_TOOL_SLUG:
            raise UnknownBrokerTool(slug)
        return {
            "provider": provider,
            "slug": slug,
            "arguments": dict(arguments),
            "account": account_id,
            "idempotency_key": idempotency_key,
        }

    def file_outputs(self, response: dict[str, object]) -> tuple[BrokerFile, ...]:
        """The echoed `file_output_urls` argument, projected as produced files — a test drives the
        dynamic tools' workspace fetch with URLs it controls, through the seam's own vocabulary."""
        arguments = response.get("arguments")
        urls = arguments.get("file_output_urls") if isinstance(arguments, dict) else None
        if not isinstance(urls, list):
            return ()
        return tuple(
            BrokerFile(name=PurePosixPath(url).name, url=url)
            for url in urls
            if isinstance(url, str)
        )

    async def stage_upload(
        self,
        workspace_id: UUID,
        provider: str,
        slug: str,
        filename: str,
        mimetype: str,
        md5: str,
    ) -> StagedUpload:
        """The sample file store: a content-addressed object in the workspace the sandbox PUTs the
        bytes to, echoed back through `execute` as the tool argument — so a test drives the whole
        upload leg (mint slot, sandbox PUT, argument naming the object) through the real seam, off
        the workspace, with no live store. Re-staging an already-minted key answers a dedup hit
        (no put_url), the store-index behavior behind Composio's `type: "exists"`."""
        key = f"{BROKER_UPLOAD_PREFIX}-{md5}-{filename}"
        if key in self._minted_uploads:
            return StagedUpload(
                put_url=None,
                content_type=mimetype,
                argument={"name": filename, "mimetype": mimetype, "s3key": key},
            )
        self._minted_uploads.add(key)
        return StagedUpload(
            put_url=f"file://{WORKSPACE_DIR}/{key}",
            content_type=mimetype,
            argument={"name": filename, "mimetype": mimetype, "s3key": key},
        )

    async def search(self, workspace_id: UUID, provider: str, query: str) -> BrokerSearch:
        return BrokerSearch(
            tools=await self.tools(workspace_id, provider, query), plan=(BROKER_SEARCH_PLAN,)
        )

    async def credential(self, workspace_id: UUID, provider: str, account: str) -> Credential:
        return Credential(bearer=f"{BROKER_BEARER_PREFIX}{account}")


class ConnectorExecuteInput(BaseModel):
    tool_name: str = "sample_list"


async def connector_execute(ctx: ToolContext, args: ConnectorExecuteInput) -> ToolResult:
    """The stub connector's server-side execute path: it resolves the turn-agent's bound
    connected-account id through `connector_account` — the broker's account id a server-side
    execution API takes, holding the token itself — and records it, with the per-call
    `idempotency_key` core folds onto a side-effecting tool, through the extension's scoped store so
    both seams are read back through a public surface. An agent with no grant for the provider fails
    loud here, before any execution."""
    if ctx.ext is None:
        raise RuntimeError("sample connector tool dispatched without its ExtensionContext")
    account = await ctx.connector_account(CONNECTOR_PROVIDER)
    await ctx.ext.store.put(
        CONNECTOR_EXECUTE_KEY,
        {
            "account": account,
            "tool_name": args.tool_name,
            "idempotency_key": ctx.idempotency_key,
        },
    )
    return ToolResult(content=(TextContent(text=account),))
