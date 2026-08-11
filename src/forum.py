"""The 'sessions' forum: one forum post (thread) per agent session.

Each session lives in its own thread under a forum channel named 'sessions',
created on demand. The thread name is the session title; the starter post carries
the repo, branch, working directory, and session id.
"""

from __future__ import annotations

import os
import subprocess

import discord

FORUM_NAME = "sessions"


def git_info(cwd: str) -> tuple[str, str]:
    """Return (repo, branch) for a directory, best effort."""

    def run(args: list[str]) -> str:
        try:
            out = subprocess.run(
                ["git", "-C", cwd, *args],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    top = run(["rev-parse", "--show-toplevel"])
    repo = os.path.basename(top) if top else os.path.basename(os.path.abspath(cwd))
    branch = run(["rev-parse", "--abbrev-ref", "HEAD"]) or "-"
    return repo or "repo", branch


async def ensure_forum(guild: discord.Guild) -> discord.ForumChannel:
    """Find the 'sessions' forum, creating it if needed."""
    for channel in guild.channels:
        if isinstance(channel, discord.ForumChannel) and channel.name == FORUM_NAME:
            return channel
    return await guild.create_forum(
        name=FORUM_NAME, reason="discord-remote-agent session threads"
    )


def _session_embed(title: str, session_id: str, cwd: str) -> discord.Embed:
    repo, branch = git_info(cwd)
    embed = discord.Embed(title=title[:256], color=0x5865F2)
    embed.add_field(name="repo", value=repo, inline=True)
    embed.add_field(name="branch", value=branch, inline=True)
    embed.add_field(name="cwd", value=f"`{cwd}`", inline=False)
    embed.add_field(name="session", value=f"`{session_id}`", inline=False)
    return embed


async def create_session_thread(
    forum: discord.ForumChannel, name: str, session_id: str, cwd: str
) -> discord.Thread:
    """Create a forum post for a session and return its thread."""
    result = await forum.create_thread(
        name=(name or "session")[:100],
        embed=_session_embed(name or "session", session_id, cwd),
        reason="new agent session",
    )
    return result.thread
