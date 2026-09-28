"""The rules the matrix surface holds without the runtime: what it may import, which events may
found a turn, the identifiers it derives, the messages it writes, who a line addresses, the labels
a question is answered by, and the frontmatter contract its skill is loaded under. The contract
modules import neither `ufo` nor an HTTP client, and a skill is data on disk, so these run on a
checkout with only `pytest` and `pyyaml` installed.

**The wire carve-outs.** `searchable` strikes the wire identifiers out of a text and leaves the prose
the transition-language ban is about. Two carve-outs, each scoped to a version this repo does not get
to rename: `PROTOCOL_NAMES` is a dotted Matrix event or algorithm name (`m.megolm.v1.aes-sha2`), and
`WIRE_VERSIONS` is a version token a wire value carries — quoted alone, as an `EncryptedFile` names
its format (`"v": "v2"`); a path segment, as a homeserver names its API (`/_matrix/client/v1/media`);
or the tail of a quoted value, as the spec names a SAS MAC method (`"hkdf-hmac-sha256.v2"`).

Each strikes to a space rather than to nothing, because removing a token joins what sat either side
of it: `x"v1"legacy` would collapse to `xlegacy` and pass a ban that `x legacy` fails.

The boundary is the quoting, and it is honest about the cost: a bare `v1` in a sentence fails, a
backticked `v2` fails, and a version someone puts in double quotes mid-sentence passes. Reading a
version out of a string literal is what writing the wire value looks like; the narrower rule — knowing
every way a constant or a JSON field might be spelled — would fail on the next spelling rather than on
the next piece of transition language.

**The seam guard's reach.** `guarded_paths` is the one walk both the guard and the test of its reach
call. Written twice, the reach test asserted the reach of its own copy — which is how narrowing the
guard to `PACKAGE.glob` left three seam tests green while `migrations/` stopped being read. The
exclusion is one path and not one filename, so a `client.py` in any subdirectory the package grows is
still read.

`wire_callers` qualifies by class, because a bare function name is ambiguous across a package: a
second class growing its own `send` would read as the seam itself. It matches an attribute call on the
method name, which is the boundary of what it claims — a send reached through an alias, a `getattr`
lookup or a lambda body is invisible to it. `dotted` names a module by the directories it sits in for
the same reason."""

import ast
import json
import re
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from ufo_ext_matrix.addressed import Bot, addresses
from ufo_ext_matrix.e2ee import EXTRA
from ufo_ext_matrix.events import (
    SURFACE,
    TEXT_MSGTYPE,
    answer_txn_id,
    file_txn_id,
    gaps,
    invites,
    localpart,
    media_parts,
    next_batch,
    part_txn_id,
    permalink,
    poll_answer,
    poll_txn_id,
    room_file,
    room_key,
    room_message,
    room_names,
    say_txn_id,
    server_name,
    timeline,
    txn_id,
)
from ufo_ext_matrix.messages import (
    EVENT_LIMIT_BYTES,
    FENCE,
    PART_BUDGET_BYTES,
    _cut,
    edit_content,
    file_content,
    html_body,
    message_content,
    msgtype_for,
    parts,
    reply_relation,
)
from ufo_ext_matrix.questions import (
    LABELLED_HINT,
    ONE_HINT,
    SEVERAL_HINT,
    Asked,
    Choice,
    answer_words,
    choices,
    label,
    letters,
    option_of,
    poll_choices,
    poll_content,
    question_block,
    settled_block,
)

EXTENSION = Path(__file__).resolve().parents[1]
PACKAGE = EXTENSION / "ufo_ext_matrix"
REPO = EXTENSION.parents[1]
SKILLS_ROOT = PACKAGE / "skills"
SKILL_NAMES = ("matrix-setup",)
DESCRIPTION_WORD_BUDGET = 50
THIRD_PARTY = frozenset({"httpx", "sqlalchemy", "alembic", "pydantic"})
E2EE_THIRD_PARTY = frozenset({"vodozemac", "cryptography"})
TRANSITION_WORDS = re.compile(r"\b(legacy|deprecated|formerly|for now|TODO|v1|v2)\b", re.IGNORECASE)
PROTOCOL_NAMES = re.compile(r"\bm\.[a-z_]+(\.[a-z0-9_-]+)+")
WIRE_VERSIONS = re.compile(r'"v\d+"|/v\d+(?=[/"])|\.v\d+(?=")')

BOT = "@ufo:example.org"
ROOM = "!room:example.org"
PURE = ("events.py", "messages.py", "addressed.py", "questions.py")
TRANSPORT = "client.py"
SEAM = "surface.MatrixSurface.send"
WIRE = ("send_message", "send_event")
ROLLOUT = Asked("Ship it?", ("Ship it", "Hold"))
TIMING = Asked("When?", ("Today", "Tomorrow"))
FLAVOURS = Asked("Which ones?", ("Apples", "Pears", "Plums"), multi_select=True)
ENVELOPE_BYTES = 2048


def text_event(body: str = "hello", **content: object) -> dict:
    return {
        "type": "m.room.message",
        "event_id": "$one",
        "sender": "@alice:example.org",
        "content": {"msgtype": "m.text", "body": body, **content},
    }


def imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module)
    return names


