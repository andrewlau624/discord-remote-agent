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


def _run_git(cwd: str, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", cwd, *args], capture_output=True, text=True, timeout=timeout
    )


def list_repos(base: str) -> list[str]:
    """Directories directly under base that are git repos."""
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    return [
        os.path.join(base, name)
        for name in names
        if os.path.exists(os.path.join(base, name, ".git"))
    ]


def list_worktrees(repo_dir: str) -> list[tuple[str, str]]:
    """(path, branch) for each checkout of a repo, main checkout first."""
    try:
        out = _run_git(repo_dir, "worktree", "list", "--porcelain")
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    entries: list[tuple[str, str]] = []
    path, branch = "", ""
    for line in [*out.stdout.splitlines(), ""]:
        if line.startswith("worktree "):
            path = line[len("worktree ") :]
        elif line.startswith("branch refs/heads/"):
            branch = line[len("branch refs/heads/") :]
        elif line.startswith("detached"):
            branch = "(detached)"
        elif not line and path:
            entries.append((path, branch or "?"))
            path, branch = "", ""
    return entries


def resolve_worktree(repo_dir: str, branch: str) -> str:
    """Worktree path for a branch: reuse an existing checkout, otherwise
    create <repo>/.worktrees/<branch>. The branch is created if it does not
    exist. Raises RuntimeError with git's message on failure."""
    for path, wt_branch in list_worktrees(repo_dir):
        if wt_branch == branch:
            return path
    path = os.path.join(repo_dir, ".worktrees", branch.replace("/", "-"))
    _ensure_excluded(repo_dir, ".worktrees/")
    try:
        out = _run_git(repo_dir, "worktree", "add", path, branch, timeout=60)
        if out.returncode != 0:
            out = _run_git(repo_dir, "worktree", "add", "-b", branch, path, timeout=60)
        if out.returncode != 0:
            raise RuntimeError(out.stderr.strip().splitlines()[-1] if out.stderr.strip() else "git worktree add failed")
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(str(exc)) from exc
    return path


def _ensure_excluded(repo_dir: str, pattern: str) -> None:
    """Add a pattern to .git/info/exclude so worktrees don't show as untracked."""
    try:
        git_dir = _run_git(repo_dir, "rev-parse", "--git-common-dir").stdout.strip()
        if not git_dir:
            return
        if not os.path.isabs(git_dir):
            git_dir = os.path.join(repo_dir, git_dir)
        exclude = os.path.join(git_dir, "info", "exclude")
        os.makedirs(os.path.dirname(exclude), exist_ok=True)
        existing = ""
        if os.path.exists(exclude):
            with open(exclude, encoding="utf-8") as fh:
                existing = fh.read()
        if pattern not in existing.split("\n"):
            with open(exclude, "a", encoding="utf-8") as fh:
                prefix = "" if not existing or existing.endswith("\n") else "\n"
                fh.write(f"{prefix}{pattern}\n")
    except (OSError, subprocess.SubprocessError):
        pass


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
