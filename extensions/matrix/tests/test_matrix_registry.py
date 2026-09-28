"""The manifest against the real runtime: the installed entry point loads through ufo's own loader,
the surface registers as durable with all three of its delivery handlers, the tools validate beside
every builtin, the setup action addresses as the `matrix` surface row's own, the skill parses into
the registry, and the migrations land the since, answering, asking, claim, link and crypto tables on
database. Skipped where `ufo` is not installed."""

import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("ufo", reason="install ufo from git to run the registry integration test")

from cryptography.fernet import Fernet  # noqa: E402

from ufo.db import apply_migrations  # noqa: E402
from ufo.host.ext.loader import (  # noqa: E402
    discovered,
    durable_surfaces,
    migration_locations,
    validate_ext_tools,
)
from ufo.host.kinds.surface_kind import registered_surfaces  # noqa: E402
from ufo.runtime.access.credentials import CredentialStore  # noqa: E402
from ufo.runtime.skills.runtime import parse_skill  # noqa: E402
from ufo.sdk.objects import SURFACE_KIND  # noqa: E402
from ufo_ext_matrix.crypto import STORE_KEY_SLOT  # noqa: E402
from ufo_ext_matrix.manifest import (  # noqa: E402
    CONNECT_TOOL,
    LINK_TOOL,
    SKILL_NAMES,
    UNLINK_TOOL,
)
from ufo_ext_matrix.surface import BOTS_ENV, HOMESERVER_SLOT, TOKEN_SLOT  # noqa: E402

MIGRATIONS = Path(__file__).resolve().parents[1] / "ufo_ext_matrix" / "migrations"


@pytest.fixture(scope="module")
def manifest():
    found = discovered()
    assert "matrix" in found, "install this repo (pip install -e .) so its entry point registers"
    return found["matrix"][0]


def test_the_entry_point_declares_one_durable_listening_surface(manifest) -> None:
    [surface] = manifest.surfaces
    assert surface.name == "matrix"
    assert surface.listen is not None and surface.post is not None
    assert surface.identify is not None
    assert surface.attach is not None and surface.speak is not None
    assert surface.routes == () and not surface.addressed
    assert durable_surfaces((manifest,)) == frozenset({"matrix"})
    assert registered_surfaces((manifest,))["matrix"].durable


def test_the_credentials_and_the_deploy_key(manifest) -> None:
    assert {slot.name for slot in manifest.credentials} == {
        HOMESERVER_SLOT,
        TOKEN_SLOT,
        STORE_KEY_SLOT,
    }
    assert all(slot.injection is None for slot in manifest.credentials)
    assert manifest.deploy_keys == ("MATRIX_BOTS",)
    assert manifest.deploy_keys == (BOTS_ENV.removeprefix("UFO_"),)


def test_the_tools_validate_beside_the_builtins(manifest) -> None:
    assert [tool.name for tool in manifest.tools] == [CONNECT_TOOL, LINK_TOOL, UNLINK_TOOL]
    validate_ext_tools((manifest,), CredentialStore(Fernet(Fernet.generate_key())))


def test_connect_is_an_instance_action_on_the_matrix_surface_row(manifest) -> None:
    """Setup belongs to the surface it sets up: core offers the action on the `matrix` row alone and
    refuses any other target, so no turn holds it as a tool of its own. Linking is the other shape —
    unbound, so a turn holds it wherever a member asks."""
    tools = {tool.name: tool for tool in manifest.tools}
    connect = tools[CONNECT_TOOL]
    assert connect.bound is not None
    assert (connect.bound.kind, connect.bound.binding, connect.bound.name) == (
        SURFACE_KIND,
        "instance",
        "matrix",
    )
    assert connect.canonical_id == f"action:{SURFACE_KIND}:{CONNECT_TOOL}"
    assert connect.side_effecting
    assert tools[LINK_TOOL].bound is None and tools[UNLINK_TOOL].bound is None


def test_the_manifest_ships_its_setup_skill(manifest) -> None:
    assert tuple(spec.path.name for spec in manifest.skills) == SKILL_NAMES
    for spec in manifest.skills:
        skill = parse_skill(spec.path)
        assert skill.name == spec.path.name
        assert skill.instructions.strip()
        assert skill.depends == ()


def test_the_loader_finds_the_migrations() -> None:
    assert str(MIGRATIONS) in {str(Path(p).resolve()) for p in migration_locations()}


def test_the_migrations_create_the_extension_tables(tmp_path: Path) -> None:
    database = tmp_path / "ufo.db"
    apply_migrations(f"sqlite+aiosqlite:///{database}")
    with sqlite3.connect(database) as connection:

        def columns(table: str) -> list[str]:
            return [row[1] for row in connection.execute(f"pragma table_info({table})")]

        assert columns("matrix_ext_since") == [
            "workspace_id",
            "installation_id",
            "since",
            "updated_at",
        ]
        assert columns("matrix_ext_answering") == [
            "workspace_id",
            "turn_id",
            "room_id",
            "event_id",
            "thread_root",
            "created_at",
        ]
        assert columns("matrix_ext_asking") == [
            "workspace_id",
            "turn_id",
            "room_id",
            "event_id",
            "created_at",
        ]
        assert columns("matrix_ext_claim") == [
            "workspace_id",
            "mxid",
            "member_id",
            "code_hash",
            "attempts",
            "expires_at",
        ]
        assert columns("matrix_ext_link") == [
            "workspace_id",
            "mxid",
            "member_id",
            "proved_by",
            "updated_at",
        ]
        assert columns("matrix_ext_crypto") == [
            "workspace_id",
            "user_id",
            "device_id",
            "kind",
            "name",
            "value",
            "updated_at",
        ]
