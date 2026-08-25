"""Scraping the login TUI: link out, token in, code accepted either way."""

from __future__ import annotations

from src.auth import clean, code_from_reply, find_token, find_url

RAW = (
    "\x1b[?25lWelcome to Claude Code v2.1.220\r\n"
    "\x1b]8;id=1;https://claude.com/cai/oauth/authorize?code=true&client_id=abc"
    "&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback"
    "\x07https://claude.com/cai/oauth/authorize?code=true&client_id=abc\x1b]8;;\x07\r\n"
    "Paste code here if prompted >\r\n"
)


def test_url_is_found_through_ansi_noise():
    url = find_url(RAW)
    assert url is not None
    assert url.startswith("https://claude.com/cai/oauth/authorize")
    assert "client_id=abc" in url


def test_token_is_found_unwrapped():
    text = "sk-ant-oat01-Abc_123-\r\n456def more"
    assert find_token(text) == "sk-ant-oat01-Abc_123-456def"


def test_clean_strips_control_bytes():
    assert "\x1b" not in clean(RAW)


def test_code_from_bare_reply():
    assert code_from_reply("  abcd1234-ef56-7890  ") == "abcd1234-ef56-7890"


def test_code_extracted_from_full_callback_url():
    value = "https://platform.claude.com/oauth/code/callback?code=xyz987#/auth"
    assert code_from_reply(value) == "xyz987"


def test_gibberish_is_rejected():
    assert code_from_reply("hello world, what is this?") is None
