"""Turn plumbing, and the lock the context warning must not be holding.

The warning waits on a human reaction for minutes, and its handoff action
calls back into `ask`, which takes the session's own non-reentrant lock. Run
inside the lock it froze the thread outright, so the dispatch point is pinned.
"""

from __future__ import annotations

import asyncio

import pytest

from src.providers.base import Block, BlockKind, ContextState
from src.session import Session


class Provider:
    session_id = "s1"

    def __init__(self, pct: float = 95.0):
        self.last_context = ContextState(used=pct, limit=100)

    async def run_turn(self, text: str):
        yield Block(BlockKind.TEXT, body=f"reply to {text}")

    async def title(self):
        return None


class Channel:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def typing(self):
        class T:
            async def __aenter__(inner):
                return inner

            async def __aexit__(inner, *a):
                return False

        return T()

    async def send(self, *args, **kwargs):
        self.sent.append(kwargs.get("content") or (args[0] if args else ""))


class Board:
    async def apply(self, *a):
        pass

    def clear(self):
        pass


def build(bot, channel, pct: float = 95.0) -> Session:
    s = Session.__new__(Session)
    s._bot = bot
    s._store = type("S", (), {"set_session_id": lambda *a: None})()
    s.channel_id = 1
    s.provider = Provider(pct)
    s.provider_name = "claude"
    s._lock = asyncio.Lock()
    s._named = True
    s._board = Board()
    s._warned_pct = 0.0
    # The real _channel() isinstance-checks against discord.abc.Messageable,
    # which a stub cannot satisfy.
    async def _channel():
        return channel

    s._channel = _channel
    return s


class Bot:
    config = type("C", (), {"context_warn_at": 75, "context_warn_again_at": 90})()
    prefs = None

    def __init__(self, channel):
        self._channel = channel
        self.asked = None

    def get_channel(self, _cid):
        return self._channel

    async def handle_context_warning(self, channel, session, state):
        # Exactly what the real handler's handoff branch does.
        self.asked = await session.ask("write a brief")


def test_warning_handler_can_take_the_lock_without_deadlocking():
    channel = Channel()
    bot = Bot(channel)
    session = build(bot, channel)

    async def go():
        await asyncio.wait_for(session.handle_message("hello"), timeout=5)

    try:
        asyncio.run(go())
    except asyncio.TimeoutError:
        pytest.fail("deadlock: handle_message never returned")

    assert "reply to hello" in channel.sent, "the turn itself must still run"
    assert bot.asked == "reply to write a brief", "the warning must have fired"
    assert not session._lock.locked()


def test_no_warning_when_context_is_low():
    channel = Channel()
    bot = Bot(channel)
    session = build(bot, channel, pct=10.0)
    asyncio.run(asyncio.wait_for(session.handle_message("hi"), timeout=5))
    assert bot.asked is None


def test_ask_returns_text_without_posting():
    channel = Channel()
    session = build(Bot(channel), channel)
    out = asyncio.run(session.ask("question"))
    assert out == "reply to question"
    assert channel.sent == [], "ask must not post the answer to the channel"
