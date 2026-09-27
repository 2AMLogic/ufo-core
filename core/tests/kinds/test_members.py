import asyncio
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
import ufo_ext_sample.manifest as sample
import yaml
from cryptography.fernet import Fernet
from dbos import EnqueueOptions
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection
from ufo_ext_sample.members import (
    ADD_DESCRIPTION,
    ADDED_NOTE,
    NOTIFY_DESCRIPTION,
    RECORDED_NOTICE,
    RECORDED_TO_TELL_NOTICE,
    REFUSED_INVITEE,
    AddRefused,
)
from ufo_ext_sample.tools import NOTE_TABLE

from ufo.blob import FilesystemBlobStore
from ufo.db import workspace_tx
from ufo.harness.sandbox.session import ExecResult, SandboxHandle, SandboxSession, SandboxSpec
from ufo.host.ext import loader
from ufo.host.ext.loader import core_actions, load_manifests, turn_tools, validate_ext_tools
from ufo.host.kinds.members import (
    ADD_MEMBER_LEAD,
    ADD_MEMBER_RULES,
    ADD_MEMBER_TOOL,
    MEMBER_KIND,
    AddMember,
    AddMemberInput,
    add_member_action,
)
from ufo.runtime.access.credentials import CredentialStore
from ufo.runtime.ext.manifest import Manifest
from ufo.runtime.objects import AdminRequired, UnknownObject, VerbNotSupported
from ufo.runtime.seats import SEAT_REFUSAL_MESSAGE, MemberAdded, MemberAddedSpec, create_member
from ufo.runtime.surfaces.admission import Admission
from ufo.runtime.tools.context import SpawnResult, SpeakerRequired, ToolContext
from ufo.runtime.tools.registry import ToolDef
from ufo.runtime.turns.audience import (
    Audience,
    conversation_audience,
    foreign_room_audience,
)
from ufo.runtime.workspace import ws
from ufo.schema import tables
from ufo.schema.records import CANCELLED, Agent, TerminalFrame, Turn

LOCK_OBSERVE_TIMEOUT_SECONDS = 5


class _UntouchedCarrier:
    async def create(self, spec: SandboxSpec) -> SandboxHandle:
        raise AssertionError

    async def write(self, handle: SandboxHandle, path: str, content: bytes) -> None: ...

    async def exec(
        self,
        handle: SandboxHandle,
        argv: tuple[str, ...],
        timeout_s: int,
        model_command: str | None = None,
    ) -> ExecResult:
        raise AssertionError


async def _unavailable_spawn(
    profile: str,
    payload: dict[str, object],
    background: bool = False,
) -> SpawnResult:
    raise AssertionError


async def _seed() -> tuple[UUID, UUID, UUID, UUID, UUID]:
    workspace_id, main_agent, child_agent, admin_id, member_id = (uuid4() for _ in range(5))
    async with workspace_tx() as connection:
        await connection.execute(
            sa.insert(tables.workspace).values(
                id=workspace_id,
                created_at=sa.func.now(),
                updated_at=sa.func.now(),
            )
        )
        for agent_id, name, is_main in (
            (main_agent, "ufo", True),
            (child_agent, "research", False),
        ):
            await connection.execute(
                sa.insert(tables.agent).values(
                    id=agent_id,
                    workspace_id=workspace_id,
                    name=name,
                    prompt="p",
                    model="m",
                    is_main=is_main,
                    created_at=sa.func.now(),
                    updated_at=sa.func.now(),
                )
            )
        for row_id, email, is_admin in (
            (admin_id, "admin@example.com", True),
            (member_id, "member@example.com", False),
        ):
            await connection.execute(
                sa.insert(tables.member).values(
                    id=row_id,
                    workspace_id=workspace_id,
                    email=email,
                    is_admin=is_admin,
                    seated_at=sa.func.now(),
                    created_at=sa.func.now(),
                    updated_at=sa.func.now(),
                )
            )
    return workspace_id, main_agent, child_agent, admin_id, member_id


