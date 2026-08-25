"""The sessions forums: one thread per agent session, grouped by provider.

Each provider gets its own forum channel -- `sessions-claude`, `sessions-opencode`
-- so threads never mix agents and the forum name doubles as a picker. The
starter post carries the repo, branch, working directory, provider and session
id.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import discord


def forum_name(provider: str) -> str:
    """Forum channel for one provider's session threads."""
    return f"sessions-{provider}"


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


def resolve_worktree(repo_dir: str, branch: str) -> tuple[str, list[str]]:
    """Worktree path for a branch: reuse an existing checkout, otherwise
    create <repo>/.worktrees/<branch>. The branch is created if it does not
    exist. Gitignored .env* files are carried over from the main checkout.
    Returns (path, copied env files). Raises RuntimeError on failure."""
    for path, wt_branch in list_worktrees(repo_dir):
        if wt_branch == branch:
            return path, _copy_envs(repo_dir, path)
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
    return path, _copy_envs(repo_dir, path)


def _copy_envs(repo_dir: str, worktree_dir: str) -> list[str]:
    """Copy gitignored .env* files from the main checkout into a worktree.

    Worktrees start clean, so local-only env files do not come along on their
    own. Existing files in the worktree are never overwritten. Returns the
    repo-relative paths copied."""
    if os.path.realpath(worktree_dir) == os.path.realpath(repo_dir):
        return []
    try:
        out = _run_git(
            repo_dir, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory"
        )
    except (OSError, subprocess.SubprocessError):
        return []
    copied: list[str] = []
    for rel in out.stdout.splitlines():
        rel = rel.strip()
        # Ignored directories are collapsed to one "dir/" entry, which skips
        # things like node_modules without walking them.
        if not rel or rel.endswith("/"):
            continue
        if not os.path.basename(rel).startswith(".env"):
            continue
        src = os.path.join(repo_dir, rel)
        dst = os.path.join(worktree_dir, rel)
        if not os.path.isfile(src) or os.path.exists(dst):
            continue
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
        except OSError:
            continue
        copied.append(rel)
    return copied


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


async def ensure_forum(guild: discord.Guild, provider: str = "claude") -> discord.ForumChannel:
    """Find the provider's sessions forum, creating it if needed."""
    name = forum_name(provider)
    for channel in guild.channels:
        if isinstance(channel, discord.ForumChannel) and channel.name == name:
            return channel
    return await guild.create_forum(
        name=name, reason="discord-remote-agent session threads"
    )


def _session_embed(
    title: str, session_id: str, cwd: str, provider: str = "claude"
) -> discord.Embed:
    repo, branch = git_info(cwd)
    embed = discord.Embed(title=title[:256], color=0x5865F2)
    embed.add_field(name="provider", value=provider, inline=True)
    embed.add_field(name="repo", value=repo, inline=True)
    embed.add_field(name="branch", value=branch, inline=True)
    embed.add_field(name="cwd", value=f"`{cwd}`", inline=False)
    embed.add_field(name="session", value=f"`{session_id}`", inline=False)
    return embed


async def create_session_thread(
    forum: discord.ForumChannel,
    name: str,
    session_id: str,
    cwd: str,
    provider: str = "claude",
) -> discord.Thread:
    """Create a forum post for a session and return its thread."""
    result = await forum.create_thread(
        name=(name or "session")[:100],
        embed=_session_embed(name or "session", session_id, cwd, provider),
        reason="new agent session",
    )
    return result.thread
