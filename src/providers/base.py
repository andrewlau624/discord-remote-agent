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


@dataclass
class Block:
    """A single unit of agent output, provider-agnostic."""

    kind: BlockKind
    body: str = ""
    title: str | None = None
    is_error: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


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