def _context(
    workspace_id: UUID,
    agent_id: UUID,
    speaker_id: UUID,
    audience: Audience | None = None,
) -> ToolContext:
    return ToolContext(
        sandbox=SandboxSession(
            carrier=_UntouchedCarrier(),
            handle=SandboxHandle(conversation_id=uuid4(), container_id="test"),
        ),
        blob=FilesystemBlobStore(root=Path()),
        turn=Turn(
            id=uuid4(),
            workspace_id=workspace_id,
            conversation_id=uuid4(),
            agent_id=agent_id,
            seq=1,
            status="running",
            inbound="manage members",
            created_at=datetime(2026, 7, 27, tzinfo=UTC),
        ),
        agent=Agent(prompt="p", model="m"),
        spawn=_unavailable_spawn,
        speaker_member_id=speaker_id,
        audience=audience or conversation_audience(speaker_id),
        artifact_token_secret="",
    )


def _tool(name: str) -> ToolDef:
    tools, _, _ = turn_tools((), None, audience=conversation_audience(None))
    return next(tool for tool in tools if tool.name == name)


async def _text(tool: ToolDef, ctx: ToolContext, **args: object) -> str:
    result = await tool.handler(ctx, tool.input_model.model_validate({**args}))
    assert result.is_error is False
    return result.content[0].text


def _manifest(member_id: UUID, admin: bool, seated: bool = True) -> str:
    return yaml.safe_dump(
        {
            "kind": MEMBER_KIND,
            "name": str(member_id),
            "spec": {"admin": admin, "seated": seated},
        }
    )


@pytest.mark.parametrize("database_url", ["sqlite"], indirect=True)
async def test_main_agent_admin_manages_roles_through_member_objects(db: None) -> None:
    workspace_id, main_agent, child_agent, admin_id, member_id = await _seed()
    with ws(workspace_id):
        member_listing = json.loads(
            await _text(
                _tool("object_list"),
                _context(workspace_id, child_agent, member_id),
                kind=MEMBER_KIND,
            )
        )
        assert [row["name"] for row in member_listing["objects"]] == [str(member_id)]

        admin = _context(workspace_id, main_agent, admin_id)
        admin_listing = json.loads(await _text(_tool("object_list"), admin, kind=MEMBER_KIND))
        assert {row["name"] for row in admin_listing["objects"]} == {
            str(admin_id),
            str(member_id),
        }
        await _text(
            _tool("object_apply"),
            admin,
            manifest=_manifest(member_id, True),
        )
        await _text(
            _tool("object_apply"),
            _context(workspace_id, main_agent, member_id),
            manifest=_manifest(admin_id, False),
        )
    async with workspace_tx() as connection:
        roles = {
            row.id: row.is_admin
            for row in (
                await connection.execute(
                    sa.select(tables.member.c.id, tables.member.c.is_admin).where(
                        tables.member.c.id.in_((admin_id, member_id))
                    )
                )
            )
        }
    assert roles == {admin_id: False, member_id: True}


@pytest.mark.parametrize("database_url", ["sqlite"], indirect=True)
async def test_member_get_reports_role_and_seat_and_delete_is_refused(db: None) -> None:
    workspace_id, main_agent, _, admin_id, member_id = await _seed()
    with ws(workspace_id):
        admin = _context(workspace_id, main_agent, admin_id)
        fetched = yaml.safe_load(
            await _text(
                _tool("object_get"),
                admin,
                ref=f"{MEMBER_KIND}/{member_id}",
            )
        )
        assert fetched["ref"] == f"{MEMBER_KIND}/{member_id}"
        assert fetched["spec"] == {"admin": False, "seated": True}
        assert fetched["status"] == {
            "email": "member@example.com",
            "seated": True,
        }
        assert fetched["links"] == []
        assert datetime.fromisoformat(fetched["created_at"])
        assert datetime.fromisoformat(fetched["updated_at"])

        delete_tool = _tool("object_delete")
        with pytest.raises(VerbNotSupported, match="cannot be deleted"):
            await delete_tool.handler(
                admin,
                delete_tool.input_model.model_validate(
                    {
                        "kind": MEMBER_KIND,
                        "name": str(member_id),
                    }
                ),
            )


@pytest.mark.parametrize("database_url", ["sqlite"], indirect=True)
async def test_managing_members_without_a_speaker_asks_who_is_asking(db: None) -> None:
    """Who is speaking is answered before what they may do."""
    workspace_id, main_agent, _, _, member_id = await _seed()
    speakerless = replace(_context(workspace_id, main_agent, member_id), speaker_member_id=None)
    with ws(workspace_id):
        with pytest.raises(SpeakerRequired, match="requested_by"):
            await _tool("object_apply").handler(
                speakerless,
                _tool("object_apply").input_model.model_validate(
                    {"manifest": _manifest(member_id, True)}
                ),
            )
        with pytest.raises(SpeakerRequired, match="requested_by"):
            await _add(speakerless, email="newhire@example.com")
    assert await _member_row(workspace_id, "newhire@example.com") is None


