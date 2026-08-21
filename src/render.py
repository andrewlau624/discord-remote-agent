"""Render Blocks into Discord embeds, chunking long bodies across messages.

Discord caps embed descriptions at 4096 chars. Long output is split into several
embeds; anything past a few chunks is attached in full as a .txt file so nothing
is lost.
"""

from __future__ import annotations

import io
import json

import discord

from src.providers.base import Block, BlockKind, TaskStatus

_CHUNK = 3800  # leave room for code fences under the 4096 limit
_CONTENT_LIMIT = 2000  # Discord message content cap
_MAX_EMBEDS = 6  # beyond this, attach the rest as a file

_STYLE = {
    BlockKind.THINKING: ("\U0001f4ad Thinking", 0x95A5A6),
    BlockKind.TEXT: (None, 0x5865F2),
    BlockKind.TOOL_CALL: ("\U0001f527 Tool", 0x3498DB),
    BlockKind.TOOL_RESULT: ("✅ Result", 0x2ECC71),
    BlockKind.STATUS: ("ℹ️", 0x2B2D31),
    BlockKind.ERROR: ("❌ Error", 0xE74C3C),
    BlockKind.TASK: ("\U0001f916 Task", 0x9B59B6),
}

_TASK_STYLE = {
    TaskStatus.RUNNING: ("\U0001f916", 0x9B59B6),
    TaskStatus.DONE: ("✅", 0x2ECC71),
    TaskStatus.FAILED: ("❌", 0xE74C3C),
}

_CODE_TOOLS = {"Bash"}


def _fence(text: str, lang: str = "") -> str:
    # Break up any triple backticks so they can't close the fence early.
    safe = text.replace("```", "`​`​`")
    return f"```{lang}\n{safe}\n```"


def _question_md(tool_input: dict) -> str:
    """Render an AskUserQuestion input as readable markdown instead of raw JSON."""
    out: list[str] = []
    for q in tool_input.get("questions") or []:
        header = str(q.get("header", "")).strip()
        question = str(q.get("question", "")).strip()
        out.append(f"**{header}**  {question}" if header else f"**{question}**")
        for opt in q.get("options") or []:
            label = str(opt.get("label", "")).strip()
            desc = " ".join(str(opt.get("description", "")).split())
            out.append(f"• **{label}**: {desc}" if desc else f"• **{label}**")
        if q.get("multiSelect"):
            out.append("_pick any_")
        out.append("")
    return "\n".join(out).strip()


def ask_question_text(question: dict) -> str:
    """One AskUserQuestion question as a big, readable message.

    A markdown heading makes it easy to see, and the option descriptions ride
    along here since the poll buttons only carry short labels.
    """
    header = str(question.get("header", "")).strip()
    qtext = str(question.get("question", "")).strip()
    lines: list[str] = [f"## {header or qtext or 'Question'}"]
    if header and qtext:
        lines.append(qtext)
    for opt in question.get("options") or []:
        label = str(opt.get("label", "")).strip()
        desc = " ".join(str(opt.get("description", "")).split())
        lines.append(f"• **{label}** {desc}" if desc else f"• **{label}**")
    lines.append("• **Other** type your own answer")
    return "\n".join(lines)[:2000]


def _chunks(text: str, size: int) -> list[str]:
    if not text:
        return [""]
    return [text[i : i + size] for i in range(0, len(text), size)]


def render_block(block: Block) -> list[dict]:
    """Return a list of `channel.send(**kwargs)` dicts for one block.

    Assistant text goes out as plain messages so it reads at normal chat size
    with markdown intact. Other kinds render as colored embeds, and a very long
    body is attached as a file so nothing is lost.
    """
    if block.kind == BlockKind.TEXT:
        body = (block.body or "").strip()
        if not body:
            return []
        return [{"content": part} for part in _chunks(body, _CONTENT_LIMIT)]

    default_title, color = _STYLE[block.kind]
    if block.kind == BlockKind.TOOL_RESULT and block.is_error:
        color = _STYLE[BlockKind.ERROR][1]
    title = block.title or default_title
    body = block.body or ""

    fenced = block.kind in (BlockKind.TOOL_CALL, BlockKind.TOOL_RESULT)
    lang = "bash" if block.meta.get("tool") in _CODE_TOOLS else ""

    files: list[discord.File] = []
    parts = _chunks(body, _CHUNK)
    truncated = False
    if len(parts) > _MAX_EMBEDS:
        files.append(
            discord.File(io.BytesIO(body.encode("utf-8")), filename=f"{block.kind.value}.txt")
        )
        parts = parts[:_MAX_EMBEDS]
        truncated = True

    embeds: list[discord.Embed] = []
    for i, part in enumerate(parts):
        text = part if part else "(empty)"
        description = _fence(text, lang) if fenced else text
        if len(description) > 4096:
            description = description[: 4096 - 4] + "\n..."
        embed = discord.Embed(description=description, color=color)
        if i == 0 and title:
            embed.title = title[:256]
        embeds.append(embed)

    if truncated and embeds:
        embeds[-1].set_footer(text="output truncated, full content attached")

    msgs: list[dict] = [{"embed": e} for e in embeds]
    if files:
        if msgs:
            msgs[-1]["files"] = files
        else:
            msgs.append({"files": files})
    return msgs


