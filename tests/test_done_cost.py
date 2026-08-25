"""Done-line cost accounting: cumulative report in, per-run share out."""

from __future__ import annotations

import asyncio

from claude_agent_sdk import ResultMessage

import src.providers.claude as C


def provider() -> C.ClaudeProvider:
    p = C.ClaudeProvider.__new__(C.ClaudeProvider)
    p._queue = asyncio.Queue()
    p._ledger = C.TaskLedger()
    p._stalled = False
    p.session_id = None
    p.last_context = None
    p._request_usage = None
    p._cost_seen = 0.0
    return p


def result(cost: float) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s1",
        total_cost_usd=cost,
    )


def test_first_run_shows_its_own_cost():
    block = provider()._done_block(result(0.05))
    assert "this run" not in block.body and "$0.0500" in block.body


def test_later_runs_show_the_delta_and_total():
    p = provider()
    assert p._done_block(result(0.05)) is not None
    block = p._done_block(result(0.08))
    assert "$0.0300 this run" in block.body and "$0.08 session" in block.body


def test_cost_reset_is_treated_as_new_spend():
    p = provider()
    p._done_block(result(1.00))
    # A fresh CLI process restarts the total from near zero; the new figure
    # is small enough to print on its own.
    block = p._done_block(result(0.02))
    assert block.body == "1 turn(s) · $0.0200"


def test_zero_or_missing_cost_omits_the_money_segment():
    block = provider()._done_block(result(0.0))
    assert "$" not in block.body
    assert "1 turn(s)" in block.body