@pytest.mark.parametrize("database_url", ["sqlite"], indirect=True)
async def test_child_agent_cannot_change_another_members_role(db: None) -> None:
    workspace_id, _, child_agent, admin_id, member_id = await _seed()
    with ws(workspace_id), pytest.raises(AdminRequired, match="main agent"):
        await _tool("object_apply").handler(
            _context(workspace_id, child_agent, admin_id),
            _tool("object_apply").input_model.model_validate(
                {
                    "manifest": _manifest(member_id, True),
                }
            ),
        )


@pytest.mark.parametrize("database_url", ["sqlite"], indirect=True)
async def test_last_admin_cannot_be_removed(db: None) -> None:
    workspace_id, main_agent, _, admin_id, _ = await _seed()
    with ws(workspace_id), pytest.raises(ValueError, match="at least one admin"):
        await _tool("object_apply").handler(
            _context(workspace_id, main_agent, admin_id),
            _tool("object_apply").input_model.model_validate(
                {
                    "manifest": _manifest(admin_id, False),
                }
            ),
        )


@pytest.mark.parametrize("database_url", ["postgres"], indirect=True)
async def test_demoted_admin_cannot_finish_a_role_change(
    db: None,
    database_url: str,
) -> None:
    if not database_url.startswith("postgresql"):
        pytest.skip("row-lock interleaving requires PostgreSQL")
    workspace_id, main_agent, _, _admin_id, second_admin = await _seed()
    target = uuid4()
    async with workspace_tx() as connection:
        await connection.execute(
            sa.update(tables.member).where(tables.member.c.id == second_admin).values(is_admin=True)
        )
        await connection.execute(
            sa.insert(tables.member).values(
                id=target,
                workspace_id=workspace_id,
                email="target@example.com",
                is_admin=False,
                seated_at=sa.func.now(),
                created_at=sa.func.now(),
                updated_at=sa.func.now(),
            )
        )
    tool = _tool("object_apply")
    args = tool.input_model.model_validate(
        {
            "manifest": _manifest(target, True),
        }
    )
    with ws(workspace_id):
        async with workspace_tx() as demotion:
            holder_pid = (await demotion.execute(sa.text("select pg_backend_pid()"))).scalar_one()
            await demotion.execute(
                sa.select(tables.workspace.c.id)
                .where(tables.workspace.c.id == workspace_id)
                .with_for_update()
            )
            pending = asyncio.create_task(
                tool.handler(_context(workspace_id, main_agent, second_admin), args)
            )
            async with asyncio.timeout(LOCK_OBSERVE_TIMEOUT_SECONDS):
                while True:
                    async with workspace_tx() as observer:
                        blocked = (
                            await observer.execute(
                                sa.text(
                                    "select exists ("
                                    "select 1 from pg_stat_activity "
                                    "where cast(:holder as integer) = any(pg_blocking_pids(pid))"
                                    ")"
                                ),
                                {"holder": holder_pid},
                            )
                        ).scalar_one()
                    if blocked:
                        break
            await demotion.execute(
                sa.update(tables.member)
                .where(tables.member.c.id == second_admin)
                .values(is_admin=False)
            )
        with pytest.raises(AdminRequired, match="workspace admin"):
            await pending
        async with workspace_tx() as connection:
            assert not (
                await connection.execute(
                    sa.select(tables.member.c.is_admin).where(tables.member.c.id == target)
                )
            ).scalar_one()


async def _member_row(workspace_id: UUID, email: str) -> sa.Row | None:
    async with workspace_tx() as connection:
        return (
            await connection.execute(
                sa.select(
                    tables.member.c.id,
                    tables.member.c.is_admin,
                    tables.member.c.seated_at,
                ).where(
                    tables.member.c.workspace_id == workspace_id,
                    tables.member.c.email == email,
                )
            )
        ).one_or_none()


async def _add(ctx: ToolContext, **args: object) -> str:
    return await _text(add_member_action(None), ctx, **args)


