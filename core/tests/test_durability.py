import ast
import base64
import importlib
import io
import pickle
import sys
import types
from collections.abc import Iterator
from dataclasses import make_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import get_args
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError, create_model
from ufo_ext_context_compact.compaction import _CompactionRequest
from ufo_ext_skill_create.manifest import UserSkillSpec

from ufo.harness import durability
from ufo.harness.durability import MOVED_MODULES, ReplaySafeSerializer
from ufo.host.kinds.members import add_member_action
from ufo.runtime import engine as engine_module
from ufo.runtime.engine import Arrival, DispatchResult, _AuthorizationPreflight
from ufo.runtime.seats import MemberAdded, MemberAddedSpec
from ufo.runtime.tools.registry import TRUSTED_TOOL_INPUT
from ufo.schema.records import MEMBER_ADMISSION

SERIALIZER = ReplaySafeSerializer()

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOTS = ("core/src", "extensions")
RECORDED_346716E = Path(__file__).parent / "fixtures" / "dbos_346716e"
RECORDED_ADD_MEMBER_PREFLIGHTS = (
    RECORDED_346716E / "add_member" / "_preflight_member_authorizations.b64"
)
STEP_DECORATOR = "@DBOS.step"
RECORDABLE_LEAVES = (type(None), bool, int, float, str, bytes, UUID, datetime, Decimal)

RECORDED_SHAPE = create_model("Record", __module__=__name__, id=(int, ...))
SHAPE_WITH_ADDED_FIELD = create_model(
    "Record", __module__=__name__, id=(int, ...), source=(str, "member")
)
SHAPE_WITH_ADDED_REQUIRED_FIELD = create_model(
    "Record", __module__=__name__, id=(int, ...), source=(str, ...)
)

Record = RECORDED_SHAPE


def _rebind(monkeypatch: pytest.MonkeyPatch, shape: type[BaseModel]) -> None:
    monkeypatch.setattr(sys.modules[__name__], "Record", shape)


def test_a_field_added_after_the_recording_replays_with_its_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rebind(monkeypatch, RECORDED_SHAPE)
    recording = SERIALIZER.serialize(RECORDED_SHAPE(id=7))
    _rebind(monkeypatch, SHAPE_WITH_ADDED_FIELD)
    replayed = SERIALIZER.deserialize(recording)
    assert type(replayed) is SHAPE_WITH_ADDED_FIELD
    assert replayed.id == 7
    assert replayed.source == "member"


def test_a_field_added_without_a_default_fails_the_replay_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rebind(monkeypatch, RECORDED_SHAPE)
    recording = SERIALIZER.serialize(RECORDED_SHAPE(id=7))
    _rebind(monkeypatch, SHAPE_WITH_ADDED_REQUIRED_FIELD)
    with pytest.raises(ValidationError, match="source"):
        SERIALIZER.deserialize(recording)


def test_a_field_dropped_after_the_recording_is_ignored_on_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rebind(monkeypatch, SHAPE_WITH_ADDED_FIELD)
    recording = SERIALIZER.serialize(SHAPE_WITH_ADDED_FIELD(id=7, source="agent"))
    _rebind(monkeypatch, RECORDED_SHAPE)
    replayed = SERIALIZER.deserialize(recording)
    assert type(replayed) is RECORDED_SHAPE
    assert replayed.id == 7
    assert not hasattr(replayed, "source")


def test_step_outputs_round_trip_unchanged() -> None:
    batch = (
        Arrival(
            id=uuid4(),
            speaker_member_id=uuid4(),
            admission_source=MEMBER_ADMISSION,
            rendered="run the ablation",
        ),
        Arrival(id=uuid4(), denial="the hook declined it"),
    )
    assert SERIALIZER.deserialize(SERIALIZER.serialize(batch)) == batch
    result = DispatchResult(tool_use_id="c1", text="done", is_error=False)
    assert SERIALIZER.deserialize(SERIALIZER.serialize(result)) == result


def test_values_that_are_not_models_round_trip_unchanged() -> None:
    for value in (
        None,
        "text",
        {"args": (1, 2), "kwargs": {"at": datetime(2026, 8, 13, tzinfo=UTC)}},
        [uuid4(), 3.5],
    ):
        assert SERIALIZER.deserialize(SERIALIZER.serialize(value)) == value
    error = SERIALIZER.deserialize(SERIALIZER.serialize(ValueError("boom")))
    assert type(error) is ValueError
    assert error.args == ("boom",)


