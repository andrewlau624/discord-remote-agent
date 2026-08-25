"""Reading context fullness, and deciding when to warn about it."""

from __future__ import annotations

from claude_agent_sdk import ResultMessage

import src.providers.claude as C
from src.providers.base import ContextState
from src.session import Session


def usage(**over) -> dict:
    base = {
        "inputTokens": 0,
        "outputTokens": 0,
        "cacheReadInputTokens": 0,
        "cacheCreationInputTokens": 0,
        "contextWindow": 0,
    }
    base.update(over)
    return base


def test_used_counts_fresh_and_cached_input():
    request = {
        "input_tokens": 10_000,
        "cache_read_input_tokens": 80_000,
        "cache_creation_input_tokens": 10_000,
    }
    state = C._context_from_usage(
        request, {"opus": usage(contextWindow=200_000)}
    )
    assert state.used == 100_000
    assert state.pct == 50.0
    assert state.remaining == 100_000


def test_cumulative_totals_are_never_summed_against_one_window():
    # model_usage accumulates across the session; summing it against a single
    # turn's window is what used to report thousands of percent.
    model_usage = {
        "opus": usage(
            inputTokens=500_000,
            cacheReadInputTokens=4_000_000,
            contextWindow=200_000,
        )
    }
    state = C._context_from_usage({"input_tokens": 30_000}, model_usage)
    assert state.used == 30_000
    assert state.pct == 15.0


def test_the_window_comes_from_model_usage_even_when_models_differ():
    state = C._context_from_usage(
        {"input_tokens": 90_000},
        {"opus": usage(contextWindow=100_000), "sonnet": usage(contextWindow=200_000)},
    )
    assert state.limit == 200_000
    assert state.pct == 45.0


def test_missing_or_unusable_usage_reports_nothing():
    assert C._context_from_usage(None, None) is None
    assert C._context_from_usage({}, {}) is None
    assert C._context_from_usage({"input_tokens": 5}, {"m": usage(contextWindow=0)}) is None


def done_block(model_usage):
    p = C.ClaudeProvider.__new__(C.ClaudeProvider)
    p.last_context = None
    p._request_usage = {
        "input_tokens": 20_000,
        "cache_read_input_tokens": 74_000,
    }
    p._cost_seen = 0.0
    message = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=3,
        session_id="s",
        total_cost_usd=0.0412,
        model_usage=model_usage,
    )
    return p, p._done_block(message)


def test_done_line_carries_the_percentage():
    p, block = done_block({"opus": usage(contextWindow=200_000)})
    assert "47% ctx" in block.body
    assert "$0.0412" in block.body and "3 turn(s)" in block.body
    assert p.last_context.pct == 47.0


def test_done_line_omits_the_percentage_when_unknown():
    p = C.ClaudeProvider.__new__(C.ClaudeProvider)
    p.last_context = None
    p._request_usage = None
    p._cost_seen = 0.0
    message = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=3,
        session_id="s",
        total_cost_usd=0.0412,
        model_usage=None,
    )
    block = p._done_block(message)
    assert "ctx" not in block.body
    assert p.last_context is None


def session_at(pct: float) -> Session:
    s = Session.__new__(Session)
    s.provider = type("P", (), {"last_context": ContextState(used=pct, limit=100)})()
    s._warned_pct = 0.0
    return s


def test_quiet_below_the_threshold():
    assert session_at(60)._pending_warning(75, 90) is None


def test_each_threshold_fires_exactly_once():
    s = session_at(60)
    assert s._pending_warning(75, 90) is None

    s.provider.last_context = ContextState(used=78, limit=100)
    assert s._pending_warning(75, 90) is not None, "crossing warn_at should warn"
    assert s._pending_warning(75, 90) is None, "but only once"

    s.provider.last_context = ContextState(used=85, limit=100)
    assert s._pending_warning(75, 90) is None, "still below warn_again_at"

    s.provider.last_context = ContextState(used=92, limit=100)
    assert s._pending_warning(75, 90) is not None, "crossing warn_again_at re-arms"
    assert s._pending_warning(75, 90) is None


def test_jumping_past_both_thresholds_warns_once_at_the_higher_one():
    s = session_at(95)
    assert s._pending_warning(75, 90) is not None
    assert s._warned_pct == 90.0
    assert s._pending_warning(75, 90) is None


def test_reset_re_arms_after_a_compact_or_handoff():
    s = session_at(95)
    s._pending_warning(75, 90)
    s.reset_context_warnings()
    assert s._pending_warning(75, 90) is not None


def test_zero_threshold_disables_warnings():
    assert session_at(99)._pending_warning(0, 0) is None
