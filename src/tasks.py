"""Live status messages for delegated tasks (subagents and workflows).

A running subagent emits a steady stream of progress events. Posting one
message each would flood the channel and run straight into Discord's per-channel
rate limit, so each task instead owns a single message that is edited in place:
posted when the task starts, updated as it works, finalized when it finishes.

Edits are throttled, but a terminal update always goes through — the last thing
a task says is the part worth keeping.
"""

from __future__ import annotations

import logging
import time

import discord

from src.providers.base import Block, TaskStatus
from src.render import task_embed

log = logging.getLogger("src")

#: Minimum seconds between in-place edits of a running task's message.
_EDIT_INTERVAL = 5.0


class _LiveTask:
    __slots__ = ("message", "last_edit")

    def __init__(self, message: discord.Message, last_edit: float) -> None:
        self.message = message
        self.last_edit = last_edit


class TaskBoard:
    """Owns one live Discord message per in-flight task, keyed by task id."""

    def __init__(self) -> None:
        self._live: dict[str, _LiveTask] = {}

    async def apply(self, channel: discord.abc.Messageable, block: Block) -> None:
        """Post, update, or finalize the message for the task in `block`."""
        if block.task_id is None:
            return
        status = block.task_status or TaskStatus.RUNNING
        entry = self._live.get(block.task_id)

        if entry is None:
            # A task whose first sighting is already terminal (a fast subagent,
            # or one that started before this board existed) still deserves a
            # message — just post it in its final state.
            await self._post(channel, block)
            return

        if status.is_terminal:
            self._live.pop(block.task_id, None)
            await self._edit(entry, block)
            return

        now = time.monotonic()
        if now - entry.last_edit < _EDIT_INTERVAL:
            return
        entry.last_edit = now
        await self._edit(entry, block)

    async def _post(self, channel: discord.abc.Messageable, block: Block) -> None:
        try:
            message = await channel.send(embed=task_embed(block))
        except discord.HTTPException as exc:
            log.warning("Could not post task status: %s", exc)
            return
        status = block.task_status or TaskStatus.RUNNING
        if not status.is_terminal and block.task_id is not None:
            self._live[block.task_id] = _LiveTask(message, time.monotonic())

    async def _edit(self, entry: _LiveTask, block: Block) -> None:
        try:
            await entry.message.edit(embed=task_embed(block))
        except discord.HTTPException as exc:
            log.warning("Could not update task status: %s", exc)

    def clear(self) -> None:
        """Forget every tracked task. Called when a session is torn down."""
        self._live.clear()
