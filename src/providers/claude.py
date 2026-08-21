"""Claude Code provider built on the Claude Agent SDK.

Holds one connected ClaudeSDKClient per channel. Each message is a turn: query
then drain the response, mapping SDK blocks to our Block type. Tools not in
allowed_tools hit can_use_tool, which we send to the broker for approval --
except edits under acceptEdits, which this layer approves itself because the
CLI's mode never reaches a tool the callback already intercepted.

A turn ends at a result frame, but only once no delegated task is still in
flight. The CLI emits a result when the *turn* ends, not when the *run* ends:
a subagent or workflow keeps going past it and wakes the parent for a follow-up
turn later. Stopping at the first result would end the turn while that work is
still running, leaving its output unread until the next prompt dragged it out.
TaskLedger tracks the in-flight set so we can tell the two apart.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from typing import Any, AsyncIterator

from claude_agent_sdk import (
    TERMINAL_TASK_STATUSES,
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    Message,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    get_session_info,
    get_session_messages,
    list_sessions,
)

from src.providers.base import (
    Block,
    BlockKind,
    ContextState,
    PermissionBroker,
    Provider,
    TaskStatus,
)

log = logging.getLogger("src")

_SETTING_SOURCES = ["user", "project", "local"]

# Task types whose completion wakes the parent for a follow-up turn, so a
# result frame arriving while one is in flight does not end the run. Mirrors
# the SDK's own DEFERRING_TASK_TYPES: background shells, monitors, and
# teammates are deliberately excluded because they may never reach a terminal
# status, and waiting on one would hang the turn forever.
_DEFERRING_TASK_TYPES = frozenset({"local_agent", "local_workflow"})

# Give up on a turn if the stream goes completely quiet for this long while we
# are still waiting on tasks. Releases the session lock instead of wedging it;
# whatever arrives late is recovered by _drain_stale on the next turn.
_IDLE_TIMEOUT = 600.0

# How long the stream must be silent before a backlog drain is considered done.
_DRAIN_QUIET = 1.0

# Tools acceptEdits is supposed to wave through. The CLI's own acceptEdits
# never gets a say: the SDK only shadows can_use_tool for bypassPermissions and
# for whole-tool allowed_tools entries, so under acceptEdits the callback still
# fires and the broker polls for every edit. Honouring the mode here is what
# makes it behave as its name claims.
_EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})

# How long to keep reading after a result that a settled task says should be
# followed by a continuation turn. Waking the parent costs a model round-trip,
# so this has to outlast normal API latency; it only ever delays the "Done"
# status, since everything before it has already been rendered.
_CONTINUATION_GRACE = 30.0


class TaskLedger:
    """Delegated tasks (subagents, workflows) that are still running.

    Terminal completion can arrive as either a task_notification or a
    task_updated patch — not every task emits both — so both clear the entry.
    """

    def __init__(self) -> None:
        self._inflight: set[str] = set()

    @property
    def idle(self) -> bool:
        """True when no delegated task is running, so a result ends the run."""
        return not self._inflight

    def __len__(self) -> int:
        return len(self._inflight)

    def observe(self, message: Message) -> bool:
        """Fold one message into the in-flight set.

        Returns True when a *tracked* task just reached a terminal state. That
        is the cue that the CLI will wake the parent for a continuation turn,
        which the caller needs in order to tell a run-ending result apart from
        one that merely closes the turn that dispatched the task.
        """
        if isinstance(message, TaskStartedMessage):
            if message.task_type in _DEFERRING_TASK_TYPES:
                self._inflight.add(message.task_id)
        elif isinstance(message, TaskNotificationMessage):
            return self._settle(message.task_id)
        elif isinstance(message, TaskUpdatedMessage):
            if message.status in TERMINAL_TASK_STATUSES:
                return self._settle(message.task_id)
        return False

    def _settle(self, task_id: str) -> bool:
        """Clear a task, reporting whether it was one we were tracking.

        Terminal completion can arrive twice for the same task (a notification
        and a patch); only the first is reported, so a continuation is never
        counted more than once.
        """
        if task_id in self._inflight:
            self._inflight.discard(task_id)
            return True
        return False



def session_cwd(session_id: str) -> str | None:
    """Resolve a session's working directory from Claude's own session data."""
    info = get_session_info(session_id)
    return info.cwd if info else None


def recent_sessions(limit: int = 100) -> list:
    """All resumable sessions, newest first, across every project."""
    return list_sessions(limit=limit)


def session_title(session_id: str) -> str | None:
    """The agent-generated title/summary for a session, if any."""
    info = get_session_info(session_id)
    if not info:
        return None
    return info.custom_title or info.summary or None


def textual_history(session_id: str) -> list[tuple[str, str]]:
    """Prior conversation as (role, text), skipping tool calls and results."""
    out: list[tuple[str, str]] = []
    for m in get_session_messages(session_id):
        message = getattr(m, "message", None)
        if not isinstance(message, dict):
            continue
        role = message.get("role", "")
        content = message.get("content")
        if isinstance(content, str):
            if content.strip():
                out.append((role, content))
            continue
        if not isinstance(content, list):
            continue
        texts = [
            b["text"]
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()
        ]
        if texts:
            out.append((role, "\n".join(texts)))
    return out


def _build_options(
    *,
    cwd: str,
    resume: str | None,
    session_id: str | None,
    allowed_tools: list[str],
    skills: Any,
    model: str | None,
    can_use_tool: Any = None,
    permission_mode: str = "default",
) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        cwd=cwd,
        resume=resume,
        session_id=session_id,
        permission_mode=permission_mode,
        allowed_tools=allowed_tools,
        can_use_tool=can_use_tool,
        model=model,
        skills=skills,
        setting_sources=_SETTING_SOURCES,
    )


async def fetch_commands(
    cwd: str, *, skills: Any, model: str | None
) -> list[dict[str, Any]]:
    """Connect a throwaway client just to read available skills/commands."""
    options = _build_options(
        cwd=cwd,
        resume=None,
        session_id=None,
        allowed_tools=[],
        skills=skills,
        model=model,
    )
    client = ClaudeSDKClient(options=options)
    await client.connect()
    try:
        info = await client.get_server_info()
    finally:
        await client.disconnect()
    return list(info.get("commands", [])) if info else []


def _stringify_tool_result(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict):
            if item.get("type") == "text" and "text" in item:
                parts.append(str(item["text"]))
            else:
                parts.append(json.dumps(item, ensure_ascii=False))
        else:
            parts.append(str(item))
    return "\n".join(parts)


def _format_ask_question(tool_input: dict[str, Any]) -> str:
    lines: list[str] = []
    for q in tool_input.get("questions") or []:
        header = str(q.get("header", "")).strip()
        question = str(q.get("question", "")).strip()
        lines.append(f"{header}: {question}" if header else question)
        for opt in q.get("options") or []:
            label = str(opt.get("label", "")).strip()
            desc = " ".join(str(opt.get("description", "")).split())
            lines.append(f"  - {label}: {desc}" if desc else f"  - {label}")
        lines.append("")
    return "\n".join(lines).strip()


def _context_from_usage(model_usage: dict[str, Any] | None) -> ContextState | None:
    """Context fullness from a result's per-model usage.

    `model_usage` has one entry per model that ran, and a subagent on a
    smaller model would skew any total. The fullest window is the one that
    matters -- it is the one that will force a compaction -- so entries are
    scored individually and the highest kept.
    """
    best: ContextState | None = None
    for name, usage in (model_usage or {}).items():
        if not isinstance(usage, dict):
            continue
        limit = usage.get("contextWindow") or 0
        if limit <= 0:
            continue
        used = (
            (usage.get("inputTokens") or 0)
            + (usage.get("cacheReadInputTokens") or 0)
            + (usage.get("cacheCreationInputTokens") or 0)
        )
        state = ContextState(used=used, limit=limit, model=name)
        if best is None or state.pct > best.pct:
            best = state
    return best


def _format_task_usage(usage: dict[str, Any] | None) -> str:
    """One-line usage summary for a delegated task, e.g. '45.2k tokens · 12 tools · 8.3s'."""
    if not usage:
        return ""
    bits: list[str] = []
    tokens = usage.get("total_tokens")
    if tokens:
        bits.append(f"{tokens:,} tokens")
    tools = usage.get("tool_uses")
    if tools:
        bits.append(f"{tools} tools")
    duration = usage.get("duration_ms")
    if duration:
        bits.append(f"{duration / 1000:.1f}s")
    return " · ".join(bits)


def _format_task_progress(message: TaskProgressMessage) -> str:
    bits = [_format_task_usage(message.usage)]
    if message.last_tool_name:
        bits.append(f"last: {message.last_tool_name}")
    return " · ".join(b for b in bits if b) or "running…"


def _format_tool_input(name: str, tool_input: dict[str, Any]) -> str:
    if name == "Bash":
        return str(tool_input.get("command", ""))
    if name == "AskUserQuestion":
        return _format_ask_question(tool_input)
    if name in ("Write", "Edit", "Read", "NotebookEdit"):
        path = tool_input.get("file_path", "")
        extra = ""
        if name == "Write" and "content" in tool_input:
            extra = f"\n{tool_input['content']}"
        return f"{path}{extra}"
    try:
        return json.dumps(tool_input, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(tool_input)


class ClaudeProvider(Provider):
    def __init__(
        self,
        *,
        cwd: str,
        channel_id: int,
        broker: PermissionBroker,
        allowed_tools: list[str],
        skills: Any = "all",
        model: str | None = None,
        resume: str | None = None,
        session_id: str | None = None,
        permission_mode: str = "default",
    ) -> None:
        self.cwd = cwd
        self.channel_id = channel_id
        self.broker = broker
        self.allowed_tools = allowed_tools
        self.skills = skills
        self.model = model
        self._resume = resume
        self._new_session_id = session_id
        self.session_id = resume or session_id
        self.permission_mode = permission_mode
        self._client: ClaudeSDKClient | None = None
        # A background task continuously drains the SDK's message stream into
        # this queue, and every turn pulls from the queue instead of the
        # stream directly. This is deliberate, not just convenient: an async
        # generator (receive_messages()) has no task of its own, so awaiting
        # it inline and cancelling on an idle timeout would inject
        # CancelledError into its currently-suspended frame and tear it down
        # for good — silently killing the stream for the rest of the session.
        # A dedicated pump task absorbs that: cancelling our *wait* on the
        # queue (asyncio.Queue.get is cancellation-safe) never touches the
        # pump, which keeps reading regardless of whether anyone is currently
        # waiting on it — so nothing is lost between turns either.
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._reader_task: asyncio.Task[None] | None = None
        self._ledger = TaskLedger()
        # Set when a turn gives up after _IDLE_TIMEOUT of silence. Cleared by
        # _drain_stale once it has flushed whatever arrived late.
        self._stalled = False

    async def _can_use_tool(self, tool_name, tool_input, context):  # noqa: ANN001
        # AskUserQuestion is not an approval; poll the user and feed the answer
        # back through the deny message, which the model reads as the result.
        if tool_name == "AskUserQuestion":
            answer = await self.broker.ask(self.channel_id, tool_input)
            return PermissionResultDeny(message=answer)
        if self.permission_mode == "acceptEdits" and tool_name in _EDIT_TOOLS:
            return PermissionResultAllow()
        allowed, reason = await self.broker.request(
            self.channel_id, tool_name, tool_input
        )
        if allowed:
            return PermissionResultAllow()
        return PermissionResultDeny(message=reason or "Denied by user.")

    async def start(self) -> None:
        options = _build_options(
            cwd=self.cwd,
            resume=self._resume,
            session_id=None if self._resume else self._new_session_id,
            allowed_tools=self.allowed_tools,
            skills=self.skills,
            model=self.model,
            can_use_tool=self._can_use_tool,
            permission_mode=self.permission_mode,
        )
        self._client = ClaudeSDKClient(options=options)
        await self._client.connect()
        self._reader_task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        """Continuously drain the SDK's message stream into our queue.

        Runs independently of any turn's read timeout — see the comment on
        `_queue` in __init__ for why this indirection exists. A read failure
        (the CLI process died, the transport errored) is queued as the
        exception itself so `_next_message` can surface it to a turn instead
        of leaving that turn waiting forever.
        """
        assert self._client is not None
        try:
            async for message in self._client.receive_messages():
                await self._queue.put(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - forwarded to the consumer, not swallowed
            await self._queue.put(exc)

    async def list_commands(self) -> list[dict[str, Any]]:
        if self._client is None:
            return []
        info = await self._client.get_server_info()
        return list(info.get("commands", [])) if info else []

    async def title(self) -> str | None:
        if not self.session_id:
            return None
        return session_title(self.session_id)

    async def set_mode(self, mode: str) -> None:
        if self._client is not None:
            await self._client.set_permission_mode(mode)
        self.permission_mode = mode

    async def run_turn(self, text: str) -> AsyncIterator[Block]:
        if self._client is None or self._reader_task is None:
            raise RuntimeError("Provider not started")
        async for block in self._drain_stale():
            yield block
        await self._client.query(text)
        async for block in self._read_until_run_end():
            yield block

    async def _next_message(self, timeout: float = _IDLE_TIMEOUT) -> Any:
        item = await asyncio.wait_for(self._queue.get(), timeout=timeout)
        if isinstance(item, BaseException):
            raise item
        return item

    async def _drain_stale(self) -> AsyncIterator[Block]:
        """Flush output a prior idle-timeout left unread, if any.

        Pulls from the same stream a fresh turn would use, stopping once it
        has gone quiet for _DRAIN_QUIET seconds rather than at a single
        message — a stalled turn may have left more than one message queued.

        Runs whenever the queue is non-empty, not only after a stall. Nothing
        should be pending at the start of a turn, so anything sitting there is
        output from the previous run that we stopped reading too early. Left
        in place it would be interleaved with this turn's output and, worse,
        its trailing result would end this turn before the new prompt was
        answered — the "one turn behind" failure. Flushing first keeps the
        stream and the turn boundaries aligned.
        """
        if not self._stalled and self._queue.empty():
            return
        while True:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=_DRAIN_QUIET)
            except TimeoutError:
                break
            if isinstance(item, BaseException):
                # The stream died while we were away. Leave it queued-shaped
                # for the turn proper to raise rather than losing it here.
                self._queue.put_nowait(item)
                break
            self._ledger.observe(item)
            for block in self._map(item):
                yield block
        self._stalled = False

    async def _read_until_run_end(self) -> AsyncIterator[Block]:
        """Read messages until the run — not just the turn — ends.

        A ResultMessage ends the *turn*; the run only ends there if no
        delegated task (subagent, workflow) is still in flight. Otherwise the
        CLI will wake us with a follow-up turn once it finishes, so we keep
        reading past it instead of returning early.

        An idle ledger is not on its own proof the run is over. A task that
        settles *before* the result of the turn that spawned it leaves the
        ledger empty at that result, yet its completion still wakes the parent
        for a continuation turn. Returning there would strand that turn's
        output in the queue, unread until the next prompt dragged it out —
        the bot going quiet until you say "continue". So the two cases are
        told apart by what already happened this run:

        - a result was already consumed while a task was in flight, so the
          continuation has been read and this result ends it;
        - otherwise a settled task means the continuation is still coming, and
          we keep reading through a grace window to catch it.
        """
        deferred_result = False  # consumed a result while a task was running
        settled_since_result = False  # a tracked task finished; a turn follows
        pending_done: Block | None = None  # Done for a result held pending
        while True:
            waiting = pending_done is not None
            try:
                message = await self._next_message(
                    _CONTINUATION_GRACE if waiting else _IDLE_TIMEOUT
                )
            except TimeoutError:
                if waiting:
                    # The continuation never came, so the result we held was
                    # the end of the run after all. Close the turn out rather
                    # than holding the session open for the full idle timeout.
                    log.debug("No continuation within %.0fs", _CONTINUATION_GRACE)
                    yield pending_done
                    return
                self._stalled = True
                log.warning(
                    "Turn stalled: no output for %.0fs with %d task(s) in flight",
                    _IDLE_TIMEOUT,
                    len(self._ledger),
                )
                yield Block(
                    BlockKind.ERROR,
                    title="Turn stalled",
                    body=(
                        f"No output for {int(_IDLE_TIMEOUT)}s while waiting on "
                        f"{len(self._ledger)} task(s). Releasing the session; "
                        "anything that arrives late will surface at the start "
                        "of the next message."
                    ),
                    is_error=True,
                )
                return

            # The continuation arrived, so the held result was not the end.
            pending_done = None
            if self._ledger.observe(message):
                settled_since_result = True
            for block in self._map(message):
                yield block
            if not isinstance(message, ResultMessage):
                continue

            if message.is_error:
                return
            if not self._ledger.idle:
                # Ends the turn, not the run: delegated work is still going.
                deferred_result = True
                settled_since_result = False
                continue
            if deferred_result or not settled_since_result:
                # Either the continuation has already been read, or no task
                # ran at all. Nothing more is coming.
                yield self._done_block(message)
                return
            # A task settled with no result deferred, so this closes the turn
            # that dispatched it and the continuation is still to come. Hold
            # the Done and keep reading for it.
            settled_since_result = False
            pending_done = self._done_block(message)

    def _done_block(self, message: ResultMessage) -> Block:
        bits = [f"{message.num_turns} turn(s)"]
        if message.total_cost_usd:
            bits.append(f"${message.total_cost_usd:.4f}")
        state = _context_from_usage(message.model_usage)
        if state is not None:
            self.last_context = state
            bits.append(f"{state.pct:.0f}% ctx")
        return Block(BlockKind.STATUS, title="Done", body=" · ".join(bits))

    async def context_usage(self) -> dict[str, Any] | None:
        if self._client is None:
            return None
        return await self._client.get_context_usage()

    def _map(self, message: Any) -> list[Block]:
        blocks: list[Block] = []

        if isinstance(message, TaskStartedMessage):
            blocks.append(
                Block(
                    BlockKind.TASK,
                    title=message.description or "Task",
                    body="starting…",
                    task_id=message.task_id,
                    task_status=TaskStatus.RUNNING,
                )
            )
            return blocks

        if isinstance(message, TaskProgressMessage):
            blocks.append(
                Block(
                    BlockKind.TASK,
                    title=message.description or "Task",
                    body=_format_task_progress(message),
                    task_id=message.task_id,
                    task_status=TaskStatus.RUNNING,
                )
            )
            return blocks

        if isinstance(message, TaskNotificationMessage):
            failed = message.status in ("failed", "stopped", "killed")
            blocks.append(
                Block(
                    BlockKind.TASK,
                    title=message.summary or "Task",
                    body=_format_task_usage(message.usage),
                    task_id=message.task_id,
                    task_status=TaskStatus.FAILED if failed else TaskStatus.DONE,
                    is_error=failed,
                )
            )
            return blocks

        if isinstance(message, TaskUpdatedMessage):
            # Terminal completion sometimes arrives only as this patch, with
            # no task_notification. Non-terminal patches carry nothing worth
            # rendering, so those are dropped.
            if message.status in TERMINAL_TASK_STATUSES:
                failed = message.status in ("failed", "killed")
                blocks.append(
                    Block(
                        BlockKind.TASK,
                        title="Task",
                        body=message.status,
                        task_id=message.task_id,
                        task_status=TaskStatus.FAILED if failed else TaskStatus.DONE,
                        is_error=failed,
                    )
                )
            return blocks

        if isinstance(message, SystemMessage):
            if message.subtype == "init":
                sid = message.data.get("session_id")
                if sid:
                    self.session_id = sid
            return blocks

        if isinstance(message, AssistantMessage):
            if message.session_id:
                self.session_id = message.session_id
            for b in message.content:
                if isinstance(b, TextBlock):
                    if b.text.strip():
                        blocks.append(Block(BlockKind.TEXT, body=b.text))
                elif isinstance(b, ThinkingBlock):
                    if b.thinking.strip():
                        blocks.append(Block(BlockKind.THINKING, body=b.thinking))
                elif isinstance(b, ToolUseBlock):
                    blocks.append(
                        Block(
                            BlockKind.TOOL_CALL,
                            title=b.name,
                            body=_format_tool_input(b.name, b.input),
                            meta={"tool": b.name},
                        )
                    )
            return blocks

        if isinstance(message, UserMessage):
            content = message.content
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, ToolResultBlock):
                        blocks.append(
                            Block(
                                BlockKind.TOOL_RESULT,
                                body=_stringify_tool_result(b.content),
                                is_error=bool(b.is_error),
                            )
                        )
            return blocks

        if isinstance(message, ResultMessage):
            if message.session_id:
                self.session_id = message.session_id
            if message.is_error:
                detail = ""
                if message.errors:
                    detail = ": " + "; ".join(message.errors)
                blocks.append(
                    Block(
                        BlockKind.ERROR,
                        title="Turn failed",
                        body=(message.result or message.subtype) + detail,
                        is_error=True,
                    )
                )
            # "Done" is not emitted here. Only the reader knows whether a
            # result ends the run or merely the turn that dispatched a task,
            # so it yields the status itself once it has decided.
            return blocks

        return blocks

    async def interrupt(self) -> None:
        if self._client is not None:
            await self._client.interrupt()

    async def stop(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        if self._client is not None:
            await self._client.disconnect()
            self._client = None
