"""Turning Discord attachments into something the agent can actually read.

Text-ish files under a size floor are inlined straight into the prompt so the
agent sees their contents without spending a tool call. Everything else --
images, PDFs, big files -- is saved to disk and named by absolute path; both
Claude Code and opencode can open image paths with their Read tool. The file
outlives the message on purpose: an agent may only get around to reading it
several turns later.
"""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

import discord

#: Text files at or under this many characters are pasted into the prompt;
#: larger ones are referenced by path instead.
_INLINE_LIMIT = 24_000

#: Discord itself caps uploads near 25 MiB; anything bigger cannot arrive.
_MAX_BYTES = 24 * 1024 * 1024

_TEXT_EXTS = {
    ".bash", ".c", ".cfg", ".conf", ".cpp", ".css", ".csv", ".env", ".go",
    ".h", ".hpp", ".html", ".ini", ".java", ".js", ".json", ".jsx", ".kt",
    ".log", ".md", ".php", ".pl", ".properties", ".py", ".rb", ".rs", ".sh",
    ".sql", ".svg", ".toml", ".ts", ".tsx", ".txt", ".xml", ".yaml", ".yml",
    ".zsh",
}

_TEXT_MIMES = ("text/", "application/json", "application/xml", "application/yaml")


def _is_text(attachment: discord.Attachment) -> bool:
    ctype = (attachment.content_type or "").lower()
    if any(ctype.startswith(p) for p in _TEXT_MIMES):
        return True
    return Path(attachment.filename).suffix.lower() in _TEXT_EXTS


@dataclass
class Attachment:
    """One saved upload: where it lives and, if small enough, its text."""

    name: str
    path: Path | None
    inline: str | None = None
    skipped: str | None = None


async def collect(message: discord.Message, base: Path | None = None) -> list[Attachment]:
    """Download a message's attachments, inlining small text ones."""
    if not message.attachments:
        return []
    folder = (base or Path(tempfile.gettempdir())) / (
        f"discord-agent-files/{message.channel.id}"
    )
    out: list[Attachment] = []
    for att in message.attachments:
        name = Path(att.filename).name or "file"
        if att.size > _MAX_BYTES:
            out.append(Attachment(name=name, path=None, skipped="too large"))
            continue
        try:
            data = await att.read()
        except (discord.HTTPException, asyncio.TimeoutError):
            out.append(Attachment(name=name, path=None, skipped="download failed"))
            continue
        dest = folder / f"{uuid.uuid4().hex[:8]}-{name}"
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(dest.write_bytes, data)
        except OSError:
            out.append(Attachment(name=name, path=None, skipped="could not save"))
            continue
        item = Attachment(name=name, path=dest)
        text = _decoded_text(data) if _is_text(att) else None
        if text is not None and len(text) <= _INLINE_LIMIT:
            item.inline = text
        out.append(item)
    return out


def _decoded_text(data: bytes) -> str | None:
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, ValueError):
            continue
    return None


def compose_note(attachments: list[Attachment]) -> str:
    """The prompt suffix describing what came with this message."""
    parts: list[str] = []
    for att in attachments:
        if att.skipped:
            parts.append(f"[attached file: {att.name} — not available ({att.skipped})]")
        elif att.inline is not None:
            body = att.inline.rstrip("\n")
            if len(body) > _INLINE_LIMIT:
                body = body[:_INLINE_LIMIT] + "\n…[truncated]"
            safe = body.replace("```", "`\u200b`\u200b`")
            parts.append(f"[attached file: {att.name}]\n```\n{safe}\n```")
        elif att.path is not None:
            parts.append(
                f"[attached file: {att.name}] saved at `{att.path}` — "
                "read it from that path to see it."
            )
    if not parts:
        return ""
    header = (
        f"The user attached {len(parts)} file(s):" if len(parts) > 1
        else "The user attached a file:"
    )
    return header + "\n" + "\n\n".join(parts)
