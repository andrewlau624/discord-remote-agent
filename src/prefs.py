"""Display preferences: which block kinds render.

An interactive reaction panel toggles each setting and shows its current state.
Prefs persist to a small JSON file so they survive restarts.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Awaitable, Callable

import discord

from src.providers.base import BlockKind

_IDLE_TIMEOUT = 120

# Ordered (emoji, attribute, label) for each toggle shown in the panel.
_TOGGLES = [
    ("🧠", "thinking", "Thinking"),
    ("🔧", "tool_calls", "Tool calls"),
    ("📥", "tool_results", "Tool results"),
]

_FIELDS = ("thinking", "tool_calls", "tool_results")


@dataclass
class DisplayPrefs:
    thinking: bool = True
    tool_calls: bool = True
    tool_results: bool = True

    def shows(self, kind: BlockKind) -> bool:
        """Whether a block of this kind should be rendered. Text, status, and
        errors always show; only the noisy kinds are toggleable."""
        if kind == BlockKind.THINKING:
            return self.thinking
        if kind == BlockKind.TOOL_CALL:
            return self.tool_calls
        if kind == BlockKind.TOOL_RESULT:
            return self.tool_results
        return True

    @classmethod
    def load(cls, path: str) -> "DisplayPrefs":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        valid = {k: data[k] for k in _FIELDS if isinstance(data.get(k), bool)}
        return cls(**valid)

    def save(self, path: str) -> None:
        try:
            Path(path).write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        except OSError:
            pass


def _panel_embed(prefs: DisplayPrefs) -> discord.Embed:
    lines = [
        f"{emoji} {label}  {'✅' if getattr(prefs, attr) else '⬜'}"
        for emoji, attr, label in _TOGGLES
    ]
    return discord.Embed(
        title="View settings",
        description="\n".join(lines) + "\n\nReact to toggle. ✅ on, ⬜ off.",
        color=0x5865F2,
    )


async def view_panel(
    bot: discord.Client,
    channel: discord.abc.Messageable,
    prefs: DisplayPrefs,
    on_change: Callable[[str, bool], Awaitable[None] | None] | None = None,
) -> None:
    """Post a settings panel and toggle prefs as the owner reacts.

    Runs until the panel goes idle. on_change fires after each flip with the
    attribute name and its new value.
    """
    message = await channel.send(embed=_panel_embed(prefs))
    emap = {emoji: attr for emoji, attr, _ in _TOGGLES}
    for emoji in emap:
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            pass

    def check(payload: discord.RawReactionActionEvent) -> bool:
        return (
            payload.message_id == message.id
            and str(payload.emoji) in emap
            and payload.user_id != getattr(bot.user, "id", 0)
        )

    while True:
        try:
            payload = await bot.wait_for(
                "raw_reaction_add", check=check, timeout=_IDLE_TIMEOUT
            )
        except asyncio.TimeoutError:
            break
        guild = bot.get_guild(payload.guild_id) if payload.guild_id else None
        if guild is None or guild.owner_id != payload.user_id:
            continue
        attr = emap[str(payload.emoji)]
        setattr(prefs, attr, not getattr(prefs, attr))
        if on_change is not None:
            res = on_change(attr, getattr(prefs, attr))
            if asyncio.iscoroutine(res):
                await res
        try:
            await message.edit(embed=_panel_embed(prefs))
        except discord.HTTPException:
            pass
        member = payload.member or guild.get_member(payload.user_id)
        if member is not None:
            try:
                await message.remove_reaction(payload.emoji, member)
            except discord.HTTPException:
                pass
