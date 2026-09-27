"""The todo checklist pack: a nested board, the status verb that moves items on it, and the verb
that hands items to a subagent.

The board is not turn-scoped state the agent holds in context; it is the extension's own durable
record, kept in the pack's scoped store keyed by the conversation so a later turn in the same
conversation reads back the list it left. Every conversation holds exactly one board and writes
only its own — a subagent runs on a conversation of its own, so two children working at once
cannot clobber each other and no read has to merge.

An item may hold items, and that is what lets one board carry work at two sizes: the shape of the
job at the top, the moves it takes underneath. A node's status is computed from its children rather
than set, so a parent cannot be marked done over unfinished work; only a leaf takes a status
directly. `delegate_todos` hands leaves to a subagent, which opens a board of its own and delivers
its result back — the parent marks them then. A child holds the two bookkeeping verbs, so it nests
its own work the way its parent does; it does not hold `delegate_todos`. That verb starts a turn
and binds the speaker's authority, and a subagent's profile allowlist is what decides which turns
it may start — a spawning verb in the subagent default set would reach past it, which is why no
other one is there.

Every turn on a conversation carrying a board opens holding it: a heartbeat fire and a subagent's
returning result both arrive with fresh context, and a tool the agent must remember to call is no
use to a turn that does not remember there is anything to call it about."""

import json
from pathlib import Path
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ufo.sdk.context import ExtensionContext
from ufo.sdk.manifest import (
    CONVERSATION_TASK_DESCRIPTION_MAX_CHARS,
    CONVERSATION_TASK_TITLE_MAX_CHARS,
    CONVERSATION_TASKS_MAX,
    ConversationSlotContext,
    ConversationSlotProvider,
    ConversationTask,
    HookContext,
    HookOutcome,
    HookSpec,
    InjectContext,
    Manifest,
    MetricSpec,
    PromptSection,
    TasksSlotPayload,
)
from ufo.sdk.o11y import emit_metric
from ufo.sdk.tools import (
    TextContent,
    ToolContext,
    ToolDef,
    ToolFailure,
    ToolResult,
)

NAME = "todos"
VERSION = "0.1.0"
UPDATE_TODO_LIST_TOOL = "update_todo_list"
UPDATE_TODO_STATUS_TOOL = "update_todo_status"
DELEGATE_TODOS_TOOL = "delegate_todos"
LIST_WRITTEN_METRIC = "todo_list_written_total"
STATUS_SET_METRIC = "todo_status_set_total"
DELEGATED_METRIC = "todo_delegated_total"
BOARD_INJECTED_METRIC = "todo_board_injected_total"
BOARD_KEY_PREFIX = "board/"
"""The board's key. A nested board cannot be read by the image this release replaces: its
`TodoTask` declares no `tasks` and ignores unknown keys, so validating one drops every node's
children and the next write flattens the board for good, and its `TodoStatus` holds no `blocked`,
so a board carrying one raises instead of reading. The migrate Job completes before the fleet
rolls and both images then serve the same conversations, so the shape moves to a key the outgoing
one never reads. Boards open before this release stay at the old key, unread — a conversation
mid-flight opens a fresh list once."""
MAX_DEPTH = 3
PATH_SEPARATOR = "."

TodoStatus = Literal["pending", "in_progress", "completed", "blocked"]
PENDING: TodoStatus = "pending"
IN_PROGRESS: TodoStatus = "in_progress"
COMPLETED: TodoStatus = "completed"
BLOCKED: TodoStatus = "blocked"

UPDATE_TODO_LIST_DESCRIPTION = (
    "Create or revise a task checklist to track progress on complex, multi-step requests. Use for "
    "any task with multiple steps or tool calls. A task may hold its own sub-tasks, so the shape "
    "of the job goes at the top level and the moves it takes go underneath. The checklist appears "
    "in the UI to show progress. Create at the START of work, not after."
)
UPDATE_TODO_STATUS_DESCRIPTION = (
    "Update task statuses in the checklist. Address a task by its path — '2' is the second "
    "top-level task, '2.1' is that task's first sub-task. Mark tasks 'in_progress' when starting "
    "and 'completed' when done — immediately, don't batch. Mark 'blocked' when a task cannot "
    "proceed. Multiple tasks can be in_progress simultaneously for parallel work. A task that has "
    "sub-tasks takes its status from them and cannot be set directly."
)
DELEGATE_TODOS_DESCRIPTION = (
    "Hand one or more tasks on the checklist to a subagent, which keeps a checklist of its own and "
    "delivers its result back to this conversation. Use it instead of spawn for work the checklist "
    "already holds, so the board records who has what. The tasks are marked in_progress; mark them "
    "completed or blocked yourself when the result arrives. Call it once per subagent — issue "
    "several calls in one response to run subagents in parallel."
)