def _sample_add_member() -> ToolDef:
    manifest = next(manifest for manifest in load_manifests() if manifest.name == sample.NAME)
    _, _, verbs = turn_tools(
        (manifest,),
        CredentialStore(fernet=Fernet(Fernet.generate_key())),
        audience=conversation_audience(None),
    )
    return verbs.actions[MEMBER_KIND][ADD_MEMBER_TOOL].action


async def _sample_note(workspace_id: UUID) -> str | None:
    async with workspace_tx() as connection:
        return (
            await connection.execute(
                sa.select(NOTE_TABLE.c.note).where(NOTE_TABLE.c.workspace_id == workspace_id)
            )
        ).scalar_one_or_none()


async def _nothing(connection: AsyncConnection, added: MemberAdded) -> None:
    return None


EMAILING_LISTENER = MemberAddedSpec(
    handler=_nothing,
    notify_description=(
        "Whether to email them that they were added, with a link to sign in — true is the "
        "default, so an email goes out unless you set false. When the member asks for the add to "
        "be silent ('don't notify them', 'add them quietly'), pass false; if you are not sure "
        "whether they want an email sent, ask before adding."
    ),
    description=(
        "Adding emails them a link to sign in by default; set notify to false when the member "
        "asks for the add to be silent, and ask before adding if unsure whether an email should "
        "go out."
    ),
    notice=lambda notify: (
        "They can speak to the agent now."
        + (" They will get an email with a link to sign in." if notify else "")
    ),
)
EMAILING_DESCRIPTION = (
    "Add someone to this workspace by their email, optionally as an admin, before they have ever "
    "contacted the agent, at any email domain — an outside contractor or advisor is added the "
    "same way as a colleague. Adding emails them a link to sign in by default; set notify to false "
    "when the member asks for the add to be silent, and ask before adding if unsure whether an "
    "email should go out. Any member using the main agent may add a member unless the workspace "
    "turned members_can_add off; only a workspace admin may add an admin. Changing an existing "
    "member's role or seat is an apply on the member object, not this."
)
EMAILING_INPUT_SCHEMA = {
    "additionalProperties": False,
    "properties": {
        "email": {
            "description": "The email of the person to add, at any domain.",
            "format": "email",
            "title": "Email",
            "type": "string",
        },
        "admin": {
            "default": False,
            "description": "Whether they administer the workspace — a workspace admin manages "
            "every member's role and seat, and reads the workspace's shape.",
            "title": "Admin",
            "type": "boolean",
        },
        "notify": {
            "default": True,
            "description": "Whether to email them that they were added, with a link to sign in — "
            "true is the default, so an email goes out unless you set false. When the member asks "
            "for the add to be silent ('don't notify them', 'add them quietly'), pass false; if "
            "you are not sure whether they want an email sent, ask before adding.",
            "title": "Notify",
            "type": "boolean",
        },
        "requested_by": {
            "type": "string",
            "format": "uuid",
            "description": "Message ref that explicitly requested this call. Required for any "
            "member-specific authority or capability, including admin actions; omit only for "
            "conversation-common work.",
        },
    },
    "required": ["email"],
    "title": "AddMemberInput",
    "type": "object",
}


def test_without_a_listener_add_member_takes_no_notify_and_promises_nothing() -> None:
    tool = add_member_action(None)
    assert tool.description == f"{ADD_MEMBER_LEAD} {ADD_MEMBER_RULES}"
    assert set(tool.input_model.model_fields) == {"email", "admin"}
    with pytest.raises(ValidationError):
        tool.input_model.model_validate({"email": "new@example.com", "notify": False})


def test_an_emailing_listener_splices_its_text_into_add_member_byte_for_byte() -> None:
    schema = add_member_action(EMAILING_LISTENER).schema()
    assert schema.description == EMAILING_DESCRIPTION
    assert json.dumps(schema.input_schema) == json.dumps(EMAILING_INPUT_SCHEMA)


