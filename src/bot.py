"""Discord bot: slash + chat commands and message forwarding."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid

import discord
from discord import app_commands
from discord.ext import commands

from src.config import Config
from src.forum import create_session_thread, ensure_forum, git_info
from src.paginator import paginate
from src.permissions import DiscordPermissionBroker
from src.providers import claude as claude_provider
from src.providers.base import Provider
from src.providers.claude import ClaudeProvider
from src.render import command_pages, history_embeds, session_pages
from src.session import Session
from src.store import Store

log = logging.getLogger("src")

SUPPORTED_PROVIDERS = ("claude",)

# Permission modes exposed as choices, plus friendly aliases.
MODES = ("default", "acceptEdits", "plan", "bypassPermissions")
MODE_ALIASES = {
    "auto": "acceptEdits",
    "edit": "acceptEdits",
    "edits": "acceptEdits",
    "accept": "acceptEdits",
    "bypass": "bypassPermissions",
}


class RemoteAgentBot(commands.Bot):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # privileged, enable it in the Dev Portal
        intents.reactions = True
        super().__init__(
            command_prefix=config.prefix, intents=intents, help_command=None
        )

        self.config = config
        self.store = Store(config.db_path)
        self.broker = DiscordPermissionBroker(self, config.approval_timeout)
        self.sessions: dict[int, Session] = {}
        self.pending_provider: dict[int, str] = {}
        self._tasks: set[asyncio.Task] = set()

    async def setup_hook(self) -> None:
        self.tree.interaction_check = self._owner_check  # type: ignore[assignment]
        register_slash(self)
        register_chat(self)
        if self.config.guild_id:
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, getattr(self.user, "id", "?"))

    # ---- gating ----------------------------------------------------------

    @staticmethod
    def _is_server_owner(user_id: int, guild: discord.Guild | None) -> bool:
        return guild is not None and guild.owner_id == user_id

    async def _owner_check(self, interaction: discord.Interaction) -> bool:
        if self._is_server_owner(interaction.user.id, interaction.guild):
            return True
        try:
            await interaction.response.send_message(
                "Only the server owner can use this bot.", ephemeral=True
            )
        except discord.HTTPException:
            pass
        return False

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---- providers -------------------------------------------------------

    def _make_provider(
        self, name: str, *, cwd: str, channel_id: int, resume: str | None, session_id: str | None
    ) -> Provider:
        if name == "claude":
            return ClaudeProvider(
                cwd=cwd,
                channel_id=channel_id,
                broker=self.broker,
                allowed_tools=list(self.config.auto_approve_tools),
                skills=self.config.skills,
                model=self.config.model,
                resume=resume,
                session_id=session_id,
            )
        raise ValueError(f"Provider '{name}' is not implemented yet.")

    async def _resume_pin(self, channel_id: int) -> Session | None:
        pin = self.store.get(channel_id)
        if pin is None or not pin.session_id:
            return None
        cwd = claude_provider.session_cwd(pin.session_id) or self.config.launch_cwd
        provider = self._make_provider(
            pin.provider, cwd=cwd, channel_id=channel_id, resume=pin.session_id, session_id=None
        )
        await provider.start()
        session = Session(
            self, self.store, channel_id, provider, pin.provider, pre_named=True
        )
        self.sessions[channel_id] = session
        return session

    # ---- shared command logic -------------------------------------------

    def _resolve_cwd(self, cwd: str | None) -> str:
        """Resolve a cwd argument against the configured base path.

        No argument uses the base. A relative argument hangs off the base, so
        `new discord-remote-agent` becomes `<base>/discord-remote-agent`.
        Absolute paths and `~` are used as given.
        """
        base = os.path.expanduser(self.config.default_cwd or self.config.launch_cwd)
        if not cwd:
            return base
        path = os.path.expanduser(cwd)
        if os.path.isabs(path):
            return path
        return os.path.join(base, path)

    async def _open_thread(
        self, guild: discord.Guild, name: str, session_id: str, cwd: str
    ) -> discord.Thread:
        forum = await ensure_forum(guild)
        return await create_session_thread(forum, name, session_id, cwd)

    async def _post_history(self, thread: discord.Thread, session_id: str) -> None:
        """Replay prior textual conversation into a freshly opened thread."""
        try:
            history = claude_provider.textual_history(session_id)
        except Exception as exc:
            log.warning("Could not load history: %s", exc)
            return
        embeds = history_embeds(history)
        for embed in embeds:
            try:
                await thread.send(embed=embed)
            except discord.HTTPException:
                pass

    async def do_new(self, guild: discord.Guild | None, cwd: str | None) -> str:
        if guild is None:
            return "Run this in a server."
        provider_name = self.pending_provider.get(guild.id, "claude")
        work_dir = self._resolve_cwd(cwd)
        if not os.path.isdir(work_dir):
            return f"`{work_dir}` is not a directory."
        session_id = str(uuid.uuid4())
        repo, branch = git_info(work_dir)
        try:
            thread = await self._open_thread(
                guild, f"{repo} ({branch})", session_id, work_dir
            )
        except discord.Forbidden:
            return "I need Manage Channels to create the sessions forum."
        except discord.HTTPException as exc:
            return f"Could not create the session thread: `{exc}`"

        try:
            provider = self._make_provider(
                provider_name, cwd=work_dir, channel_id=thread.id, resume=None, session_id=session_id
            )
            await provider.start()
        except Exception as exc:
            return f"Failed to start: `{exc}`"
        self.sessions[thread.id] = Session(
            self, self.store, thread.id, provider, provider_name, pre_named=False
        )
        self.store.pin(thread.id, provider_name, provider.session_id or session_id)
        return f"Started a **{provider_name}** session in {thread.mention}."

    async def do_resume(
        self, guild: discord.Guild | None, session_id: str, cwd: str | None
    ) -> str:
        if guild is None:
            return "Run this in a server."
        pin = self.store.find_by_session_id(session_id)
        provider_name = pin.provider if pin else self.pending_provider.get(guild.id, "claude")
        work_dir = self._resolve_cwd(cwd) if cwd else claude_provider.session_cwd(session_id)
        if not work_dir or not os.path.isdir(work_dir):
            return "Could not find that session's directory. Pass a cwd."

        # Reuse the existing thread if it is still around.
        if pin:
            existing = self.get_channel(pin.channel_id)
            if existing is not None:
                if pin.channel_id in self.sessions:
                    return f"That session is already open in {existing.mention}."
                try:
                    if isinstance(existing, discord.Thread) and existing.archived:
                        await existing.edit(archived=False)
                    await self._resume_pin(pin.channel_id)
                except Exception as exc:
                    return f"Failed to resume: `{exc}`"
                return f"Resumed in {existing.mention}."

        name = claude_provider.session_title(session_id) or "{} ({})".format(*git_info(work_dir))
        try:
            thread = await self._open_thread(guild, name, session_id, work_dir)
        except discord.Forbidden:
            return "I need Manage Channels to create the sessions forum."
        except discord.HTTPException as exc:
            return f"Could not create the session thread: `{exc}`"

        try:
            provider = self._make_provider(
                provider_name, cwd=work_dir, channel_id=thread.id, resume=session_id, session_id=None
            )
            await provider.start()
        except Exception as exc:
            return f"Failed to resume: `{exc}`"
        if pin and pin.channel_id != thread.id:
            self.store.unpin(pin.channel_id)
        self.sessions[thread.id] = Session(
            self, self.store, thread.id, provider, provider_name, pre_named=True
        )
        self.store.pin(thread.id, provider_name, session_id)
        self._spawn(self._post_history(thread, session_id))
        return f"Resumed in {thread.mention}."

    async def do_stop(self, channel: discord.abc.GuildChannel) -> str:
        cid = channel.id
        if self.store.get(cid) is None:
            return "No session here."
        session = self.sessions.pop(cid, None)
        if session is not None:
            try:
                await session.stop()
            except Exception as exc:
                log.warning("Error stopping session: %s", exc)
        self.store.unpin(cid)
        if isinstance(channel, discord.Thread):
            try:
                await channel.edit(archived=True, locked=True)
            except discord.HTTPException:
                pass
        return "Stopped and archived this session."

    async def do_interrupt(self, channel: discord.abc.GuildChannel) -> str:
        session = self.sessions.get(channel.id)
        if session is None:
            return "No live session here."
        await session.interrupt()
        return "Interrupt sent."

    async def do_provider(self, guild: discord.Guild | None, name: str) -> str:
        if guild is None:
            return "Run this in a server."
        if name not in SUPPORTED_PROVIDERS:
            return f"Unknown provider. Options: {', '.join(SUPPORTED_PROVIDERS)}."
        self.pending_provider[guild.id] = name
        return f"Provider set to **{name}** for the next new session."

    async def do_mode(self, channel: discord.abc.GuildChannel, mode: str) -> str:
        mode = MODE_ALIASES.get(mode.lower(), mode)
        if mode not in ("default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"):
            return f"Unknown mode. Options: {', '.join(MODES)}."
        session = self.sessions.get(channel.id) or await self._resume_pin(channel.id)
        if session is None:
            return "No session here. Run this in a session thread."
        try:
            await session.provider.set_mode(mode)
        except Exception as exc:
            return f"Could not switch mode: `{exc}`"
        return f"Mode set to **{mode}**."

    async def do_skill(
        self, channel: discord.abc.GuildChannel, name: str, args: str | None
    ) -> str:
        session = self.sessions.get(channel.id) or await self._resume_pin(channel.id)
        if session is None:
            return "Start a session here first."
        clean = name.lstrip("/")
        text = f"/{clean}" + (f" {args}" if args else "")
        self._spawn(session.handle_message(text))
        return f"Running `/{clean}`"

    async def do_list(self, channel: discord.abc.Messageable) -> None:
        sessions = claude_provider.recent_sessions(100)
        pinned = {p.session_id: p.channel_id for p in self.store.list_all() if p.session_id}
        self._spawn(paginate(self, channel, session_pages(sessions, pinned)))

    async def do_skills(self, channel: discord.abc.Messageable) -> None:
        session = self.sessions.get(getattr(channel, "id", 0))
        if session is not None:
            cmds = await session.provider.list_commands()
        else:
            cmds = await claude_provider.fetch_commands(
                self.config.launch_cwd, skills=self.config.skills, model=self.config.model
            )
        self._spawn(paginate(self, channel, command_pages(cmds)))

    def help_text(self) -> str:
        p = self.config.prefix
        rows = [
            ("new [repo]", "start a session (repo name under the base path, or a full path)"),
            ("resume <id> [cwd]", "resume a session in a thread"),
            ("list", "show resumable sessions"),
            ("skills", "list available skills"),
            ("skill <name> [args]", "run a skill in this session thread"),
            ("mode <name>", "switch permission mode (default, acceptEdits, plan, bypassPermissions)"),
            ("provider <name>", "set provider for the next new"),
            ("interrupt", "stop the current turn"),
            ("stop", "end this session and archive its thread"),
        ]
        lines = [f"Commands (use `/` or `{p}`):"]
        lines += [f"`{p}{cmd}` {desc}" for cmd, desc in rows]
        return "\n".join(lines)

    # ---- message forwarding ---------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        if not self._is_server_owner(message.author.id, message.guild):
            return
        content = message.content
        if content.startswith(self.config.prefix):
            await self.process_commands(message)
            return
        if not content.strip():
            return
        session = self.sessions.get(message.channel.id)
        if session is None:
            try:
                session = await self._resume_pin(message.channel.id)
            except Exception as exc:
                await message.channel.send(f"⚠️ Could not resume: `{exc}`")
                return
        if session is None:
            return
        await session.handle_message(content)


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------


def register_slash(bot: RemoteAgentBot) -> None:
    @bot.tree.command(name="new", description="Start a session pinned to this channel.")
    @app_commands.describe(cwd="Repo name under the base path, or a full path")
    async def new(interaction: discord.Interaction, cwd: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        msg = await bot.do_new(interaction.guild, cwd)
        await interaction.followup.send(msg, ephemeral=True)

    @bot.tree.command(name="resume", description="Resume a session in a new thread.")
    @app_commands.describe(session_id="Agent session id", cwd="Override working directory")
    async def resume(
        interaction: discord.Interaction, session_id: str, cwd: str | None = None
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        msg = await bot.do_resume(interaction.guild, session_id, cwd)
        await interaction.followup.send(msg, ephemeral=True)

    @bot.tree.command(name="list", description="Show resumable sessions.")
    async def list_cmd(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await bot.do_list(interaction.channel)
        await interaction.followup.send("Posted the list here.", ephemeral=True)

    @bot.tree.command(name="skills", description="List available skills / commands.")
    async def skills(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await bot.do_skills(interaction.channel)
        await interaction.followup.send("Posted the skills here.", ephemeral=True)

    @bot.tree.command(name="skill", description="Run a skill / command in this session.")
    @app_commands.describe(name="Skill name (see /skills)", args="Optional arguments")
    async def skill(
        interaction: discord.Interaction, name: str, args: str | None = None
    ) -> None:
        msg = await bot.do_skill(interaction.channel, name, args)
        await interaction.response.send_message(msg, ephemeral=True)

    @bot.tree.command(name="provider", description="Set the provider for the next /new.")
    @app_commands.choices(
        name=[app_commands.Choice(name=p, value=p) for p in SUPPORTED_PROVIDERS]
    )
    async def provider_cmd(
        interaction: discord.Interaction, name: app_commands.Choice[str]
    ) -> None:
        msg = await bot.do_provider(interaction.guild, name.value)
        await interaction.response.send_message(msg, ephemeral=True)

    @bot.tree.command(name="mode", description="Switch this session's permission mode.")
    @app_commands.choices(mode=[app_commands.Choice(name=m, value=m) for m in MODES])
    async def mode_cmd(
        interaction: discord.Interaction, mode: app_commands.Choice[str]
    ) -> None:
        msg = await bot.do_mode(interaction.channel, mode.value)
        await interaction.response.send_message(msg, ephemeral=True)

    @bot.tree.command(name="interrupt", description="Interrupt the running turn.")
    async def interrupt(interaction: discord.Interaction) -> None:
        msg = await bot.do_interrupt(interaction.channel)
        await interaction.response.send_message(msg, ephemeral=True)

    @bot.tree.command(name="stop", description="Stop this channel's session and free it.")
    async def stop(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        msg = await bot.do_stop(interaction.channel)
        await interaction.followup.send(msg, ephemeral=True)


# ---------------------------------------------------------------------------
# Chat (prefix) commands
# ---------------------------------------------------------------------------


def register_chat(bot: RemoteAgentBot) -> None:
    @bot.command(name="new")
    async def new_cmd(ctx: commands.Context, cwd: str | None = None) -> None:
        await ctx.channel.send(await bot.do_new(ctx.guild, cwd))

    @bot.command(name="resume")
    async def resume_cmd(
        ctx: commands.Context, session_id: str, cwd: str | None = None
    ) -> None:
        await ctx.channel.send(await bot.do_resume(ctx.guild, session_id, cwd))

    @bot.command(name="list")
    async def list_cmd(ctx: commands.Context) -> None:
        await bot.do_list(ctx.channel)

    @bot.command(name="skills")
    async def skills_cmd(ctx: commands.Context) -> None:
        await bot.do_skills(ctx.channel)

    @bot.command(name="skill")
    async def skill_cmd(ctx: commands.Context, name: str, *, args: str | None = None) -> None:
        await ctx.channel.send(await bot.do_skill(ctx.channel, name, args))

    @bot.command(name="provider")
    async def provider_cmd(ctx: commands.Context, name: str) -> None:
        await ctx.channel.send(await bot.do_provider(ctx.guild, name))

    @bot.command(name="mode")
    async def mode_cmd(ctx: commands.Context, mode: str) -> None:
        await ctx.channel.send(await bot.do_mode(ctx.channel, mode))

    @bot.command(name="interrupt")
    async def interrupt_cmd(ctx: commands.Context) -> None:
        await ctx.channel.send(await bot.do_interrupt(ctx.channel))

    @bot.command(name="stop")
    async def stop_cmd(ctx: commands.Context) -> None:
        await ctx.channel.send(await bot.do_stop(ctx.channel))

    @bot.command(name="help")
    async def help_cmd(ctx: commands.Context) -> None:
        await ctx.channel.send(bot.help_text())

    async def on_command_error(ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.channel.send(f"Missing argument: {error.param.name}")
            return
        await ctx.channel.send(f"Command error: `{error}`")

    bot.add_listener(on_command_error)


def run(config: Config) -> None:
    logging.basicConfig(level=logging.INFO)
    bot = RemoteAgentBot(config)
    bot.run(config.token, log_handler=None)
