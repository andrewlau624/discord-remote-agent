"""Which tools run without asking, and the panel for changing that.

Auto-approval used to be a list in config.toml that fed `allowed_tools`. That
had two problems: you had to already know a tool's exact name to add it, and
`allowed_tools` is honoured by the SDK *before* our permission callback, so an
entry there was invisible to the bot and impossible to change without a
restart. Approval now lives here instead and is enforced in the broker, which
means it can be toggled live -- and, because every call reaches the broker, the
bot learns tool names as they come up rather than needing them declared.

The panel is paged: Discord allows twenty reactions per message and MCP servers
push the tool count well past that.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

import discord

_IDLE_TIMEOUT = 180

#: Claude Code's built-ins, so the panel is useful before anything has run.
#: Anything else -- MCP tools, new built-ins -- is added as it is first seen.
KNOWN_TOOLS = (
    "Bash",
    "BashOutput",
    "Edit",
    "Glob",
    "Grep",
    "KillShell",
    "MultiEdit",
    "NotebookEdit",
    "NotebookRead",
    "Read",
    "Task",
    "TodoWrite",
    "WebFetch",
    "WebSearch",
    "Write",
)

#: Keycap digits, one per tool row on a page.
_DIGITS = ["%d️⃣" % i for i in range(1, 10)]
_PREV, _NEXT, _ALL = "◀", "▶", "⚡"
_PER_PAGE = len(_DIGITS)


@dataclass
class ApprovalPrefs:
    """Tools that run unattended, plus the master switch."""

    #: Approve every tool call without asking. Overrides `approved` entirely.
    accept_all: bool = False
    #: Tool names that run without a prompt.
    approved: set[str] = field(default_factory=set)
    #: Every tool name the broker has been asked about. Purely so the panel
    #: can offer real names instead of making you guess them.
    seen: set[str] = field(default_factory=set)

    def allows(self, tool_name: str) -> bool:
        return self.accept_all or tool_name in self.approved

    def note_seen(self, tool_name: str) -> bool:
        """Record a tool name. True if it is one we had not seen before."""
        if not tool_name or tool_name in self.seen:
            return False
        self.seen.add(tool_name)
        return True

    def tools(self) -> list[str]:
        """Every tool the panel should offer, approved ones first."""
        names = set(KNOWN_TOOLS) | self.seen | self.approved
        return sorted(names, key=lambda n: (n not in self.approved, n.lower()))

    @classmethod
    def load(cls, path: str, seed: list[str] | None = None) -> "ApprovalPrefs":
        """Read from disk, falling back to `seed` the first time.

        `seed` is the old config.toml list, so an existing setup keeps its
        behaviour on upgrade; once the file exists the panel owns the state.
        """
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls(approved=set(seed or ()), seen=set(seed or ()))
        return cls(
            accept_all=bool(data.get("accept_all", False)),
            approved={str(t) for t in data.get("approved", []) if str(t).strip()},
            seen={str(t) for t in data.get("seen", []) if str(t).strip()},
        )

    def save(self, path: str) -> None:
        try:
            Path(path).write_text(
                json.dumps(
                    {
                        "accept_all": self.accept_all,
                        "approved": sorted(self.approved),
                        "seen": sorted(self.seen),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass


def _page(prefs: ApprovalPrefs, idx: int) -> tuple[list[str], int]:
    tools = prefs.tools()
    total = max(1, -(-len(tools) // _PER_PAGE))
    idx %= total
    return tools[idx * _PER_PAGE : (idx + 1) * _PER_PAGE], total


def panel_embed(prefs: ApprovalPrefs, idx: int) -> discord.Embed:
    rows, total = _page(prefs, idx)
    if prefs.accept_all:
        desc = (
            "⚡ **Accept all is ON** — every tool runs without asking, in every "
            "thread, and this survives restarts.\n\nThe per-tool settings below "
            "are ignored while it is on."
        )
    else:
        desc = (
            "\n".join(
                f"{_DIGITS[i]} {'✅' if t in prefs.approved else '⬜'} `{t}`"
                for i, t in enumerate(rows)
            )
            or "_No tools yet._"
        )
        desc += "\n\nTap a number to toggle. ✅ runs unattended, ⬜ asks first."
    embed = discord.Embed(
        title="Auto-approve",
        description=desc,
        color=0xE67E22 if prefs.accept_all else 0x5865F2,
    )
    embed.add_field(
        name="​",
        value=f"{_ALL} toggle accept-all · {_PREV}{_NEXT} page",
        inline=False,
    )
    embed.set_footer(
        text=f"Page {idx % total + 1}/{total} · "
        f"{len(prefs.approved)} of {len(prefs.tools())} tools auto-approved"
    )
    return embed


async def approvals_panel(
    bot: discord.Client,
    channel: discord.abc.Messageable,
    prefs: ApprovalPrefs,
    on_change: Callable[[], Awaitable[None] | None] | None = None,
) -> None:
    """Post the panel and apply the owner's toggles until it goes idle."""
    idx = 0
    message = await channel.send(embed=panel_embed(prefs, idx))
    rows, _ = _page(prefs, idx)
    for emoji in [*_DIGITS[: max(len(rows), 1)], _ALL, _PREV, _NEXT]:
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            pass

    watched = {*_DIGITS, _ALL, _PREV, _NEXT}

    def check(payload: discord.RawReactionActionEvent) -> bool:
        return (
            payload.message_id == message.id
            and str(payload.emoji) in watched
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

        emoji = str(payload.emoji)
        changed = True
        if emoji == _NEXT:
            idx, changed = idx + 1, False
        elif emoji == _PREV:
            idx, changed = idx - 1, False
        elif emoji == _ALL:
            prefs.accept_all = not prefs.accept_all
        else:
            rows, _ = _page(prefs, idx)
            slot = _DIGITS.index(emoji)
            if slot < len(rows):
                tool = rows[slot]
                # Toggling is by name, so a tool keeps its setting even as the
                # list grows and rows shift underneath it.
                if tool in prefs.approved:
                    prefs.approved.discard(tool)
                else:
                    prefs.approved.add(tool)
            else:
                changed = False

        if changed and on_change is not None:
            res = on_change()
            if asyncio.iscoroutine(res):
                await res
        try:
            await message.edit(embed=panel_embed(prefs, idx))
        except discord.HTTPException:
            pass

        member = payload.member or guild.get_member(payload.user_id)
        if member is not None:
            try:
                await message.remove_reaction(payload.emoji, member)
            except discord.HTTPException:
                pass