def searchable(text: str) -> str:
    """`text` with the wire identifiers struck out, leaving the prose the ban is about; the
    carve-outs and what they cost are in the module docstring."""
    return WIRE_VERSIONS.sub(" ", PROTOCOL_NAMES.sub(" ", text))


@pytest.mark.parametrize(
    "path", sorted(PACKAGE.rglob("*.py")), ids=lambda p: str(p.relative_to(PACKAGE))
)
def test_imports_only_the_sdk(path: Path) -> None:
    """Upstream gates extensions on `ufo.sdk`; a reach past it breaks on the next release."""
    allowed = sys.stdlib_module_names | THIRD_PARTY | E2EE_THIRD_PARTY | {"ufo_ext_matrix"}
    for name in imported_modules(path):
        top = name.split(".")[0]
        if top == "ufo":
            assert name == "ufo.sdk" or name.startswith("ufo.sdk."), name
        else:
            assert top in allowed, name


def guarded_modules(tree: ast.AST) -> set[str]:
    """The top-level names imported inside a `try` that handles `ImportError`."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        if not any(
            isinstance(h.type, ast.Name) and h.type.id == "ImportError" for h in node.handlers
        ):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Import):
                found |= {alias.name.split(".")[0] for alias in inner.names}
            elif isinstance(inner, ast.ImportFrom) and inner.module:
                found.add(inner.module.split(".")[0])
    return found


@pytest.mark.parametrize(
    "path", sorted(PACKAGE.rglob("*.py")), ids=lambda p: str(p.relative_to(PACKAGE))
)
def test_the_e2ee_libraries_are_imported_under_a_guard(path: Path) -> None:
    """`vodozemac` and `cryptography` ship in the `matrix-e2ee` extra, so a module that reads one
    reads it inside a `try`/`except ImportError` and the package imports without either."""
    guarded = guarded_modules(ast.parse(path.read_text()))
    for name in imported_modules(path):
        top = name.split(".")[0]
        if top in E2EE_THIRD_PARTY:
            assert top in guarded, f"{path.name} imports {top} outside an ImportError guard"


@pytest.mark.parametrize("module", PURE)
def test_the_contract_modules_import_nothing_installed(module: str) -> None:
    for name in imported_modules(PACKAGE / module):
        top, _, leaf = name.partition(".")
        if top == "ufo_ext_matrix":
            assert f"{leaf}.py" in PURE, name
        else:
            assert top in sys.stdlib_module_names, name


def test_the_package_declares_its_entry_point_and_client() -> None:
    project = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]
    assert project["entry-points"]["ufo.extension"]["matrix"] == "ufo_ext_matrix.manifest:manifest"
    assert any(dep.startswith("httpx") for dep in project["dependencies"])


def test_the_e2ee_libraries_are_an_extra_and_not_baseline() -> None:
    """One distribution ships `pulse` beside this surface, so an install for the skill pack alone
    pulls no native crypto: the two libraries are the `matrix-e2ee` extra's and nothing else's."""
    project = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]
    extra = project["optional-dependencies"][EXTRA]
    assert {dep.split(">")[0] for dep in extra} == set(E2EE_THIRD_PARTY)
    for dep in extra:
        assert not any(base.startswith(dep.split(">")[0]) for base in project["dependencies"]), dep


def own_calls(node: ast.AST) -> Iterator[ast.Call]:
    """The calls in a function's own body, stopping at a nested function so the name reported holds
    the call rather than merely enclosing it."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.AsyncFunctionDef | ast.FunctionDef | ast.ClassDef):
            continue
        if isinstance(child, ast.Call):
            yield child
        yield from own_calls(child)


def dotted(path: Path) -> str:
    """A module's name for a failure to quote, qualified by the directories it sits in, so a
    `client.py` under a subdirectory does not report under the transport's own name."""
    return ".".join(path.relative_to(PACKAGE).with_suffix("").parts)


def wire_callers(source: str, module: str) -> set[str]:
    """`module.Class.function` for every function in `source` that puts an event on the wire,
    qualified by class."""
    callers: set[str] = set()

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            elif isinstance(child, ast.AsyncFunctionDef | ast.FunctionDef):
                if any(
                    isinstance(call.func, ast.Attribute) and call.func.attr in WIRE
                    for call in own_calls(child)
                ):
                    callers.add(f"{prefix}{child.name}")
                walk(child, f"{prefix}{child.name}.")
            else:
                walk(child, prefix)

    walk(ast.parse(source), f"{module}.")
    return callers


def guarded_paths() -> list[Path]:
    """Every module the seam guard reads: the package, less the transport, which is the wire.
    The guard and the test of its reach call this one walk."""
    return sorted(path for path in PACKAGE.rglob("*.py") if path != PACKAGE / TRANSPORT)


def test_only_one_seam_puts_a_message_on_the_wire() -> None:
    """`MatrixSurface.send` is where an event meets `outbound`, so an encrypted room takes ciphertext
    whichever handler speaks; a handler calling the client's own send would reach it in the clear."""
    callers = {
        caller
        for path in guarded_paths()
        for caller in wire_callers(path.read_text(), dotted(path))
    }
    assert callers == {SEAM}, f"a message leaves outside the seam, from {sorted(callers)}"


