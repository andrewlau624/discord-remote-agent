"""The #terminal channel: owner messages run as shell commands.

!terminal binds the channel to a working directory. Every plain message the
owner sends there is executed with the shell and the output is relayed back,
chunked into code fences, with anything oversized attached as a file. cd is
handled in-process so the working directory persists between commands.
Bindings live in memory; run !terminal again after a bot restart.
"""

from __future__ import annotations

import asyncio
import io
import os

import discord

CHANNEL_NAME = "terminal"
_TIMEOUT = 300  # seconds per command
_CHUNK = 1900  # per-message cap under Discord's 2000 limit
_MAX_INLINE = 3 * _CHUNK  # beyond this, attach the full output as a file


class TerminalManager:
    def __init__(self) -> None:
        self._cwds: dict[int, str] = {}

    def bind(self, channel_id: int, cwd: str) -> None:
        self._cwds[channel_id] = cwd

    def handles(self, channel_id: int) -> bool:
        return channel_id in self._cwds

    def cwd(self, channel_id: int) -> str | None:
        return self._cwds.get(channel_id)

    async def run(self, channel: discord.abc.Messageable, command: str) -> None:
        cwd = self._cwds.get(channel.id)
        if cwd is None:
            return
        command = command.strip()
        if command == "cd" or command.startswith("cd "):
            await self._cd(channel, cwd, command[2:].strip())
            return

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            await channel.send(f"`{exc}`")
            return
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await channel.send(f"⏱️ Timed out after {_TIMEOUT}s.")
            return

        text = out.decode(errors="replace").rstrip()
        status = "" if proc.returncode == 0 else f"exit {proc.returncode}"
        if not text:
            await channel.send(f"`({status or 'ok, no output'})`")
            return
        if len(text) > _MAX_INLINE:
            head = text[:_CHUNK]
            note = f"Output is {len(text)} chars; full log attached."
            file = discord.File(io.BytesIO(text.encode()), filename="output.txt")
            await channel.send(f"```\n{head}\n```{note}", file=file)
        else:
            for i in range(0, len(text), _CHUNK):
                await channel.send(f"```\n{text[i : i + _CHUNK]}\n```")
        if status:
            await channel.send(f"`({status})`")

    async def _cd(
        self, channel: discord.abc.Messageable, cwd: str, target: str
    ) -> None:
        path = os.path.expanduser(target) if target else os.path.expanduser("~")
        if not os.path.isabs(path):
            path = os.path.normpath(os.path.join(cwd, path))
        if not os.path.isdir(path):
            await channel.send(f"`cd: no such directory: {target}`")
            return
        self._cwds[channel.id] = path
        await channel.send(f"📂 `{path}`")


async def ensure_terminal_channel(guild: discord.Guild) -> discord.TextChannel:
    """Find the #terminal channel, creating it if needed."""
    for ch in guild.text_channels:
        if ch.name == CHANNEL_NAME:
            return ch
    return await guild.create_text_channel(
        CHANNEL_NAME, reason="discord-remote-agent terminal"
    )
