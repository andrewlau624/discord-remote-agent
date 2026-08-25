"""The browser behind #control.

BrowserService owns the whole Playwright lifecycle (driver, browser, context,
page) and is the only place that talks to it. Every public method returns a
PageState -- a screenshot plus the hints drawn on it -- so callers never touch
a Page object.

Two ways to get a page, chosen by ControlSettings.mode:

  LAUNCH  start a private headless Chromium with an empty profile.
  ATTACH  connect over CDP to a Chrome that is already running and drive the
          tab it already has open, logins and all.

The difference matters at teardown: anything this service started, it closes;
anything it merely borrowed, it leaves exactly as it found it. That is what
_owns_browser and _owns_page track.

Runs on the bot's own event loop via playwright.async_api; no threads.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from src.control.grammar import select_hint
from src.control.types import (
    Action,
    ActionKind,
    BrowserMode,
    ControlError,
    ControlSettings,
    Hint,
    Key,
    PageState,
)

_HINTS_JS = Path(__file__).with_name("hints.js").read_text(encoding="utf-8")
_CLEAR_JS = "() => document.getElementById('__drc_hints')?.remove()"
_SCROLL_FRACTION = 0.8  # of a viewport per scroll action

# Kinds that can start a page load: a real navigation, or a click/Enter that
# might submit a form or follow a link. These get the full settle so the
# screenshot doesn't land mid-load. Everything else -- typing, scrolling,
# non-Enter keys, a bare re-screenshot -- cannot navigate the page, so they
# use the short settle instead.
_MAY_NAVIGATE = frozenset(
    {
        ActionKind.GOTO,
        ActionKind.RELOAD,
        ActionKind.BACK,
        ActionKind.FORWARD,
        ActionKind.CLICK_HINT,
        ActionKind.ENTER,
    }
)


class BrowserService:
    def __init__(self, settings: ControlSettings) -> None:
        self._settings = settings
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._ctx: BrowserContext | None = None
        self._page: Page | None = None
        self._hints: tuple[Hint, ...] = ()
        # False when attached: a borrowed Chrome and a borrowed tab outlive the
        # session. Set by _launch/_attach, read only by close().
        self._owns_browser = True
        self._owns_page = True

    @property
    def running(self) -> bool:
        return self._page is not None and not self._page.is_closed()

    async def start(self) -> PageState:
        """Get a page ready to drive. Takes a couple of seconds."""
        if self._pw is not None:
            raise ControlError("The browser is already running.")
        self._pw = await async_playwright().start()
        try:
            match self._settings.mode:
                case BrowserMode.LAUNCH:
                    await self._launch()
                case BrowserMode.ATTACH:
                    await self._attach()
                case _:
                    raise NotImplementedError(
                        f"Unhandled browser mode: {self._settings.mode}"
                    )
        except BaseException:
            await self.close()
            raise
        return await self.capture()

    async def _launch(self) -> None:
        """Our own headless Chromium, from an empty profile."""
        assert self._pw is not None
        self._browser = await self._pw.chromium.launch(headless=True)
        # A fresh context every session: nothing stays logged in, so the panel
        # can never post authenticated pages into Discord.
        self._ctx = await self._browser.new_context(
            viewport={
                "width": self._settings.viewport_width,
                "height": self._settings.viewport_height,
            }
        )
        self._page = await self._ctx.new_page()
        self._owns_browser = self._owns_page = True
        with _as_control_error("Opening the home page"):
            await self._page.goto(
                self._settings.home_url,
                wait_until="domcontentloaded",
                timeout=self._settings.action_timeout_ms,
            )

    async def _attach(self) -> None:
        """Borrow a Chrome that is already running, over CDP.

        Adopts the tab that Chrome already has open rather than navigating to
        home_url: the whole reason to attach is that the tab in front of you is
        already signed in and where you want it. Only an empty window gets sent
        to home_url.
        """
        assert self._pw is not None
        url = self._settings.cdp_url
        try:
            self._browser = await self._pw.chromium.connect_over_cdp(url)
        except PlaywrightError as exc:
            raise ControlError(
                f"Nothing is listening at {url}. Start Chrome with `make chrome`, "
                "sign in there, then run `!control start` again."
            ) from exc
        self._owns_browser = False
        if not self._browser.contexts:
            raise ControlError(
                f"Chrome at {url} has no open window. Open one and try again."
            )
        # contexts[0] is the profile's own context, so its pages are the real,
        # signed-in tabs -- new_context() here would be a fresh incognito
        # profile and defeat the point of attaching.
        self._ctx = self._browser.contexts[0]
        existing = [page for page in self._ctx.pages if not page.is_closed()]
        if existing:
            self._page = existing[-1]
            self._owns_page = False
            await self._page.bring_to_front()
            return
        self._page = await self._ctx.new_page()
        self._owns_page = True
        with _as_control_error("Opening the home page"):
            await self._page.goto(
                self._settings.home_url,
                wait_until="domcontentloaded",
                timeout=self._settings.action_timeout_ms,
            )

    async def act(self, action: Action) -> PageState:
        """Perform one action, let the page settle, and re-capture."""
        page = self._require_page()
        with _as_control_error(f"`{action.kind}`"):
            await self._perform(page, action)
            await asyncio.sleep(self.settle_ms(action) / 1000)
            # The action may have opened a tab, or closed this one.
            self._follow_new_tabs()
            return await self._capture(self._require_page())

    def settle_ms(self, action: Action) -> int:
        """How long to let the page settle before screenshotting."""
        if action.kind in _MAY_NAVIGATE:
            return self._settings.settle_ms
        # Enter submits forms wherever it is pressed, so it navigates too.
        if action.kind is ActionKind.KEY and action.key is Key.ENTER:
            return self._settings.settle_ms
        return self._settings.quick_settle_ms

    async def capture(self) -> PageState:
        self._follow_new_tabs()
        page = self._require_page()
        with _as_control_error("Taking a screenshot"):
            return await self._capture(page)

    async def close(self) -> None:
        """Tear down whatever we started, and let go of whatever we borrowed.

        Safe to call twice, and from either the End button or the idle timer.
        """
        closeable = []
        if self._owns_page and self._page is not None:
            closeable.append(self._page)
        # A borrowed context belongs to the user's profile -- closing it would
        # shut every tab in their Chrome. Only ours is ever ours to close.
        if self._owns_browser and self._ctx is not None:
            closeable.append(self._ctx)
        # Always safe: on a CDP connection close() drops the connection rather
        # than killing the browser it is attached to.
        if self._browser is not None:
            closeable.append(self._browser)
        for target in closeable:
            try:
                await target.close()
            except PlaywrightError:
                pass  # already gone; keep unwinding the rest
        if self._pw is not None:
            await self._pw.stop()
        self._page = self._ctx = self._browser = self._pw = None
        self._owns_browser = self._owns_page = True
        self._hints = ()

    # ---- internals -------------------------------------------------------

    def _follow_new_tabs(self) -> None:
        """Move to the newest tab when one appears, or when ours goes away.

        Sign-in flows routinely open a popup or a target=_blank tab. Without
        this the panel keeps screenshotting the page underneath, so the click
        looks like it did nothing and there is no way to reach the popup.

        context.pages is in creation order, so the newest tab is simply the
        last one. A tab the user's own browser opened is never ours to close,
        hence the ownership flip.
        """
        if self._ctx is None:
            return
        live = [page for page in self._ctx.pages if not page.is_closed()]
        if not live or live[-1] is self._page:
            return
        self._page = live[-1]
        self._owns_page = False

    def _require_page(self) -> Page:
        if self._page is None or self._page.is_closed():
            raise ControlError(
                "The browser is not running. Run `!control start` again."
            )
        return self._page

    async def _perform(self, page: Page, action: Action) -> None:
        timeout = self._settings.action_timeout_ms
        scroll = int(self._settings.viewport_height * _SCROLL_FRACTION)
        match action.kind:
            case ActionKind.CLICK_HINT:
                await self._click_hint(page, action)
            case ActionKind.GOTO:
                await page.goto(
                    action.text, wait_until="domcontentloaded", timeout=timeout
                )
            case ActionKind.TYPE:
                await page.keyboard.type(action.text, delay=15)
            case ActionKind.KEY:
                await page.keyboard.press(action.key.value)
            case ActionKind.ENTER:
                await page.keyboard.press("Enter")
            case ActionKind.BACK:
                await page.go_back(timeout=timeout)
            case ActionKind.FORWARD:
                await page.go_forward(timeout=timeout)
            case ActionKind.RELOAD:
                await page.reload(timeout=timeout)
            case ActionKind.SCROLL_UP:
                await page.mouse.wheel(0, -scroll)
            case ActionKind.SCROLL_DOWN:
                await page.mouse.wheel(0, scroll)
            case ActionKind.REFRESH:
                pass  # nothing to do; the capture below is the point
            case ActionKind.END:
                raise NotImplementedError(
                    "END is handled by ControlManager, not the browser."
                )
            case _:
                raise NotImplementedError(f"Unhandled action kind: {action.kind}")

    async def _click_hint(self, page: Page, action: Action) -> None:
        hint = select_hint(self._hints, action.hint)
        try:
            await page.click(
                f'[data-drc-hint="{hint.index}"]',
                timeout=self._settings.action_timeout_ms,
            )
        except PlaywrightTimeoutError as exc:
            raise ControlError(
                f"Hint {hint.index} (`{hint.label}`) did not take the click -- "
                "the page has probably changed. Hit Re-screenshot."
            ) from exc

    async def _capture(self, page: Page) -> PageState:
        """Draw badges, shoot the viewport, then clean the page back up."""
        raw = await page.evaluate(_HINTS_JS, self._settings.max_hints)
        # Viewport only: hints are viewport-relative, so a full-page shot would
        # put every badge in the wrong place.
        png = await page.screenshot(type="png")
        await page.evaluate(_CLEAR_JS)
        self._hints = tuple(Hint(**record) for record in raw)
        return PageState(
            url=page.url,
            title=await page.title(),
            hints=self._hints,
            png=png,
        )


@contextmanager
def _as_control_error(what: str) -> Iterator[None]:
    """Turn Playwright's failures into something the panel can show.

    Only Playwright's own exceptions are translated; anything else is a bug and
    is left to propagate.
    """
    try:
        yield
    except ControlError:
        raise  # already user-facing (e.g. a stale hint)
    except PlaywrightTimeoutError as exc:
        raise ControlError(f"{what} timed out.") from exc
    except PlaywrightError as exc:
        raise ControlError(f"{what} failed: {exc.message.splitlines()[0]}") from exc
