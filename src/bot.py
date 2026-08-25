"""Discord bot: prefix commands and message forwarding."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands

from src.approvals import ApprovalPrefs, approvals_panel
from src.attachments import collect as collect_attachments
from src.attachments import compose_note
from src.auth import AuthStore, LoginManager
from src.config import Config
from src.forum import (
    create_session_thread,
    ensure_forum,
    git_info,
    list_repos,
    list_worktrees,
    resolve_worktree,
)
from src.paginator import paginate
from src.permissions import DiscordPermissionBroker
from src.prefs import DisplayPrefs, view_panel
from src.providers import (
    PROVIDERS,
    create_provider,
    modes_for_provider,
    provider_module,
    recent_sessions,
    session_cwd,
    session_title,
    textual_history,
)
from src.providers.base import ContextState
from src.render import (
    command_pages,
    context_embed,
    handoff_embed,
    history_embeds,
    repo_pages,
    session_pages,
)
from src.handoff import BRIEF_PROMPT, compose_seed, working_state_async
from src.session import Session
from src.store import Store
from src.context import warn_panel

log = logging.getLogger("src")

DEFAULT_PROVIDER = "claude"

# Permission mode aliases shared by parsing and switching. Each provider
# accepts its own subset -- see src/providers.
MODE_ALIASES = {
    "edit": "acceptEdits",
    "edits": "acceptEdits",
    "accept": "acceptEdits",
    "bypass": "bypassPermissions",
}


def canonical_mode(mode: str | None) -> str | None:
    if not mode:
        return None
    return MODE_ALIASES.get(mode.lower(), mode)


async def provider_session_cwd(provider: str, session_id: str) -> str | None:
    """A session's working directory from the provider's own records."""
    try:
        return await session_cwd(provider, session_id)
    except Exception as exc:
        log.warning("Could not resolve cwd for %s session %s: %s", provider, session_id, exc)
        return None


async def provider_session_title(provider: str, session_id: str) -> str | None:
    """A session's title from the provider's own records."""
    try:
        return await session_title(provider, session_id)
    except Exception as exc:
        log.warning("Could not resolve title for %s session %s: %s", provider, session_id, exc)
        return None


def _parse_new_args(
    text: str,
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """Parse `new` arguments into (repo, branch, mode, provider, error).

    Accepts key:value tokens (repo: branch: mode: provider:) in any order. A
    bare first token is the repo; a bare mode name also works, so
    `new myrepo bypass` still does what it looks like."""
    repo = branch = mode = provider = None
    known_modes: set[str] = set()
    for name in PROVIDERS:
        try:
            known_modes.update(modes_for_provider(name))
        except Exception:
            continue
    for tok in text.split():
        key, sep, val = tok.partition(":")
        if sep and key.lower() in ("repo", "branch", "mode", "provider"):
            if key.lower() == "repo":
                repo = val or None
            elif key.lower() == "branch":
                branch = val or None
            elif key.lower() == "provider":
                provider = val.lower() or None
                if provider not in PROVIDERS:
                    return None, None, None, None, (
                        f"Unknown provider `{val}`. Options: {', '.join(PROVIDERS)}."
                    )
            else:
                mode = canonical_mode(val) or None
        elif repo is None and canonical_mode(tok) not in known_modes:
            repo = tok
        elif mode is None and canonical_mode(tok) in known_modes:
            mode = canonical_mode(tok)
        else:
            return None, None, None, None, (
                f"Unrecognized argument `{tok}`. "
                "Use `new [repo:<name>] [branch:<branch>] [mode:<mode>] [provider:<name>]`."
            )
    return repo, branch, mode, provider, None


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
        self._approvals_path = str(Path(config.db_path).with_name("approvals.json"))
        # config.toml's auto_approve seeds the very first run so an existing
        # setup keeps working; after that the panel owns the list.
        self.approvals = ApprovalPrefs.load(
            self._approvals_path, seed=list(config.auto_approve_tools)
        )
        self.broker = DiscordPermissionBroker(
            self,
            config.approval_timeout,
            approvals=self.approvals,
            on_new_tool=lambda _name: self.approvals.save(self._approvals_path),
        )
        self.sessions: dict[int, Session] = {}
        self.pending_provider: dict[int, str] = {}
        self._tasks: set[asyncio.Task] = set()
        self._prefs_path = str(Path(config.db_path).with_name("prefs.json"))
        self.prefs = DisplayPrefs.load(self._prefs_path)
        # Named Anthropic accounts for remote login/switching; the active
        # token is injected into newly launched Claude sessions.
        self.auth = AuthStore.load(str(Path(config.db_path).with_name("auth.json")))
        self.login = LoginManager(self.auth)

    async def setup_hook(self) -> None:
        register_chat(self)

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, getattr(self.user, "id", "?"))

    # ---- gating ----------------------------------------------------------

    @staticmethod
    def _is_server_owner(user_id: int, guild: discord.Guild | None) -> bool:
        return guild is not None and guild.owner_id == user_id

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---- providers -------------------------------------------------------

    def _provider_for_new(self, guild: discord.Guild | None, override: str | None = None) -> str:
        if override:
            return override
        return self.pending_provider.get(guild.id, DEFAULT_PROVIDER) if guild else DEFAULT_PROVIDER

    def _provider_options(self, name: str) -> dict:
        options: dict = {}
        if name == "claude":
            options["skills"] = self.config.skills
            token = getattr(self.auth, "token", None)
            if token:
                options["anthropic_token"] = token
        return options

    async def _make_provider(
        self,
        name: str,
        *,
        cwd: str,
        channel_id: int,
        resume: str | None,
        session_id: str | None,
        mode: str = "default",
    ) -> Any:
        return await create_provider(
            name,
            cwd=cwd,
            channel_id=channel_id,
            broker=self.broker,
            model=self.config.provider_models.get(name),
            resume=resume,
            session_id=session_id,
            mode=mode,
            options=self._provider_options(name),
        )

    async def _resume_pin(self, channel_id: int) -> Session | None:
        pin = self.store.get(channel_id)
        if pin is None or not pin.session_id:
            return None
        cwd = await provider_session_cwd(pin.provider, pin.session_id) or self.config.launch_cwd
        provider = await self._make_provider(
            pin.provider,
            cwd=cwd,
            channel_id=channel_id,
            resume=pin.session_id,
            session_id=None,
            mode=pin.mode,
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
        self, guild: discord.Guild, name: str, session_id: str, cwd: str,
        provider: str = DEFAULT_PROVIDER,
    ) -> discord.Thread:
        forum = await ensure_forum(guild, provider)
        return await create_session_thread(forum, name, session_id, cwd, provider)

    async def _post_history(self, thread: discord.Thread, session_id: str, provider: str) -> None:
        """Replay prior textual conversation into a freshly opened thread."""
        try:
            history = await textual_history(provider, session_id)
        except Exception as exc:
            log.warning("Could not load history: %s", exc)
            return
        embeds = history_embeds(history)
        for embed in embeds:
            try:
                await thread.send(embed=embed)
            except discord.HTTPException:
                pass

    async def do_new(
        self,
        guild: discord.Guild | None,
        cwd: str | None,
        mode: str | None = None,
        branch: str | None = None,
        provider_override: str | None = None,
    ) -> str:
        if guild is None:
            return "Run this in a server."
        mode = canonical_mode(mode)
        provider_name = self._provider_for_new(guild, provider_override)
        allowed_modes = modes_for_provider(provider_name)
        if not mode:
            mode = "default"
        if mode not in allowed_modes:
            return (
                f"`{provider_name}` does not support mode `{mode}`. "
                f"Options: {', '.join(allowed_modes)}."
            )
        work_dir = self._resolve_cwd(cwd)
        if not os.path.isdir(work_dir):
            return f"`{work_dir}` is not a directory."
        carried: list[str] = []
        if branch:
            # Reuses an existing checkout of that branch, or creates a
            # worktree under <repo>/.worktrees/ so the main checkout is
            # never disturbed. `repos` lists what already exists.
            try:
                work_dir, carried = await asyncio.to_thread(
                    resolve_worktree, work_dir, branch
                )
            except RuntimeError as exc:
                return f"Could not prepare a worktree for `{branch}`: `{exc}`"
        session_id = str(uuid.uuid4())
        repo, branch = git_info(work_dir)
        try:
            thread = await self._open_thread(
                guild, f"{repo} ({branch})", session_id, work_dir, provider_name
            )
        except discord.Forbidden:
            return "I need Manage Channels to create the sessions forum."
        except discord.HTTPException as exc:
            return f"Could not create the session thread: `{exc}`"

        try:
            provider = await self._make_provider(
                provider_name,
                cwd=work_dir,
                channel_id=thread.id,
                resume=None,
                session_id=session_id,
                mode=mode,
            )
            await provider.start()
        except Exception as exc:
            return f"Failed to start: `{exc}`"
        self.sessions[thread.id] = Session(
            self, self.store, thread.id, provider, provider_name, pre_named=False
        )
        self.store.pin(thread.id, provider_name, provider.session_id or session_id)
        if mode != "default":
            self.store.set_mode(thread.id, mode)
        started = f"Started a **{provider_name}** session in {thread.mention}."
        if mode != "default":
            started += f" Mode: **{mode}**."
        if carried:
            started += f" Carried {len(carried)} env file(s): {', '.join(f'`{c}`' for c in carried[:5])}."
        return started

    async def do_resume(
        self,
        guild: discord.Guild | None,
        session_id: str | None,
        cwd: str | None,
        channel: discord.abc.GuildChannel | None = None,
    ) -> str:
        if guild is None:
            return "Run this in a server."
        if session_id is None:
            return await self._resume_here(channel)
        pin = self.store.find_by_session_id(session_id)
        provider_name = pin.provider if pin else self._provider_for_new(guild)
        work_dir = cwd
        if work_dir:
            work_dir = self._resolve_cwd(work_dir)
        else:
            work_dir = await provider_session_cwd(provider_name, session_id)
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

        title = await provider_session_title(provider_name, session_id)
        name = title or "{} ({})".format(*git_info(work_dir))
        try:
            thread = await self._open_thread(guild, name, session_id, work_dir, provider_name)
        except discord.Forbidden:
            return "I need Manage Channels to create the sessions forum."
        except discord.HTTPException as exc:
            return f"Could not create the session thread: `{exc}`"

        try:
            provider = await self._make_provider(
                provider_name,
                cwd=work_dir,
                channel_id=thread.id,
                resume=session_id,
                session_id=None,
                mode=pin.mode if pin else "default",
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
        if pin and pin.mode != "default":
            self.store.set_mode(thread.id, pin.mode)
        self._spawn(self._post_history(thread, session_id, provider_name))
        return f"Resumed in {thread.mention}."

    async def _resume_here(self, channel: discord.abc.GuildChannel | None) -> str:
        """Restart the session this thread is already bound to.

        `stop` leaves the row in place precisely so this works: the thread
        still knows its session id, so no argument is needed.
        """
        if channel is None:
            return "Run this in a session thread."
        pin = self.store.get(channel.id)
        if pin is None or not pin.session_id:
            return (
                "No session is bound to this thread. Pass a session id, or "
                f"use `{self.config.prefix}list` to find one."
            )
        if channel.id in self.sessions:
            return "This session is already running."
        if isinstance(channel, discord.Thread) and channel.archived:
            try:
                await channel.edit(archived=False)
            except discord.HTTPException as exc:
                return f"Could not unarchive this thread: `{exc}`"
        try:
            session = await self._resume_pin(channel.id)
        except Exception as exc:
            return f"Failed to resume: `{exc}`"
        if session is None:
            return "Could not resume: that session's data is missing."
        self.store.set_status(channel.id, "live")
        return "Resumed this session."

    async def do_stop(self, channel: discord.abc.GuildChannel, forget: bool = False) -> str:
        """Shut the session down, keeping the binding so it can restart here.

        The row is marked stopped rather than deleted, which is what lets a
        bare `resume` in this thread pick the same session back up. The thread
        is archived but deliberately not locked: a locked thread cannot be
        posted in, so resuming in place would be impossible. `forget` is the
        hard delete for when you want the binding gone for good.
        """
        cid = channel.id
        pin = self.store.get(cid)
        if pin is None:
            return "No session here."
        session = self.sessions.pop(cid, None)
        if session is not None:
            try:
                await session.stop()
            except Exception as exc:
                log.warning("Error stopping session: %s", exc)
        if forget:
            self.store.unpin(cid)
        else:
            self.store.set_status(cid, "stopped")
        if isinstance(channel, discord.Thread):
            try:
                await channel.edit(archived=True)
            except discord.HTTPException:
                pass
        if forget:
            return "Stopped and forgot this session. `resume` will not bring it back."
        return (
            "Stopped and archived this session. Post `"
            f"{self.config.prefix}resume` here to start it back up."
        )

    async def do_interrupt(self, channel: discord.abc.GuildChannel) -> str:
        session = self.sessions.get(channel.id)
        if session is None:
            return "No live session here."
        await session.interrupt()
        return "Interrupt sent."

    async def do_provider(self, guild: discord.Guild | None, name: str) -> str:
        if guild is None:
            return "Run this in a server."
        name = name.lower()
        if name not in PROVIDERS:
            return f"Unknown provider. Options: {', '.join(PROVIDERS)}."
        self.pending_provider[guild.id] = name
        return f"Provider set to **{name}** for the next new session."

    async def do_mode(self, channel: discord.abc.GuildChannel, mode: str) -> str:
        mode = canonical_mode(mode)
        if mode is None:
            return "Pass a mode to switch to."
        session = self.sessions.get(channel.id) or await self._resume_pin(channel.id)
        if session is None:
            return "No session here. Run this in a session thread."
        allowed = modes_for_provider(session.provider_name)
        if mode not in allowed:
            return (
                f"`{session.provider_name}` does not support mode `{mode}`. "
                f"Options: {', '.join(allowed)}."
            )
        relaunched = False
        try:
            if mode == "bypassPermissions":
                # The CLI only honors bypass when the session is launched with
                # it, so restart the provider resuming the same session.
                await self._relaunch_with_mode(channel.id, session, mode)
                relaunched = True
            else:
                await session.provider.set_mode(mode)
        except Exception as exc:
            return f"Could not switch mode: `{exc}`"
        self.store.set_mode(channel.id, mode)
        suffix = " (session relaunched)" if relaunched else ""
        return f"Mode set to **{mode}**{suffix}. It sticks across restarts."

    async def _relaunch_with_mode(
        self, channel_id: int, session: Session, mode: str
    ) -> None:
        """Restart a session's provider so a launch-only mode can apply."""
        old = session.provider
        session_id = old.session_id
        if not session_id:
            raise RuntimeError("No session id to resume with yet. Send a message first.")
        cwd = getattr(old, "cwd", None) or self.config.launch_cwd
        await old.stop()
        provider = await self._make_provider(
            session.provider_name,
            cwd=cwd,
            channel_id=channel_id,
            resume=session_id,
            session_id=None,
            mode=mode,
        )
        await provider.start()
        session.provider = provider

    async def handle_context_warning(
        self,
        channel: discord.abc.Messageable,
        session: Session,
        state: ContextState,
    ) -> None:
        """Offer the context actions, then carry out whichever was picked."""
        action = await warn_panel(self, channel, state, self.config.prefix)
        if action == "compact":
            # /compact is the CLI's own in-place summarize; after it the
            # window is smaller, so the warning should be able to fire again.
            session.reset_context_warnings()
            await channel.send("Compacting…")
            self._spawn(session.handle_message("/compact"))
        elif action == "handoff":
            error = await self.do_handoff(channel)
            if error:
                await channel.send(error)

    async def do_handoff(self, channel: discord.abc.GuildChannel) -> str | None:
        """Start a fresh session in a new thread, carrying the work over.

        The outgoing session writes its own brief first: it still has the full
        context, so nothing else can summarize it as well. That brief, the
        recent exchange and the repo's actual state become the new session's
        first message.

        Returns an error to report, or None on success -- success posts its own
        cross-links, and a caller echoing a reply into the old thread would
        unarchive the thread this just archived.
        """
        guild = getattr(channel, "guild", None)
        if guild is None:
            return "Run this in a server."
        session = self.sessions.get(channel.id)
        if session is None:
            return "No live session here to hand off."
        pin = self.store.get(channel.id)
        old_session_id = session.provider.session_id
        work_dir = getattr(session.provider, "cwd", None) or self.config.launch_cwd

        await channel.send("📝 Asking this session to write a handoff brief…")
        try:
            brief = await session.ask(BRIEF_PROMPT)
        except Exception as exc:
            return f"Could not get a handoff brief: `{exc}`"
        if not brief:
            return "The session returned an empty brief; not handing off."

        history: list[tuple[str, str]] = []
        if old_session_id:
            try:
                history = await textual_history(session.provider_name, old_session_id)
            except Exception as exc:
                log.warning("Could not read history for handoff: %s", exc)
        state = await working_state_async(work_dir)
        seed = compose_seed(brief, history, state, old_session_id=old_session_id)

        repo, branch = git_info(work_dir)
        new_session_id = str(uuid.uuid4())
        try:
            thread = await self._open_thread(
                guild, f"{repo} ({branch}) cont.", new_session_id, work_dir,
                session.provider_name,
            )
        except discord.Forbidden:
            return "I need Manage Channels to create the sessions forum."
        except discord.HTTPException as exc:
            return f"Could not create the new thread: `{exc}`"

        provider_name = pin.provider if pin else session.provider_name
        mode = pin.mode if pin else "default"
        try:
            provider = await self._make_provider(
                provider_name,
                cwd=work_dir,
                channel_id=thread.id,
                resume=None,
                session_id=new_session_id,
                mode=mode,
            )
            await provider.start()
        except Exception as exc:
            return f"Could not start the new session: `{exc}`"

        new_session = Session(
            self, self.store, thread.id, provider, provider_name, pre_named=True
        )
        self.sessions[thread.id] = new_session
        self.store.pin(thread.id, provider_name, new_session_id)
        if mode != "default":
            self.store.set_mode(thread.id, mode)

        # Retire the old one only once the new thread is genuinely up, so a
        # failure above leaves the original session intact and usable.
        await self._retire_after_handoff(channel, thread)
        await thread.send(embed=handoff_embed(brief, channel))
        self._spawn(new_session.handle_message(seed))
        return None

    async def _retire_after_handoff(
        self, old: discord.abc.GuildChannel, new: discord.Thread
    ) -> None:
        """Stop the outgoing session and point it at its replacement."""
        session = self.sessions.pop(old.id, None)
        if session is not None:
            try:
                await session.stop()
            except Exception as exc:
                log.warning("Error stopping handed-off session: %s", exc)
        if self.store.get(old.id) is not None:
            self.store.set_status(old.id, "stopped")
        try:
            await old.send(f"↪️ Continued in {new.mention}. This session is stopped.")
        except discord.HTTPException:
            pass
        if isinstance(old, discord.Thread):
            try:
                await old.edit(archived=True)
            except discord.HTTPException:
                pass

    async def do_context(self, channel: discord.abc.GuildChannel) -> None:
        """Post the live context breakdown for this thread's session."""
        session = self.sessions.get(channel.id)
        if session is None:
            await channel.send("No live session here. Start or resume one first.")
            return
        try:
            usage = await session.provider.context_usage()
        except Exception as exc:
            await channel.send(f"Could not read context usage: `{exc}`")
            return
        if not usage:
            await channel.send("This provider does not report context usage.")
            return
        await channel.send(embed=context_embed(usage))

    async def do_approvals(self, channel: discord.abc.Messageable) -> None:
        self._spawn(
            approvals_panel(self, channel, self.approvals, self._save_approvals)
        )

    def _save_approvals(self) -> None:
        self.approvals.save(self._approvals_path)

    async def do_view(self, channel: discord.abc.Messageable) -> None:
        self._spawn(view_panel(self, channel, self.prefs, self._on_pref_change))

    async def _on_pref_change(self, attr: str, value: bool) -> None:
        self.prefs.save(self._prefs_path)

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

    async def do_repos(self, channel: discord.abc.Messageable) -> None:
        base = os.path.expanduser(self.config.default_cwd or self.config.launch_cwd)

        def collect() -> list[tuple[str, str, list[tuple[str, str]]]]:
            out = []
            for path in list_repos(base):
                _, current = git_info(path)
                out.append((os.path.basename(path), current, list_worktrees(path)))
            return out

        repos = await asyncio.to_thread(collect)
        self._spawn(paginate(self, channel, repo_pages(repos)))

    async def do_list(self, channel: discord.abc.Messageable) -> None:
        """Resumable sessions across every provider, tagged by origin."""
        pins = [p for p in self.store.list_all() if p.session_id]
        pinned: dict[str, int] = {}
        stopped: set[str] = set()
        for p in pins:
            pinned[p.session_id] = p.channel_id
            if p.stopped:
                stopped.add(p.session_id)
        pages: list[discord.Embed] = []
        for name in PROVIDERS:
            try:
                sessions = await recent_sessions(name, 100)
            except Exception as exc:
                log.warning("Could not list %s sessions: %s", name, exc)
                continue
            if not sessions:
                continue
            pages.extend(session_pages(sessions, pinned, stopped))
        if not pages:
            pages = [discord.Embed(title="Resumable sessions", description="Nothing to show.", color=0x5865F2)]
        self._spawn(paginate(self, channel, pages))

    async def do_skills(self, channel: discord.abc.Messageable) -> None:
        cid = getattr(channel, "id", 0)
        session = self.sessions.get(cid)
        if session is not None:
            cmds = await session.provider.list_commands()
        else:
            # No live session here; show what the pending provider offers.
            name = self.pending_provider.get(
                getattr(getattr(channel, "guild", None), "id", 0), DEFAULT_PROVIDER
            )
            try:
                mod = provider_module(name)
                cmds = await mod.fetch_commands(
                    self.config.launch_cwd,
                    skills=self.config.skills,
                    model=self.config.provider_models.get(name),
                )
            except Exception as exc:
                await channel.send(f"Could not list skills for **{name}**: `{exc}`")
                return
        self._spawn(paginate(self, channel, command_pages(cmds)))

    def help_text(self) -> str:
        p = self.config.prefix
        rows = [
            ("new [repo:X] [branch:Y] [mode:Z] [provider:P]", "start a session; branch runs in a worktree; mode is provider-specific (claude: default/acceptEdits/auto/plan/bypassPermissions, opencode: default/acceptEdits/plan); thread opens under sessions-<provider>"),
            ("repos", "list repos under the base path with their branches and worktrees"),
            ("resume [id] [cwd]", "restart this thread's session; with an id, resume that one"),
            ("list", "show resumable sessions across providers"),
            ("skills", "list available skills"),
            ("skill <name> [args]", "run a skill in this session thread"),
            ("mode <name>", "switch permission mode (per provider, see new)"),
            ("context", "show how full this session's context window is, and what fills it"),
            ("handoff", "carry this session into a fresh thread with a brief, recent turns and repo state"),
            ("auto-approve", "pick which tools run without asking (⚡ approves everything)"),
            ("view", "toggle what shows (thinking, tool calls, tool results, tasks)"),
            ("provider <name>", "set provider for the next new (threads open under sessions-claude / sessions-opencode)"),
            ("login [name]", "log into an Anthropic account from here: open the link, paste back the code"),
            ("accounts", "list saved Anthropic accounts"),
            ("account <name>", "make <name> active for new sessions"),
            ("logout <name>", "forget a saved account"),
            ("interrupt", "stop the current turn"),
            ("stop [forget]", "end this session and archive the thread; `forget` drops the binding"),
        ]
        lines = [f"Commands (prefix `{p}`):"]
        lines += [f"`{p}{cmd}` {desc}" for cmd, desc in rows]
        return "\n".join(lines)

    # ---- message forwarding ---------------------------------------------

    async def on_command_error(self, ctx: commands.Context, error: Exception) -> None:
        """Override the default handler so errors land in chat, not stderr."""
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.channel.send(f"Missing argument: {error.param.name}")
            return
        await ctx.channel.send(f"Command error: `{error}`")

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        if not self._is_server_owner(message.author.id, message.guild):
            return
        # A login code reply is consumed by the login flow, not a turn --
        # unless it is a command, so `cancel-login` stays reachable.
        if (
            self.login.waiting_channel == message.channel.id
            and not message.content.startswith(self.config.prefix)
        ):
            result = await self.login.submit(message.content)
            await message.channel.send(result)
            return
        # A pending "Other" answer is captured by the broker, not a new turn.
        if message.channel.id in self.broker.awaiting_text:
            return
        content = message.content
        if content.startswith(self.config.prefix):
            await self.process_commands(message)
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

        note = ""
        if message.attachments:
            saved = await collect_attachments(message)
            note = compose_note(saved)
        text = "\n\n".join(part for part in (content.strip(), note) if part)
        if not text.strip():
            return
        await session.handle_message(text)


