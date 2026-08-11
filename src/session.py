"""Per-channel session: forwards input to a provider and renders its output.

A lock serializes turns so quick successive messages queue up instead of
overlapping on one agent.
"""

from __future__ import annotations

import asyncio

import discord

from src.providers.base import Provider
from src.render import render_block
from src.store import Store


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
                        embeds, files = render_block(block)
                        for embed in embeds:
                            await channel.send(embed=embed, files=files)
                            files = []
                        for extra in files:
                            await channel.send(file=extra)
            except Exception as exc:
                await channel.send(f"⚠️ Session error: `{type(exc).__name__}: {exc}`")

            if self.provider.session_id:
                self._store.set_session_id(self.channel_id, self.provider.session_id)

            await self._maybe_name_thread()

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
        await self.provider.stop()
