"""Per-channel session: forwards input to a provider and renders its output.

A lock serializes turns so quick successive messages queue up instead of
overlapping on one agent. Most blocks are posted as new messages; TASK blocks
go to the TaskBoard instead, which keeps one live message per subagent or
workflow and edits it in place.
"""

from __future__ import annotations

import asyncio
import logging

import discord

from src.providers.base import BlockKind, ContextState, Provider
from src.render import render_block
from src.store import Store
from src.tasks import TaskBoard

log = logging.getLogger("src")


class Session:
    def __init__(
        self,
        bot: discord.Client,
        store: Store,
        channel_id: int,
        provider: Provider,
        provider_name: str,
        pre_named: bool = False,
    ) -> None:
        self._bot = bot
        self._store = store
        self.channel_id = channel_id
        self.provider = provider
        self.provider_name = provider_name
        self._lock = asyncio.Lock()
        self._named = pre_named
        self._board = TaskBoard()
        #: Highest context threshold already warned about, so a session that
        #: sits at 78% for twenty turns warns once rather than every turn.
        self._warned_pct = 0.0

    async def _channel(self) -> discord.abc.Messageable | None:
        ch = self._bot.get_channel(self.channel_id)
        if isinstance(ch, discord.abc.Messageable):
            return ch
        return None

    async def handle_message(self, text: str) -> None:
        channel = await self._channel()
        if channel is None:
            return

        async with self._lock:
            try:
                prefs = getattr(self._bot, "prefs", None)
                async with channel.typing():
                    async for block in self.provider.run_turn(text):
                        if prefs is not None and not prefs.shows(block.kind):
                            continue
                        if block.kind is BlockKind.TASK:
                            await self._board.apply(channel, block)
                            continue
                        for msg in render_block(block):
                            await channel.send(**msg)
            except Exception as exc:
                await channel.send(f"⚠️ Session error: `{type(exc).__name__}: {exc}`")

            if self.provider.session_id:
                self._store.set_session_id(self.channel_id, self.provider.session_id)

            await self._maybe_name_thread()

        # Deliberately outside the lock. The warning waits on a human reaction
        # for as long as fifteen minutes, and its handoff action calls back
        # into `ask`, which takes this same non-reentrant lock -- holding it
        # here would freeze the thread and deadlock the handoff outright.
        await self._maybe_warn_context(channel)

    async def _maybe_warn_context(self, channel: discord.abc.Messageable) -> None:
        """Hand a threshold crossing to the bot, which owns the response."""
        config = getattr(self._bot, "config", None)
        handler = getattr(self._bot, "handle_context_warning", None)
        if config is None or handler is None:
            return
        state = self._pending_warning(
            getattr(config, "context_warn_at", 0),
            getattr(config, "context_warn_again_at", 0),
        )
        if state is None:
            return
        try:
            await handler(channel, self, state)
        except Exception as exc:  # a warning must never break the turn
            log.warning("Context warning failed: %s", exc)

    async def ask(self, text: str) -> str:
        """Run a turn and return its text, posting nothing.

        Used for asking the session about itself -- the handoff brief -- where
        the answer is an input to the next step rather than something to show.
        Takes the same lock as a normal turn, so it queues behind live work
        instead of interleaving with it.
        """
        async with self._lock:
            out: list[str] = []
            async for block in self.provider.run_turn(text):
                if block.kind is BlockKind.TEXT and block.body:
                    out.append(block.body)
            return "\n".join(out).strip()

    @property
    def context(self) -> ContextState | None:
        return self.provider.last_context

    def _pending_warning(self, warn_at: int, warn_again_at: int) -> ContextState | None:
        """The context state to warn about now, or None to stay quiet.

        Each threshold fires at most once; crossing the higher one re-arms.
        """
        state = self.provider.last_context
        if state is None or warn_at <= 0:
            return None
        for level in sorted({warn_at, warn_again_at}, reverse=True):
            if state.pct >= level > self._warned_pct:
                self._warned_pct = float(level)
                return state
        return None

    def reset_context_warnings(self) -> None:
        """Forget what has been warned about -- after a compact or handoff."""
        self._warned_pct = 0.0

    async def _maybe_name_thread(self) -> None:
        """Rename the session thread once the agent has titled the session."""
        if self._named:
            return
        try:
            title = await self.provider.title()
        except Exception:
            return
        if not title:
            return
        channel = self._bot.get_channel(self.channel_id)
        if isinstance(channel, discord.Thread):
            try:
                await channel.edit(name=title[:100])
                self._named = True
            except discord.HTTPException:
                pass

    async def interrupt(self) -> None:
        await self.provider.interrupt()

    async def stop(self) -> None:
        self._board.clear()
        await self.provider.stop()