def task_embed(block: Block) -> discord.Embed:
    """One embed for a delegated task's live status message.

    Returns a single embed (not a list of sends) because TaskBoard edits one
    message in place for the life of the task rather than posting per update.
    """
    status = block.task_status or TaskStatus.RUNNING
    emoji, color = _TASK_STYLE[status]
    title = f"{emoji} {(block.title or 'Task').strip()}"
    body = (block.body or "").strip()
    if status is TaskStatus.RUNNING:
        body = f"{body}\n_running…_" if body else "_running…_"
    return discord.Embed(title=title[:256], description=body[:4096], color=color)


_HISTORY_COLOR = 0x4E5058
_HISTORY_MAX_EMBEDS = 30


def history_embeds(history: list[tuple[str, str]]) -> list[discord.Embed]:
    """Pack prior (role, text) turns into embeds, newest kept if it overflows."""
    if not history:
        return []
    embeds: list[discord.Embed] = []
    buf = ""

    def flush() -> None:
        nonlocal buf
        if buf.strip():
            embeds.append(discord.Embed(description=buf.strip()[:4096], color=_HISTORY_COLOR))
        buf = ""

    for role, text in history:
        label = "You" if role == "user" else "Claude"
        block = f"**{label}:** {text}"
        for part in _chunks(block, _CHUNK):
            if len(buf) + len(part) + 2 > _CHUNK:
                flush()
            buf += part + "\n\n"
    flush()

    trimmed = len(embeds) > _HISTORY_MAX_EMBEDS
    if trimmed:
        embeds = embeds[-_HISTORY_MAX_EMBEDS:]
    embeds[0].title = "Session history" + (" (earlier turns omitted)" if trimmed else "")
    return embeds


def permission_embed(tool_name: str, tool_input: dict) -> discord.Embed:
    if tool_name == "Bash":
        body = _fence(str(tool_input.get("command", "")), "bash")
    elif tool_name == "AskUserQuestion":
        body = _question_md(tool_input) or "(no questions)"
    elif tool_name in ("Write", "Edit", "NotebookEdit"):
        path = tool_input.get("file_path", "")
        body = f"**{path}**"
        if tool_name == "Write" and "content" in tool_input:
            body += "\n" + _fence(str(tool_input["content"])[:1500])
    else:
        try:
            body = _fence(json.dumps(tool_input, ensure_ascii=False, indent=2)[:1500])
        except (TypeError, ValueError):
            body = _fence(str(tool_input)[:1500])

    if len(body) > 4096:
        body = body[: 4096 - 4] + "\n..."

    embed = discord.Embed(
        title=f"\U0001f6d1 Tool request: {tool_name}", description=body, color=0xF1C40F
    )
    return embed


def handoff_embed(brief: str, source: object = None) -> discord.Embed:
    """The carried-over brief, shown in the new thread as a record.

    The agent receives the full brief in its seed message; this is for you, so
    it is truncated to whatever an embed will hold rather than split.
    """
    body = (brief or "").strip()
    if len(body) > 4000:
        body = body[:4000] + "\n\n…truncated; the new session received it in full."
    embed = discord.Embed(
        title="↪️ Carried over from the previous session",
        description=body or "_No brief was produced._",
        color=0x9B59B6,
    )
    mention = getattr(source, "mention", None)
    if mention:
        embed.set_footer(text="Continued from a session that ran out of context.")
        embed.add_field(name="Previous thread", value=mention, inline=False)
    return embed


