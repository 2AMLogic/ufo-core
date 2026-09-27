import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from ufo.sdk.context import ExtensionContext, JsonValue
from ufo.sdk.objects import (
    AdminRequired,
    ObjectActionRequestTarget,
    ObjectActionTarget,
    ObjectDetail,
    ObjectListQuery,
    ObjectPage,
    ObjectRow,
    VerbNotSupported,
    object_page,
)
from ufo.sdk.surfaces import AskQuestion, AskUserInput
from ufo.sdk.tools import TextContent, ToolContext, ToolResult

WIDGET_KIND = "sample_widget"
WIDGET_KEY_PREFIX = "object:widget:"
AUDIT_ACTION = "audit"
POLISH_ACTION = "polish"
ENGRAVE_ACTION = "engrave"
DIVINE_ACTION = "divine"
CALIBRATE_ACTION = "calibrate"
BLESS_ACTION = "bless"
BESEECH_ACTION = "beseech"
BLESS_CANONICAL_ID = f"action:{WIDGET_KIND}:{BLESS_ACTION}"
AUDIT_KEY = "action:audit"
POLISH_KEY = "action:polish"
ENGRAVE_KEY = "action:engrave"
ENGRAVE_PRESENTATION_LABEL = "Engrave"
ENGRAVE_PRESENTATION_CONFIRM = "Engrave this widget?"
DIVINE_KEY = "action:divine"
CALIBRATE_KEY = "action:calibrate"
BLESS_KEY = "action:bless"
BESEECH_KEY = "action:beseech"
BESEECH_DIRECTIVE = "ask the member privately"
BESEECH_TITLE = "The probe widgets need a decision."
BLESS_FAILURE = "the sample bless action was asked to fail"
DIVINATION = "the divination speaks in an untrusted voice"
RELIC_KIND = "sample_relic"
RELIC_NAME = "meteor-shard"
RELIC_INSCRIPTION = "the sample relic is excavated, never authored"
RELIC_REFUSAL = "sample relics are read-only — they are excavated, never applied or deleted"
WIDGET_DELETE_GATE = "only a workspace admin can delete a sample widget"
WIDGET_GUIDANCE = (
    "Apply a color and size to create or update a probe widget; any member may write, and "
    "delete requires a workspace admin."
)
RELIC_GUIDANCE = (
    "Read-only probes of the object surface: list and get them, but every mutation is "
    "refused — relics are excavated, never authored."
)


class WidgetSpec(BaseModel):
    """The probe kind's authored spec — `extra="forbid"` as the registration gate requires."""

    model_config = ConfigDict(extra="forbid")
    color: str
    size: int = 1


class RelicSpec(BaseModel):
    """The read-only probe kind's spec: system-produced, never authored."""

    model_config = ConfigDict(extra="forbid")
    inscription: str


class StoredWidget(BaseModel):
    """A widget's persisted row: the applied spec, the timestamps the envelope renders, and the
    generation replaced on every apply — the fence the verbs and instance actions check. Crosses
    the `ext_store` boundary, so it validates on the way back out."""

    model_config = ConfigDict(extra="forbid")
    spec: WidgetSpec
    created_at: datetime
    updated_at: datetime
    generation: UUID


