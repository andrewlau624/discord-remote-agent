"""Tool approvals via Discord polls.

request() posts the tool detail, then a poll with Approve/Deny. It waits for the
owner's vote and denies on timeout so a forgotten prompt can't hang a session.
If polls can't be sent, it falls back to emoji reactions.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import discord

from dra.render import permission_embed

_APPROVE = "✅"
_DENY = "🛑"


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
            return False, "No channel available to request approval."

        await channel.send(embed=permission_embed(tool_name, tool_input))

        try:
            return await self._ask_poll(channel, tool_name)
        except (discord.HTTPException, TypeError):
            return await self._ask_reactions(channel, tool_name)

    async def _ask_poll(
        self, channel: discord.abc.Messageable, tool_name: str
    ) -> tuple[bool, str | None]:
        poll = discord.Poll(
            question=f"Approve {tool_name}?", duration=timedelta(hours=1)
        )
        poll.add_answer(text="Approve", emoji=_APPROVE)  # answer_id 1
        poll.add_answer(text="Deny", emoji=_DENY)  # answer_id 2
        message = await channel.send(poll=poll)

        def check(payload: discord.RawPollVoteActionEvent) -> bool:
            return payload.message_id == message.id and payload.user_id in self._owner_ids

        try:
            payload = await self._bot.wait_for(
                "raw_poll_vote_add", check=check, timeout=self._timeout
            )
            allowed = payload.answer_id == 1
        except asyncio.TimeoutError:
            allowed = False
            payload = None

        try:
            await message.end_poll()
        except discord.HTTPException:
            pass

        if allowed:
            return True, None
        return False, "Approval timed out." if payload is None else "Denied by user."

    async def _ask_reactions(
        self, channel: discord.abc.Messageable, tool_name: str
    ) -> tuple[bool, str | None]:
        message = await channel.send(f"Approve **{tool_name}**? React {_APPROVE} or {_DENY}.")
        await message.add_reaction(_APPROVE)
        await message.add_reaction(_DENY)

        def check(reaction: discord.Reaction, user: discord.abc.User) -> bool:
            return (
                reaction.message.id == message.id
                and user.id in self._owner_ids
                and str(reaction.emoji) in (_APPROVE, _DENY)
            )

        try:
            reaction, _user = await self._bot.wait_for(
                "reaction_add", check=check, timeout=self._timeout
            )
        except asyncio.TimeoutError:
            return False, "Approval timed out."

        return (str(reaction.emoji) == _APPROVE), (
            None if str(reaction.emoji) == _APPROVE else "Denied by user."
        )
