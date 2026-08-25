"""Routing and lifetime for #control.

ControlManager is the seam. Typed messages and button presses both become an
Action and go through one method, _act, which is the only place the browser is
touched and the only place the panel is redrawn. One lock serializes
everything, so button-mashing and idle expiry cannot interleave with an
in-flight page load.

There is one session for the whole bot, because there is one Chromium process.
Bindings live in memory; run !control start again after a restart.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

import discord

from src.control.browser import BrowserService
from src.control.grammar import parse_action
from src.control.panel import ControlPanel
from src.control.types import (
    Action,
    ActionKind,
    ControlError,
    ControlSettings,
    PageState,
)

log = logging.getLogger("src")

CHANNEL_NAME = "control"


class _IdleTimer:
    """Fires on_expire once the deadline passes with no touch().

    One long-lived task that rechecks a deadline, rather than a task cancelled
    and respawned per action: touch() is a single assignment, so there is no
    cancel/await race and no wakeup between actions.
    """

    def __init__(
        self, seconds: float, on_expire: Callable[[], Awaitable[None]]
    ) -> None:
        self._seconds = seconds
        self._on_expire = on_expire
        self._loop = asyncio.get_running_loop()
        self._deadline = self._loop.time() + seconds
        self._task = self._loop.create_task(self._run())

    def touch(self) -> None:
        self._deadline = self._loop.time() + self._seconds

    def cancel(self) -> None:
        self._task.cancel()

    async def _run(self) -> None:
        while True:
            remaining = self._deadline - self._loop.time()
            if remaining <= 0:
                break
            # A touch() during this sleep just extends the next pass.
            await asyncio.sleep(remaining)
        await self._on_expire()


@dataclass(slots=True)
class _ControlSession:
    channel_id: int
    browser: BrowserService
    panel: ControlPanel
    lock: asyncio.Lock
    idle: _IdleTimer | None = None
    state: PageState | None = None
    # Cleared the first time Discord refuses a delete, so a missing Manage
    # Messages permission costs one rejected call and one log line, not one
    # per command.
    can_delete: bool = True


class ControlManager:
    def __init__(self, settings: ControlSettings) -> None:
        self._settings = settings
        self._session: _ControlSession | None = None

    def handles(self, channel_id: int) -> bool:
        return self._session is not None and self._session.channel_id == channel_id

    async def start(self, channel: discord.TextChannel, owner_id: int) -> str:
        if self._session is not None:
            return (
                f"Control is already live in <#{self._session.channel_id}>. "
                "End it first."
            )
        session = _ControlSession(
            channel_id=channel.id,
            browser=BrowserService(self._settings),
            panel=ControlPanel(channel, owner_id, self._act),
            lock=asyncio.Lock(),
        )
        self._session = session
        # Armed before the launch, so a browser that never comes up still expires.
        session.idle = _IdleTimer(self._settings.idle_timeout, self._expire)
        try:
            session.state = await session.browser.start()
        except ControlError as exc:
            await self.end("it could not start")
            return f"Could not start the browser: `{exc}`"
        await session.panel.render(session.state)
        session.idle.touch()
        minutes = self._settings.idle_timeout // 60
        return (
            f"{channel.mention} is live. Type a hint number to click it, "
            f"`go <url>` to navigate, or just type to enter text. "
            f"Ends after {minutes}m idle."
        )

    async def end(self, reason: str) -> str:
        session = self._session
        if session is None:
            return "No control session is running."
        # Claimed before any await, so a second caller (idle timer racing the
        # End button) sees None and does nothing.
        self._session = None
        if session.idle is not None:
            session.idle.cancel()
        async with session.lock:
            await session.browser.close()
        await session.panel.close(reason)
        return f"Control session ended ({reason})."

    async def run(self, message: discord.Message) -> None:
        """Handle one message typed in #control.

        The command is deleted as it is read, and errors go into the panel's
        status line rather than messages of their own. Between them the panel
        is the only thing this feature ever leaves in the channel, so it stays
        put at the bottom instead of scrolling away above a wall of commands.
        """
        if self._session is None:
            return
        await self._consume(message)
        try:
            action = parse_action(message.content)
        except ValueError as exc:
            await self._show_status(f"⚠️ {exc}")
            return
        await self._act(action)

    async def _consume(self, message: discord.Message) -> None:
        """Delete a command once it has been read."""
        session = self._session
        if session is None or not session.can_delete:
            return
        try:
            await message.delete()
        except discord.Forbidden:
            session.can_delete = False
            log.warning(
                "Cannot tidy #control: the bot needs Manage Messages there. "
                "The panel will repost itself to stay in view instead."
            )
        except discord.HTTPException:
            pass  # already gone, or a blip; not worth failing the action over

    async def _show_status(self, status: str) -> None:
        """Redraw the current page with a message in its status line."""
        session = self._session
        if session is None or session.state is None:
            return
        async with session.lock:
            await session.panel.render(session.state, status=status)

    # ---- the one path everything takes -----------------------------------

    async def _act(self, action: Action) -> None:
        session = self._session
        if session is None:
            return
        if action.kind is ActionKind.END:
            await self.end("stopped")
            return
        async with session.lock:
            if session.state is None:
                raise RuntimeError("Session accepted input before its first render.")
            session.idle.touch()
            try:
                session.state = await session.browser.act(action)
                status = ""
            except ControlError as exc:
                status = f"⚠️ {exc}"
            await session.panel.render(session.state, status=status)
            session.idle.touch()
        if not session.browser.running:
            await self.end("the browser stopped")

    async def _expire(self) -> None:
        minutes = self._settings.idle_timeout // 60
        await self.end(f"idle for {minutes}m")


async def ensure_control_channel(guild: discord.Guild) -> discord.TextChannel:
    """Find the #control channel, creating it if needed."""
    for ch in guild.text_channels:
        if ch.name == CHANNEL_NAME:
            return ch
    return await guild.create_text_channel(
        CHANNEL_NAME, reason="discord-remote-agent control"
    )
