"""Render normalized Blocks into Discord embeds (+ file attachments).

Discord limits we respect: embed description <= 4096 chars, message content
<= 2000. Long tool output is truncated in the embed and attached in full as a
.txt file so nothing is silently lost.
"""

from __future__ import annotations

import io

import discord

from dra.providers.base import Block, BlockKind

# description budget, leaving room for code fences / ellipsis
_MAX_DESC = 3800
_FILE_THRESHOLD = 3800

_STYLE = {
    BlockKind.THINKING: ("\U0001f4ad Thinking", 0x95A5A6),      # grey
    BlockKind.TEXT: (None, 0x5865F2),                            # blurple
    BlockKind.TOOL_CALL: ("\U0001f527 Tool", 0x3498DB),          # blue
    BlockKind.TOOL_RESULT: ("✅ Result", 0x2ECC71),          # green
    BlockKind.STATUS: ("ℹ️", 0x2B2D31),                # dark
    BlockKind.ERROR: ("❌ Error", 0xE74C3C),                 # red
}

# Tools whose bodies read better as a shell/code block.
_CODE_TOOLS = {"Bash"}


def _fence(text: str, lang: str = "") -> str:
    return f"```{lang}\n{text}\n```"


def render_block(block: Block) -> tuple[list[discord.Embed], list[discord.File]]:
    """Return (embeds, files) for one block."""
    default_title, color = _STYLE[block.kind]
    if block.kind == BlockKind.TOOL_RESULT and block.is_error:
        color = _STYLE[BlockKind.ERROR][1]

    title = block.title or default_title
    body = block.body or ""
    files: list[discord.File] = []

    # Decide whether to code-fence the body.
    fence_lang = ""
    use_fence = False
    if block.kind == BlockKind.TOOL_CALL:
        use_fence = True
        fence_lang = "bash" if block.meta.get("tool") in _CODE_TOOLS else ""
    elif block.kind == BlockKind.TOOL_RESULT:
        use_fence = True

    truncated = False
    if len(body) > _FILE_THRESHOLD and block.kind in (
        BlockKind.TOOL_RESULT,
        BlockKind.TOOL_CALL,
    ):
        # Attach full body; show a head in the embed.
        buf = io.BytesIO(body.encode("utf-8"))
        files.append(discord.File(buf, filename=f"{block.kind.value}.txt"))
        body = body[:_MAX_DESC]
        truncated = True

    if use_fence:
        description = _fence(body if body else "(empty)", fence_lang)
    else:
        description = body if body else "(empty)"

    if len(description) > 4096:
        description = description[: 4096 - 4] + "\n..."

    embed = discord.Embed(description=description, color=color)
    if title:
        embed.title = title[:256]
    if truncated:
        embed.set_footer(text="output truncated, full content attached")

    return [embed], files


def permission_embed(tool_name: str, tool_input: dict) -> discord.Embed:
    """Embed shown alongside Approve/Deny buttons."""
    if tool_name == "Bash":
        body = _fence(str(tool_input.get("command", "")), "bash")
    elif tool_name in ("Write", "Edit", "NotebookEdit"):
        path = tool_input.get("file_path", "")
        body = f"**{path}**"
        if tool_name == "Write" and "content" in tool_input:
            preview = str(tool_input["content"])[:1500]
            body += "\n" + _fence(preview)
    else:
        import json

        try:
            body = _fence(json.dumps(tool_input, ensure_ascii=False, indent=2)[:1500])
        except (TypeError, ValueError):
            body = _fence(str(tool_input)[:1500])

    if len(body) > 4096:
        body = body[: 4096 - 4] + "\n..."

    embed = discord.Embed(
        title=f"\U0001f6d1 Approve tool: {tool_name}",
        description=body,
        color=0xF1C40F,  # amber
    )
    embed.set_footer(text="Only the owner can decide.")
    return embed
