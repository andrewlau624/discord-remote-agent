"""Claude Code provider built on the Claude Agent SDK.

Holds one connected ClaudeSDKClient per channel. Each message is a turn: query
then drain the response, mapping SDK blocks to our Block type. Tools not in
allowed_tools hit can_use_tool, which we send to the broker for approval.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    get_session_info,
    get_session_messages,
    list_sessions,
)

from src.providers.base import Block, BlockKind, PermissionBroker, Provider

_SETTING_SOURCES = ["user", "project", "local"]


def session_cwd(session_id: str) -> str | None:
    """Resolve a session's working directory from Claude's own session data."""
    info = get_session_info(session_id)
    return info.cwd if info else None


def recent_sessions(limit: int = 100) -> list:
    """All resumable sessions, newest first, across every project."""
    return list_sessions(limit=limit)


def session_title(session_id: str) -> str | None:
    """The agent-generated title/summary for a session, if any."""
    info = get_session_info(session_id)
    if not info:
        return None
    return info.custom_title or info.summary or None


def textual_history(session_id: str) -> list[tuple[str, str]]:
    """Prior conversation as (role, text), skipping tool calls and results."""
    out: list[tuple[str, str]] = []
    for m in get_session_messages(session_id):
        message = getattr(m, "message", None)
        if not isinstance(message, dict):
            continue
        role = message.get("role", "")
        content = message.get("content")
        if isinstance(content, str):
            if content.strip():
                out.append((role, content))
            continue
        if not isinstance(content, list):
            continue
        texts = [
            b["text"]
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()
        ]
        if texts:
            out.append((role, "\n".join(texts)))
    return out


def _build_options(
    *,
    cwd: str,
    resume: str | None,
    session_id: str | None,
    allowed_tools: list[str],
    skills: Any,
    model: str | None,
    can_use_tool: Any = None,
) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        cwd=cwd,
        resume=resume,
        session_id=session_id,
        permission_mode="default",
        allowed_tools=allowed_tools,
        can_use_tool=can_use_tool,
        model=model,
        skills=skills,
        setting_sources=_SETTING_SOURCES,
    )


async def fetch_commands(
    cwd: str, *, skills: Any, model: str | None
) -> list[dict[str, Any]]:
    """Connect a throwaway client just to read available skills/commands."""
    options = _build_options(
        cwd=cwd,
        resume=None,
        session_id=None,
        allowed_tools=[],
        skills=skills,
        model=model,
    )
    client = ClaudeSDKClient(options=options)
    await client.connect()
    try:
        info = await client.get_server_info()
    finally:
        await client.disconnect()
    return list(info.get("commands", [])) if info else []


def _stringify_tool_result(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict):
            if item.get("type") == "text" and "text" in item:
                parts.append(str(item["text"]))
            else:
                parts.append(json.dumps(item, ensure_ascii=False))
        else:
            parts.append(str(item))
    return "\n".join(parts)


def _format_ask_question(tool_input: dict[str, Any]) -> str:
    lines: list[str] = []
    for q in tool_input.get("questions") or []:
        header = str(q.get("header", "")).strip()
        question = str(q.get("question", "")).strip()
        lines.append(f"{header}: {question}" if header else question)
        for opt in q.get("options") or []:
            label = str(opt.get("label", "")).strip()
            desc = " ".join(str(opt.get("description", "")).split())
            lines.append(f"  - {label}: {desc}" if desc else f"  - {label}")
        lines.append("")
    return "\n".join(lines).strip()


def _format_tool_input(name: str, tool_input: dict[str, Any]) -> str:
    if name == "Bash":
        return str(tool_input.get("command", ""))
    if name == "AskUserQuestion":
        return _format_ask_question(tool_input)
    if name in ("Write", "Edit", "Read", "NotebookEdit"):
        path = tool_input.get("file_path", "")
        extra = ""
        if name == "Write" and "content" in tool_input:
            extra = f"\n{tool_input['content']}"
        return f"{path}{extra}"
    try:
        return json.dumps(tool_input, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(tool_input)


class ClaudeProvider(Provider):
    def __init__(
        self,
        *,
        cwd: str,
        channel_id: int,
        broker: PermissionBroker,
        allowed_tools: list[str],
        skills: Any = "all",
        model: str | None = None,
        resume: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self.cwd = cwd
        self.channel_id = channel_id
        self.broker = broker
        self.allowed_tools = allowed_tools
        self.skills = skills
        self.model = model
        self._resume = resume
        self._new_session_id = session_id
        self.session_id = resume or session_id
        self._client: ClaudeSDKClient | None = None

    async def _can_use_tool(self, tool_name, tool_input, context):  # noqa: ANN001
        # AskUserQuestion is not an approval; poll the user and feed the answer
        # back through the deny message, which the model reads as the result.
        if tool_name == "AskUserQuestion":
            answer = await self.broker.ask(self.channel_id, tool_input)
            return PermissionResultDeny(message=answer)
        allowed, reason = await self.broker.request(
            self.channel_id, tool_name, tool_input
        )
        if allowed:
            return PermissionResultAllow()
        return PermissionResultDeny(message=reason or "Denied by user.")

    async def start(self) -> None:
        options = _build_options(
            cwd=self.cwd,
            resume=self._resume,
            session_id=None if self._resume else self._new_session_id,
            allowed_tools=self.allowed_tools,
            skills=self.skills,
            model=self.model,
            can_use_tool=self._can_use_tool,
        )
        self._client = ClaudeSDKClient(options=options)
        await self._client.connect()

    async def list_commands(self) -> list[dict[str, Any]]:
        if self._client is None:
            return []
        info = await self._client.get_server_info()
        return list(info.get("commands", [])) if info else []

    async def title(self) -> str | None:
        if not self.session_id:
            return None
        return session_title(self.session_id)

    async def set_mode(self, mode: str) -> None:
        if self._client is not None:
            await self._client.set_permission_mode(mode)

    async def run_turn(self, text: str) -> AsyncIterator[Block]:
        if self._client is None:
            raise RuntimeError("Provider not started")
        await self._client.query(text)
        async for message in self._client.receive_response():
            for block in self._map(message):
                yield block

    def _map(self, message: Any) -> list[Block]:
        blocks: list[Block] = []

        if isinstance(message, SystemMessage):
            if message.subtype == "init":
                sid = message.data.get("session_id")
                if sid:
                    self.session_id = sid
            return blocks

        if isinstance(message, AssistantMessage):
            if message.session_id:
                self.session_id = message.session_id
            for b in message.content:
                if isinstance(b, TextBlock):
                    if b.text.strip():
                        blocks.append(Block(BlockKind.TEXT, body=b.text))
                elif isinstance(b, ThinkingBlock):
                    if b.thinking.strip():
                        blocks.append(Block(BlockKind.THINKING, body=b.thinking))
                elif isinstance(b, ToolUseBlock):
                    blocks.append(
                        Block(
                            BlockKind.TOOL_CALL,
                            title=b.name,
                            body=_format_tool_input(b.name, b.input),
                            meta={"tool": b.name},
                        )
                    )
            return blocks

        if isinstance(message, UserMessage):
            content = message.content
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, ToolResultBlock):
                        blocks.append(
                            Block(
                                BlockKind.TOOL_RESULT,
                                body=_stringify_tool_result(b.content),
                                is_error=bool(b.is_error),
                            )
                        )
            return blocks

        if isinstance(message, ResultMessage):
            if message.session_id:
                self.session_id = message.session_id
            if message.is_error:
                detail = ""
                if message.errors:
                    detail = ": " + "; ".join(message.errors)
                blocks.append(
                    Block(
                        BlockKind.ERROR,
                        title="Turn failed",
                        body=(message.result or message.subtype) + detail,
                        is_error=True,
                    )
                )
            else:
                bits = [f"{message.num_turns} turn(s)"]
                if message.total_cost_usd:
                    bits.append(f"${message.total_cost_usd:.4f}")
                blocks.append(
                    Block(BlockKind.STATUS, title="Done", body=" · ".join(bits))
                )
            return blocks

        return blocks

    async def interrupt(self) -> None:
        if self._client is not None:
            await self._client.interrupt()

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
            self._client = None