def test_the_name_is_not_the_default_serializers() -> None:
    assert SERIALIZER.name() not in ("py_pickle", "portable_json", "custom_serializer")


def test_recordings_reference_rebuild_at_its_permanent_address() -> None:
    raw = base64.b64decode(SERIALIZER.serialize(RECORDED_SHAPE(id=7)))
    assert b"ufo.harness.durability" in raw
    assert b"_rebuild" in raw


def test_a_recording_from_a_moved_module_replays_through_the_current_path() -> None:
    """A recording made by the release before a module move names the old path; replay on this
    build must land on the class at its current home."""
    recorded_class = type("DispatchResult", (DispatchResult,), {})
    recorded_class.__module__ = "ufo.loop.engine"
    legacy = types.ModuleType("ufo.loop.engine")
    legacy.DispatchResult = recorded_class
    sys.modules["ufo.loop.engine"] = legacy
    try:
        recording = SERIALIZER.serialize(
            recorded_class(tool_use_id="c1", text="done", is_error=False)
        )
    finally:
        del sys.modules["ufo.loop.engine"]
    assert b"ufo.loop.engine" in base64.b64decode(recording)
    replayed = SERIALIZER.deserialize(recording)
    assert type(replayed) is DispatchResult
    assert replayed == DispatchResult(tool_use_id="c1", text="done", is_error=False)


def test_a_compaction_recording_from_before_the_package_rename_replays() -> None:
    """`_compact` is memoized, so a recording the outgoing image wrote names its step input under
    the package path that image shipped."""
    recorded_class = type("_CompactionRequest", (_CompactionRequest,), {})
    recorded_class.__module__ = "ufo_ext_context_summarization.compaction"
    legacy = types.ModuleType("ufo_ext_context_summarization.compaction")
    legacy._CompactionRequest = recorded_class
    legacy_package = types.ModuleType("ufo_ext_context_summarization")
    legacy_package.compaction = legacy
    sys.modules["ufo_ext_context_summarization"] = legacy_package
    sys.modules["ufo_ext_context_summarization.compaction"] = legacy
    try:
        recording = SERIALIZER.serialize(
            recorded_class(messages=(), reason="auto", active_requests=())
        )
    finally:
        del sys.modules["ufo_ext_context_summarization.compaction"]
        del sys.modules["ufo_ext_context_summarization"]
    assert b"ufo_ext_context_summarization.compaction" in base64.b64decode(recording)
    replayed = SERIALIZER.deserialize(recording)
    assert type(replayed) is _CompactionRequest


def test_a_rebuild_reference_from_before_its_move_still_resolves() -> None:
    from ufo.harness import durability

    legacy = types.ModuleType("ufo.durability")
    legacy._rebuild = durability._rebuild
    sys.modules["ufo.durability"] = legacy
    original = durability._rebuild.__module__
    durability._rebuild.__module__ = "ufo.durability"
    try:
        recording = SERIALIZER.serialize(RECORDED_SHAPE(id=7))
    finally:
        durability._rebuild.__module__ = original
        del sys.modules["ufo.durability"]
    assert b"\x8c\x0eufo.durability" in base64.b64decode(recording)
    replayed = SERIALIZER.deserialize(recording)
    assert replayed.id == 7


def test_every_moved_module_maps_to_a_module_that_imports() -> None:
    for new in MOVED_MODULES.values():
        importlib.import_module(new)


def _module_name(path: Path) -> str:
    parts = [path.stem]
    package = path.parent
    while (package / "__init__.py").exists():
        parts.append(package.name)
        package = package.parent
    return ".".join(reversed(parts))


def _step_returns() -> Iterator[tuple[str, object]]:
    for root in SOURCE_ROOTS:
        for path in sorted((REPOSITORY_ROOT / root).glob("**/*.py")):
            yield from _step_returns_in(path)


def _step_returns_in(path: Path) -> Iterator[tuple[str, object]]:
    source = path.read_text()
    if STEP_DECORATOR in source:
        module = importlib.import_module(_module_name(path))
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            decorators = {ast.unparse(decorator).split("(")[0] for decorator in node.decorator_list}
            if "DBOS.step" not in decorators:
                continue
            assert node.returns is not None, f"{node.name} declares no return type"
            yield node.name, eval(ast.unparse(node.returns), vars(module))


