"""The context-pressure warning and its actions.

A session that fills its context window degrades quietly: the agent starts
losing the earlier half of the conversation. This posts one warning when
fullness crosses a threshold and offers the two things worth doing about it --
compact in place, or hand off to a fresh session -- so the choice arrives
before the loss does rather than after.

The panel uses reactions rather than components, matching the view panel and
the paginator; only the server owner's clicks count.
"""

from __future__ import annotations

import asyncio

import discord

from src.providers.base import ContextState
from src.render import context_color, usage_bar

_COMPACT = "🗜️"
_HANDOFF = "🔄"
_IGNORE = "✖️"

#: Stop waiting for a choice after this long. The warning stays visible; it
#: just no longer listens, so a session left alone overnight is not holding a
#: reaction listener open forever.
_IDLE_TIMEOUT = 900

_ACTIONS = {_COMPACT: "compact", _HANDOFF: "handoff", _IGNORE: "ignore"}


def warn_embed(state: ContextState, prefix: str = "!") -> discord.Embed:
    embed = discord.Embed(
        title="Context is filling up",
        description=(
            f"`{usage_bar(state.pct)}` **{state.pct:.0f}%**\n"
            f"{state.used:,} / {state.limit:,} tokens · "
            f"{state.remaining:,} left"
        ),
        color=context_color(state.pct),
    )
    embed.add_field(
        name="What now?",
        value=(
            f"{_COMPACT} **Compact** — summarize in place and keep going here\n"
            f"{_HANDOFF} **Hand off** — open a fresh thread carrying a brief\n"
            f"{_IGNORE} **Ignore** — carry on; autocompact still applies"
        ),
        inline=False,
    )
    embed.set_footer(text=f"Or run {prefix}context for the full breakdown.")
    return embed


async def warn_panel(
    bot: discord.Client,
    channel: discord.abc.Messageable,
    state: ContextState,
    prefix: str = "!",
) -> str | None:
    """Post the warning and wait for the owner to pick an action.

    Returns "compact", "handoff", "ignore", or None if nobody answered.
    """
    message = await channel.send(embed=warn_embed(state, prefix))
    for emoji in _ACTIONS:
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            pass

    def check(payload: discord.RawReactionActionEvent) -> bool:
        return (
            payload.message_id == message.id
            and str(payload.emoji) in _ACTIONS
            and payload.user_id != getattr(bot.user, "id", 0)
        )

    while True:
        try:
            payload = await bot.wait_for(
                "raw_reaction_add", check=check, timeout=_IDLE_TIMEOUT
            )
        except asyncio.TimeoutError:
            return None
        guild = bot.get_guild(payload.guild_id) if payload.guild_id else None
        if guild is None or guild.owner_id != payload.user_id:
            continue
        action = _ACTIONS[str(payload.emoji)]
        # Leave the chosen action visible but stop offering the others, so the
        # panel reads as settled rather than still waiting.
        try:
            await message.clear_reactions()
        except discord.HTTPException:
            pass
        embed = message.embeds[0] if message.embeds else warn_embed(state, prefix)
        embed.set_footer(text=f"Chose: {action}")
        try:
            await message.edit(embed=embed)
        except discord.HTTPException:
            pass
        return action