def test_the_seam_guard_catches_a_bypass_wherever_it_is_written() -> None:
    """The guard earned its place on a rebase where two branches combined to bypass it. A bypass is
    caught in a class and at module level alike, each named by where it sits."""
    bypass = (
        "class Ear:\n"
        "    async def leak(self, client, room_id):\n"
        '        await client.send_event(room_id, "m.room.message", "txn", {})\n'
        "\n\n"
        "async def slip(client, room_id):\n"
        '    await client.send_message(room_id, "txn", {})\n'
    )
    assert wire_callers(bypass, "feedback") == {"feedback.Ear.leak", "feedback.slip"}


def test_the_seam_guard_reads_every_module_but_the_transport() -> None:
    """A glob matching nothing satisfies an equality against one name as well as a clean package does,
    so the reach itself is asserted rather than described."""
    assert sorted(PACKAGE.rglob(TRANSPORT)) == [PACKAGE / TRANSPORT]
    read = set(guarded_paths())
    assert {PACKAGE / "surface.py", PACKAGE / "feedback.py"} <= read
    # A module in a subdirectory is what tells `rglob` from `glob`. `migrations/` is the one the
    # package already has, so narrowing the walk stops being invisible.
    nested = {path for path in read if path.parent != PACKAGE}
    assert nested, "the walk reaches no subpackage, so a narrowed walk would read the same set"
    assert nested <= read
    assert wire_callers((PACKAGE / TRANSPORT).read_text(), "client"), (
        "the transport is skipped because it is the wire; a transport holding no send means this "
        "exclusion now hides the seam"
    )


def test_deploy_keys_are_bare_names_core_prefixes() -> None:
    """Core prefixes `UFO_` itself, so a key already carrying it doubles into a `UFO_UFO_...` nobody
    should set. This suite installs no `ufo`, so it reads the manifest's source."""
    tree = ast.parse((PACKAGE / "manifest.py").read_text())
    keys: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "Manifest":
            continue
        for keyword in node.keywords:
            if keyword.arg != "deploy_keys":
                continue
            assert isinstance(keyword.value, ast.Tuple), "the contract expects a literal tuple"
            for element in keyword.value.elts:
                assert isinstance(element, ast.Constant) and isinstance(element.value, str), (
                    "the contract expects literal deploy keys"
                )
                keys.append(element.value)
    assert keys, "the manifest declares no deploy_keys"
    assert all(not key.startswith("UFO_") for key in keys)


@pytest.mark.parametrize(
    "path",
    sorted(p for p in EXTENSION.rglob("*") if p.suffix in {".py", ".md"}),
    ids=lambda p: str(p.relative_to(EXTENSION)),
)
def test_no_transition_language(path: Path) -> None:
    """Every file reads as if designed this way from the start. The wire identifiers `searchable`
    strikes out are the one exception, and widening it is a visible choice, not a side effect."""
    if path.name == "test_matrix_contracts.py":
        return
    assert TRANSITION_WORDS.search(searchable(path.read_text())) is None


def test_the_wire_carve_outs_do_not_launder_prose() -> None:
    """A carve-out that grows quietly is a ban that stopped holding, so both its edges are asserted —
    the right one especially, where dropping a lookahead turns `the /v1 rewrite` into prose."""
    passes = (
        'sealed = {"v": "v2"}',
        'ALGORITHM = "m.megolm.v1.aes-sha2"',
        'KEY = "A256CTR"',
        'MEDIA = "/_matrix/client/v1/media"',
    )
    fails = (
        "the v2 format",
        "the `v2` format",
        "kept for v1 readers",
        "a TODO here",
        'x"v1"legacy',
        "the /v1 rewrite",
        "dropped in /v1",
    )
    for source in passes:
        assert TRANSITION_WORDS.search(searchable(source)) is None, source
    for source in fails:
        assert TRANSITION_WORDS.search(searchable(source)) is not None, source


def test_readme_matches_the_pack_shape() -> None:
    readme = (EXTENSION / "README.md").read_text()
    headings = ("## What it adds", "## Install", "## Connect", "## Tests", "## License", "## Traps")
    for heading in headings:
        assert heading in readme


def frontmatter(name: str) -> dict:
    raw = (SKILLS_ROOT / name / "SKILL.md").read_text()
    assert raw.startswith("---"), f"{name}: SKILL.md must open with ---"
    _, meta, _ = raw.split("---", 2)
    return yaml.safe_load(meta)


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_a_skill_name_matches_its_directory(name: str) -> None:
    assert frontmatter(name)["name"] == name


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_a_skill_description_routes(name: str) -> None:
    """The description is a routing budget: an over-long one loads the skill on the wrong turn, and
    one that does not say what the skill is not for loads it on a neighbouring ask."""
    description = frontmatter(name)["description"]
    assert description.startswith("Load when")
    assert len(description.split()) <= DESCRIPTION_WORD_BUDGET
    assert "Not for" in description


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_a_skill_wires_its_dependencies_rather_than_asking(name: str) -> None:
    """`metadata.depends` is the only mechanism that pulls another skill in, so it is declared even
    where this skill stands alone."""
    assert isinstance(frontmatter(name)["metadata"]["depends"], list)


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_a_skill_closes_with_traps(name: str) -> None:
    _, _, body = (SKILLS_ROOT / name / "SKILL.md").read_text().split("---", 2)
    assert body.strip()
    assert "## Traps" in body


