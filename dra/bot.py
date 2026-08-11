"""Discord bot: slash commands + message forwarding for remote agent control."""

from __future__ import annotations

import logging
import os
import uuid

import discord
from discord import app_commands
from discord.ext import commands

from dra.config import Config
from dra.permissions import DiscordPermissionBroker
from dra.providers import claude as claude_provider
from dra.providers.base import Provider
from dra.providers.claude import ClaudeProvider
from dra.render import command_embeds
from dra.session import Session
from dra.store import Store

log = logging.getLogger("dra")

SUPPORTED_PROVIDERS = ("claude",)


class RemoteAgentBot(commands.Bot):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # privileged, enable it in the Dev Portal
        super().__init__(command_prefix="!", intents=intents)

        self.config = config
        self.store = Store(config.db_path)
        self.broker = DiscordPermissionBroker(
            self, config.owner_ids, config.approval_timeout
        )
        self.sessions: dict[int, Session] = {}  # live pins
        self.pending_provider: dict[int, str] = {}

    async def setup_hook(self) -> None:
        self.tree.interaction_check = self._owner_check  # type: ignore[assignment]
        register_commands(self)
        if self.config.guild_id:
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, getattr(self.user, "id", "?"))

    def is_owner_id(self, user_id: int) -> bool:
        return user_id in self.config.owner_ids

    async def _owner_check(self, interaction: discord.Interaction) -> bool:
        if self.is_owner_id(interaction.user.id):
            return True
        try:
            await interaction.response.send_message(
                "You are not authorized to use this bot.", ephemeral=True
            )
        except discord.HTTPException:
            pass
        return False

    def _make_provider(
        self,
        name: str,
        *,
        cwd: str,
        channel_id: int,
        resume: str | None,
        session_id: str | None,
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
        """Bring a stored-but-not-live pin back online, if it has a session id."""
        pin = self.store.get(channel_id)
        if pin is None or not pin.session_id:
            return None
        cwd = claude_provider.session_cwd(pin.session_id) or self.config.launch_cwd
        provider = self._make_provider(
            pin.provider,
            cwd=cwd,
            channel_id=channel_id,
            resume=pin.session_id,
            session_id=None,
        )
        await provider.start()
        session = Session(self, self.store, channel_id, provider, pin.provider)
        self.sessions[channel_id] = session
        return session

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        if not self.is_owner_id(message.author.id) or not message.content.strip():
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
        await session.handle_message(message.content)


def register_commands(bot: RemoteAgentBot) -> None:
    @bot.tree.command(name="new", description="Start a session pinned to this channel.")
    @app_commands.describe(cwd="Working directory (defaults to where the bot runs)")
    async def new(interaction: discord.Interaction, cwd: str | None = None) -> None:
        channel_id = interaction.channel_id
        assert channel_id is not None
        if bot.store.get(channel_id) is not None:
            await interaction.response.send_message(
                "This channel is already pinned to a session. Use `/stop` first.",
                ephemeral=True,
            )
            return

        provider_name = bot.pending_provider.get(channel_id, "claude")
        work_dir = cwd or bot.config.launch_cwd
        if not os.path.isdir(work_dir):
            await interaction.response.send_message(
                f"`{work_dir}` is not a directory.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)
        session_id = str(uuid.uuid4())
        try:
            provider = bot._make_provider(
                provider_name,
                cwd=work_dir,
                channel_id=channel_id,
                resume=None,
                session_id=session_id,
            )
            await provider.start()
        except Exception as exc:
            await interaction.followup.send(f"Failed to start: `{exc}`")
            return

        bot.sessions[channel_id] = Session(
            bot, bot.store, channel_id, provider, provider_name
        )
        bot.store.pin(channel_id, provider_name, provider.session_id or session_id)
        await interaction.followup.send(
            f"Pinned a **{provider_name}** session to this channel in `{work_dir}`. "
            "Type here to talk to the agent."
        )

    @bot.tree.command(name="resume", description="Resume a session and pin it here.")
    @app_commands.describe(session_id="Agent session id", cwd="Override working directory")
    async def resume(
        interaction: discord.Interaction, session_id: str, cwd: str | None = None
    ) -> None:
        channel_id = interaction.channel_id
        assert channel_id is not None
        if bot.store.get(channel_id) is not None:
            await interaction.response.send_message(
                "This channel is already pinned to a session. Use `/stop` first.",
                ephemeral=True,
            )
            return

        pin = bot.store.find_by_session_id(session_id)
        provider_name = pin.provider if pin else bot.pending_provider.get(channel_id, "claude")
        work_dir = cwd or claude_provider.session_cwd(session_id)
        if not work_dir or not os.path.isdir(work_dir):
            await interaction.response.send_message(
                "Could not find that session's directory. Pass `cwd`.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)
        try:
            provider = bot._make_provider(
                provider_name,
                cwd=work_dir,
                channel_id=channel_id,
                resume=session_id,
                session_id=None,
            )
            await provider.start()
        except Exception as exc:
            await interaction.followup.send(f"Failed to resume: `{exc}`")
            return

        if pin and pin.channel_id != channel_id:
            bot.store.unpin(pin.channel_id)
        bot.sessions[channel_id] = Session(
            bot, bot.store, channel_id, provider, provider_name
        )
        bot.store.pin(channel_id, provider_name, session_id)
        await interaction.followup.send(f"Resumed `{session_id}` in `{work_dir}`.")

    @bot.tree.command(name="list", description="List channels pinned to a session.")
    async def list_pins(interaction: discord.Interaction) -> None:
        pins = bot.store.list_all()
        if not pins:
            await interaction.response.send_message("No pinned sessions.", ephemeral=True)
            return
        lines = []
        for p in pins:
            live = "🟢" if p.channel_id in bot.sessions else "⚪"
            label = ""
            if p.session_id:
                info = claude_provider.get_session_info(p.session_id)
                if info:
                    label = info.custom_title or info.summary or info.cwd or ""
            sid = p.session_id or "(pending)"
            lines.append(f"{live} <#{p.channel_id}> · **{p.provider}** · `{sid}` · {label}")
        embed = discord.Embed(
            title="Pinned sessions", description="\n".join(lines)[:4096], color=0x5865F2
        )
        embed.set_footer(text="🟢 live · ⚪ pinned, resumes when you send a message")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="provider", description="Set the provider for the next /new.")
    @app_commands.choices(
        name=[app_commands.Choice(name=p, value=p) for p in SUPPORTED_PROVIDERS]
    )
    async def provider_cmd(
        interaction: discord.Interaction, name: app_commands.Choice[str]
    ) -> None:
        channel_id = interaction.channel_id
        assert channel_id is not None
        bot.pending_provider[channel_id] = name.value
        await interaction.response.send_message(
            f"Provider for this channel set to **{name.value}**.", ephemeral=True
        )

    @bot.tree.command(name="skills", description="List available skills / commands.")
    async def skills(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        channel_id = interaction.channel_id or 0
        session = bot.sessions.get(channel_id)
        try:
            if session is not None:
                cmds = await session.provider.list_commands()
            else:
                cmds = await claude_provider.fetch_commands(
                    bot.config.launch_cwd, skills=bot.config.skills, model=bot.config.model
                )
        except Exception as exc:
            await interaction.followup.send(f"Could not read skills: `{exc}`", ephemeral=True)
            return
        embeds = command_embeds(cmds)
        await interaction.followup.send(embed=embeds[0], ephemeral=True)
        for extra in embeds[1:]:
            await interaction.followup.send(embed=extra, ephemeral=True)

    @bot.tree.command(name="skill", description="Run a skill / command in this session.")
    @app_commands.describe(name="Skill name (see /skills)", args="Optional arguments")
    async def skill(
        interaction: discord.Interaction, name: str, args: str | None = None
    ) -> None:
        channel_id = interaction.channel_id or 0
        session = bot.sessions.get(channel_id)
        if session is None:
            session = await bot._resume_pin(channel_id)
        if session is None:
            await interaction.response.send_message(
                "Start a session here first with `/new`.", ephemeral=True
            )
            return
        clean = name.lstrip("/")
        text = f"/{clean}" + (f" {args}" if args else "")
        await interaction.response.send_message(f"Running `/{clean}`", ephemeral=True)
        await session.handle_message(text)

    @bot.tree.command(name="interrupt", description="Interrupt the running turn.")
    async def interrupt(interaction: discord.Interaction) -> None:
        session = bot.sessions.get(interaction.channel_id or 0)
        if session is None:
            await interaction.response.send_message("No live session here.", ephemeral=True)
            return
        await session.interrupt()
        await interaction.response.send_message("Interrupt sent.", ephemeral=True)

    @bot.tree.command(name="stop", description="Stop this channel's session and free it.")
    async def stop(interaction: discord.Interaction) -> None:
        channel_id = interaction.channel_id or 0
        if bot.store.get(channel_id) is None:
            await interaction.response.send_message("No session pinned here.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        session = bot.sessions.pop(channel_id, None)
        if session is not None:
            try:
                await session.stop()
            except Exception as exc:
                log.warning("Error stopping session: %s", exc)
        bot.store.unpin(channel_id)
        await interaction.followup.send("Stopped. Channel is free for a new `/new`.", ephemeral=True)


def run(config: Config) -> None:
    logging.basicConfig(level=logging.INFO)
    bot = RemoteAgentBot(config)
    bot.run(config.token, log_handler=None)
