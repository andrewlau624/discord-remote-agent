"""Tests for BrowserService's pure logic.

Only settle_ms is covered: it is branchy, and getting it wrong is a subtle bug
(a screenshot taken mid-navigation looks like a blank or stale page, not like
an error). Everything else in BrowserService needs a real Chromium and is
covered by running the bot.
"""

from __future__ import annotations

import pytest

from src.control.browser import BrowserService
from src.control.types import (
    Action,
    ActionKind,
    BrowserMode,
    ControlSettings,
    Key,
)

_SETTINGS = ControlSettings(settle_ms=400, quick_settle_ms=150)


@pytest.fixture
def service() -> BrowserService:
    return BrowserService(_SETTINGS)


@pytest.mark.parametrize(
    "kind",
    [
        ActionKind.GOTO,
        ActionKind.RELOAD,
        ActionKind.BACK,
        ActionKind.FORWARD,
        ActionKind.CLICK_HINT,
        ActionKind.ENTER,
    ],
)
def test_actions_that_can_navigate_get_the_full_settle(
    service: BrowserService, kind: ActionKind
) -> None:
    assert service.settle_ms(Action(kind=kind)) == 400


@pytest.mark.parametrize(
    "kind", [ActionKind.TYPE, ActionKind.SCROLL_UP, ActionKind.SCROLL_DOWN, ActionKind.REFRESH]
)
def test_actions_that_cannot_navigate_get_the_quick_settle(
    service: BrowserService, kind: ActionKind
) -> None:
    assert service.settle_ms(Action(kind=kind)) == 150


def test_enter_key_gets_the_full_settle(service: BrowserService) -> None:
    """`key enter` submits forms, so it navigates just like the Enter button."""
    action = Action(kind=ActionKind.KEY, key=Key.ENTER)
    assert service.settle_ms(action) == 400


@pytest.mark.parametrize("key", [Key.TAB, Key.ESCAPE, Key.DOWN, Key.PAGEDOWN])
def test_other_keys_get_the_quick_settle(service: BrowserService, key: Key) -> None:
    assert service.settle_ms(Action(kind=ActionKind.KEY, key=key)) == 150


def test_no_cdp_url_means_launch_our_own_browser() -> None:
    assert ControlSettings().mode is BrowserMode.LAUNCH


def test_a_cdp_url_means_attach() -> None:
    settings = ControlSettings(cdp_url="http://localhost:9222")
    assert settings.mode is BrowserMode.ATTACH


def test_blank_cdp_url_is_not_mistaken_for_an_endpoint() -> None:
    """from_toml strips, so a commented-out-by-blanking value must still launch."""
    assert ControlSettings.from_toml({"cdp_url": "   "}).mode is BrowserMode.LAUNCH


def test_a_fresh_service_assumes_it_owns_everything() -> None:
    """Ownership starts true so a failure before _attach() runs still tears down
    what little got built, rather than leaking it as 'borrowed'."""
    fresh = BrowserService(ControlSettings(cdp_url="http://localhost:9222"))
    assert fresh._owns_browser and fresh._owns_page
