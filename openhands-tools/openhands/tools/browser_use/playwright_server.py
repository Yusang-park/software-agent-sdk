from __future__ import annotations

import base64
import fnmatch
import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, cast
from urllib.parse import urlparse
from uuid import uuid4

from playwright.async_api import (
    Browser,
    BrowserContext,
    CDPSession,
    Error as PlaywrightError,
    FloatRect,
    Locator,
    Page,
    Playwright,
    Request,
    Route,
    StorageState,
    TimeoutError as PlaywrightTimeoutError,
    ViewportSize,
    async_playwright,
)

from openhands.tools.browser_use.recording import RecordingSession
from openhands.tools.browser_use.screencast import ScreencastSession
from openhands.tools.browser_use.semantic import FIND_VISIBLE_TEXT_SCRIPT


_INDEX_ATTRIBUTE = "data-oh-browser-index"
_STATE_SCRIPT = r"""
() => {
  const INDEX = 'data-oh-browser-index';
  const LIMIT = 100;
  const rendered = (element) => {
    if (element.getClientRects().length === 0) return false;
    for (let node = element; node; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (style.visibility === 'hidden' || style.display === 'none') return false;
      if (Number.parseFloat(style.opacity || '1') <= 0.05) return false;
    }
    return true;
  };
  document.querySelectorAll(`[${INDEX}]`).forEach((element) => {
    element.removeAttribute(INDEX);
  });
  const selector = [
    'a[href]', 'button', 'input', 'textarea', 'select',
    '[role="button"]', '[role="link"]', '[tabindex]'
  ].join(',');
  const candidates = Array.from(document.querySelectorAll(selector))
    .filter((element) => {
      const rect = element.getBoundingClientRect();
      return rendered(element) && rect.width > 0 && rect.height > 0;
    }).slice(0, LIMIT);
  const interactive = candidates.map((element, index) => {
    element.setAttribute(INDEX, String(index));
    const rect = element.getBoundingClientRect();
    return {
      index,
      tag: element.tagName.toLowerCase(),
      role: (element.getAttribute('role') || '').slice(0, 80),
      type: (element.getAttribute('type') || '').slice(0, 80),
      name: (element.getAttribute('aria-label') ||
        element.getAttribute('placeholder') || '').slice(0, 240),
      text: (element.innerText || element.value || '')
        .trim().replace(/\s+/g, ' ').slice(0, 240),
      disabled: Boolean(element.disabled),
      x: Math.round(rect.x),
      y: Math.round(rect.y),
    };
  });
  const outlineSelector = [
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6', '[role="heading"]',
    'main', 'nav', 'aside', 'section', 'article', 'form',
    '[role="main"]', '[role="navigation"]', '[role="complementary"]',
    '[role="region"]', '[role="form"]'
  ].join(',');
  const outline = [];
  for (const element of document.querySelectorAll(outlineSelector)) {
    if (!rendered(element)) continue;
    const rect = element.getBoundingClientRect();
    const tag = element.tagName.toLowerCase();
    const role = (element.getAttribute('role') || tag).toLowerCase();
    const stableId = (element.id || '').slice(0, 120);
    const name = (element.getAttribute('aria-label') || element.innerText || '')
      .trim().replace(/\s+/g, ' ').slice(0, 160);
    if (!name && !stableId && !['main', 'nav', 'aside'].includes(tag)) continue;
    outline.push({
      kind: /^h[1-6]$/.test(tag) || role === 'heading' ? 'heading' : 'landmark',
      tag,
      role: tag === 'nav' && role === 'nav' ? 'navigation' : role,
      name: name || stableId || role,
      id: stableId,
      y: Math.round(rect.top + scrollY),
      location: rect.bottom < 0
        ? 'above' : rect.top > innerHeight ? 'below' : 'viewport',
    });
    if (outline.length === 80) break;
  }
  const root = document.documentElement;
  const body = document.body;
  const pageWidth = Math.max(root.scrollWidth, body ? body.scrollWidth : 0);
  const pageHeight = Math.max(root.scrollHeight, body ? body.scrollHeight : 0);
  const below = Math.max(pageHeight - (scrollY + innerHeight), 0);
  return {
    url: location.href,
    title: document.title,
    tabs: [],
    interactive_elements: interactive,
    viewport: {width: innerWidth, height: innerHeight},
    page: {width: pageWidth, height: pageHeight},
    scroll: {x: scrollX, y: scrollY},
    pages_above: innerHeight ? Math.round(scrollY / innerHeight * 10) / 10 : 0,
    pages_below: innerHeight ? Math.round(below / innerHeight * 10) / 10 : 0,
    semantic_outline: {
      items: outline,
      total: outline.length,
      truncated: outline.length === 80,
    },
  };
}
"""


