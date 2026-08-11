"""Per-channel session: forwards input to a provider and renders its output.

A lock serializes turns so quick successive messages queue up instead of
overlapping on one agent.
"""

from __future__ import annotations

import asyncio

import discord

from dra.providers.base import Provider
from dra.render import render_block
from dra.store import Store


class Session:
    def __init__(
        self,
        bot: discord.Client,
        store: Store,
        channel_id: int,
        provider: Provider,
        provider_name: str,
    ) -> None:
        self._bot = bot
        self._store = store
        self.channel_id = channel_id
        self.provider = provider
        self.provider_name = provider_name
        self._lock = asyncio.Lock()

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
                async with channel.typing():
                    async for block in self.provider.run_turn(text):
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

    async def interrupt(self) -> None:
        await self.provider.interrupt()

    async def stop(self) -> None:
        await self.provider.stop()
