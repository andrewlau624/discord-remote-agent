"""The Done line's numbers: context fullness and cost.

`model_usage` on a result is *cumulative* across the session, so summing it
against a single-turn window reported absurd percentages (6000%+). The truth
for "how full is the window" is the last assistant request's input side; the
truth for "what did this run cost" is the delta of the cumulative figure.
"""

from __future__ import annotations

from src.providers.claude import _context_from_usage
from src.providers.base import ContextState


def _state(used: int, limit: int) -> dict:
    return {
        "input_tokens": used,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    } | {"contextWindow": limit}


def test_request_usage_is_the_window_content():
    model_usage = {
        # Cumulative across five turns: far past the window.
        "claude-opus-4-6": {
            "inputTokens": 500_000,
            "cacheReadInputTokens": 4_000_000,
            "cacheCreationInputTokens": 10_000,
            "contextWindow": 200_000,
        }
    }
    request = {"input_tokens": 30_000, "cache_read_input_tokens": 120_000}
    state = _context_from_usage(request, model_usage)
    assert state == ContextState(used=150_000, limit=200_000, model="claude-opus-4-6")
    assert state.pct == 75.0


def test_cumulative_fallback_never_exceeds_the_window():
    model_usage = {
        "m": {
            "inputTokens": 900_000,
            "cacheReadInputTokens": 9_000_000,
            "cacheCreationInputTokens": 0,
            "contextWindow": 100_000,
        }
    }
    state = _context_from_usage(None, model_usage)
    assert state.used <= state.limit


def test_no_window_means_no_reading():
    assert _context_from_usage({"input_tokens": 5}, {"m": {"contextWindow": 0}}) is None