SECTION_NAME = "todo_list"
SECTION_BODY = (Path(__file__).parent / "prompts" / "todo_list_section.md").read_text().strip()


class TodoTask(BaseModel):
    """One item, and the items it is made of. `tasks` is what makes the board nested: a task that
    holds sub-tasks takes its status from them (`rolled_up`) rather than carrying one of its own,
    so a node cannot read as done over an unfinished child."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, description="The task text.")
    status: TodoStatus = Field(default=PENDING, description="The task's current status.")
    tasks: tuple["TodoTask", ...] = Field(
        default=(), description="Sub-tasks this task is made of, if it has any."
    )

    @property
    def rolled_up(self) -> TodoStatus:
        """A leaf's own status; a node's read off its children. Blocked wins over in_progress, and
        in_progress over pending, so a node reports the most arresting thing under it — nothing
        under it is what the reader has to act on next."""
        if not self.tasks:
            return self.status
        children = tuple(task.rolled_up for task in self.tasks)
        if all(status == COMPLETED for status in children):
            return COMPLETED
        if BLOCKED in children:
            return BLOCKED
        if IN_PROGRESS in children or COMPLETED in children:
            return IN_PROGRESS
        return PENDING


class TodoBoard(BaseModel):
    """The durable checklist the pack keeps in its scoped store — a validated shape every tool
    reads and writes, distinct from the wire input models the model fills."""

    model_config = ConfigDict(extra="forbid")

    title: str
    tasks: list[TodoTask]

    @model_validator(mode="after")
    def _within_bounds(self) -> Self:
        if _depth(self.tasks) > MAX_DEPTH:
            raise ValueError(f"todo list nests deeper than {MAX_DEPTH} levels")
        return self


def _depth(tasks: tuple[TodoTask, ...] | list[TodoTask]) -> int:
    return 0 if not tasks else 1 + max(_depth(task.tasks) for task in tasks)


def _leaves(task: TodoTask) -> tuple[TodoTask, ...]:
    """A status set on a node is never read: the node derives from its leaves."""
    if not task.tasks:
        return (task,)
    return tuple(leaf for child in task.tasks for leaf in _leaves(child))


def walk(
    tasks: tuple[TodoTask, ...] | list[TodoTask], prefix: str = ""
) -> tuple[tuple[str, TodoTask], ...]:
    """Every task with the path that addresses it, depth-first in board order — the one traversal
    the status verb, the delegation verb, the injection, the slot and the eval graders all read, so
    a path means the same thing to the model, the member, and the record."""
    found: list[tuple[str, TodoTask]] = []
    for index, task in enumerate(tasks, start=1):
        path = f"{prefix}{index}"
        found.append((path, task))
        found.extend(walk(task.tasks, f"{path}{PATH_SEPARATOR}"))
    return tuple(found)


class UpdateTodoListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(description="Title of the todo list.")
    tasks: tuple[TodoTask, ...] = Field(
        description="Complete list of tasks — this REPLACES the existing list entirely."
    )


class TodoStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        pattern=r"^\d+(\.\d+)*$",
        description="The task's path: '2' for the second top-level task, '2.1' for its first "
        "sub-task.",
    )
    status: TodoStatus = Field(description="New status for the task.")


class UpdateTodoStatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    updates: tuple[TodoStatusUpdate, ...] = Field(
        min_length=1, description="List of status updates to apply."
    )


class DelegateTodosInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str = Field(
        description="The subagent profile or workspace agent taking this work, e.g. 'coding'."
    )
    paths: tuple[str, ...] = Field(
        min_length=1,
        description="Paths of the tasks this subagent takes, e.g. ['2', '4.1']. Their sub-tasks go "
        "with it. They are marked in_progress; mark them yourself when the result arrives.",
    )
    payload: dict[str, Any] = Field(
        description="Arguments matching the target's input schema, exactly as `spawn` takes them. "
        "Targets name the work differently — `coding` and `research` take `objective`, most "
        "profiles take `task` — so write these tasks out in the field the target declares. The "
        "spawn-catalog skill lists every target and its payload."
    )


def _require_ext(ctx: ToolContext) -> ExtensionContext:
    if ctx.ext is None:
        raise RuntimeError("todo tools require the todos extension context")
    return ctx.ext


def _board_key(conversation_id: UUID) -> str:
    return f"{BOARD_KEY_PREFIX}{conversation_id}"


def render(board: TodoBoard) -> str:
    """The board as a path-addressed indented list — what every tool returns and what the hook
    injects, so the model reads one rendering of its work wherever it meets it."""
    lines = [board.title] if board.title else []
    for path, task in walk(board.tasks):
        indent = "  " * path.count(PATH_SEPARATOR)
        lines.append(f"{indent}{path}. [{task.rolled_up}] {task.description}")
    return "\n".join(lines) if lines else "the list is empty"


def _board_result(board: TodoBoard) -> ToolResult:
    return ToolResult(content=(TextContent(text=render(board)),))


async def _read_board(ext: ExtensionContext, key: str) -> TodoBoard | None:
    raw = await ext.store.get(key)
    return None if raw is None else TodoBoard.model_validate(raw)


async def _write_board(ext: ExtensionContext, key: str, board: TodoBoard) -> None:
    await ext.store.put(key, json.loads(board.model_dump_json()))


async def _require_board(ctx: ToolContext) -> tuple[ExtensionContext, str, TodoBoard]:
    ext = _require_ext(ctx)
    key = _board_key(ctx.turn.conversation_id)
    board = await _read_board(ext, key)
    if board is None or not board.tasks:
        raise ValueError("no todo list — call update_todo_list first")
    return ext, key, board


def _at(board: TodoBoard, path: str) -> TodoTask:
    found = dict(walk(board.tasks)).get(path)
    if found is None:
        addressable = ", ".join(item for item, _task in walk(board.tasks))
        raise ValueError(f"no todo at path {path!r}; the list holds: {addressable}")
    return found


async def update_todo_list(ctx: ToolContext, args: UpdateTodoListInput) -> ToolResult:
    ext = _require_ext(ctx)
    board = TodoBoard(title=args.title, tasks=list(args.tasks))
    await _write_board(ext, _board_key(ctx.turn.conversation_id), board)
    emit_metric(LIST_WRITTEN_METRIC, tasks=str(len(tuple(walk(board.tasks)))))
    return _board_result(board)


async def update_todo_status(ctx: ToolContext, args: UpdateTodoStatusInput) -> ToolResult:
    """Every update in one call, against one read of the board.

    The board is one key, so a handler that reads it and writes it back cannot run beside itself:
    both tools leave `parallel_safe` false, and the engine then dispatches their same-round calls in
    sequence. Two concurrent calls would each write a board missing the other's move."""
    ext, key, board = await _require_board(ctx)
    for update in args.updates:
        task = _at(board, update.path)
        if task.tasks:
            raise ValueError(
                f"todo {update.path!r} has sub-tasks, so it takes its status from them; set the "
                "sub-tasks instead"
            )
        task.status = update.status
        emit_metric(STATUS_SET_METRIC, status=update.status)
    await _write_board(ext, key, board)
    return _board_result(board)


