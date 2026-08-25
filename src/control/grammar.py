"""Parsing for what the owner types in #control. Pure: no I/O, no browser.

The whole grammar is `parse_action`. It is deliberately tiny, because the
common case is a bare hint number and everything unrecognized is typed into
the page verbatim.
"""

from __future__ import annotations

from typing import Sequence
from urllib.parse import urlparse

from src.control.types import Action, ActionKind, ControlError, Hint, Key

_GO = "go"
_TYPE = "type"
_KEY = "key"
_ALLOWED_SCHEMES = ("http", "https")

_USAGE = (
    "Type a hint number to click it, `go <url>`, `type <text>`, or `key <name>`."
)


def parse_action(raw: str) -> Action:
    """Turn a #control message into an Action.

    Checked in order: a bare number clicks that hint, then the three keyword
    forms, then anything else is typed into the page exactly as written. That
    last case is the documented default, not a fallback for bad input -- use
    `type 5` to type the digit 5 rather than click hint 5.
    """
    text = raw.lstrip()
    stripped = text.strip()
    if not stripped:
        raise ValueError(f"Nothing to do. {_USAGE}")
    lowered = stripped.lower()

    # isascii guards against digits like "²", which pass isdigit but not int().
    if stripped.isascii() and stripped.isdigit():
        return Action(kind=ActionKind.CLICK_HINT, hint=int(stripped))

    if lowered.startswith(f"{_TYPE} "):
        # Verbatim after the single delimiting space, so interior and trailing
        # whitespace survive.
        return Action(kind=ActionKind.TYPE, text=text[len(_TYPE) + 1 :])

    if lowered == _GO or lowered.startswith(f"{_GO} "):
        return Action(
            kind=ActionKind.GOTO, text=normalize_url(stripped[len(_GO) :].strip())
        )

    if lowered == _KEY or lowered.startswith(f"{_KEY} "):
        return Action(kind=ActionKind.KEY, key=_parse_key(stripped[len(_KEY) :].strip()))

    return Action(kind=ActionKind.TYPE, text=raw)


def normalize_url(raw: str) -> str:
    """Complete a bare domain to https, and reject every non-web scheme.

    This is a security boundary, not a convenience: `file:///Users/me/.ssh/id_rsa`
    or `javascript:` would otherwise render local files or run script inside the
    page and post the result as a PNG in Discord.
    """
    candidate = raw.strip()
    if not candidate:
        raise ValueError("Usage: `go <url>`")

    if "://" in candidate:
        parsed = urlparse(candidate)
        scheme = parsed.scheme.lower()
        if scheme not in _ALLOWED_SCHEMES:
            raise ValueError(
                f"Only http and https URLs are allowed, not `{scheme}:`."
            )
        if not parsed.netloc:
            raise ValueError(f"`{candidate}` is not a valid URL.")
        return candidate

    # No explicit scheme. A colon here is either a port ("localhost:3000") or a
    # scheme we refuse to follow ("javascript:alert(1)", "data:text/html,...").
    head, sep, rest = candidate.partition(":")
    if sep and not rest[:1].isdigit():
        raise ValueError(f"Only http and https URLs are allowed, not `{head}:`.")
    # Dev servers speak plain http, and https to one of these is a connection
    # error rather than a redirect -- so guessing https for `localhost:3000`
    # just makes `go` look broken. Public hosts still default to https.
    scheme = "http" if _is_loopback(head) else "https"
    return f"{scheme}://{candidate}"


def _is_loopback(host: str) -> bool:
    host = host.lower()
    return host in ("localhost", "127.0.0.1", "[::1]", "0.0.0.0") or host.endswith(
        ".localhost"
    )


def select_hint(hints: Sequence[Hint], index: int) -> Hint:
    """Find a hint by its badge number, or explain why it is not there."""
    for hint in hints:
        if hint.index == index:
            return hint
    if not hints:
        raise ControlError(
            "This page has no hints. Hit Re-screenshot, or scroll to something clickable."
        )
    raise ControlError(f"No hint {index}. This page has hints 1-{len(hints)}.")


def _parse_key(name: str) -> Key:
    if not name:
        raise ValueError(f"Usage: `key <name>`. Known keys: {_key_names()}.")
    try:
        return Key[name.upper()]
    except KeyError:
        raise ValueError(f"Unknown key `{name}`. Known keys: {_key_names()}.") from None


def _key_names() -> str:
    return ", ".join(key.name.lower() for key in Key)
