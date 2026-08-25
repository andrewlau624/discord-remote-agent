"""Per-channel session: forwards input to a provider and renders its output.

A lock serializes turns so quick successive messages queue up instead of
overlapping on one agent. Most blocks are posted as new messages; TASK blocks
go to the TaskBoard instead, which keeps one live message per subagent or
workflow and edits it in place.

After each turn a short-lived tail watcher drains whatever arrives late --
subagents finishing after their result, continuations waking the parent --
so output shows up on its own instead of waiting for the next user message
to drag it out.
"""

from __future__ import annotations

import asyncio
import logging
import time

import discord

from src.providers.base import BlockKind, ContextState, Provider
from src.render import render_block
from src.store import Store
from src.tasks import TaskBoard

log = logging.getLogger("src")

#: How long a message waits behind an active turn before it says so.
_QUEUE_ACK_AFTER = 3.0

#: How long the post-turn watcher waits for a first late block, and how long
#: it keeps following output once some has arrived (quiet gap between blocks).
_TAIL_FIRST_WAIT = 30.0
_TAIL_BUDGET = 600.0


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
        #: True while the post-turn tail watcher is running, so overlapping
        #: turn ends do not stack watchers on one queue.
        self._watching = False
        #: Highest context threshold already warned about, so a session that
        #: sits at 78% for twenty turns warns once rather than every turn.
        self._warned_pct = 0.0

    async def _channel(self) -> discord.abc.Messageable | None:
        ch = self._bot.get_channel(self.channel_id)
        if isinstance(ch, discord.abc.Messageable):
            return ch
        return None

    async def _acquire_turn(self, channel: discord.abc.Messageable) -> None:
        """Take the turn lock, telling the user if they are waiting in line.

        Discord shows nothing while a message silently waits on the lock; the
        typing indicator belongs to whichever turn is running, not to the
        queue. Without an ack, sending into a busy thread feels like shouting
        into a void -- the "is it stuck?" experience.
        """
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=_QUEUE_ACK_AFTER)
            return
        except TimeoutError:
            pass
        except asyncio.CancelledError:
            raise
        try:
            await channel.send(
                "⏳ Queued behind work still running here — yours is next."
            )
        except Exception:  # a failed ack must not stop the queued turn
            pass
        await self._lock.acquire()

    async def handle_message(self, text: str) -> None:
        channel = await self._channel()
        if channel is None:
            return

        held = False
        try:
            await self._acquire_turn(channel)
            held = True
            prefs = getattr(self._bot, "prefs", None)
            async with channel.typing():
                async for block in self.provider.run_turn(text):
                    await self._render(channel, block, prefs)
        except Exception as exc:
            await channel.send(f"⚠️ Session error: `{type(exc).__name__}: {exc}`")
        finally:
            if held:
                self._lock.release()

        if self.provider.session_id:
            self._store.set_session_id(self.channel_id, self.provider.session_id)

        await self._maybe_name_thread()

        # Deliberately outside the lock. The warning waits on a human reaction
        # for as long as fifteen minutes, and its handoff action calls back
        # into `ask`, which takes this same non-reentrant lock -- holding it
        # here would freeze the thread and deadlock the handoff outright.
        await self._maybe_warn_context(channel)

        spawn = getattr(self._bot, "_spawn", None)
        if spawn is not None:
            spawn(self._watch_tail())

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

    async def _watch_tail(self) -> None:
        """Post output that lands after the turn ended, unprompted.

        A result frame can close the turn while delegated work keeps going;
        the provider queues what comes back, and without this loop it sits
        there until the next message. Watch until a full quiet window passes
        with nothing arriving, or the budget runs out.
        """
        if self._watching:
            return
        self._watching = True
        try:
            channel = await self._channel()
            if channel is None:
                return
            prefs = getattr(self._bot, "prefs", None)
            deadline = time.monotonic() + _TAIL_BUDGET
            while time.monotonic() < deadline and not self._lock.locked():
                posted = False
                # No typing indicator here on purpose: waiting quietly for
                # late work is not the same as working, and showing "typing…"
                # through every idle window reads as stuck.
                first = None
                async for block in self.provider.drain_pending(_TAIL_FIRST_WAIT):
                    first = block
                    break
                if first is None:
                    return
                try:
                    await self._render(channel, first, prefs)
                    posted = True
                    async for block in self.provider.drain_pending():
                        await self._render(channel, block, prefs)
                        posted = True
                except Exception as exc:
                    log.warning("Tail watch failed: %s", exc)
                    return
                if not posted:
                    return
        finally:
            self._watching = False

    async def _render(
        self,
        channel: discord.abc.Messageable,
        block,
        prefs=None,
    ) -> None:
        if prefs is not None and not prefs.shows(block.kind):
            return
        if block.kind is BlockKind.TASK:
            await self._board.apply(channel, block)
            return
        for msg in render_block(block):
            await channel.send(**msg)

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
