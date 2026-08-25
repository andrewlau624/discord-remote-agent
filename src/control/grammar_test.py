"""Tests for the #control grammar.

This is the only branchy, I/O-free logic in the subsystem, and the piece most
likely to break silently: the bare-number vs literal-text collision and the
URL scheme allowlist both have real consequences.
"""

from __future__ import annotations

import pytest

from src.control.grammar import normalize_url, parse_action, select_hint
from src.control.types import ActionKind, ControlError, Hint, Key


def _hint(index: int) -> Hint:
    return Hint(index=index, label=f"el{index}", tag="a", x=0, y=0, w=10, h=10)


# ---- clicking by number ---------------------------------------------------


@pytest.mark.parametrize("raw,expected", [("7", 7), ("  7  ", 7), ("007", 7)])
def test_bare_number_clicks_that_hint(raw: str, expected: int) -> None:
    action = parse_action(raw)
    assert action.kind is ActionKind.CLICK_HINT
    assert action.hint == expected


@pytest.mark.parametrize("raw", ["-1", "1e3", "1.5", "3 4"])
def test_non_integers_are_typed_not_clicked(raw: str) -> None:
    assert parse_action(raw).kind is ActionKind.TYPE


def test_superscript_digits_do_not_crash_int() -> None:
    # "²".isdigit() is True but int("²") raises; it must fall through to TYPE.
    action = parse_action("²")
    assert action.kind is ActionKind.TYPE
    assert action.text == "²"


# ---- typing ---------------------------------------------------------------


def test_type_prefix_forces_a_literal_number() -> None:
    """The collision that matters: how you type "5" instead of clicking hint 5."""
    action = parse_action("type 5")
    assert action.kind is ActionKind.TYPE
    assert action.text == "5"


def test_type_preserves_interior_and_trailing_whitespace() -> None:
    assert parse_action("type  hello ").text == " hello "


def test_unrecognized_input_is_typed_verbatim() -> None:
    action = parse_action("hello world")
    assert action.kind is ActionKind.TYPE
    assert action.text == "hello world"


def test_empty_input_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_action("   ")


# ---- navigation -----------------------------------------------------------


def test_go_is_case_insensitive_and_completes_bare_domains() -> None:
    action = parse_action("GO example.com")
    assert action.kind is ActionKind.GOTO
    assert action.text == "https://example.com"


def test_go_keeps_an_explicit_scheme() -> None:
    assert parse_action("go http://example.com/a?b=c").text == "http://example.com/a?b=c"


def test_go_allows_a_localhost_port() -> None:
    """localhost:3000 must not be mistaken for a "localhost:" scheme."""
    assert normalize_url("localhost:3000") == "http://localhost:3000"


def test_loopback_defaults_to_http() -> None:
    """Dev servers speak http; guessing https makes `go` fail to connect."""
    assert normalize_url("127.0.0.1:54321") == "http://127.0.0.1:54321"


def test_public_hosts_still_default_to_https() -> None:
    assert normalize_url("example.com") == "https://example.com"
    assert normalize_url("example.com:8443") == "https://example.com:8443"


def test_an_explicit_scheme_is_always_respected() -> None:
    """Only bare hosts get a guess; a typed scheme is never rewritten."""
    assert normalize_url("https://localhost:3000") == "https://localhost:3000"
    assert normalize_url("http://example.com") == "http://example.com"


@pytest.mark.parametrize(
    "raw",
    [
        "file:///etc/passwd",
        "file:///Users/me/.ssh/id_rsa",
        "javascript:alert(1)",
        "data:text/html,<script>1</script>",
        "ftp://example.com",
    ],
)
def test_non_web_schemes_are_rejected(raw: str) -> None:
    """Security boundary: these would render local files or run script."""
    with pytest.raises(ValueError):
        normalize_url(raw)


def test_go_without_a_url_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_action("go")


# ---- keys -----------------------------------------------------------------


def test_key_maps_to_playwright_names() -> None:
    action = parse_action("key enter")
    assert action.kind is ActionKind.KEY
    assert action.key is Key.ENTER
    assert action.key.value == "Enter"


def test_unknown_key_is_rejected_with_the_known_list() -> None:
    with pytest.raises(ValueError, match="escape"):
        parse_action("key bogus")


def test_key_without_a_name_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_action("key")


# ---- hint lookup ----------------------------------------------------------


def test_select_hint_returns_the_match() -> None:
    assert select_hint([_hint(1), _hint(2)], 2).index == 2


def test_select_hint_reports_the_available_range() -> None:
    with pytest.raises(ControlError, match="1-2"):
        select_hint([_hint(1), _hint(2)], 9)


def test_select_hint_on_an_empty_page_says_so() -> None:
    with pytest.raises(ControlError, match="no hints"):
        select_hint([], 1)
