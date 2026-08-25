"""Where a turn ends when subagents are involved.

The CLI emits a result when the *turn* ends, not the *run*. A subagent that
settles before that result leaves the ledger empty at it, yet its completion
still wakes the parent for a continuation turn. Reading these sequences wrong
is what made the bot go quiet until you said "continue", so each ordering is
pinned here.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TextBlock,
)

import src.providers.claude as C
from src.providers.base import BlockKind


@pytest.fixture(autouse=True)
def _fast_timeouts(monkeypatch):
    """Keep the no-continuation case from waiting the real grace period."""
    monkeypatch.setattr(C, "_CONTINUATION_GRACE", 0.4)
    monkeypatch.setattr(C, "_IDLE_TIMEOUT", 3.0)
    monkeypatch.setattr(C, "_DRAIN_QUIET", 0.2)


def assistant(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="m")


def result(turns: int = 1, error: bool = False) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=error,
        num_turns=turns,
        session_id="s1",
        total_cost_usd=0.01,
    )


def started(task_id: str) -> TaskStartedMessage:
    return TaskStartedMessage(
        subtype="task_started",
        data={},
        task_id=task_id,
        description="sub",
        uuid="u",
        session_id="s1",
        task_type="local_agent",
    )


def notify(task_id: str, status: str = "completed") -> TaskNotificationMessage:
    return TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id=task_id,
        status=status,
        output_file="",
        summary="done",
        uuid="u",
        session_id="s1",
    )


def provider(messages: list) -> C.ClaudeProvider:
    p = C.ClaudeProvider.__new__(C.ClaudeProvider)
    p._queue = asyncio.Queue()
    p._ledger = C.TaskLedger()
    p._stalled = False
    p.session_id = None
    p.last_context = None
    p._request_usage = None
    p._cost_seen = 0.0
    for m in messages:
        p._queue.put_nowait(m)
    return p


def read(messages: list) -> tuple[list[str], int]:
    """Drive a turn to completion; return its text blocks and Done count."""
    p = provider(messages)
    texts: list[str] = []
    dones = 0

    async def go():
        nonlocal dones
        async for block in p._read_until_run_end():
            if block.kind is BlockKind.TEXT:
                texts.append(block.body.strip())
            elif block.kind is BlockKind.STATUS and block.title == "Done":
                dones += 1

    asyncio.run(asyncio.wait_for(go(), timeout=5))
    assert not p._stalled, "turn should not have stalled"
    return texts, dones


def test_plain_turn_ends_at_its_result():
    assert read([assistant("hi"), result()]) == (["hi"], 1)


def test_task_outliving_its_turn_is_waited_for():
    # The common ordering: result first, task settles after, continuation last.
    texts, dones = read(
        [started("t1"), result(), notify("t1"), assistant("after sub"), result(2)]
    )
    assert texts == ["after sub"]
    assert dones == 1, "only the run-ending result should report Done"


def test_task_settling_before_the_result_still_reads_the_continuation():
    # The regression: ledger is already empty at the result, but a continuation
    # is still coming. Returning here stranded its output until the next prompt.
    texts, dones = read(
        [started("t1"), notify("t1"), result(), assistant("continuation"), result(2)]
    )
    assert texts == ["continuation"]
    assert dones == 1


def test_missing_continuation_ends_the_turn_after_the_grace():
    started_at = time.monotonic()
    texts, dones = read([started("t1"), notify("t1"), result()])
    assert texts == []
    assert dones == 1, "the held Done must still be emitted"
    assert time.monotonic() - started_at < 3.0, "must not wait the full idle timeout"


def test_several_subagents_settling_early():
    texts, dones = read(
        [
            started("t1"),
            started("t2"),
            notify("t1"),
            notify("t2"),
            result(),
            assistant("both done"),
            result(2),
        ]
    )
    assert texts == ["both done"]
    assert dones == 1


def test_error_result_ends_immediately():
    p = provider([result(error=True)])

    async def go():
        return [b async for b in p._read_until_run_end()]

    blocks = asyncio.run(asyncio.wait_for(go(), timeout=5))
    assert not any(b.title == "Done" for b in blocks)


def _drain(messages: list, stalled: bool = False) -> list[str]:
    p = provider(messages)
    p._stalled = stalled

    async def go():
        return [
            b.body.strip()
            async for b in p._drain_stale()
            if b.kind is BlockKind.TEXT
        ]

    return asyncio.run(go())


def test_backlog_is_flushed_even_without_a_stall():
    # Output left unread by an early return must not interleave with the next
    # turn, so any non-empty queue is drained before the next query.
    assert _drain([assistant("late output")]) == ["late output"]


def test_empty_queue_costs_nothing():
    start = time.monotonic()
    assert _drain([]) == []
    assert time.monotonic() - start < 0.05, "must not pay the quiet-period wait"