def usage_bar(pct: float, width: int = 20) -> str:
    """A fixed-width meter, so successive readings line up when scrolling."""
    filled = max(0, min(width, round(pct / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def context_color(pct: float) -> int:
    if pct >= 90:
        return 0xE74C3C
    if pct >= 75:
        return 0xE67E22
    return 0x2ECC71


def context_embed(usage: dict) -> discord.Embed:
    """The `/context` breakdown: how full the window is and what is filling it."""
    total = int(usage.get("totalTokens") or 0)
    limit = int(usage.get("maxTokens") or 0)
    raw = int(usage.get("rawMaxTokens") or 0)
    pct = float(usage.get("percentage") or 0.0)

    embed = discord.Embed(
        title="Context window",
        description=f"`{usage_bar(pct)}` **{pct:.1f}%**\n{total:,} / {limit:,} tokens",
        color=context_color(pct),
    )

    # Biggest consumers first -- that is what you act on.
    cats = [c for c in usage.get("categories") or [] if (c.get("tokens") or 0) > 0]
    if cats:
        lines = [
            f"`{(c.get('tokens') or 0):>8,}`  {c.get('name', '?')}"
            + ("  _(deferred)_" if c.get("isDeferred") else "")
            for c in sorted(cats, key=lambda c: c.get("tokens") or 0, reverse=True)
        ]
        embed.add_field(name="Breakdown", value="\n".join(lines)[:1024], inline=False)

    files = usage.get("memoryFiles") or []
    if files:
        lines = [
            f"`{(f.get('tokens') or 0):>6,}`  {f.get('path', '?')}" for f in files[:10]
        ]
        if len(files) > 10:
            lines.append(f"…and {len(files) - 10} more")
        embed.add_field(name="Memory files", value="\n".join(lines)[:1024], inline=False)

    foot = [str(usage.get("model") or "unknown model")]
    if raw and limit and raw != limit:
        # The gap is the autocompact buffer, which explains a limit that looks
        # smaller than the model's advertised window.
        foot.append(f"window {raw:,}, {raw - limit:,} reserved for autocompact")
    foot.append("autocompact on" if usage.get("isAutoCompactEnabled") else "autocompact off")
    embed.set_footer(text=" · ".join(foot))
    return embed


def _field_pages(
    items: list[tuple[str, str]], title: str, per_page: int = 8
) -> list[discord.Embed]:
    """One field per item so entries render with clear separation."""
    if not items:
        return [discord.Embed(title=title, description="Nothing to show.", color=0x5865F2)]
    pages: list[discord.Embed] = []
    for i in range(0, len(items), per_page):
        embed = discord.Embed(title=title, color=0x5865F2)
        for name, value in items[i : i + per_page]:
            embed.add_field(name=name[:256], value=(value or "​")[:1024], inline=False)
        pages.append(embed)
    return pages


def command_pages(commands: list[dict]) -> list[discord.Embed]:
    """Paginated skill/command list."""
    items: list[tuple[str, str]] = []
    for cmd in sorted(commands, key=lambda c: str(c.get("name", ""))):
        name = str(cmd.get("name", "")).strip()
        if not name:
            continue
        desc = " ".join(str(cmd.get("description", "")).split())
        if len(desc) > 160:
            desc = desc[:157] + "..."
        items.append((f"/{name}", desc or "​"))
    return _field_pages(items, f"Skills ({len(items)})")


def repo_pages(repos: list[tuple[str, str, list[tuple[str, str]]]]) -> list[discord.Embed]:
    """Paginated repos with their checkouts.

    Each item is (name, branch, worktrees) where worktrees is (path, branch)
    with the main checkout first."""
    items: list[tuple[str, str]] = []
    for name, branch, trees in repos:
        lines = [f"`{b}` {p}" for p, b in trees]
        items.append((f"📁 {name} ({branch})", "\n".join(lines) or "no checkouts"))
    return _field_pages(items, f"Repos ({len(items)})", per_page=6)


def session_pages(
    sessions: list,
    pinned: dict[str, int],
    stopped: set[str] | None = None,
) -> list[discord.Embed]:
    """Paginated resumable sessions.

    `pinned` maps session_id -> channel_id for every thread-bound session;
    `stopped` is the subset whose thread is bound but shut down. Those keep a
    distinct mark, since a stopped session still has a thread to restart in
    and should not look identical to a live one.
    """
    stopped = stopped or set()
    items: list[tuple[str, str]] = []
    for s in sessions:
        sid = getattr(s, "session_id", "")
        title = getattr(s, "custom_title", None) or getattr(s, "summary", None) or "untitled"
        title = " ".join(str(title).split())[:80]
        cwd = getattr(s, "cwd", "") or ""
        if sid in stopped:
            mark = "⏸️"
        elif sid in pinned:
            mark = "📌"
        else:
            mark = "▫️"
        items.append((f"{mark} {title}", f"`{sid}`\n{cwd}"))
    return _field_pages(items, f"Resumable sessions ({len(items)})")
