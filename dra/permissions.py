"""Tool approvals via Discord buttons.

request() posts an embed with Approve/Deny buttons and waits for the owner to
click. On timeout it denies so a forgotten prompt can't hang a session.
"""

from __future__ import annotations

import asyncio
from typing import Any

import discord

from dra.render import permission_embed


class _ApprovalView(discord.ui.View):
    def __init__(self, owner_ids: frozenset[int], timeout: float) -> None:
        super().__init__(timeout=timeout)
        self._owner_ids = owner_ids
        self.future: asyncio.Future[bool] = asyncio.get_event_loop().create_future()

    async def _resolve(self, interaction: discord.Interaction, allowed: bool) -> None:
        if interaction.user.id not in self._owner_ids:
            await interaction.response.send_message(
                "You are not allowed to decide this.", ephemeral=True
            )
            return
        if not self.future.done():
            self.future.set_result(allowed)
        verb = "Approved" if allowed else "Denied"
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(
            content=f"**{verb}** by {interaction.user.display_name}", view=self
        )
        self.stop()

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, _button) -> None:  # noqa: ANN001
        await self._resolve(interaction, True)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, _button) -> None:  # noqa: ANN001
        await self._resolve(interaction, False)


class DiscordPermissionBroker:
    def __init__(self, bot: discord.Client, owner_ids: frozenset[int], timeout: int):
        self._bot = bot
        self._owner_ids = owner_ids
        self._timeout = timeout

    async def request(
        self,
        channel_id: int,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> tuple[bool, str | None]:
        channel = self._bot.get_channel(channel_id)
        if channel is None or not isinstance(channel, discord.abc.Messageable):
            # No channel to ask in, so deny.
            return False, "No channel available to request approval."

        view = _ApprovalView(self._owner_ids, timeout=float(self._timeout))
        message = await channel.send(embed=permission_embed(tool_name, tool_input), view=view)

        try:
            allowed = await asyncio.wait_for(view.future, timeout=self._timeout)
        except asyncio.TimeoutError:
            for child in view.children:
                child.disabled = True  # type: ignore[attr-defined]
            try:
                await message.edit(content="⏱️ Timed out, denied.", view=view)
            except discord.HTTPException:
                pass
            return False, "Approval timed out."

        return allowed, None if allowed else "Denied by user."