# The element a capture is of: the section that owns the text the caller named.
# Playwright's element screenshot is what kevin-slack-bot's `screenshot.py`
# does with `locator.screenshot()`, and it is what makes a picture of one
# section a picture of that section -- a viewport frame shows whatever the
# page had at that scroll position, and on Pilot 007f2c76 (2026-09-10) that
# was the lookalike card above the one the check named.
_CAPTURE_TARGET_SCRIPT = r"""
(wanted) => {
  const rendered = (element) => {
    if (element.getClientRects().length === 0) return false;
    for (let node = element; node; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (style.visibility === 'hidden' || style.display === 'none') return false;
    }
    return true;
  };
  const byId = document.getElementById(wanted);
  let match = byId && rendered(byId) ? byId : null;
  if (!match) {
    const needle = wanted.toLowerCase();
    const holders = Array.from(document.querySelectorAll('body *')).filter(
      (element) => rendered(element)
        && (element.innerText || '').toLowerCase().includes(needle)
    );
    // The deepest holders: those none of whose descendants also hold the
    // text; among them the one whose own text is shortest -- the label
    // itself rather than a paragraph that mentions it.
    const deepest = holders.filter(
      (element) => !holders.some(
        (other) => other !== element && element.contains(other)
      )
    );
    deepest.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
    match = deepest[0] || null;
  }
  if (!match) return null;
  const SECTION = [
    'section', 'article', 'aside', 'form', 'li', 'fieldset', 'table',
    '[role="region"]', '[role="article"]', '[role="complementary"]',
    '[role="group"]', '[role="dialog"]', '[data-testid]'
  ].join(',');
  const tooTall = Math.max(innerHeight * 3, 1200);
  // Climb from the text to the section that owns it: the nearest ancestor
  // that is a section-shaped element and not most of the page.
  let node = match;
  let chosen = null;
  while (node && node !== document.body && node.tagName !== 'MAIN') {
    const height = node.getBoundingClientRect().height;
    if (height > tooTall) break;
    if (node !== match && node.matches(SECTION) && height >= 40) {
      chosen = node;
      break;
    }
    node = node.parentElement;
  }
  if (!chosen) {
    // No section-shaped ancestor: the tallest ancestor that still fits a
    // few screens, so the picture is the block around the text and not a
    // single line of it.
    node = match;
    chosen = match;
    while (node && node !== document.body && node.tagName !== 'MAIN') {
      if (node.getBoundingClientRect().height > tooTall) break;
      chosen = node;
      node = node.parentElement;
    }
  }
  // The card around the section, when there is one: the nearest ancestor
  // that paints itself -- a border, a shadow, a rounded corner, a background
  // of its own -- and is not much bigger than the section. A section is
  // usually the content of a panel, and a picture of the content alone
  // shows none of the panel (Pilot 4625ca37, 2026-09-11: the Noteworthy
  // Insights section came back without the card that frames it).
  const paints = (element) => {
    const style = getComputedStyle(element);
    if (style.boxShadow && style.boxShadow !== 'none') return true;
    if (parseFloat(style.borderTopWidth) > 0 || parseFloat(style.borderLeftWidth) > 0) {
      return true;
    }
    if (parseFloat(style.borderTopLeftRadius) > 0) return true;
    const background = style.backgroundColor;
    if (!background || background === 'transparent') return false;
    const parent = element.parentElement;
    const parentBackground = parent ? getComputedStyle(parent).backgroundColor : '';
    return background !== 'rgba(0, 0, 0, 0)' && background !== parentBackground;
  };
  const sectionHeight = chosen.getBoundingClientRect().height;
  for (let node = chosen.parentElement; node && node !== document.body
       && node.tagName !== 'MAIN'; node = node.parentElement) {
    const height = node.getBoundingClientRect().height;
    if (height > tooTall || height > sectionHeight * 1.6 + 240) break;
    if (paints(node)) { chosen = node; break; }
  }
  // The frame around the card, when there is one: an ancestor that hugs the
  // chosen element by no more than padding on every side. A page column, a
  // grid cell or the content area is never that close, so it can never be
  // picked; a wrapper holding the card plus a header or a sibling is bigger
  // than the bound and is skipped, which is right -- it is not the frame.
  const HUG = 48;
  for (let level = 0; level < 3; level += 1) {
    const parent = chosen.parentElement;
    if (!parent || parent === document.body || parent.tagName === 'MAIN') break;
    const inner = chosen.getBoundingClientRect();
    const outer = parent.getBoundingClientRect();
    const gaps = [inner.left - outer.left, outer.right - inner.right,
                  inner.top - outer.top, outer.bottom - inner.bottom];
    // Hugs: no side further than padding. Adds: at least one side further
    // than the card itself, or the wrapper is the same box under another
    // name and there is nothing to gain by taking it.
    const hugs = gaps.every((gap) => gap <= HUG);
    const adds = gaps.some((gap) => gap > 0.5);
    if (!hugs || !adds) break;
    chosen = parent;
  }
  // A section shorter than the viewport is centred, a taller one starts at
  // the top; fixed and sticky elements over it are hidden for the picture
  // by `_HIDE_OVERLAYS_SCRIPT`.
  const fits = chosen.getBoundingClientRect().height < innerHeight;
  chosen.scrollIntoView({block: fits ? 'center' : 'start', inline: 'nearest'});
  return chosen;
}
"""

