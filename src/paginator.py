"""Emoji-react pagination for list-style output.

Posts one embed at a time and flips pages when the server owner reacts. Only the
owner's reactions count. Stops listening after a period of no interaction.
"""

from __future__ import annotations

import asyncio

import discord

_PREV = "◀"
_NEXT = "▶"
_IDLE_TIMEOUT = 180


async def paginate(
    bot: discord.Client,
    channel: discord.abc.Messageable,
    pages: list[discord.Embed],
) -> None:
    if not pages:
        pages = [discord.Embed(description="Nothing to show.")]

    total = len(pages)
    idx = 0

    def stamp(i: int) -> discord.Embed:
        pages[i].set_footer(text=f"Page {i + 1}/{total}")
        return pages[i]

    message = await channel.send(embed=stamp(idx))
    if total == 1:
        return

    guild = getattr(channel, "guild", None)
    owner_id = guild.owner_id if guild else None
    await message.add_reaction(_PREV)
    await message.add_reaction(_NEXT)

    def check(payload: discord.RawReactionActionEvent) -> bool:
        return (
            payload.message_id == message.id
            and payload.user_id == owner_id
            and str(payload.emoji) in (_PREV, _NEXT)
        )

    while True:
        try:
            payload = await bot.wait_for(
                "raw_reaction_add", check=check, timeout=_IDLE_TIMEOUT
            )
        except asyncio.TimeoutError:
            break

        idx = (idx + 1) % total if str(payload.emoji) == _NEXT else (idx - 1) % total
        await message.edit(embed=stamp(idx))

        # Clear the click right away so the next tap registers.
        member = payload.member or (guild.get_member(payload.user_id) if guild else None)
        if member is not None:
            try:
                await message.remove_reaction(payload.emoji, member)
            except discord.HTTPException:
                pass

