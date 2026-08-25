"""Types for the #control browser panel.

The vocabulary of the subsystem lives here: what a user can ask for
(ActionKind, Key), what the browser hands back (Hint, PageState), and how the
feature is configured (ControlSettings). Nothing here imports from src.*, so
it can be read on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ActionKind(StrEnum):
    """Every distinct thing the panel can do to the page.

    END is the one kind that never reaches the browser; ControlManager
    intercepts it and tears the session down.
    """

    CLICK_HINT = "click_hint"
    GOTO = "goto"
    TYPE = "type"
    KEY = "key"
    BACK = "back"
    FORWARD = "forward"
    RELOAD = "reload"
    SCROLL_UP = "scroll_up"
    SCROLL_DOWN = "scroll_down"
    ENTER = "enter"
    REFRESH = "refresh"
    END = "end"


class Key(StrEnum):
    """Keys the user may press by name, mapped to Playwright's key strings.

    An allowlist rather than passing text through: a typo fails in the parser
    with a clear message instead of deep inside Playwright.
    """

    ENTER = "Enter"
    TAB = "Tab"
    ESCAPE = "Escape"
    BACKSPACE = "Backspace"
    DELETE = "Delete"
    UP = "ArrowUp"
    DOWN = "ArrowDown"
    LEFT = "ArrowLeft"
    RIGHT = "ArrowRight"
    HOME = "Home"
    END = "End"
    PAGEUP = "PageUp"
    PAGEDOWN = "PageDown"


class ControlCommand(StrEnum):
    """Arguments accepted by `!control`."""

    START = "start"
    END = "end"


class BrowserMode(StrEnum):
    """Where the page the panel drives comes from.

    LAUNCH starts a private headless Chromium with an empty profile: no
    cookies, nothing logged in, and every session starts from scratch.

    ATTACH connects over CDP to a Chrome you are already running, and drives
    the tab that is open in it. That Chrome's profile carries your real logins,
    which is the point -- and also means the panel will screenshot whatever is
    on screen into Discord. Only attach to a browser you are willing to have
    photographed.

    Derived from whether cdp_url is set, so there is one knob in config, not
    two that can disagree.
    """

    LAUNCH = "launch"
    ATTACH = "attach"


@dataclass(frozen=True, slots=True)
class Action:
    """One requested operation. Exactly one of hint/text/key is set, per kind."""

    kind: ActionKind
    hint: int | None = None
    text: str | None = None
    key: Key | None = None


@dataclass(frozen=True, slots=True)
class Hint:
    """A numbered, clickable element found in the current viewport.

    The geometry is viewport-relative CSS pixels and is used only to draw the
    badge and to describe the element in errors. Clicking goes by the stamped
    `data-drc-hint` attribute, never by these coordinates.
    """

    index: int
    label: str
    tag: str
    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True, slots=True)
class PageState:
    """A rendered snapshot of the page. The contract between browser and panel."""

    url: str
    title: str
    hints: tuple[Hint, ...]
    png: bytes


@dataclass(frozen=True, slots=True)
class ControlSettings:
    """The [control] section of config.toml."""

    home_url: str = "https://www.google.com"
    viewport_width: int = 1280
    viewport_height: int = 800
    idle_timeout: int = 300
    settle_ms: int = 400
    quick_settle_ms: int = 150
    action_timeout_ms: int = 10_000
    max_hints: int = 150
    # Blank launches a private headless Chromium. Set it to a CDP endpoint
    # ("http://localhost:9222") to drive a Chrome you are already signed into.
    cdp_url: str = ""

    @property
    def mode(self) -> BrowserMode:
        return BrowserMode.ATTACH if self.cdp_url else BrowserMode.LAUNCH

    @classmethod
    def from_toml(cls, section: dict) -> "ControlSettings":
        defaults = cls()
        return cls(
            home_url=str(section.get("home_url", defaults.home_url)).strip()
            or defaults.home_url,
            cdp_url=str(section.get("cdp_url", defaults.cdp_url)).strip(),
            viewport_width=int(section.get("viewport_width", defaults.viewport_width)),
            viewport_height=int(
                section.get("viewport_height", defaults.viewport_height)
            ),
            idle_timeout=int(section.get("idle_timeout", defaults.idle_timeout)),
            settle_ms=int(section.get("settle_ms", defaults.settle_ms)),
            quick_settle_ms=int(
                section.get("quick_settle_ms", defaults.quick_settle_ms)
            ),
            action_timeout_ms=int(
                section.get("action_timeout_ms", defaults.action_timeout_ms)
            ),
            max_hints=int(section.get("max_hints", defaults.max_hints)),
        )


class ControlError(RuntimeError):
    """An expected, user-facing failure.

    Raised for anything the owner can act on: a stale hint number, a rejected
    URL, a browser that is no longer running. Its str() is shown in the panel's
    status line. Unexpected failures are left to propagate.
    """