# ---------------------------------------------------------------------------
# Chat (prefix) commands
# ---------------------------------------------------------------------------


def register_chat(bot: RemoteAgentBot) -> None:
    @bot.command(name="new")
    async def new_cmd(ctx: commands.Context, *, rest: str = "") -> None:
        repo, branch, mode, provider, err = _parse_new_args(rest)
        if err:
            await ctx.channel.send(err)
            return
        await ctx.channel.send(
            await bot.do_new(ctx.guild, repo, mode, branch, provider)
        )

    @bot.command(name="repos")
    async def repos_cmd(ctx: commands.Context) -> None:
        await bot.do_repos(ctx.channel)

    @bot.command(name="resume")
    async def resume_cmd(
        ctx: commands.Context, session_id: str | None = None, cwd: str | None = None
    ) -> None:
        # No id means "restart whatever this thread was bound to", which is
        # how a thread that was stopped comes back.
        await ctx.channel.send(
            await bot.do_resume(ctx.guild, session_id, cwd, ctx.channel)
        )

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

    @bot.command(name="handoff")
    async def handoff_cmd(ctx: commands.Context) -> None:
        error = await bot.do_handoff(ctx.channel)
        if error:
            await ctx.channel.send(error)

    @bot.command(name="context")
    async def context_cmd(ctx: commands.Context) -> None:
        await bot.do_context(ctx.channel)

    @bot.command(name="auto-approve", aliases=["auto", "approvals"])
    async def approvals_cmd(ctx: commands.Context) -> None:
        await bot.do_approvals(ctx.channel)

    @bot.command(name="view")
    async def view_cmd(ctx: commands.Context) -> None:
        await bot.do_view(ctx.channel)

    @bot.command(name="interrupt")
    async def interrupt_cmd(ctx: commands.Context) -> None:
        await ctx.channel.send(await bot.do_interrupt(ctx.channel))

    @bot.command(name="stop")
    async def stop_cmd(ctx: commands.Context, *, rest: str = "") -> None:
        forget = rest.strip().lower() in ("forget", "--forget")
        await ctx.channel.send(await bot.do_stop(ctx.channel, forget=forget))

    @bot.command(name="login", aliases=["signin"])
    async def login_cmd(ctx: commands.Context, name: str | None = None) -> None:
        """Start a Claude subscription login; paste back the code it gives."""
        if ctx.guild is None or not bot._is_server_owner(ctx.author.id, ctx.guild):
            return
        account = (name or f"account-{len(bot.auth.accounts) + 1}").strip()
        try:
            url = await bot.login.start(ctx.channel.id, account)
        except RuntimeError as exc:
            await ctx.channel.send(f"Login failed to start: `{exc}`")
            return
        await ctx.channel.send(
            f"**Logging in as `{account}`**\n"
            f"1. Open this link and approve access: {url}\n"
            "2. Copy the code it shows you and paste it here."
        )

    @bot.command(name="cancel-login")
    async def cancel_login_cmd(ctx: commands.Context) -> None:
        await bot.login.cancel()
        await ctx.channel.send("Login cancelled.")

    @bot.command(name="accounts")
    async def accounts_cmd(ctx: commands.Context) -> None:
        auth = bot.auth
        if not auth.accounts:
            await ctx.channel.send("No saved accounts. Use `login [name]` first.")
            return
        lines = [
            ("👉 " if name == auth.active else "   ") + f"`{name}`"
            for name in sorted(auth.accounts)
        ]
        await ctx.channel.send(
            "**Anthropic accounts** (👉 active for new sessions)\n" + "\n".join(lines)
        )

    @bot.command(name="account")
    async def account_cmd(ctx: commands.Context, name: str) -> None:
        if bot.auth.switch(name):
            await ctx.channel.send(f"New sessions will use **{name}**.")
        else:
            known = ", ".join(sorted(bot.auth.accounts)) or "none saved"
            await ctx.channel.send(f"No account named `{name}`. Known: {known}.")

    @bot.command(name="logout")
    async def logout_cmd(ctx: commands.Context, name: str) -> None:
        if bot.auth.forget(name):
            await ctx.channel.send(f"Forgot **{name}**. Running sessions are unaffected.")
        else:
            await ctx.channel.send(f"No account named `{name}`.")

    @bot.command(name="help")
    async def help_cmd(ctx: commands.Context) -> None:
        await ctx.channel.send(bot.help_text())


def run(config: Config) -> None:
    logging.basicConfig(level=logging.INFO)
    bot = RemoteAgentBot(config)
    bot.run(config.token, log_handler=None)