# An element screenshot is the element's box *as painted*, so a fixed or sticky
# element outside it -- a site header, a cookie bar -- lands inside the picture.
# Scrolling the section to the top of the viewport puts its top edge exactly
# under a fixed header: on Pilot cef12908 (2026-09-10) the Noteworthy Insights
# section was photographed with the site's header over its title and date
# range. Such elements are hidden for the capture and restored after it; one
# inside the section, or one that contains it, is part of what was asked for.
_HIDE_OVERLAYS_SCRIPT = r"""
(target) => {
  let hidden = 0;
  for (const node of document.querySelectorAll('body *')) {
    if (node.contains(target) || target.contains(node)) continue;
    const position = getComputedStyle(node).position;
    if (position !== 'fixed' && position !== 'sticky') continue;
    node.dataset.ohCaptureVisibility = node.style.visibility;
    node.style.visibility = 'hidden';
    hidden += 1;
  }
  return hidden;
}
"""

_RESTORE_OVERLAYS_SCRIPT = r"""
() => {
  for (const node of document.querySelectorAll('[data-oh-capture-visibility]')) {
    node.style.visibility = node.dataset.ohCaptureVisibility;
    delete node.dataset.ohCaptureVisibility;
  }
}
"""


# How long a capture waits for a section that is still mounting: four looks a
# half second apart, two seconds in all.
CAPTURE_ELEMENT_ATTEMPTS = 4
CAPTURE_ELEMENT_RETRY_MS = 500
# The page around the captured element, in CSS pixels on every side. An element
# screenshot is clipped to the element's box, so a white card on a white
# page reads as bare content -- its corners, shadow and margin are outside the
# box (Pilot d5c378d9, 2026-09-11: the Noteworthy Insights panel came back as
# text on white). A margin shows the card as a card.
CAPTURE_ELEMENT_MARGIN_PX = 16
# When nothing on the page shows the text, the page is walked to the bottom a
# screen at a time so anything deferred mounts, looking again after each
# step. On Pilot 4625ca37 (2026-09-11) three of four captures of a section
# that mounts on scroll answered "no element shows", and the run spent
# twenty calls scrolling and reading source between them.
CAPTURE_ELEMENT_WALK_STEPS = 40
CAPTURE_ELEMENT_WALK_SETTLE_MS = 250
_MOUNT_WALK_SCRIPT = """
() => {
  scrollBy(0, Math.round(innerHeight * 0.9));
  return scrollY + innerHeight >= document.documentElement.scrollHeight - 2;
}
"""


