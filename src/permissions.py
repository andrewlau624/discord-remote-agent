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

from src.render import ask_question_text, permission_embed

_APPROVE = "✅"
_DENY = "🛑"
_SUBMIT = "🆗"  # finalizes a multi-select answer poll
_OTHER = "Other"  # always-present poll choice that lets you type a free answer


class DiscordPermissionBroker:
    def __init__(self, bot: discord.Client, timeout: int):
        self._bot = bot
        self._timeout = timeout
        # When True, tool approvals are granted without polling. This lives on
        # the broker rather than the CLI permission mode because the CLI
        # refuses runtime switches into bypassPermissions unless the session
        # was launched with --dangerously-skip-permissions.
        self.auto_accept = False
        # Channels waiting for a typed "Other" answer. on_message skips
        # forwarding a message here so it becomes the answer, not a new turn.
        self.awaiting_text: set[int] = set()

    async def request(
        self,
        channel_id: int,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> tuple[bool, str | None]:
        channel = self._bot.get_channel(channel_id)
        if channel is None or not isinstance(channel, discord.abc.Messageable):
            return False, "No channel available to request approval."
        guild = getattr(channel, "guild", None)
        if guild is None:
            return False, "Approvals only work in a server."
        owner_id = guild.owner_id

        await channel.send(embed=permission_embed(tool_name, tool_input))

        try:
            return await self._ask_poll(channel, tool_name, owner_id)
        except (discord.HTTPException, TypeError):
            return await self._ask_reactions(channel, tool_name, owner_id)

    # ---- structured questions (AskUserQuestion) --------------------------

    async def ask(self, channel_id: int, tool_input: dict[str, Any]) -> str:
        """Turn an AskUserQuestion into one poll per question and feed the
        owner's picks back to the agent as text."""
        channel = self._bot.get_channel(channel_id)
        if channel is None or not isinstance(channel, discord.abc.Messageable):
            return "No channel available to ask the question."
        guild = getattr(channel, "guild", None)
        if guild is None:
            return "Questions only work in a server."
        owner_id = guild.owner_id

        questions = tool_input.get("questions") or []
        if not questions:
            return "No question to answer. Continue with your best judgment."

        parts: list[str] = []
        for q in questions:
            header = str(q.get("header") or q.get("question") or "Answer").strip()
            try:
                picks = await self._answer_poll(channel, owner_id, q)
            except (discord.HTTPException, TypeError):
                picks = []
            parts.append(f"{header}: {', '.join(picks)}" if picks else f"{header}: (no answer)")

        return (
            "The user answered your question(s):\n"
            + "\n".join(parts)
            + "\nProceed with these selections and do not ask again."
        )

    async def _answer_poll(
        self, channel: discord.abc.Messageable, owner_id: int, question: dict[str, Any]
    ) -> list[str]:
        options = question.get("options") or []
        # Leave a slot for the always-present "Other" choice.
        labels = [str(o.get("label", "?"))[:55] or "?" for o in options][:9]
        labels.append(_OTHER)
        multiple = bool(question.get("multiSelect"))

        # Big, readable question with option descriptions, then the poll.
        await channel.send(ask_question_text(question))

        q_text = str(question.get("header") or question.get("question") or "Choose")[:300]
        poll = discord.Poll(question=q_text, duration=timedelta(hours=1), multiple=multiple)
        for label in labels:
            poll.add_answer(text=label)
        message = await channel.send(poll=poll)
        id_to_label = {i + 1: labels[i] for i in range(len(labels))}

        if multiple:
            try:
                await message.add_reaction(_SUBMIT)
            except discord.HTTPException:
                pass
            selected = await self._collect_multi(message, owner_id)
        else:
            selected = await self._collect_single(message, owner_id)

        try:
            await message.end_poll()
        except discord.HTTPException:
            pass

        picks = [id_to_label[a] for a in sorted(selected) if a in id_to_label]
        if _OTHER in picks:
            picks.remove(_OTHER)
            typed = await self._collect_other_text(channel, owner_id)
            if typed:
                picks.append(typed)
        return picks

    async def _collect_other_text(
        self, channel: discord.abc.Messageable, owner_id: int
    ) -> str:
        """Wait for the owner to type their own answer after picking Other."""
        await channel.send("You picked **Other**. Type your answer here.")
        self.awaiting_text.add(channel.id)

        def check(message: discord.Message) -> bool:
            return (
                message.channel.id == channel.id
                and message.author.id == owner_id
                and bool(message.content.strip())
            )

        try:
            message = await self._bot.wait_for(
                "message", check=check, timeout=self._timeout
            )
            return message.content.strip()
        except asyncio.TimeoutError:
            return ""
        finally:
            self.awaiting_text.discard(channel.id)

    async def _collect_single(self, message: discord.Message, owner_id: int) -> set[int]:
        def check(payload: discord.RawPollVoteActionEvent) -> bool:
            return payload.message_id == message.id and payload.user_id == owner_id

        try:
            payload = await self._bot.wait_for(
                "raw_poll_vote_add", check=check, timeout=self._timeout
            )
            return {payload.answer_id}
        except asyncio.TimeoutError:
            return set()

    async def _collect_multi(self, message: discord.Message, owner_id: int) -> set[int]:
        """Track the owner's ticks and finalize when they react 🆗 or time out."""
        selected: set[int] = set()
        loop = asyncio.get_event_loop()
        deadline = loop.time() + self._timeout

        def vote_check(payload: discord.RawPollVoteActionEvent) -> bool:
            return payload.message_id == message.id and payload.user_id == owner_id

        def submit_check(payload: discord.RawReactionActionEvent) -> bool:
            return (
                payload.message_id == message.id
                and payload.user_id == owner_id
                and str(payload.emoji) == _SUBMIT
            )

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            waiters = {
                asyncio.ensure_future(
                    self._bot.wait_for("raw_poll_vote_add", check=vote_check, timeout=remaining)
                ): "add",
                asyncio.ensure_future(
                    self._bot.wait_for("raw_poll_vote_remove", check=vote_check, timeout=remaining)
                ): "remove",
                asyncio.ensure_future(
                    self._bot.wait_for("raw_reaction_add", check=submit_check, timeout=remaining)
                ): "submit",
            }
            done, pending = await asyncio.wait(
                waiters.keys(), return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

            finalize = False
            progressed = False
            for task in done:
                kind = waiters[task]
                try:
                    result = task.result()
                except (asyncio.TimeoutError, Exception):
                    continue
                progressed = True
                if kind == "submit":
                    finalize = True
                elif kind == "add":
                    selected.add(result.answer_id)
                elif kind == "remove":
                    selected.discard(result.answer_id)
            if finalize or not progressed:
                break
        return selected

    async def _ask_poll(
        self, channel: discord.abc.Messageable, tool_name: str, owner_id: int
    ) -> tuple[bool, str | None]:
        poll = discord.Poll(
            question=f"Approve {tool_name}?", duration=timedelta(hours=1)
        )
        poll.add_answer(text="Approve", emoji=_APPROVE)  # answer_id 1
        poll.add_answer(text="Deny", emoji=_DENY)  # answer_id 2
        message = await channel.send(poll=poll)

        def check(payload: discord.RawPollVoteActionEvent) -> bool:
            return payload.message_id == message.id and payload.user_id == owner_id

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
        self, channel: discord.abc.Messageable, tool_name: str, owner_id: int
    ) -> tuple[bool, str | None]:
        message = await channel.send(f"Approve **{tool_name}**? React {_APPROVE} or {_DENY}.")
        await message.add_reaction(_APPROVE)
        await message.add_reaction(_DENY)

        def check(reaction: discord.Reaction, user: discord.abc.User) -> bool:
            return (
                reaction.message.id == message.id
                and user.id == owner_id
                and str(reaction.emoji) in (_APPROVE, _DENY)
            )

        try:
            reaction, user = await self._bot.wait_for(
                "reaction_add", check=check, timeout=self._timeout
            )
        except asyncio.TimeoutError:
            return False, "Approval timed out."

        try:
            await message.remove_reaction(reaction.emoji, user)
        except discord.HTTPException:
            pass

        return (str(reaction.emoji) == _APPROVE), (
            None if str(reaction.emoji) == _APPROVE else "Denied by user."
        )
