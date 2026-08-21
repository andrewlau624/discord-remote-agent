"""Provider interface.

Each provider turns its own output stream into a list of Blocks so the renderer
and session loop stay provider-agnostic. Before running a tool that isn't
pre-approved, a provider asks the PermissionBroker.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol, runtime_checkable


class BlockKind(str, enum.Enum):
    THINKING = "thinking"
    TEXT = "text"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    STATUS = "status"
    ERROR = "error"
    TASK = "task"


class TaskStatus(str, enum.Enum):
    """Lifecycle of one delegated task (a subagent or a workflow).

    RUNNING blocks update a live message in place; DONE and FAILED are terminal
    and finalize it.
    """

    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self is not TaskStatus.RUNNING


@dataclass
class Block:
    """A single unit of agent output, provider-agnostic."""

    kind: BlockKind
    body: str = ""
    title: str | None = None
    is_error: bool = False
    meta: dict[str, Any] = field(default_factory=dict)
    #: Set on TASK blocks only: which delegated task this describes. Repeated
    #: blocks with the same id update one live message instead of stacking up.
    task_id: str | None = None
    task_status: TaskStatus | None = None


@runtime_checkable
class PermissionBroker(Protocol):
    """Decides whether a tool call may proceed (e.g. via Discord buttons)."""

    async def request(
        self,
        channel_id: int,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Return (allowed, deny_reason). deny_reason is shown to the agent."""
        ...

    async def ask(self, channel_id: int, tool_input: dict[str, Any]) -> str:
        """Poll the user for a structured answer; return it as feedback text."""
        ...


class Provider(ABC):
    """A live agent session bound to one working directory."""

    #: Populated once the underlying agent reports its session id.
    session_id: str | None = None

    @abstractmethod
    async def start(self) -> None:
        """Connect (and resume, if `session_id` was set before start)."""

    @abstractmethod
    def run_turn(self, text: str) -> AsyncIterator[Block]:
        """Send input and yield blocks until the turn ends (async generator)."""

    @abstractmethod
    async def list_commands(self) -> list[dict[str, Any]]:
        """Available skills/commands, as {name, description} dicts."""

    @abstractmethod
    async def title(self) -> str | None:
        """The session's title once the agent has named it, else None."""

    @abstractmethod
    async def set_mode(self, mode: str) -> None:
        """Switch the agent's permission mode for this session."""

    @abstractmethod
    async def interrupt(self) -> None:
        """Interrupt the in-flight turn, if any."""

    @abstractmethod
    async def stop(self) -> None:
        """Tear down the session."""
