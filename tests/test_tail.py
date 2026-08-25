"""The tail watcher and the queued-message ack.

Late output -- a subagent finishing after its result, a continuation turn --
must surface on its own instead of waiting for the next user message to drag
it out, and a message sent while work is running must say so.
"""

from __future__ import annotations

import asyncio

import pytest
from claude_agent_sdk import AssistantMessage, TextBlock

import src.session as S
from src.providers.base import Block, BlockKind
from src.providers.claude import TaskLedger


class FakeChannel:
    id = 1

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content)
        return None

    def typing(self):
        class _Ctx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

        return _Ctx()


class FakeStore:
    def set_session_id(self, *a):
        pass


class FakeBot:
    prefs = None

    def __init__(self) -> None:
        self.channel = FakeChannel()

    def get_channel(self, cid):
        return self.channel if cid == self.channel.id else None

    def _spawn(self, coro):
        task = asyncio.get_running_loop().create_task(coro)
        return task


@pytest.fixture(autouse=True)
def fast_timeouts(monkeypatch):
    monkeypatch.setattr(S, "_TAIL_FIRST_WAIT", 0.05)
    monkeypatch.setattr(S, "_QUEUE_ACK_AFTER", 0.05)


def make_session(bot: FakeBot) -> S.Session:
    session = S.Session.__new__(S.Session)
    session._bot = bot
    session._store = FakeStore()
    session.channel_id = bot.channel.id
    session._lock = asyncio.Lock()
    session._named = True
    session._warned_pct = 0.0
    session._watching = False
    session._board = S.TaskBoard()
    # Bypass discord's Messageable isinstance check with a resolvable channel.
    session._channel = lambda: _return(bot.channel)
    return session


async def _return(value):
    return value


def claude_provider():
    from src.providers.claude import AssistantMessage, ClaudeProvider, TextBlock

    p = ClaudeProvider.__new__(ClaudeProvider)
    p._queue = asyncio.Queue()
    p._ledger = TaskLedger()
    p._stalled = False
    p.session_id = None
    p.last_context = None
    p._request_usage = None
    p._cost_seen = 0.0
    p._map = lambda m: [Block(BlockKind.TEXT, body=m.content[0].text)]
    p.AssistantMessage = AssistantMessage
    p.TextBlock = TextBlock
    return p


def test_late_output_surfaces_without_a_new_prompt():
    bot = FakeBot()
    provider = claude_provider()
    session = make_session(bot)
    session.provider = provider
    # A subagent finished after its turn was closed out; its output is sitting
    # in the provider queue.
    provider._queue.put_nowait(
        AssistantMessage(content=[TextBlock(text="subagent finished")], model="m")
    )

    async def go():
        await session._watch_tail()

    asyncio.run(asyncio.wait_for(go(), timeout=5))
    assert any("subagent finished" in str(m) for m in bot.channel.sent), (
        "tail output must post without waiting for another user message"
    )


def test_tail_watch_ends_when_nothing_arrives():
    bot = FakeBot()
    session = make_session(bot)
    session.provider = claude_provider()

    async def go():
        await asyncio.wait_for(session._watch_tail(), timeout=3)

    asyncio.run(go())  # returns instead of hanging on an empty queue


class TurnProvider:
    """A provider whose single turn yields one text block."""

    def __init__(self) -> None:
        self.session_id = None

    def run_turn(self, text: str):
        async def gen():
            yield Block(BlockKind.TEXT, body=f"echo:{text}")

        return gen()

    def drain_pending(self, first_wait: float = 0.0):
        async def gen():
            return
            yield

        return gen()


def test_message_while_busy_gets_an_ack_then_runs():
    bot = FakeBot()
    provider = TurnProvider()
    session = make_session(bot)
    session.provider = provider

    async def go():
        # Hold the lock like a live turn would.
        await session._lock.acquire()
        worker = asyncio.create_task(session.handle_message("hello"))
        await asyncio.sleep(3 * S._QUEUE_ACK_AFTER)
        assert not worker.done(), "the message must wait, not run early"
        assert any("Queued" in str(m) for m in bot.channel.sent), (
            "the user must be told their message is waiting behind live work"
        )
        session._lock.release()
        await asyncio.wait_for(worker, timeout=5)

    asyncio.run(asyncio.wait_for(go(), timeout=10))
    assert any("echo:hello" in str(m) for m in bot.channel.sent)


def test_fast_turns_never_ack():
    bot = FakeBot()
    session = make_session(bot)
    session.provider = TurnProvider()

    async def go():
        await session.handle_message("hi")

    asyncio.run(asyncio.wait_for(go(), timeout=5))
    assert all("Queued" not in str(m) for m in bot.channel.sent)