SILENCES = (
    '[pack] name = "assistant"',
    "matrix_store_key",
    "matrix.crypto_no_keys",
    "matrix-e2ee",
    "matrix.crypto_extra_missing",
    "matrix.crypto_store_locked",
    "second client",
    "same user",
    "never invited",
)


def test_setup_names_every_silence_a_misconfigured_bot_answers_with() -> None:
    """Each of these presents only as the agent not answering, so a reader without the list has nothing
    to go on; three name the log line that tells them apart and the fourth has none to name."""
    traps = (SKILLS_ROOT / "matrix-setup" / "SKILL.md").read_text().split("## Traps", 1)[1]
    for tell in SILENCES:
        assert tell in traps, tell


def test_a_plain_text_message_is_a_member_message() -> None:
    message = room_message(ROOM, text_event("hi there"))
    assert message is not None
    assert (message.room_id, message.event_id, message.sender, message.body) == (
        ROOM,
        "$one",
        "@alice:example.org",
        "hi there",
    )


@pytest.mark.parametrize(
    "event",
    [
        {**text_event(), "type": "m.room.encrypted", "content": {"algorithm": "m.megolm.v1"}},
        {**text_event(), "type": "m.room.redaction", "redacts": "$zero", "content": {}},
        text_event("* fixed", **{"m.relates_to": {"rel_type": "m.replace", "event_id": "$zero"}}),
        text_event("* fixed", **{"m.new_content": {"msgtype": "m.text", "body": "fixed"}}),
        {**text_event(), "unsigned": {"redacted_because": {"type": "m.room.redaction"}}},
        {**text_event(), "content": {"msgtype": "m.notice", "body": "a bot's line"}},
        {**text_event(), "content": {}},
        text_event("   "),
        {**text_event(), "state_key": ""},
    ],
    ids=[
        "encrypted",
        "redaction",
        "edit",
        "new-content",
        "redacted",
        "notice",
        "redacted-content",
        "blank",
        "state",
    ],
)
def test_what_founds_no_turn(event: dict) -> None:
    assert room_message(ROOM, event) is None


UFO = Bot(mxid=BOT, display_name="ufo", said=frozenset({"$asked"}))


def addressed(**content: object) -> bool:
    message = room_message(ROOM, text_event(str(content.pop("body", "hey")), **content))
    assert message is not None
    return addresses(message, UFO)


@pytest.mark.parametrize(
    "content",
    [
        {"m.mentions": {"user_ids": [BOT]}},
        {"formatted_body": f'<a href="https://matrix.to/#/{BOT}">ufo</a>: hey'},
        {"body": f"{BOT} summarize the week"},
        {"body": "ufo: summarize the week"},
        {"body": "hey UFO, summarize the week"},
        {"m.relates_to": {"m.in_reply_to": {"event_id": "$asked"}}},
    ],
    ids=["mention", "pill", "mxid", "name", "any-case", "reply"],
)
def test_what_addresses_the_bot(content: dict) -> None:
    assert addressed(**content)


@pytest.mark.parametrize(
    "content",
    [
        {"body": "lunch?"},
        {"m.mentions": {"user_ids": ["@alice:example.org"]}},
        {"formatted_body": '<a href="https://matrix.to/#/@alice:example.org">alice</a>: hey'},
        {"formatted_body": f'<a href="https://matrix.to/#/{BOT}.evil">ufo</a>: hey'},
        {"body": "@ufobot:example.org summarize the week"},
        {"body": "ufology is a field"},
        {"m.relates_to": {"m.in_reply_to": {"event_id": "$alice"}}},
    ],
    ids=[
        "nobody",
        "someone-else",
        "their-pill",
        "a-longer-server",
        "a-longer-mxid",
        "a-longer-word",
        "their-message",
    ],
)
def test_what_addresses_somebody_else(content: dict) -> None:
    assert not addressed(**content)


def test_a_bot_with_no_display_name_is_still_addressed_by_its_mxid() -> None:
    message = room_message(ROOM, text_event(f"{BOT} hello"))
    assert message is not None
    assert addresses(message, Bot(mxid=BOT))
    assert not addresses(room_message(ROOM, text_event("hello")), Bot(mxid=BOT))  # type: ignore[arg-type]


def test_a_rich_reply_names_the_message_it_answers() -> None:
    message = room_message(
        ROOM, text_event("1", **{"m.relates_to": {"m.in_reply_to": {"event_id": "$asked"}}})
    )
    assert message is not None and message.replying_to == "$asked"
    assert room_message(ROOM, text_event("hi")).replying_to is None  # type: ignore[union-attr]


def test_one_question_is_answered_by_a_number() -> None:
    assert question_block("Rollout", (ROLLOUT,)) == (
        "Rollout\n\nShip it?\n1. Ship it\n2. Hold\n\n" + ONE_HINT
    )
    assert choices("2", (ROLLOUT,)) == (Choice(question=0, option=1),)


def test_several_questions_are_answered_by_label() -> None:
    asked = (ROLLOUT, TIMING)
    written = question_block("Rollout", asked)
    assert "1a. Ship it" in written and "2b. Tomorrow" in written
    assert written.endswith(LABELLED_HINT)
    assert choices("1a 2b", asked) == (Choice(0, 0), Choice(1, 1))


def test_a_bare_number_answers_nothing_where_several_questions_are_asked() -> None:
    assert choices("1", (ROLLOUT, TIMING)) == ()