async def delegate_todos(ctx: ToolContext, args: DelegateTodosInput) -> ToolResult:
    """Hand the named tasks to one subagent and mark them in_progress.

    The payload is the model's, not this tool's. Targets declare their own input contracts —
    `coding` and `research` take `objective`, most profiles take `task` — so a fixed key here would
    make every one of those targets impossible to delegate to, and the refusal would name a field
    the caller has no way to set.

    This verb is the parent's. `requester_member_id` carries the speaker, the way the builtin spawn
    tool does. Without it a
    workspace agent that is not visible to the whole workspace refuses even its own owner, and a
    target that needs the member's own model key looks up an empty account set and refuses a member
    who has connected one. The engine only fills `speaker_member_id` for a tool that declares
    member authority, so this verb declares it and the two bookkeeping verbs do not.

    The child is spawned with this call's own idempotency key, so a dispatch step re-executed by
    crash recovery reconnects to the child it already started rather than paying for a second one.
    It delivers its own result to this conversation, so this turn ends rather than holding the
    conversation's partition open while the child works.

    The board is written only once the child is running: a spawn that never started would otherwise
    leave tasks reading in_progress with nothing working on them, which is the one state the board
    cannot be read out of."""
    ext, key, board = await _require_board(ctx)
    handed = tuple((path, _at(board, path)) for path in args.paths)
    try:
        result = await ctx.spawn(
            args.target,
            args.payload,
            background=True,
            dedup_key=ctx.idempotency_key,
            delivers_result=True,
            requester_member_id=ctx.speaker_member_id,
            requesting_message_ref=ctx.requesting_message_ref,
        )
    except Exception as error:
        return _delegation_refused(args, error).result()
    for _path, task in handed:
        for leaf in _leaves(task):
            leaf.status = IN_PROGRESS
    await _write_board(ext, key, board)
    emit_metric(DELEGATED_METRIC, target=args.target, tasks=str(len(handed)))
    return ToolResult(
        content=(
            TextContent(
                text=f"{args.target} is taking {', '.join(path for path, _task in handed)} as "
                f"subagent {result.turn_id}. It delivers its result to this conversation when it "
                "finishes. End your turn; you will be woken with its answer, and you mark these "
                f"tasks then.\n\n{render(board)}"
            ),
        )
    )