@pytest.mark.parametrize("database_url", ["sqlite"], indirect=True)
async def test_an_emailing_listener_ends_the_reply_with_its_notice(db: None) -> None:
    workspace_id, main_agent, _, admin_id, _ = await _seed()
    tool = add_member_action(EMAILING_LISTENER)
    with ws(workspace_id):
        told = await _text(tool, _context(workspace_id, main_agent, admin_id), email="a@x.com")
        quiet = await _text(
            tool, _context(workspace_id, main_agent, admin_id), email="b@x.com", notify=False
        )
    assert told == (
        "a@x.com is a workspace member. They can speak to the agent now. They will get an email "
        "with a link to sign in."
    )
    assert quiet == "b@x.com is a workspace member. They can speak to the agent now."


async def test_the_sample_listener_records_the_add_on_the_adding_transaction(db: None) -> None:
    workspace_id, main_agent, _, admin_id, _ = await _seed()
    tool = _sample_add_member()
    assert tool.description == f"{ADD_MEMBER_LEAD} {ADD_DESCRIPTION} {ADD_MEMBER_RULES}"
    assert tool.input_model.model_json_schema()["properties"]["notify"] == {
        "default": True,
        "description": NOTIFY_DESCRIPTION,
        "title": "Notify",
        "type": "boolean",
    }
    with ws(workspace_id):
        told = await _text(tool, _context(workspace_id, main_agent, admin_id), email="Told@X.com")
    row = await _member_row(workspace_id, "told@x.com")
    assert row is not None
    assert told == f"told@x.com is a workspace member. {RECORDED_TO_TELL_NOTICE}"
    assert await _sample_note(workspace_id) == ADDED_NOTE.format(
        member_id=row.id, email="told@x.com", added_by=admin_id, notify=True
    )


@pytest.mark.parametrize("database_url", ["sqlite"], indirect=True)
async def test_a_silent_add_reaches_the_listener_as_one_not_to_tell(db: None) -> None:
    workspace_id, main_agent, _, _, member_id = await _seed()
    with ws(workspace_id):
        quiet = await _text(
            _sample_add_member(),
            _context(workspace_id, main_agent, member_id),
            email="quiet@x.com",
            notify=False,
        )
    row = await _member_row(workspace_id, "quiet@x.com")
    assert row is not None and not row.is_admin
    assert quiet == f"quiet@x.com is a workspace member. {RECORDED_NOTICE}"
    assert await _sample_note(workspace_id) == ADDED_NOTE.format(
        member_id=row.id, email="quiet@x.com", added_by=member_id, notify=False
    )


async def test_a_listener_that_raises_aborts_the_add(db: None) -> None:
    workspace_id, main_agent, _, admin_id, _ = await _seed()
    with ws(workspace_id), pytest.raises(AddRefused):
        await _text(
            _sample_add_member(),
            _context(workspace_id, main_agent, admin_id),
            email=REFUSED_INVITEE,
        )
    assert await _member_row(workspace_id, REFUSED_INVITEE) is None
    assert await _sample_note(workspace_id) is None


@pytest.mark.parametrize("database_url", ["sqlite"], indirect=True)
async def test_a_listener_refuses_an_add_that_carries_no_notify(db: None) -> None:
    workspace_id, main_agent, _, admin_id, _ = await _seed()
    with ws(workspace_id), pytest.raises(TypeError, match="NotifiedAddMemberInput"):
        await AddMember(listener=EMAILING_LISTENER).add(
            _context(workspace_id, main_agent, admin_id), AddMemberInput(email="a@x.com")
        )
    assert await _member_row(workspace_id, "a@x.com") is None


def test_two_extensions_declaring_member_added_fail_the_boot() -> None:
    first = Manifest(name="first", version="1", member_added=EMAILING_LISTENER)
    second = Manifest(name="second", version="1", member_added=EMAILING_LISTENER)
    assert core_actions((first,))[0] is add_member_action(EMAILING_LISTENER)
    with pytest.raises(RuntimeError, match="two extensions declare member_added: first, second"):
        validate_ext_tools((second, first), None)