def test_several_choices_answer_one_question_that_takes_them() -> None:
    assert question_block("Fruit", (FLAVOURS,)).endswith(SEVERAL_HINT)
    assert choices("1, 3", (FLAVOURS,)) == (Choice(0, 0), Choice(0, 2))
    assert choices("1 and 2", (FLAVOURS,)) == (Choice(0, 0), Choice(0, 1))
    assert choices("2 2", (FLAVOURS,)) == (Choice(0, 1),)


def test_a_question_that_takes_one_choice_keeps_the_first_named() -> None:
    assert choices("2 1", (ROLLOUT,)) == (Choice(0, 1),)


@pytest.mark.parametrize(
    "body",
    ["ship it", "1 more thing", "hold, please", "", "9", "1c", "3a", ":-)"],
    ids=[
        "words",
        "a-number-and-words",
        "a-label-and-words",
        "nothing",
        "past-the-options",
        "past-the-letters",
        "past-the-questions",
        "punctuation",
    ],
)
def test_what_answers_no_question(body: str) -> None:
    assert choices(body, (ROLLOUT,)) == ()


def test_a_question_that_asks_for_words_is_answered_by_words() -> None:
    written = Asked("What should it say?")
    assert question_block("Copy", (written,)) == "Copy\n\nWhat should it say?"
    assert choices("something short", (written,)) == ()
    assert choices("1", (written,)) == ()


def test_the_options_chosen_are_the_words_the_answer_admits() -> None:
    assert answer_words((ROLLOUT,), (Choice(0, 0),)) == "Ship it"
    assert answer_words((FLAVOURS,), (Choice(0, 0), Choice(0, 2))) == "Apples, Plums"
    assert answer_words((ROLLOUT, TIMING), (Choice(0, 1), Choice(1, 0))) == (
        "Ship it?: Hold\nWhen?: Today"
    )


def test_the_answer_that_landed_is_marked_in_the_question_it_answered() -> None:
    written = settled_block("Rollout", (ROLLOUT,), (Choice(0, 1),))
    assert written == "Rollout\n\nShip it?\n1. Ship it\n2. Hold ✓"
    assert ONE_HINT not in written


def test_an_option_is_labelled_past_the_alphabet() -> None:
    assert [letters(index) for index in (0, 25, 26, 27)] == ["a", "z", "aa", "ab"]
    assert [option_of(letters(index)) for index in range(60)] == list(range(60))
    assert label(0, 0, 1) == "1" and label(1, 26, 2) == "2aa"


def test_a_poll_carries_the_same_labels_the_message_wrote() -> None:
    content = poll_content("Rollout", ROLLOUT, reply_relation("$asked", None))
    assert [answer["m.id"] for answer in content["m.poll"]["answers"]] == ["1", "2"]
    assert content["m.poll"]["max_selections"] == 1
    assert content["m.text"][0]["body"].startswith("Rollout")
    assert content["m.relates_to"] == {"m.in_reply_to": {"event_id": "$asked"}}
    assert poll_choices(["2"], (ROLLOUT,)) == (Choice(0, 1),)


def test_a_response_to_somebody_elses_poll_chooses_nothing() -> None:
    assert poll_choices(["a1b2c3"], (ROLLOUT,)) == ()


def test_a_tap_names_the_poll_it_answers() -> None:
    answer = poll_answer(
        ROOM,
        {
            "type": "m.poll.response",
            "event_id": "$tap",
            "sender": "@alice:example.org",
            "content": {
                "m.relates_to": {"rel_type": "m.reference", "event_id": "$poll"},
                "m.poll.response": {"answers": ["2"]},
            },
        },
    )
    assert answer is not None
    assert (answer.poll_id, answer.answers, answer.event_id) == ("$poll", ("2",), "$tap")


@pytest.mark.parametrize(
    "content",
    [
        {"m.poll.response": {"answers": ["1"]}},
        {
            "m.relates_to": {"rel_type": "m.thread", "event_id": "$poll"},
            "m.poll.response": {"answers": ["1"]},
        },
        {
            "m.relates_to": {"rel_type": "m.reference", "event_id": "$poll"},
            "m.poll.response": {"answers": []},
        },
        {"m.relates_to": {"rel_type": "m.reference", "event_id": "$poll"}},
    ],
    ids=["no-relation", "another-relation", "no-answer", "no-response"],
)
def test_what_is_no_tap(content: dict) -> None:
    event = {
        "type": "m.poll.response",
        "event_id": "$tap",
        "sender": "@alice:example.org",
        "content": content,
    }
    assert poll_answer(ROOM, event) is None


def test_a_poll_response_is_not_a_member_message() -> None:
    event = {
        "type": "m.poll.response",
        "event_id": "$tap",
        "sender": "@alice:example.org",
        "content": {"m.poll.response": {"answers": ["1"]}},
    }
    assert room_message(ROOM, event) is None


def test_an_edit_rewrites_one_message_and_says_so_to_a_client_that_shows_none() -> None:
    content = edit_content("Rollout\n1. Ship it ✓", "$asked")
    assert content["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$asked"}
    assert content["m.new_content"]["body"] == "Rollout\n1. Ship it ✓"
    assert content["body"] == "* Rollout\n1. Ship it ✓"
    assert content["m.new_content"]["formatted_body"] == content["formatted_body"]


