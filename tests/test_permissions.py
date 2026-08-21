"""Who gets to skip the approval poll.

The SDK only shadows can_use_tool for bypassPermissions and whole-tool
allowed_tools entries, so acceptEdits never reached the CLI while the bot had
a callback installed -- every edit polled. It is honoured in the callback now,
and these pin that it applies to edits only.
"""

from __future__ import annotations

import asyncio

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

import src.providers.claude as C
from src.approvals import ApprovalPrefs
from src.permissions import DiscordPermissionBroker
from src.prefs import DisplayPrefs


class SpyBroker:
    """Records what it was asked about and always denies."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def request(self, channel_id, tool_name, tool_input):
        self.calls.append(tool_name)
        return False, "denied"

    async def ask(self, channel_id, tool_input):
        return "answer"


def provider(mode: str) -> C.ClaudeProvider:
    p = C.ClaudeProvider.__new__(C.ClaudeProvider)
    p.permission_mode = mode
    p.broker = SpyBroker()
    p.channel_id = 1
    return p


@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit", "NotebookEdit"])
def test_accept_edits_allows_edit_tools_without_asking(tool):
    p = provider("acceptEdits")
    got = asyncio.run(p._can_use_tool(tool, {"file_path": "a.py"}, None))
    assert isinstance(got, PermissionResultAllow)
    assert p.broker.calls == [], "the broker should never have been consulted"


def test_accept_edits_still_gates_everything_else():
    p = provider("acceptEdits")
    got = asyncio.run(p._can_use_tool("Bash", {"command": "rm -rf /"}, None))
    assert isinstance(got, PermissionResultDeny)
    assert p.broker.calls == ["Bash"]


def test_default_mode_still_gates_edits():
    p = provider("default")
    got = asyncio.run(p._can_use_tool("Edit", {"file_path": "a.py"}, None))
    assert isinstance(got, PermissionResultDeny)
    assert p.broker.calls == ["Edit"]


def broker(prefs: ApprovalPrefs, seen: list[str] | None = None):
    b = DiscordPermissionBroker.__new__(DiscordPermissionBroker)
    b.approvals = prefs
    b._on_new_tool = (seen.append if seen is not None else None)

    class Channel:
        guild = type("G", (), {"owner_id": 1})()

        async def send(self, *a, **k):
            raise AssertionError("must not post when the answer is automatic")

    b._bot = type("B", (), {"get_channel": staticmethod(lambda _c: Channel())})()
    b._timeout = 1
    return b


def test_approved_tool_skips_the_poll():
    allowed, _ = asyncio.run(
        broker(ApprovalPrefs(approved={"Read"})).request(1, "Read", {})
    )
    assert allowed is True


def test_accept_all_approves_anything():
    allowed, _ = asyncio.run(
        broker(ApprovalPrefs(accept_all=True)).request(1, "Bash", {"command": "ls"})
    )
    assert allowed is True


def test_accept_all_does_not_depend_on_resolving_a_channel():
    # "Never ask" must not become a denial just because the channel lookup
    # failed, which would stall the agent for no reason.
    b = DiscordPermissionBroker.__new__(DiscordPermissionBroker)
    b.approvals = ApprovalPrefs(accept_all=True)
    b._on_new_tool = None
    b._bot = type("B", (), {"get_channel": staticmethod(lambda _c: None)})()
    allowed, _ = asyncio.run(b.request(1, "Bash", {}))
    assert allowed is True


def test_every_tool_asked_about_is_recorded():
    seen: list[str] = []
    b = broker(ApprovalPrefs(approved={"Read"}), seen)
    asyncio.run(b.request(1, "Read", {}))
    assert "Read" in b.approvals.seen, "the panel learns names from these calls"
    assert seen == ["Read"], "a newly seen tool should be persisted"
    asyncio.run(b.request(1, "Read", {}))
    assert seen == ["Read"], "an already-known tool should not re-save"


def test_accept_all_is_not_a_display_preference():
    assert not hasattr(DisplayPrefs(), "accept_all")
