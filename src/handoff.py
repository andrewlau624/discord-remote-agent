"""Carrying a session's thread of work into a fresh context window.

A handoff ends one session and starts another that knows what the first was
doing. Three things travel, because each covers what the others miss:

- the brief, written by the outgoing session itself -- it still holds the full
  context, so it is the best summary obtainable and needs no second model;
- the recent exchange, verbatim, so immediate detail survives paraphrase;
- the working state, read from disk, so the new session is grounded in what the
  repo actually looks like rather than what the model recalls.

Nothing here talks to Discord; composing the seed is kept separate from the
thread choreography in bot.py so it can be tested on its own.
"""

from __future__ import annotations

import asyncio
import os
import subprocess

#: Asked of the outgoing session as its final turn.
BRIEF_PROMPT = (
    "You are about to hand this session off to a fresh one with an empty "
    "context window. Write a handoff brief for the agent taking over. Cover: "
    "the goal we are working toward, what has been done so far, decisions made "
    "and why (especially ones that would be easy to reverse by accident), "
    "files changed and what changed in them, anything known to be broken or "
    "unverified, and the exact next step. Be specific -- name files, functions "
    "and commands. Write only the brief, with no preamble."
)

#: How many recent exchanges to carry verbatim. Enough to preserve the thread
#: of the current task without spending much of the fresh window.
_RECENT_TURNS = 6

_MAX_BRIEF = 6000
_MAX_MESSAGE = 1200
_MAX_DIFFSTAT = 2000


def _run_git(cwd: str, args: list[str], timeout: int = 10) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, text=True, timeout=timeout
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def working_state(cwd: str) -> str:
    """Where we are and what is uncommitted, read straight from the repo."""
    lines = [f"Working directory: {cwd}"]
    branch = _run_git(cwd, ["rev-parse", "--abbrev-ref", "HEAD"])
    if branch:
        lines.append(f"Branch: {branch}")
    head = _run_git(cwd, ["log", "-1", "--oneline"])
    if head:
        lines.append(f"HEAD: {head}")
    stat = _run_git(cwd, ["diff", "--stat", "HEAD"])
    if stat:
        lines.append("Uncommitted changes vs HEAD:\n" + stat[:_MAX_DIFFSTAT])
    else:
        lines.append("Uncommitted changes vs HEAD: none")
    untracked = _run_git(cwd, ["ls-files", "--others", "--exclude-standard"])
    if untracked:
        names = untracked.splitlines()
        shown = ", ".join(names[:10])
        if len(names) > 10:
            shown += f", …and {len(names) - 10} more"
        lines.append(f"Untracked: {shown}")
    return "\n".join(lines)


async def working_state_async(cwd: str) -> str:
    """`working_state` off the event loop -- it shells out to git."""
    if not os.path.isdir(cwd):
        return f"Working directory: {cwd} (missing)"
    return await asyncio.to_thread(working_state, cwd)


def recent_exchange(history: list[tuple[str, str]], limit: int = _RECENT_TURNS) -> str:
    """The last few turns, verbatim.

    `history` comes from `textual_history`, which has already dropped tool
    calls, tool results and thinking -- only what was actually said survives,
    which is the part worth quoting.
    """
    tail = [(role, text) for role, text in history if text.strip()][-limit:]
    if not tail:
        return ""
    out = []
    for role, text in tail:
        speaker = "User" if role == "user" else "Assistant"
        body = " ".join(text.split())
        if len(body) > _MAX_MESSAGE:
            body = body[:_MAX_MESSAGE] + " …[truncated]"
        out.append(f"**{speaker}:** {body}")
    return "\n\n".join(out)


def compose_seed(
    brief: str,
    history: list[tuple[str, str]],
    state: str,
    *,
    old_session_id: str | None = None,
) -> str:
    """Build the first message of the new session."""
    parts = [
        "This session continues earlier work that ran out of context. "
        "Below is everything carried over. Read it, then continue from the "
        "next step described in the brief. Do not restate this back to me.",
    ]
    brief = (brief or "").strip()
    if brief:
        parts.append("## Handoff brief\n\n" + brief[:_MAX_BRIEF])
    recent = recent_exchange(history)
    if recent:
        parts.append("## Recent exchange\n\n" + recent)
    if state.strip():
        parts.append("## Working state\n\n```\n" + state.strip() + "\n```")
    if old_session_id:
        parts.append(f"_Previous session: `{old_session_id}`_")
    return "\n\n".join(parts)