def test_an_answer_and_a_poll_are_sent_under_one_transaction_each() -> None:
    turn = UUID("11111111-1111-1111-1111-111111111111")
    assert answer_txn_id("$one") == answer_txn_id("$one") != answer_txn_id("$two")
    assert poll_txn_id(turn) == f"ufo-poll-{turn}"


def test_a_room_key_is_stable_and_colon_free() -> None:
    assert room_key(ROOM) == room_key(ROOM)
    assert room_key(ROOM) != room_key("!other:example.org")
    assert ":" not in room_key(ROOM)


def test_a_turn_sends_under_one_transaction_id() -> None:
    turn = UUID("00000000-0000-4000-8000-000000000001")
    assert txn_id(turn) == txn_id(UUID(str(turn)))
    assert txn_id(turn) != txn_id(UUID("00000000-0000-4000-8000-000000000002"))


def test_identifier_halves() -> None:
    assert server_name(BOT) == "example.org"
    assert server_name("@a:host:8448") == "host:8448"
    assert localpart(BOT) == "ufo"
    with pytest.raises(ValueError):
        server_name("nobody")
    assert SURFACE == "matrix"


def test_a_batch_reads_in_order() -> None:
    batch = {
        "next_batch": "s2",
        "rooms": {
            "join": {
                ROOM: {
                    "state": {"events": [{"type": "m.room.name", "content": {"name": "Ops"}}]},
                    "timeline": {"events": [text_event("a"), text_event("b")]},
                }
            },
            "invite": {
                "!new:example.org": {
                    "invite_state": {
                        "events": [
                            {
                                "type": "m.room.member",
                                "state_key": BOT,
                                "sender": "@alice:example.org",
                                "content": {"membership": "invite"},
                            }
                        ]
                    }
                }
            },
        },
    }
    assert [event["content"]["body"] for _, event in timeline(batch)] == ["a", "b"]
    assert room_names(batch) == {ROOM: "Ops"}
    assert list(invites(batch, BOT)) == [("!new:example.org", "@alice:example.org")]
    assert next_batch(batch) == "s2"
    with pytest.raises(ValueError):
        next_batch({})


def event_with_id(body: str, event_id: str) -> dict:
    return text_event(body) | {"event_id": event_id}


def test_a_cut_short_timeline_reads_its_gap_first_and_each_event_once() -> None:
    cut = {"limited": True, "prev_batch": "p1", "events": [event_with_id("c", "$c")]}
    whole = {"limited": False, "prev_batch": "p9", "events": [event_with_id("z", "$z")]}
    batch = {"rooms": {"join": {ROOM: {"timeline": cut}, "!calm:example.org": {"timeline": whole}}}}
    assert gaps(batch) == {ROOM: "p1"}
    earlier = {ROOM: [event_with_id("a", "$a"), event_with_id("b", "$b"), event_with_id("c", "$c")]}
    read = [(room, event["content"]["body"]) for room, event in timeline(batch, earlier)]
    assert read == [(ROOM, "a"), (ROOM, "b"), (ROOM, "c"), ("!calm:example.org", "z")]


def test_a_permalink_names_the_event() -> None:
    assert permalink(ROOM, "$abc") == "https://matrix.to/#/!room:example.org/$abc"


def test_a_threaded_message_carries_its_root() -> None:
    threaded = room_message(
        ROOM,
        text_event("in the thread", **{"m.relates_to": {"rel_type": "m.thread", "event_id": "$r"}}),
    )
    plain = room_message(ROOM, text_event("in the room"))
    assert threaded is not None and threaded.thread_root == "$r"
    assert plain is not None and plain.thread_root is None


def test_a_reply_relates_to_the_message_it_answers() -> None:
    assert reply_relation("$asked", None) == {"m.in_reply_to": {"event_id": "$asked"}}
    assert reply_relation("$asked", "$root") == {
        "rel_type": "m.thread",
        "event_id": "$root",
        "is_falling_back": True,
        "m.in_reply_to": {"event_id": "$asked"},
    }


def test_every_send_of_one_turn_has_its_own_transaction_id() -> None:
    turn = UUID("00000000-0000-4000-8000-000000000001")
    artifact = UUID("00000000-0000-4000-8000-00000000000a")
    reply = UUID("00000000-0000-4000-8000-00000000000b")
    ids = {
        part_txn_id(txn_id(turn), 1),
        part_txn_id(txn_id(turn), 2),
        file_txn_id(turn, artifact),
        say_txn_id(reply),
    }
    assert len(ids) == 4
    assert part_txn_id(txn_id(turn), 1) == txn_id(turn)
    assert file_txn_id(turn, artifact) == file_txn_id(turn, artifact)


@pytest.mark.parametrize(
    ("media_type", "msgtype"),
    [
        ("image/png", "m.image"),
        ("video/mp4", "m.video"),
        ("audio/ogg", "m.audio"),
        ("application/pdf", "m.file"),
        ("", "m.file"),
    ],
)
def test_a_file_is_sent_as_what_it_is(media_type: str, msgtype: str) -> None:
    assert msgtype_for(media_type) == msgtype