def test_a_third_party_extension_cannot_declare_member_added(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = SimpleNamespace(
        load=lambda: lambda: Manifest(name="outside", version="1", member_added=EMAILING_LISTENER),
        dist=SimpleNamespace(name="outside-package"),
    )
    monkeypatch.setattr(loader, "entry_points", lambda group: (entry,))
    with pytest.raises(ValueError, match="third-party extension 'outside'"):
        loader.discovered()


@pytest.mark.parametrize("database_url", ["sqlite"], indirect=True)
async def test_an_admin_adds_a_member_who_has_never_spoken(db: None) -> None:
    workspace_id, main_agent, _, admin_id, _ = await _seed()
    with ws(workspace_id):
        answer = await _add(
            _context(workspace_id, main_agent, admin_id), email="New.Person@Example.com"
        )
    row = await _member_row(workspace_id, "new.person@example.com")
    assert row is not None and not row.is_admin
    assert row.seated_at is not None
    assert answer == "new.person@example.com is a workspace member."


@pytest.mark.parametrize("database_url", ["sqlite"], indirect=True)
async def test_a_non_admin_adds_a_member(db: None) -> None:
    workspace_id, main_agent, _, _, member_id = await _seed()
    with ws(workspace_id):
        await _add(_context(workspace_id, main_agent, member_id), email="new@example.com")
    row = await _member_row(workspace_id, "new@example.com")
    assert row is not None and not row.is_admin


@pytest.mark.parametrize("database_url", ["sqlite"], indirect=True)
async def test_a_workspace_that_turned_members_adding_off_takes_an_add_from_an_admin_alone(
    db: None,
) -> None:
    workspace_id, main_agent, _, admin_id, member_id = await _seed()
    async with workspace_tx() as connection:
        await connection.execute(
            sa.update(tables.workspace)
            .where(tables.workspace.c.id == workspace_id)
            .values(members_can_add=False)
        )
    with ws(workspace_id):
        with pytest.raises(AdminRequired, match="members_can_add"):
            await _add(_context(workspace_id, main_agent, member_id), email="new@example.com")
        await _add(_context(workspace_id, main_agent, admin_id), email="kept@example.com")
    assert await _member_row(workspace_id, "new@example.com") is None
    assert await _member_row(workspace_id, "kept@example.com") is not None


@pytest.mark.parametrize("database_url", ["sqlite"], indirect=True)
async def test_a_non_admin_cannot_add_an_admin(db: None) -> None:
    workspace_id, main_agent, _, _, member_id = await _seed()
    with ws(workspace_id), pytest.raises(AdminRequired, match="workspace admin"):
        await _add(
            _context(workspace_id, main_agent, member_id), email="new@example.com", admin=True
        )
    assert await _member_row(workspace_id, "new@example.com") is None


@pytest.mark.parametrize("database_url", ["sqlite"], indirect=True)
async def test_an_email_outside_the_workspace_domain_is_added(db: None) -> None:
    """A contractor, an advisor, or a colleague at a sister company is a member an admin can staff
    the workspace with: the speaking admin is the vetting, so no domain is compared."""
    workspace_id, main_agent, _, admin_id, _ = await _seed()
    with ws(workspace_id):
        answer = await _add(
            _context(workspace_id, main_agent, admin_id), email="contractor@other.com"
        )
    row = await _member_row(workspace_id, "contractor@other.com")
    assert row is not None and not row.is_admin
    assert "contractor@other.com is a workspace member" in answer


@pytest.mark.parametrize("database_url", ["sqlite"], indirect=True)
async def test_adding_an_existing_member_refuses_and_leaves_their_role(db: None) -> None:
    workspace_id, main_agent, _, admin_id, member_id = await _seed()
    with ws(workspace_id), pytest.raises(ValueError, match="already a member"):
        await _add(
            _context(workspace_id, main_agent, admin_id),
            email="member@example.com",
            admin=True,
        )
    row = await _member_row(workspace_id, "member@example.com")
    assert row is not None and row.id == member_id and not row.is_admin


@pytest.mark.parametrize("database_url", ["sqlite"], indirect=True)
async def test_an_admins_unseat_stops_the_agent_answering_that_member(db: None) -> None:
    workspace_id, main_agent, _, admin_id, member_id = await _seed()
    conversation_id = uuid4()
    async with workspace_tx() as connection:
        await connection.execute(
            sa.insert(tables.conversation).values(
                id=conversation_id,
                workspace_id=workspace_id,
                agent_id=main_agent,
                surface="cli",
                queue_key="session",
                member_id=member_id,
                created_at=sa.func.now(),
                updated_at=sa.func.now(),
            )
        )
    admission = Admission(dbos=_SeatStubDbos(), durable_surfaces=frozenset())
    before = await admission.admit_member(workspace_id, conversation_id, "still here?", member_id)
    assert await _turn_terminal(before.turn_id) == ("queued", None)

    with ws(workspace_id):
        await _text(
            _tool("object_apply"),
            _context(workspace_id, main_agent, admin_id),
            manifest=_manifest(member_id, admin=False, seated=False),
        )

    after = await admission.admit_member(workspace_id, conversation_id, "hello?", member_id)
    assert await _turn_terminal(after.turn_id) == (CANCELLED, SEAT_REFUSAL_MESSAGE)


@dataclass
class _SeatStubDbos:
    enqueued: list[str] = field(default_factory=list)

    async def enqueue_async(self, options: EnqueueOptions, workspace_id: str, turn_id: str) -> None:
        self.enqueued.append(turn_id)


async def _turn_terminal(turn_id: UUID) -> tuple[str, str | None]:
    async with workspace_tx() as connection:
        row = (
            await connection.execute(
                sa.select(tables.turn.c.status, tables.turn.c.terminal).where(
                    tables.turn.c.id == turn_id
                )
            )
        ).one()
    text = None if row.terminal is None else TerminalFrame.model_validate(row.terminal).text
    return row.status, text


@pytest.mark.parametrize("database_url", ["sqlite"], indirect=True)
async def test_every_member_lists_the_roster_from_the_main_agent(db: None) -> None:
    workspace_id, main_agent, child_agent, admin_id, member_id = await _seed()
    with ws(workspace_id):
        listing = json.loads(
            await _text(
                _tool("object_list"),
                _context(workspace_id, main_agent, member_id),
                kind=MEMBER_KIND,
            )
        )
        assert {row["name"] for row in listing["objects"]} == {str(admin_id), str(member_id)}
        assert {row["summary"] for row in listing["objects"]} == {
            "admin@example.com, workspace admin, seated",
            "member@example.com, workspace member, seated",
        }
        opened = yaml.safe_load(
            await _text(
                _tool("object_get"),
                _context(workspace_id, main_agent, member_id),
                ref=f"{MEMBER_KIND}/{admin_id}",
            )
        )
        assert opened["spec"] == {"admin": True, "seated": True}
        assert opened["status"]["email"] == "admin@example.com"
        walled = json.loads(
            await _text(
                _tool("object_list"),
                _context(workspace_id, child_agent, member_id),
                kind=MEMBER_KIND,
            )
        )
        assert [row["name"] for row in walled["objects"]] == [str(member_id)]


@pytest.mark.parametrize("database_url", ["sqlite"], indirect=True)
async def test_an_externally_shared_room_reads_only_the_speaker(db: None) -> None:
    """The roster is internal."""
    workspace_id, main_agent, _, admin_id, member_id = await _seed()
    foreign = foreign_room_audience("slack", "C123")
    with ws(workspace_id):
        listing = json.loads(
            await _text(
                _tool("object_list"),
                _context(workspace_id, main_agent, member_id, foreign),
                kind=MEMBER_KIND,
            )
        )
        assert [row["name"] for row in listing["objects"]] == [str(member_id)]
        assert "admin@example.com" not in json.dumps(listing)
        with pytest.raises(UnknownObject):
            await _text(
                _tool("object_get"),
                _context(workspace_id, main_agent, member_id, foreign),
                ref=f"{MEMBER_KIND}/{admin_id}",
            )


@pytest.mark.parametrize("database_url", ["sqlite"], indirect=True)
async def test_object_apply_names_the_verb_that_creates_a_member(db: None) -> None:
    """The kind refuses create and points at the verb that does it, so an agent told "add
    jane@acme.com" is never left with a dead end."""
    workspace_id, main_agent, _, admin_id, _ = await _seed()
    with ws(workspace_id), pytest.raises(VerbNotSupported, match="add_member action"):
        await _tool("object_apply").handler(
            _context(workspace_id, main_agent, admin_id),
            _tool("object_apply").input_model.model_validate(
                {"manifest": _manifest(uuid4(), False)}
            ),
        )


@pytest.mark.parametrize("database_url", ["postgres"], indirect=True)
async def test_create_member_holds_the_workspace_row_before_it_inserts(
    db: None,
    database_url: str,
) -> None:
    if not database_url.startswith("postgresql"):
        pytest.skip("row-lock interleaving requires PostgreSQL")
    workspace_id, _main, _child, _admin, _member = await _seed()

    async def create() -> None:
        with ws(workspace_id):
            async with workspace_tx() as connection:
                await create_member(connection, workspace_id, "queued@example.com")

    with ws(workspace_id):
        async with workspace_tx() as holder:
            holder_pid = (await holder.execute(sa.text("select pg_backend_pid()"))).scalar_one()
            await holder.execute(
                sa.select(tables.workspace.c.id)
                .where(tables.workspace.c.id == workspace_id)
                .with_for_update()
            )
            creating = asyncio.create_task(create())
            blocked_query = ""
            async with asyncio.timeout(LOCK_OBSERVE_TIMEOUT_SECONDS):
                while not blocked_query:
                    async with workspace_tx() as observer:
                        blocked_query = (
                            await observer.execute(
                                sa.text(
                                    "select query from pg_stat_activity "
                                    "where cast(:holder as integer) = any(pg_blocking_pids(pid))"
                                ),
                                {"holder": holder_pid},
                            )
                        ).scalar_one_or_none() or ""
            assert "for update" in blocked_query.lower(), blocked_query
            assert "insert into member" not in blocked_query.lower(), blocked_query
        await creating
    assert await _member_row(workspace_id, "queued@example.com") is not None


@pytest.mark.parametrize(
    "address",
    [
        "jane doe@example.com",
        "a@b@example.com",
        "@@example.com",
        "  spaced out @example.com",
        "no-at-sign",
        "@example.com",
        "trailing@",
    ],
)
@pytest.mark.parametrize("database_url", ["sqlite"], indirect=True)
async def test_a_malformed_address_never_becomes_a_member(db: None, address: str) -> None:
    """`create_member` crosses the shape gate, so nothing a sign-in could never normalize to and
    no channel-verified join could ever equal reaches a member row."""
    workspace_id, main_agent, _, admin_id, _ = await _seed()
    with ws(workspace_id), pytest.raises(ValueError):
        await _add(_context(workspace_id, main_agent, admin_id), email=address)
    async with workspace_tx() as connection:
        added = (
            await connection.execute(
                sa.select(sa.func.count()).where(
                    tables.member.c.workspace_id == workspace_id,
                    tables.member.c.email.not_in(("admin@example.com", "member@example.com")),
                )
            )
        ).scalar_one()
    assert added == 0
    # The gate lives in the write, not only in the verb that calls it, so the two creation paths
    # that match no domain cannot mint the row either.
    with ws(workspace_id), pytest.raises(ValueError, match="local@domain"):
        async with workspace_tx() as connection:
            await create_member(connection, workspace_id, address)


@pytest.mark.parametrize("database_url", ["sqlite"], indirect=True)
async def test_membership_is_not_managed_in_an_externally_shared_channel(db: None) -> None:
    workspace_id, main_agent, _, admin_id, member_id = await _seed()
    foreign = foreign_room_audience("slack", "C900")
    with ws(workspace_id):
        with pytest.raises(AdminRequired, match="internal conversation"):
            await _add(
                _context(workspace_id, main_agent, admin_id, foreign), email="newhire@example.com"
            )
        with pytest.raises(AdminRequired, match="internal conversation"):
            await _add(
                _context(workspace_id, main_agent, admin_id, foreign),
                email="member@example.com",
            )
    assert await _member_row(workspace_id, "newhire@example.com") is None
    async with workspace_tx() as connection:
        untouched = (
            await connection.execute(
                sa.select(tables.member.c.is_admin).where(tables.member.c.id == member_id)
            )
        ).scalar_one()
    assert untouched is False


@pytest.mark.parametrize("database_url", ["sqlite"], indirect=True)
async def test_a_workspace_whose_own_address_has_no_domain_still_adds_a_member(db: None) -> None:
    """`ufoctl init --email root` mints a workspace with no domain of its own, which nothing is
    compared against: the adding admin is the authority, not the workspace's own address."""
    workspace_id, main_agent, _, admin_id, _ = await _seed()
    async with workspace_tx() as connection:
        await connection.execute(
            sa.update(tables.member)
            .where(tables.member.c.workspace_id == workspace_id)
            .values(email=sa.func.replace(tables.member.c.email, "@example.com", ""))
        )
    with ws(workspace_id):
        await _add(_context(workspace_id, main_agent, admin_id), email="bob@example.com")
    assert await _member_row(workspace_id, "bob@example.com") is not None
