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
)

from dra.providers.base import Block, BlockKind, PermissionBroker, Provider


def _stringify_tool_result(content: Any) -> str:
    """ToolResultBlock.content is str | list[dict] | None."""
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


def _format_tool_input(name: str, tool_input: dict[str, Any]) -> str:
    """Render a tool call's input compactly for display."""
    if name == "Bash":
        return str(tool_input.get("command", ""))
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
        resume: str | None = None,
        model: str | None = None,
    ) -> None:
        self.cwd = cwd
        self.channel_id = channel_id
        self.broker = broker
        self.allowed_tools = allowed_tools
        self.model = model
        self.session_id = resume  # if set, resume that session on start()
        self._client: ClaudeSDKClient | None = None

    async def _can_use_tool(self, tool_name, tool_input, context):  # noqa: ANN001
        allowed, reason = await self.broker.request(
            self.channel_id, tool_name, tool_input
        )
        if allowed:
            return PermissionResultAllow()
        return PermissionResultDeny(message=reason or "Denied by user.")

    async def start(self) -> None:
        options = ClaudeAgentOptions(
            cwd=self.cwd,
            resume=self.session_id,
            permission_mode="default",
            allowed_tools=self.allowed_tools,
            can_use_tool=self._can_use_tool,
            model=self.model,
        )
        self._client = ClaudeSDKClient(options=options)
        await self._client.connect()

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
