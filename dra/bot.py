"""Discord bot: slash commands + message forwarding for remote agent control."""

from __future__ import annotations

import logging
import os

import discord
from discord import app_commands
from discord.ext import commands

from dra.config import Config
from dra.permissions import DiscordPermissionBroker
from dra.providers.base import Provider
from dra.providers.claude import ClaudeProvider
from dra.session import Session
from dra.store import Store

log = logging.getLogger("dra")

# providers implemented so far
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
        self.sessions: dict[int, Session] = {}
        self.pending_provider: dict[int, str] = {}  # channel_id -> provider name

    # ---- lifecycle -------------------------------------------------------

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

    # ---- gating ----------------------------------------------------------

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

    # ---- provider factory ------------------------------------------------

    def _make_provider(
        self, name: str, *, cwd: str, channel_id: int, resume: str | None
    ) -> Provider:
        if name == "claude":
            return ClaudeProvider(
                cwd=cwd,
                channel_id=channel_id,
                broker=self.broker,
                allowed_tools=list(self.config.auto_approve_tools),
                resume=resume,
                model=self.config.model,
            )
        raise ValueError(f"Provider '{name}' is not implemented yet.")

    # ---- message forwarding ---------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        if not self.is_owner_id(message.author.id):
            return
        session = self.sessions.get(message.channel.id)
        if session is None or not message.content.strip():
            return
        await session.handle_message(message.content)


# ---------------------------------------------------------------------------
# Slash commands (registered against the tree in setup_hook)
# ---------------------------------------------------------------------------


def register_commands(bot: RemoteAgentBot) -> None:
    @bot.tree.command(name="new", description="Start a new agent session in this channel.")
    @app_commands.describe(
        cwd="Working directory (defaults to DEFAULT_CWD)",
        title="Optional label for /list",
    )
    async def new(
        interaction: discord.Interaction,
        cwd: str | None = None,
        title: str | None = None,
    ) -> None:
        channel_id = interaction.channel_id
        assert channel_id is not None
        if channel_id in bot.sessions:
            await interaction.response.send_message(
                "This channel already has a session. Use `/stop` first.",
                ephemeral=True,
            )
            return

        provider_name = bot.pending_provider.get(channel_id, "claude")
        work_dir = cwd or bot.config.default_cwd
        if not os.path.isdir(work_dir):
            await interaction.response.send_message(
                f"`{work_dir}` is not a directory.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)
        try:
            provider = bot._make_provider(
                provider_name, cwd=work_dir, channel_id=channel_id, resume=None
            )
            await provider.start()
        except Exception as exc:
            await interaction.followup.send(f"Failed to start: `{exc}`")
            return

        bot.sessions[channel_id] = Session(
            bot, bot.store, channel_id, provider, provider_name
        )
        bot.store.upsert(
            channel_id,
            provider_name,
            work_dir,
            session_id=provider.session_id,
            title=title,
        )
        await interaction.followup.send(
            f"Started **{provider_name}** session in `{work_dir}`. "
            "Type in this channel to talk to the agent."
        )

    @bot.tree.command(name="resume", description="Resume an existing session in this channel.")
    @app_commands.describe(
        session_id="The agent session id (see /list)",
        cwd="Working directory the session was created under (if not known)",
    )
    async def resume(
        interaction: discord.Interaction,
        session_id: str,
        cwd: str | None = None,
    ) -> None:
        channel_id = interaction.channel_id
        assert channel_id is not None
        if channel_id in bot.sessions:
            await interaction.response.send_message(
                "This channel already has a session. Use `/stop` first.",
                ephemeral=True,
            )
            return

        known = bot.store.find_by_session_id(session_id)
        provider_name = known.provider if known else bot.pending_provider.get(
            channel_id, "claude"
        )
        work_dir = cwd or (known.cwd if known else bot.config.default_cwd)
        if not os.path.isdir(work_dir):
            await interaction.response.send_message(
                f"`{work_dir}` is not a directory. Pass the original `cwd`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        try:
            provider = bot._make_provider(
                provider_name, cwd=work_dir, channel_id=channel_id, resume=session_id
            )
            await provider.start()
        except Exception as exc:
            await interaction.followup.send(f"Failed to resume: `{exc}`")
            return

        # Rebind this session id to the current channel.
        if known and known.channel_id != channel_id:
            bot.store.delete(known.channel_id)
        bot.sessions[channel_id] = Session(
            bot, bot.store, channel_id, provider, provider_name
        )
        bot.store.upsert(
            channel_id,
            provider_name,
            work_dir,
            session_id=session_id,
            title=known.title if known else None,
        )
        await interaction.followup.send(
            f"Resumed session `{session_id}` in `{work_dir}`."
        )

    @bot.tree.command(name="list", description="List known sessions.")
    async def list_sessions(interaction: discord.Interaction) -> None:
        rows = bot.store.list_all()
        if not rows:
            await interaction.response.send_message("No sessions yet.", ephemeral=True)
            return
        lines = []
        for r in rows:
            live = "🟢" if r.channel_id in bot.sessions else "⚪"
            sid = r.session_id or "(pending)"
            label = r.title or r.cwd
            lines.append(
                f"{live} <#{r.channel_id}> · **{r.provider}** · `{sid}` · {label}"
            )
        embed = discord.Embed(
            title="Sessions", description="\n".join(lines)[:4096], color=0x5865F2
        )
        embed.set_footer(text="🟢 attached in this run · ⚪ resumable via /resume")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="provider", description="Set the provider for the next /new.")
    @app_commands.describe(name="Provider to use")
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

    @bot.tree.command(name="interrupt", description="Interrupt the running turn.")
    async def interrupt(interaction: discord.Interaction) -> None:
        session = bot.sessions.get(interaction.channel_id or 0)
        if session is None:
            await interaction.response.send_message(
                "No active session here.", ephemeral=True
            )
            return
        await session.interrupt()
        await interaction.response.send_message("Interrupt sent.", ephemeral=True)

    @bot.tree.command(name="stop", description="Stop and detach this channel's session.")
    async def stop(interaction: discord.Interaction) -> None:
        channel_id = interaction.channel_id or 0
        session = bot.sessions.pop(channel_id, None)
        if session is None:
            await interaction.response.send_message(
                "No active session here.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await session.stop()
        except Exception as exc:
            log.warning("Error stopping session: %s", exc)
        bot.store.set_status(channel_id, "stopped")
        await interaction.followup.send(
            "Session stopped. The id stays in `/list` for `/resume`.", ephemeral=True
        )


def run(config: Config) -> None:
    logging.basicConfig(level=logging.INFO)
    bot = RemoteAgentBot(config)
    bot.run(config.token, log_handler=None)