def test_a_file_message_carries_its_caption_and_its_name() -> None:
    captioned = file_content(
        "m.image", "chart.png", "Last week", "image/png", 12, "mxc://s/1", {"a": "b"}
    )
    bare = file_content("m.file", "notes.md", None, "text/markdown", 3, "mxc://s/2")
    assert captioned["body"] == "Last week" and captioned["filename"] == "chart.png"
    assert captioned["info"] == {"mimetype": "image/png", "size": 12}
    assert captioned["m.relates_to"] == {"a": "b"}
    assert bare["body"] == "notes.md" and "filename" not in bare
    assert "m.relates_to" not in bare


def test_a_message_carries_the_words_and_the_same_words_as_html() -> None:
    content = message_content("**now**", TEXT_MSGTYPE, {"m.in_reply_to": {"event_id": "$a"}})
    assert content["msgtype"] == TEXT_MSGTYPE
    assert content["body"] == "**now**"
    assert content["format"] == "org.matrix.custom.html"
    assert content["formatted_body"] == "<p><strong>now</strong></p>"
    assert content["m.relates_to"] == {"m.in_reply_to": {"event_id": "$a"}}
    assert "m.relates_to" not in message_content("hi", TEXT_MSGTYPE)


@pytest.mark.parametrize(
    ("markdown", "rendered"),
    [
        ("plain words", "<p>plain words</p>"),
        ("two\nlines", "<p>two<br />lines</p>"),
        (
            "**bold** and *thin* and _thin_",
            "<p><strong>bold</strong> and <em>thin</em> and <em>thin</em></p>",
        ),
        ("call `run()` first", "<p>call <code>run()</code> first</p>"),
        ("[the report](https://ufo.test/r)", '<p><a href="https://ufo.test/r">the report</a></p>'),
        (
            "[**bold link**](https://ufo.test)",
            '<p><a href="https://ufo.test"><strong>bold link</strong></a></p>',
        ),
        ("[nope](javascript:alert)", "<p>nope</p>"),
        ("## Heading", "<h2>Heading</h2>"),
        ("- one\n- two", "<ul><li>one</li><li>two</li></ul>"),
        ("1. one\n2. two", "<ol><li>one</li><li>two</li></ol>"),
        ("> quoted\n> again", "<blockquote>quoted<br />again</blockquote>"),
        ("a < b & c", "<p>a &lt; b &amp; c</p>"),
        (
            "```python\nx = 1 < 2\n```",
            '<pre><code class="language-python">x = 1 &lt; 2</code></pre>',
        ),
        ("```\nplain\n```", "<pre><code>plain</code></pre>"),
    ],
    ids=[
        "paragraph",
        "soft-break",
        "emphasis",
        "code-span",
        "link",
        "emphasis-in-link",
        "unfollowable-link",
        "heading",
        "bullets",
        "numbers",
        "quote",
        "escaped",
        "fence-with-language",
        "fence",
    ],
)
def test_markdown_renders_as_the_subset_matrix_names(markdown: str, rendered: str) -> None:
    assert html_body(markdown) == rendered


def test_a_reply_within_the_budget_is_one_message() -> None:
    reply = "First paragraph.\n\nSecond paragraph."
    assert parts(reply) == (reply,)
    assert parts("") == ("",)


def test_a_long_reply_splits_on_paragraph_boundaries() -> None:
    paragraph = "word " * 200
    reply = "\n\n".join(f"{n}. {paragraph}" for n in range(6))
    written = parts(reply)
    assert len(written) > 1
    assert "\n\n".join(written) == reply
    assert all(len(part.encode()) <= PART_BUDGET_BYTES for part in written)


def test_no_part_leaves_a_fence_open() -> None:
    code = "\n".join(f"line_{n} = {n}" for n in range(600))
    written = parts(f"Here it is.\n\n{FENCE}python\n{code}\n{FENCE}")
    assert len(written) > 1
    for part in written:
        assert part.count(FENCE) % 2 == 0, part[:80]
    assert all(FENCE not in part or part.strip().endswith(FENCE) for part in written)
    assert "line_599 = 599" in written[-1]


def test_cut_guarantees_progress_on_any_budget() -> None:
    """`_cut` always returns and every piece but the last is non-empty: a non-positive budget takes one
    character a call rather than wedging the loop."""
    assert _cut("abc", 0) == ["a", "b", "c"]
    assert _cut("éé", 1) == ["é", "é"]
    assert all(piece for piece in _cut("x" * 10, 3)[:-1])


@pytest.mark.parametrize(
    "opener_length",
    [PART_BUDGET_BYTES - len(FENCE) - 5, PART_BUDGET_BYTES + 100],
    ids=["opener-at-the-budget", "opener-past-the-budget"],
)
def test_a_fence_with_an_opener_at_or_past_the_budget_still_returns(opener_length: int) -> None:
    """The case that wedged `parts`: a fence opener as long as the budget drives the per-line
    budget to nothing. It returns, every part is a fence a client can close, and no part is empty."""
    written = parts(f"{FENCE}{'x' * opener_length}\ncode\n{FENCE}")
    assert written
    assert all(part for part in written)
    assert "\n".join(written).count(FENCE) % 2 == 0


def test_a_split_fence_keeps_its_indentation() -> None:
    """A fenced block cut across parts keeps the whitespace its lines were written with — the
    continuation of a cut line is not stripped to fit."""
    code = "\n".join(f"    line_{n} = {n}" for n in range(300))
    written = parts(f"Here it is.\n\n{FENCE}python\n{code}\n{FENCE}")
    assert len(written) > 1
    assert any("    line_1 = 1" in part for part in written)
    assert "    line_299 = 299" in written[-1]