@dataclass(frozen=True)
class WidgetStore:
    """The full-CRUD probe store over the sample's own `ext_store` keys: apply/get/delete round a
    spec through `WIDGET_KEY_PREFIX` rows, list pages by keyset over the store's key order, and
    delete gates on a workspace admin — so the conformance tests drive create, update, paging,
    admin refusal, and delete through the real verbs and read back through this public store.
    Every apply replaces the row's generation, and each fenced verb refuses once the row under
    the name is not the one its read observed."""

    async def list(self, ctx: ToolContext, query: ObjectListQuery) -> ObjectPage:
        entries = await self._ext(ctx).store.list(WIDGET_KEY_PREFIX)
        rows = tuple(
            ObjectRow(
                name=name,
                summary=f"a {name} widget",
                fields=StoredWidget.model_validate(value).spec.model_dump(mode="json"),
            )
            for key, value in entries
            if (name := key.removeprefix(WIDGET_KEY_PREFIX))
        )
        return object_page(rows, query)

    async def get(self, ctx: ToolContext, name: str) -> ObjectDetail[WidgetSpec] | None:
        value = await self._ext(ctx).store.get(WIDGET_KEY_PREFIX + name)
        if value is None:
            return None
        stored = StoredWidget.model_validate(value)
        return ObjectDetail(
            spec=stored.spec,
            created_at=stored.created_at,
            updated_at=stored.updated_at,
            generation=stored.generation,
        )

    async def status(
        self,
        ctx: ToolContext,
        name: str,
        *,
        expected_generation: UUID | None,
    ) -> None:
        value = await self._ext(ctx).store.get(WIDGET_KEY_PREFIX + name)
        if value is None:
            return None
        self._require_current(name, StoredWidget.model_validate(value), expected_generation)
        return None

    async def apply(
        self,
        ctx: ToolContext,
        name: str,
        spec: WidgetSpec,
        old: WidgetSpec | None,
        *,
        expected_generation: UUID | None,
    ) -> None:
        ext = self._ext(ctx)
        value = await ext.store.get(WIDGET_KEY_PREFIX + name)
        now = datetime.now(UTC)
        if value is None:
            if expected_generation is not None:
                raise ValueError(f"sample widget {name!r} changed while editing")
            created_at = now
        else:
            stored = StoredWidget.model_validate(value)
            self._require_current(name, stored, expected_generation)
            created_at = stored.created_at
        await ext.store.put(
            WIDGET_KEY_PREFIX + name,
            StoredWidget(
                spec=spec, created_at=created_at, updated_at=now, generation=uuid4()
            ).model_dump(mode="json"),
        )

    async def delete(
        self,
        ctx: ToolContext,
        name: str,
        *,
        expected_generation: UUID | None,
    ) -> None:
        ext = self._ext(ctx)
        value = await ext.store.get(WIDGET_KEY_PREFIX + name)
        if value is not None:
            self._require_current(name, StoredWidget.model_validate(value), expected_generation)
        if not await ctx.require_speaking_admin(WIDGET_DELETE_GATE):
            raise AdminRequired(WIDGET_DELETE_GATE)
        await ext.store.delete(WIDGET_KEY_PREFIX + name)

    def _require_current(
        self, name: str, stored: StoredWidget, expected_generation: UUID | None
    ) -> None:
        if stored.generation != expected_generation:
            raise ValueError(f"sample widget {name!r} changed while editing")

    def _ext(self, ctx: ToolContext) -> ExtensionContext:
        if ctx.ext is None:
            raise RuntimeError("sample widget store dispatched without its ExtensionContext")
        return ctx.ext


@dataclass(frozen=True)
class RelicStore:
    """The read-only probe store: one canned instance with live status, every mutation refused —
    the shape a system-produced kind (pages, conversations) takes."""

    async def list(self, ctx: ToolContext, query: ObjectListQuery) -> ObjectPage:
        return object_page(
            (ObjectRow(name=RELIC_NAME, summary=RELIC_INSCRIPTION),),
            query,
        )

    async def get(self, ctx: ToolContext, name: str) -> ObjectDetail[RelicSpec] | None:
        if name != RELIC_NAME:
            return None
        return ObjectDetail(
            spec=RelicSpec(inscription=RELIC_INSCRIPTION), created_at=None, updated_at=None
        )

    async def status(
        self,
        ctx: ToolContext,
        name: str,
        *,
        expected_generation: UUID | None,
    ) -> dict[str, JsonValue] | None:
        return {"origin": "excavated"}

    async def apply(
        self,
        ctx: ToolContext,
        name: str,
        spec: RelicSpec,
        old: RelicSpec | None,
        *,
        expected_generation: UUID | None,
    ) -> None:
        raise VerbNotSupported(RELIC_REFUSAL)

    async def delete(
        self,
        ctx: ToolContext,
        name: str,
        *,
        expected_generation: UUID | None,
    ) -> None:
        raise VerbNotSupported(RELIC_REFUSAL)


class AuditInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str = ""


class PolishInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    coats: int = 1


class EngraveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    interrupt_once: bool = False


class DivineInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = ""


class CalibrateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    offset: int = 0


class BlessInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phrase: str = ""
    fail: bool = False


class BeseechInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = "Which widget?"