def _delegation_refused(args: DelegateTodosInput, error: Exception) -> ToolFailure:
    detail = str(error).strip() or type(error).__name__
    named = ", ".join(args.paths)
    still = f"task {named} is" if len(args.paths) == 1 else f"tasks {named} are"
    return ToolFailure(
        operation=DELEGATE_TODOS_TOOL,
        summary=f"{args.target!r} would not start: {detail}. No task moved — {still} still yours.",
    )


async def _inject_board(ctx: HookContext) -> HookOutcome:
    """`user_prompt_submit` gates, and a gating hook that raises fails closed to a Deny."""
    if ctx.turn is None:
        return None
    board = await _read_board(ctx.ext, _board_key(ctx.turn.conversation_id))
    if board is None or not board.tasks:
        return None
    outstanding = sum(1 for _path, task in walk(board.tasks) if task.rolled_up != COMPLETED)
    emit_metric(BOARD_INJECTED_METRIC, outstanding=str(bool(outstanding)).lower())
    closing = (
        "every task is complete; confirm the work is finished before saying so."
        if not outstanding
        else "update it as you go; a task you delegated is marked when its result arrives."
    )
    return InjectContext(text=f"<todo_list>\n{render(board)}\n{closing}\n</todo_list>")


async def _summarize_tasks(ctx: ConversationSlotContext) -> int | None:
    board = await _read_board(ctx.ext, _board_key(ctx.conversation_id))
    return None if board is None else len(walk(board.tasks))


async def _read_tasks(ctx: ConversationSlotContext) -> TasksSlotPayload:
    board = await _read_board(ctx.ext, _board_key(ctx.conversation_id))
    if board is None:
        return TasksSlotPayload(
            title="",
            tasks=(),
            total_count=0,
            completed_count=0,
            truncated=False,
        )
    walked = walk(board.tasks)
    truncated = (
        len(board.title) > CONVERSATION_TASK_TITLE_MAX_CHARS or len(walked) > CONVERSATION_TASKS_MAX
    )
    tasks: list[ConversationTask] = []
    for path, task in walked[:CONVERSATION_TASKS_MAX]:
        if len(task.description) > CONVERSATION_TASK_DESCRIPTION_MAX_CHARS:
            truncated = True
        tasks.append(
            ConversationTask(
                description=task.description[:CONVERSATION_TASK_DESCRIPTION_MAX_CHARS],
                status=task.rolled_up,
                depth=path.count(PATH_SEPARATOR),
            )
        )
    return TasksSlotPayload(
        title=board.title[:CONVERSATION_TASK_TITLE_MAX_CHARS],
        tasks=tuple(tasks),
        total_count=len(walked),
        completed_count=sum(1 for _path, task in walked if task.rolled_up == COMPLETED),
        truncated=truncated,
    )


TASKS_SLOT = ConversationSlotProvider(
    id="tasks",
    label="Tasks",
    icon="task",
    content=TasksSlotPayload,
    summarize=_summarize_tasks,
    read=_read_tasks,
)


def manifest() -> Manifest:
    return Manifest(
        name=NAME,
        version=VERSION,
        tools=(
            ToolDef(
                name=UPDATE_TODO_LIST_TOOL,
                description=UPDATE_TODO_LIST_DESCRIPTION,
                input_model=UpdateTodoListInput,
                handler=update_todo_list,
                side_effecting=True,
                subagent_default=True,
                binds_member_authority=False,
            ),
            ToolDef(
                name=UPDATE_TODO_STATUS_TOOL,
                description=UPDATE_TODO_STATUS_DESCRIPTION,
                input_model=UpdateTodoStatusInput,
                handler=update_todo_status,
                side_effecting=True,
                subagent_default=True,
                binds_member_authority=False,
            ),
            ToolDef(
                name=DELEGATE_TODOS_TOOL,
                description=DELEGATE_TODOS_DESCRIPTION,
                input_model=DelegateTodosInput,
                handler=delegate_todos,
                side_effecting=True,
            ),
        ),
        hooks=(HookSpec(event="user_prompt_submit", handler=_inject_board, best_effort=True),),
        prompt_sections=(PromptSection(name=SECTION_NAME, body=SECTION_BODY),),
        conversation_slots=(TASKS_SLOT,),
        metrics=(
            MetricSpec(name=LIST_WRITTEN_METRIC, kind="counter", dimensions=("tasks",)),
            MetricSpec(name=STATUS_SET_METRIC, kind="counter", dimensions=("status",)),
            MetricSpec(name=DELEGATED_METRIC, kind="counter", dimensions=("target", "tasks")),
            MetricSpec(name=BOARD_INJECTED_METRIC, kind="counter", dimensions=("outstanding",)),
        ),
    )