def _leaves(annotation: object) -> Iterator[object]:
    arguments = get_args(annotation)
    if not arguments:
        yield annotation
        return
    for argument in arguments:
        yield from _leaves(argument)


def _recordable(leaf: object) -> bool:
    if leaf is None or leaf is Ellipsis or leaf in RECORDABLE_LEAVES:
        return True
    return isinstance(leaf, type) and issubclass(leaf, BaseModel)


def test_dbos_steps_declare_data_return_types() -> None:
    steps = list(_step_returns())
    assert {"_stream_once", "_dispatch_step", "_compact"} <= {name for name, _ in steps}
    unrecordable = {
        name: leaf
        for name, annotation in steps
        for leaf in _leaves(annotation)
        if not _recordable(leaf)
    }
    assert unrecordable == {}


def test_preflight_flat_pickle_restores_nested_input_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = UserSkillSpec.model_validate({"files": {"SKILL.md": {"from": "draft.md"}}})
    record = make_dataclass(
        "_AuthorizationPreflight",
        [("args", object), ("request_target", object), ("context", object)],
        frozen=True,
        module=engine_module.__name__,
    )
    with monkeypatch.context() as patch:
        patch.setattr(engine_module, "_AuthorizationPreflight", record)
        recording = pickle.dumps(record(args, None, object()))
    restored = SERIALIZER.deserialize(base64.b64encode(recording).decode())
    assert isinstance(restored, _AuthorizationPreflight)
    replayed = SERIALIZER.deserialize(SERIALIZER.serialize(restored))
    assert isinstance(replayed, _AuthorizationPreflight)
    assert UserSkillSpec.model_validate_json(replayed.args_json) == args


def _rebuild_declaring_every_recorded_field(
    model_class: type[BaseModel], fields: dict[str, object]
) -> BaseModel:
    lost = sorted(set(fields) - set(model_class.model_fields))
    assert lost == [], f"{model_class.__qualname__} no longer declares recorded fields {lost}"
    return durability._rebuild(model_class, fields)


class _RecordedFieldsUnpickler(durability._CompatUnpickler):
    def find_class(self, module: str, name: str) -> object:
        found = super().find_class(module, name)
        return _rebuild_declaring_every_recorded_field if found is durability._rebuild else found


@pytest.fixture(scope="module")
def step_returns() -> dict[str, object]:
    return dict(_step_returns())


@pytest.mark.parametrize(
    "recording", sorted(RECORDED_346716E.glob("*.b64")), ids=lambda recording: recording.stem
)
def test_a_step_output_the_346716e_image_recorded_replays_on_this_image(
    recording: Path, step_returns: dict[str, object]
) -> None:
    assert recording.stem in step_returns, f"no step {recording.stem!r} replays this recording"
    replayed = _RecordedFieldsUnpickler(
        io.BytesIO(base64.b64decode(recording.read_text().strip()))
    ).load()
    adapter = TypeAdapter(step_returns[recording.stem])
    assert adapter.validate_python(replayed, strict=True) == replayed


async def _told(connection: object, added: MemberAdded) -> None:
    return None


@pytest.mark.parametrize(
    ("listener", "replayed"),
    [
        (None, [("told@x.test", False, None), ("quiet@x.test", True, None)]),
        (
            MemberAddedSpec(handler=_told, notify_description="", description="", notice=str),
            [("told@x.test", False, True), ("quiet@x.test", True, False)],
        ),
    ],
)
def test_an_add_member_the_346716e_image_recorded_replays_with_or_without_a_listener(
    listener: MemberAddedSpec | None, replayed: list[tuple[str, bool, bool | None]]
) -> None:
    recorded = _RecordedFieldsUnpickler(
        io.BytesIO(base64.b64decode(RECORDED_ADD_MEMBER_PREFLIGHTS.read_text().strip()))
    ).load()
    input_model = add_member_action(listener).input_model
    calls = [
        input_model.model_validate_json(preflight.args_json, context=TRUSTED_TOOL_INPUT)
        for preflight in recorded
    ]
    assert [(call.email, call.admin, call.model_dump().get("notify")) for call in calls] == replayed