def target_record(
    target: ObjectActionRequestTarget | ObjectActionTarget | None,
) -> dict[str, JsonValue] | None:
    match target:
        case None:
            return None
        case ObjectActionRequestTarget():
            return {
                "kind": target.kind,
                "name": target.name,
                "agent": target.agent,
                "generation": None,
                "expected_generation": (
                    None if target.expected_generation is None else str(target.expected_generation)
                ),
            }
        case ObjectActionTarget():
            return {
                "kind": target.kind,
                "name": target.name,
                "agent": None if target.agent is None else target.agent.name,
                "generation": None if target.generation is None else str(target.generation),
                "expected_generation": (
                    None if target.expected_generation is None else str(target.expected_generation)
                ),
            }


def _action_ext(ctx: ToolContext) -> ExtensionContext:
    if ctx.ext is None:
        raise RuntimeError("sample action dispatched without its ExtensionContext")
    return ctx.ext


async def audit(ctx: ToolContext, args: AuditInput) -> ToolResult:
    ext = _action_ext(ctx)
    await ext.store.put(
        AUDIT_KEY,
        {
            "subject": args.subject,
            "extension": ext.store.extension,
            "target": target_record(ctx.target),
        },
    )
    return ToolResult(content=(TextContent(text=f"audited {args.subject or 'the workspace'}"),))


async def polish(ctx: ToolContext, args: PolishInput) -> ToolResult:
    ext = _action_ext(ctx)
    await ext.store.put(POLISH_KEY, {"coats": args.coats, "target": target_record(ctx.target)})
    return ToolResult(content=(TextContent(text=f"polished with {args.coats} coats"),))


async def engrave(ctx: ToolContext, args: EngraveInput) -> ToolResult:
    ext = _action_ext(ctx)
    target = ctx.target
    if target is None or target.name is None:
        raise RuntimeError("sample engrave dispatched without an instance target")
    match await ext.store.get(ENGRAVE_KEY):
        case {"idempotency_key": prior_key} if prior_key == ctx.idempotency_key:
            return ToolResult(content=(TextContent(text=f"engraved {args.text!r}"),))
    if target.expected_generation is not None and target.expected_generation != target.generation:
        raise ValueError(f"sample widget {target.name!r} changed after your read")
    key = WIDGET_KEY_PREFIX + target.name
    value = await ext.store.get(key)
    if value is None:
        raise RuntimeError(f"sample widget {target.name!r} vanished after its target read")
    stored = StoredWidget.model_validate(value)
    minted = uuid4()
    await ext.store.put(
        key,
        StoredWidget(
            spec=stored.spec,
            created_at=stored.created_at,
            updated_at=datetime.now(UTC),
            generation=minted,
        ).model_dump(mode="json"),
    )
    await ext.store.put(
        ENGRAVE_KEY,
        {
            "text": args.text,
            "idempotency_key": ctx.idempotency_key,
            "target": target_record(target),
            "minted": str(minted),
        },
    )
    if args.interrupt_once:
        raise asyncio.CancelledError
    return ToolResult(content=(TextContent(text=f"engraved {args.text!r}"),))


async def divine(ctx: ToolContext, args: DivineInput) -> ToolResult:
    ext = _action_ext(ctx)
    await ext.store.put(DIVINE_KEY, {"query": args.query, "target": target_record(ctx.target)})
    return ToolResult(content=(TextContent(text=DIVINATION),))


async def calibrate(ctx: ToolContext, args: CalibrateInput) -> ToolResult:
    ext = _action_ext(ctx)
    await ext.store.put(CALIBRATE_KEY, {"offset": args.offset, "target": target_record(ctx.target)})
    return ToolResult(content=(TextContent(text=f"calibrated by {args.offset}"),))


async def bless(ctx: ToolContext, args: BlessInput) -> ToolResult:
    if args.fail:
        raise ValueError(BLESS_FAILURE)
    ext = _action_ext(ctx)
    await ext.store.put(BLESS_KEY, {"phrase": args.phrase, "target": target_record(ctx.target)})
    return ToolResult(content=(TextContent(text=f"blessed with {args.phrase!r}"),))


async def beseech(ctx: ToolContext, args: BeseechInput) -> ToolResult:
    ext = _action_ext(ctx)
    await ext.store.put(
        BESEECH_KEY, {"question": args.question, "target": target_record(ctx.target)}
    )
    payload = AskUserInput(title=BESEECH_TITLE, questions=(AskQuestion(question=args.question),))
    return ToolResult(
        content=(TextContent(text=f"{BESEECH_DIRECTIVE}\n{payload.model_dump_json()}"),)
    )