class PlaywrightBrowserServer:
    """One persistent Playwright Chromium session shared by browser tools."""

    def __init__(self, session_timeout_minutes: int = 30) -> None:
        self.session_timeout_minutes = session_timeout_minutes
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._pages: dict[str, Page] = {}
        self._allowed_domains: tuple[str, ...] = ()
        self._navigation_policy: Callable[[str], Awaitable[None]] | None = None
        self._sensitive_values: tuple[str, ...] = ()
        self._inject_scripts: list[str] = []
        self._cdp_session: CDPSession | None = None
        self._cdp_page: Page | None = None
        self._recording_session: RecordingSession | None = None
        self._screencast_session: ScreencastSession | None = None
        self._screencast_request: tuple[Any, dict[str, Any]] | None = None

    @property
    def is_live(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    @property
    def _is_recording(self) -> bool:
        return bool(self._recording_session and self._recording_session.is_active)

    @property
    def browser_session(self) -> PlaywrightBrowserServer:
        return self

    async def start(
        self,
        *,
        headless: bool,
        executable_path: str,
        chromium_sandbox: bool = False,
        window_size: ViewportSize | None = None,
        allowed_domains: list[str] | None = None,
        navigation_policy: Callable[[str], Awaitable[None]] | None = None,
        **_: Any,
    ) -> None:
        if self.is_live:
            return
        self._playwright = await async_playwright().start()
        self._allowed_domains = tuple(allowed_domains or ())
        self._navigation_policy = navigation_policy
        launch_args = ["--disable-dev-shm-usage"]
        if window_size is not None:
            launch_args.append(
                f"--window-size={window_size['width']},{window_size['height']}"
            )
        self._browser = await self._playwright.chromium.launch(
            headless=headless,
            executable_path=executable_path,
            chromium_sandbox=chromium_sandbox,
            args=launch_args,
        )
        self._context = await self._browser.new_context(
            viewport=window_size or {"width": 1280, "height": 800}
        )
        for script in self._inject_scripts:
            await self._context.add_init_script(script=script)
        if self._allowed_domains or self._navigation_policy is not None:
            await self._context.route("**/*", self._guard_route)
        self._context.on("page", self._register_page)
        self._page = await self._context.new_page()
        self._register_page(self._page)

    def set_sensitive_values(self, values: Sequence[str]) -> None:
        """Register cumulative in-memory redactions for this browser session."""
        self._sensitive_values = tuple(
            sorted(
                set(self._sensitive_values).union(value for value in values if value),
                key=len,
                reverse=True,
            )
        )

    def mask_sensitive_text(self, text: str) -> str:
        for value in self._sensitive_values:
            text = text.replace(value, "<secret>")
        return text

    def _mask_sensitive_state(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.mask_sensitive_text(value)
        if isinstance(value, dict):
            return {
                key: self._mask_sensitive_state(item) for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._mask_sensitive_state(item) for item in value]
        return value

    async def _screenshot_masks(self, page: Page) -> list[Locator]:
        elements = page.locator("body, body *")
        contents = await elements.evaluate_all(
            """(elements) => elements.map(element => [
                typeof element.value === 'string' ? element.value : '',
                ...Array.from(element.childNodes)
                  .filter(node => node.nodeType === Node.TEXT_NODE)
                  .map(node => node.textContent || '')
            ])"""
        )
        return [
            elements.nth(index)
            for index, parts in enumerate(contents)
            if any(
                secret in part for secret in self._sensitive_values for part in parts
            )
        ]

    async def browser_metadata(self) -> dict[str, str]:
        """Read the active page identity and at most 12,000 rendered characters."""
        page = self._require_page()
        return {
            "url": self.mask_sensitive_text(page.url),
            "title": self.mask_sensitive_text(await page.title()),
            "text": self.mask_sensitive_text(
                await page.locator("body").inner_text(timeout=5000)
            )[:12000],
        }

    async def navigate(self, url: str, new_tab: bool = False) -> str:
        await self._validate_navigation(url)
        page = await self._new_page() if new_tab else self._require_page()
        await page.goto(url, wait_until="domcontentloaded")
        await self._wait_for_meaningful_page(page)
        return f"Navigated to {url}"

    async def go_back(self) -> str:
        page = self._require_page()
        await page.go_back(wait_until="domcontentloaded")
        return f"Navigated back to {page.url}"

    async def get_browser_state(self, include_screenshot: bool = False) -> str:
        page = self._require_page()
        state = await page.evaluate(_STATE_SCRIPT)
        if not isinstance(state, dict):
            raise RuntimeError("Browser state response was invalid")
        state = self._mask_sensitive_state(state)
        if include_screenshot:
            if self._sensitive_values:
                screenshot = await page.screenshot(
                    type="jpeg",
                    quality=75,
                    mask=await self._screenshot_masks(page),
                    mask_color="#000000",
                )
            else:
                screenshot = await page.screenshot(type="jpeg", quality=75)
            state["screenshot"] = base64.b64encode(screenshot).decode()
        return json.dumps(state, indent=2)

    async def click(self, index: int, new_tab: bool = False) -> str:
        locator = self._indexed_locator(index)
        box = await locator.bounding_box()
        if new_tab:
            page = self._require_page()
            try:
                async with page.expect_popup(timeout=2000) as popup:
                    await locator.click()
                await self._activate_page(await popup.value)
            except PlaywrightTimeoutError:
                # The click already happened. A target that chose same-tab
                # navigation is still a successful click.
                pass
        else:
            await locator.click()
        await self._wait_for_meaningful_page(self._require_page())
        if box is not None and self._screencast_session is not None:
            self._screencast_session.notify_agent_cursor(
                box["x"] + box["width"] / 2,
                box["y"] + box["height"] / 2,
                "mouseReleased",
            )
        return f"Clicked element {index}"

    async def _wait_for_meaningful_page(self, page: Page) -> None:
        try:
            await page.wait_for_function(
                """
                () => Boolean(
                  document.body?.innerText.trim() ||
                  document.querySelector(
                    'a[href], button, input, textarea, select, canvas, [role]'
                  )
                )
                """,
                timeout=2000,
            )
        except PlaywrightTimeoutError:
            pass

    async def type_text(self, index: int, text: str, *, secret: bool = False) -> str:
        locator = self._indexed_locator(index)
        if secret:
            self.set_sensitive_values([text])
        await locator.fill(text)
        value = "<secret>" if secret else repr(text)
        return f"Typed {value} into element {index}"

    async def scroll(self, direction: str = "down") -> str:
        page = self._require_page()
        viewport = page.viewport_size or {"width": 1280, "height": 800}
        if direction not in {"up", "down"}:
            raise ValueError("Scroll direction must be 'up' or 'down'")
        delta = viewport["height"] * (1 if direction == "down" else -1)
        await page.mouse.wheel(0, delta)
        return f"Scrolled {direction}"

    async def scroll_to_text(self, text: str) -> str:
        page = self._require_page()
        found = await self._scroll_to_text_once(page, text)
        if not found:
            # Not on the page yet: walk it a screen at a time so a deferred
            # section mounts, and look again after each step.
            for _ in range(CAPTURE_ELEMENT_WALK_STEPS):
                at_bottom = await page.evaluate(_MOUNT_WALK_SCRIPT)
                await page.wait_for_timeout(CAPTURE_ELEMENT_WALK_SETTLE_MS)
                found = await self._scroll_to_text_once(page, text)
                if found or at_bottom:
                    break
        if not found:
            return (
                f"No element on the page shows {text!r}. It may not have loaded "
                "yet, may be behind a tab, or may be on another page. Read "
                "browser_get_content before concluding it is absent."
            )
        return f"Scrolled to {found!r}"

    async def _scroll_to_text_once(self, page: Page, text: str):
        return await page.evaluate(
            """
            (wanted) => {
              const exactId = document.getElementById(wanted);
              const rendered = (element) => {
                if (element.getClientRects().length === 0) return false;
                for (let node = element; node; node = node.parentElement) {
                  const style = getComputedStyle(node);
                  if (style.visibility === 'hidden' || style.display === 'none') {
                    return false;
                  }
                }
                return true;
              };
              const needle = wanted.toLowerCase();
              const holders = exactId ? [exactId] : Array.from(
                document.querySelectorAll('body *')
              ).filter((element) =>
                rendered(element)
                && (element.innerText || '').toLowerCase().includes(needle)
              );
              // Every ancestor of the element that shows the text also
              // "shows" it, and document order lists ancestors first -- so
              // the first match used to be the page's outermost container,
              // and scrolling to it jumped to the top of the page. The
              // target is the deepest holder, and among those the one whose
              // own text is shortest: the label itself, not a paragraph that
              // happens to mention it.
              const deepest = holders.filter(
                (element) => !holders.some(
                  (other) => other !== element && element.contains(other)
                )
              );
              deepest.sort((a, b) =>
                (a.innerText || '').length - (b.innerText || '').length
              );
              const target = deepest[0];
              if (!target) return false;
              target.scrollIntoView({block: 'center', inline: 'nearest'});
              return (target.innerText || '').trim() || target.id || wanted;
            }
            """,
            text,
        )

    async def find_visible_text(self, text: str, max_results: int = 10) -> str:
        page = self._require_page()
        result = await page.evaluate(
            FIND_VISIBLE_TEXT_SCRIPT, {"needle": text, "limit": max_results}
        )
        return json.dumps(result, indent=2)

    async def set_viewport(self, width: int, height: int) -> str:
        page = self._require_page()
        await page.set_viewport_size({"width": width, "height": height})
        return f"Viewport set to {width}x{height}"

    async def capture_element(self, text: str) -> str:
        """A picture of the one section that shows `text`, as JSON with the
        page address, the section's own text, and the element screenshot."""
        page = self._require_page()
        element = None
        # A section a scroll just brought into view may still be mounting:
        # on Pilot cef12908 (2026-09-10) the capture ran nine seconds after
        # the scroll, the section was on screen for the person watching, and
        # the DOM had no "Noteworthy Insights" yet. A few short waits cover
        # a mount without turning a real absence into a long stall.
        for attempt in range(CAPTURE_ELEMENT_ATTEMPTS):
            handle = await page.evaluate_handle(_CAPTURE_TARGET_SCRIPT, text)
            element = handle.as_element()
            if element is not None:
                break
            if attempt + 1 < CAPTURE_ELEMENT_ATTEMPTS:
                await page.wait_for_timeout(CAPTURE_ELEMENT_RETRY_MS)
        if element is None:
            # Not on the page yet: walk it, so a deferred section mounts.
            for _ in range(CAPTURE_ELEMENT_WALK_STEPS):
                at_bottom = await page.evaluate(_MOUNT_WALK_SCRIPT)
                await page.wait_for_timeout(CAPTURE_ELEMENT_WALK_SETTLE_MS)
                handle = await page.evaluate_handle(_CAPTURE_TARGET_SCRIPT, text)
                element = handle.as_element()
                if element is not None or at_bottom:
                    break
        if element is None:
            return json.dumps(
                {
                    "url": page.url,
                    "title": await page.title(),
                    "captured": None,
                    "error": (
                        f"No element on the page shows {text!r}, so nothing was "
                        "captured. It may not have loaded yet, may be behind a "
                        "tab, or may be on another page. Read browser_get_state "
                        "or browser_find before concluding it is absent."
                    ),
                },
                indent=2,
            )
        summary = await element.evaluate(
            r"""(element) => ({
              tag: element.tagName.toLowerCase(),
              id: (element.id || '').slice(0, 120),
              top: Math.round(element.getBoundingClientRect().top),
              text: (element.innerText || '').trim()
                .replace(/\s+/g, ' ').slice(0, 4000),
            })"""
        )
        box = await element.bounding_box()
        options: dict[str, Any] = {"type": "jpeg", "quality": 80}
        if self._sensitive_values:
            options["mask"] = await self._screenshot_masks(page)
            options["mask_color"] = "#000000"
        await element.evaluate(_HIDE_OVERLAYS_SCRIPT)
        try:
            if box:
                # The element's box plus a margin, so the picture shows the
                # card and the page around it. Taken from the viewport when
                # it fits: a full-page capture re-lays the page out at its
                # full height, and anything sized in `vh` moves everything
                # below it -- on Pilot 83f7b5a0 (2026-09-11) the clip meant
                # for the Noteworthy Insights card came back as the Event
                # Analyzer section beneath it. Only a card taller than the
                # viewport is taken from the full page, in document
                # coordinates.
                margin = CAPTURE_ELEMENT_MARGIN_PX
                viewport = page.viewport_size or {"width": 0, "height": 0}
                fits = (
                    viewport["height"] > 0
                    and box["height"] + 2 * margin <= viewport["height"]
                )
                clip: FloatRect
                if fits:
                    x = max(0.0, box["x"] - margin)
                    y = max(0.0, box["y"] - margin)
                    clip = {
                        "x": x,
                        "y": y,
                        "width": min(box["width"] + 2 * margin, viewport["width"] - x),
                        "height": min(
                            box["height"] + 2 * margin, viewport["height"] - y
                        ),
                    }
                    screenshot = await page.screenshot(clip=clip, **options)
                else:
                    scroll_x, scroll_y = await page.evaluate("() => [scrollX, scrollY]")
                    clip = {
                        "x": max(0.0, box["x"] + scroll_x - margin),
                        "y": max(0.0, box["y"] + scroll_y - margin),
                        "width": box["width"] + 2 * margin,
                        "height": box["height"] + 2 * margin,
                    }
                    screenshot = await page.screenshot(
                        full_page=True, clip=clip, **options
                    )
            else:
                screenshot = await element.screenshot(**options)
        finally:
            await page.evaluate(_RESTORE_OVERLAYS_SCRIPT)
        captured = dict(summary if isinstance(summary, dict) else {})
        if box:
            captured["box"] = {
                "x": round(box["x"]),
                "y": round(box["y"]),
                "width": round(box["width"]),
                "height": round(box["height"]),
            }
        return json.dumps(
            {
                "url": page.url,
                "title": await page.title(),
                "text": captured.pop("text", ""),
                "captured": captured,
                "screenshot": base64.b64encode(screenshot).decode(),
            },
            indent=2,
        )

    async def get_storage(self) -> str:
        context = self._require_context()
        state = cast(dict[str, Any], await context.storage_state(indexed_db=True))
        page = self._require_page()
        try:
            origin, session_storage = await page.evaluate(
                """
                () => [location.origin, Object.entries(sessionStorage).map(
                  ([name, value]) => ({name, value})
                )]
                """
            )
        except PlaywrightError:
            return json.dumps(state, indent=2)
        origins = state.setdefault("origins", [])
        stored_origin = next(
            (candidate for candidate in origins if candidate.get("origin") == origin),
            None,
        )
        if stored_origin is None:
            stored_origin = {"origin": origin, "localStorage": []}
            origins.append(stored_origin)
        stored_origin["sessionStorage"] = session_storage
        return json.dumps(state, indent=2)

    async def set_storage(self, storage_state: dict[str, Any]) -> str:
        context = self._require_context()
        playwright_state = cast(
            StorageState,
            {
                "cookies": storage_state.get("cookies", []),
                "origins": [
                    {
                        "origin": origin["origin"],
                        "localStorage": origin.get("localStorage", []),
                    }
                    for origin in storage_state.get("origins", [])
                    if origin.get("origin")
                ],
            },
        )
        await context.set_storage_state(playwright_state)
        page = self._require_page()
        current_origin = await page.evaluate("location.origin")
        for origin in storage_state.get("origins", []):
            if origin.get("origin") != current_origin:
                continue
            await page.evaluate(
                """
                (items) => {
                  sessionStorage.clear();
                  for (const item of items) {
                    sessionStorage.setItem(item.name || item.key, item.value);
                  }
                }
                """,
                origin.get("sessionStorage", []),
            )
        return "Browser storage updated successfully"

    async def get_current_page(self) -> Page:
        return self._require_page()

    async def list_tabs(self) -> str:
        self._sync_pages()
        tabs = [
            {"id": tab_id, "url": page.url, "active": page is self._page}
            for tab_id, page in self._pages.items()
        ]
        return json.dumps(tabs, indent=2)

    async def switch_tab(self, tab_id: str) -> str:
        self._sync_pages()
        page = self._pages.get(tab_id)
        if page is None:
            raise ValueError(f"Tab {tab_id!r} was not found")
        await self._activate_page(page)
        await page.bring_to_front()
        return f"Switched to tab {tab_id}"

    async def close_tab(self, tab_id: str) -> str:
        self._sync_pages()
        page = self._pages.get(tab_id)
        if page is None:
            raise ValueError(f"Tab {tab_id!r} was not found")
        await page.close()
        self._pages.pop(tab_id, None)
        if page is self._page:
            next_page = next(iter(self._pages.values()), None)
            if next_page is not None:
                await self._activate_page(next_page)
            else:
                self._page = None
        return f"Closed tab {tab_id}"

    async def get_content(self, extract_links: bool, start_from_char: int) -> str:
        page = self._require_page()
        content = await page.locator("body").inner_text()
        if extract_links:
            links = await page.locator("a[href]").evaluate_all(
                r"""
                (elements) => elements.slice(0, 200).map((element) => ({
                  text: (element.innerText || '').trim().replace(/\s+/g, ' '),
                  href: element.href,
                }))
                """
            )
            if links:
                rendered = "\n".join(
                    f"- [{link['text'] or link['href']}]({link['href']})"
                    for link in links
                )
                content = f"{content}\n\nLinks:\n{rendered}"
        if start_from_char >= len(content) and content:
            return (
                f"start_from_char ({start_from_char}) exceeds content length "
                f"({len(content)})."
            )
        limit = 30_000
        end = min(start_from_char + limit, len(content))
        chunk = content[start_from_char:end]
        continuation = (
            f" Truncated; use start_from_char={end} to continue."
            if end < len(content)
            else ""
        )
        return (
            f"<url>\n{page.url}\n</url>\n"
            f"<content_stats>\nVisible text characters: {len(content)}."
            f"{continuation}\n</content_stats>\n"
            f"<webpage_content>\n{chunk}\n</webpage_content>"
        )

    def set_inject_scripts(self, scripts: list[str]) -> None:
        self._inject_scripts = list(scripts)

    async def inject_scripts(self) -> None:
        context = self._require_context()
        for script in self._inject_scripts:
            await context.add_init_script(script=script)

    async def cdp_session(self) -> CDPSession:
        page = self._require_page()
        if self._cdp_session is None or self._cdp_page is not page:
            self._cdp_session = await self._require_context().new_cdp_session(page)
            self._cdp_page = page
        return self._cdp_session

    async def start_recording(self, output_dir: str | None = None) -> str:
        if self._recording_session is None:
            self._recording_session = RecordingSession(output_dir=output_dir)
        return await self._recording_session.start(
            self._require_context(), self._require_page
        )

    async def stop_recording(self) -> str:
        if self._recording_session is None:
            return "Error: Not recording. Call browser_start_recording first."
        result = await self._recording_session.stop()
        self._recording_session.reset()
        return result

    async def flush_recording_events(self) -> int:
        if self._recording_session is None:
            return 0
        return await self._recording_session.flush_events()

    async def restart_recording_on_new_page(self) -> None:
        if self._recording_session is not None:
            await self._recording_session.restart_on_new_page()

    async def start_screencast(self, on_frame, **kwargs: Any) -> bool:
        self._screencast_request = (on_frame, dict(kwargs))
        if self._screencast_session is not None:
            await self._screencast_session.stop()
        self._screencast_session = ScreencastSession()
        return await self._screencast_session.start(
            await self.cdp_session(), on_frame, **kwargs
        )

    async def stop_screencast(self, *, preserve_request: bool = False) -> bool:
        if not preserve_request:
            self._screencast_request = None
        if self._screencast_session is None:
            return True
        result = await self._screencast_session.stop()
        self._screencast_session = None
        return result

    async def dispatch_screencast_mouse(self, **kwargs: Any) -> None:
        if self._screencast_session is not None:
            await self._screencast_session.dispatch_mouse(**kwargs)

    async def dispatch_screencast_key(self, **kwargs: Any) -> None:
        if self._screencast_session is not None:
            await self._screencast_session.dispatch_key(**kwargs)

    async def close(self) -> None:
        await self.stop_screencast()
        if self._recording_session is not None and self._recording_session.is_active:
            await self._recording_session.stop()
        self._recording_session = None
        context, browser, playwright = self._context, self._browser, self._playwright
        self._page = None
        self._pages.clear()
        self._cdp_session = None
        self._cdp_page = None
        self._context = None
        self._browser = None
        self._playwright = None
        if context is not None:
            await context.close()
        if browser is not None:
            await browser.close()
        if playwright is not None:
            await playwright.stop()

    def _require_page(self) -> Page:
        if self._page is None or self._page.is_closed():
            raise RuntimeError("Browser session is not initialized")
        return self._page

    def _require_context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("Browser session is not initialized")
        return self._context

    def _indexed_locator(self, index: int):
        page = self._require_page()
        locator = page.locator(f'[{_INDEX_ATTRIBUTE}="{index}"]')
        return locator

    async def _new_page(self) -> Page:
        page = await self._require_context().new_page()
        await self._activate_page(page)
        return page

    async def _activate_page(self, page: Page) -> None:
        if page is self._page:
            return
        request = self._screencast_request
        if self._screencast_session is not None:
            await self.stop_screencast(preserve_request=True)
        if self._cdp_session is not None:
            try:
                await self._cdp_session.detach()
            except PlaywrightError:
                pass
        self._cdp_session = None
        self._cdp_page = None
        self._page = page
        self._register_page(page)
        if request is not None:
            await self.start_screencast(request[0], **request[1])

    def _register_page(self, page: Page) -> None:
        if any(candidate is page for candidate in self._pages.values()):
            return
        self._pages[f"tab-{uuid4().hex[:12]}"] = page

    def _sync_pages(self) -> None:
        context = self._require_context()
        live_pages = [page for page in context.pages if not page.is_closed()]
        self._pages = {
            tab_id: page
            for tab_id, page in self._pages.items()
            if any(candidate is page for candidate in live_pages)
        }
        for page in live_pages:
            self._register_page(page)

    def _validate_url(self, url: str) -> None:
        if not self._allowed_domains:
            return
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        if any(
            fnmatch.fnmatch(hostname, pattern) or hostname == pattern.removeprefix("*.")
            for pattern in self._allowed_domains
        ):
            return
        raise ValueError(f"Navigation to {hostname!r} is not allowed")

    async def _validate_navigation(self, url: str) -> None:
        self._validate_url(url)
        if self._navigation_policy is not None:
            await self._navigation_policy(url)

    async def _guard_route(self, route: Route, request: Request) -> None:
        if request.is_navigation_request() and request.frame.parent_frame is None:
            try:
                await self._validate_navigation(request.url)
            except Exception:
                await route.abort("blockedbyclient")
                return
        await route.continue_()