@pytest.mark.parametrize(
    "written",
    [
        '"' * 20000,
        "&" * 20000,
        '> "\n\n' * 4000,
        "- &\n" * 4000,
        "word" * 5000,
        "\U0001f600" * 3000,
    ],
    ids=["quotes", "ampersands", "quoted-lines", "list-items", "one-long-word", "multibyte"],
)
def test_every_part_fits_one_event_however_it_expands(written: str) -> None:
    for part in parts(written):
        event = json.dumps(message_content(part, TEXT_MSGTYPE, reply_relation("$a", "$b")))
        assert len(event.encode()) + ENVELOPE_BYTES < EVENT_LIMIT_BYTES, len(event)


def test_an_mxc_uri_names_a_server_and_one_media_id() -> None:
    """A file's location arrives inside an event, so the uri is whoever sent it speaking, not a
    value this surface chose."""
    assert media_parts("mxc://example.org/AbC123") == ("example.org", "AbC123")


@pytest.mark.parametrize(
    "uri",
    (
        "https://example.org/AbC123",
        "mxc://example.org",
        "mxc://example.org/",
        "mxc:///AbC123",
        "mxc://example.org/nested/id",
        "mxc://example.org/../../secret",
        "mxc://example.org/..",
        "mxc://example.org/.",
        "mxc://../id",
        "mxc://./id",
        "mxc://../config",
        "mxc://example.org/a%2fb",
        "mxc://example.org/id?query",
        "mxc://example.org/id#fragment",
    ),
)
def test_a_uri_carrying_a_path_of_its_own_is_not_a_media_id(uri: str) -> None:
    """A single-segment `..` contains no character a ban would name, and httpx normalises dot segments
    after the fact — so both parts are matched against what they may be."""
    assert media_parts(uri) is None


def _file_event(**content: object) -> dict[str, object]:
    base = {
        "msgtype": "m.image",
        "body": "chart.png",
        "info": {"mimetype": "image/png", "size": 4},
        "url": "mxc://example.org/AbC123",
    }
    return {
        "type": "m.room.message",
        "event_id": "$f",
        "sender": "@alice:example.org",
        "content": {**base, **content},
    }


def test_a_member_file_is_read_as_a_file() -> None:
    """The plain form: a room that is not encrypted names the `mxc://` outright."""
    shared = room_file(ROOM, _file_event())
    assert shared is not None
    assert (shared.filename, shared.media_type, shared.size_bytes) == ("chart.png", "image/png", 4)
    assert shared.url == "mxc://example.org/AbC123"
    assert shared.sealed is None


def test_a_sealed_member_file_carries_its_key_and_no_url() -> None:
    """The encrypted form, read after the device has decrypted the event: the `mxc://` moves inside
    `file`, so an event naming a bare `url` is the plain case and this one is not."""
    sealed = {"url": "mxc://example.org/AbC123", "key": {"alg": "A256CTR"}, "iv": "x"}
    shared = room_file(ROOM, _file_event(url=None, file=sealed))
    assert shared is not None
    assert shared.url == ""
    assert shared.sealed == sealed


def test_a_file_offering_both_a_url_and_a_sealed_file_is_refused() -> None:
    """The two disagree about whether the bytes are sealed. Resolving it either way lets the sender
    choose, so neither is chosen."""
    sealed = {"url": "mxc://example.org/x", "key": {"alg": "A256CTR"}, "iv": "x"}
    assert room_file(ROOM, _file_event(file=sealed)) is None


@pytest.mark.parametrize(
    "content",
    (
        {"msgtype": "m.text"},
        {"msgtype": "m.notice"},
        {"url": None},
        {"url": ""},
        {"body": "", "filename": None},
        {"m.new_content": {"msgtype": "m.image"}},
        {"m.relates_to": {"rel_type": "m.replace", "event_id": "$a"}},
    ),
    ids=("text", "notice", "no-url", "empty-url", "unnamed", "edit", "replacement"),
)
def test_what_is_not_a_member_file(content: dict[str, object]) -> None:
    """The same refusals `room_message` makes, plus a file naming neither a url nor a sealed one."""
    assert room_file(ROOM, _file_event(**content)) is None


def test_a_caption_is_what_the_member_said_and_the_name_is_the_file() -> None:
    """A file sent with words carries both and neither stands in for the other: one reading discards the
    caption, the other writes the workspace file under a sentence."""
    shared = room_file(ROOM, _file_event(body="here is the chart you asked for", filename="c.png"))
    assert shared is not None
    assert shared.caption == "here is the chart you asked for"
    assert shared.filename == "c.png"
    bare = room_file(ROOM, _file_event(body="c.png"))
    assert bare is not None and (bare.caption, bare.filename) == ("c.png", "c.png")


def test_a_caption_names_the_file_and_the_body_is_the_caption() -> None:
    """A file sent with words carries `filename` beside them; one sent bare is named by its body."""
    named = room_file(ROOM, _file_event(body="last week's numbers", filename="q3.pdf"))
    assert named is not None and named.filename == "q3.pdf"
    bare = room_file(ROOM, _file_event(body="q3.pdf"))
    assert bare is not None and bare.filename == "q3.pdf"
