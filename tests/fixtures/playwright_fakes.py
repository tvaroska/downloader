"""Shared Playwright page/context/pool fakes for unit tests without Chromium (`R111`)."""

from __future__ import annotations

from typing import Any


class FakeLocator:
    """Minimal fake for Playwright `Locator`."""

    def __init__(self, calls: list[tuple[Any, ...]]) -> None:
        self._calls = calls

    @property
    def first(self) -> FakeLocator:
        return self

    async def scroll_into_view_if_needed(self, *, timeout: int = 5000) -> None:
        self._calls.append(("scroll_into_view", timeout))


class FakeKeyboard:
    """Minimal fake for Playwright `page.keyboard`."""

    def __init__(self, calls: list[tuple[Any, ...]]) -> None:
        self._calls = calls

    async def press(self, key: str) -> None:
        self._calls.append(("keyboard.press", key))


class FakeMouse:
    """Minimal fake for Playwright `page.mouse`."""

    def __init__(self, calls: list[tuple[Any, ...]]) -> None:
        self._calls = calls

    async def wheel(self, delta_x: int, delta_y: int) -> None:
        self._calls.append(("mouse.wheel", (delta_x, delta_y)))


class FakeResponse:
    """Minimal fake for Playwright `Response`."""

    def __init__(self, url: str = "https://example.com", status: int = 200) -> None:
        self.url = url
        self.status = status
        self.ok = 200 <= status < 400


class FakePage:
    """Configurable in-memory fake for Playwright `Page`."""

    def __init__(
        self,
        *,
        url: str = "https://example.com",
        html: str = "<html><body><p>Rendered</p></body></html>",
        status: int = 200,
        pdf_bytes: bytes = b"%PDF-1.4 fake",
        screenshot_bytes: bytes = b"\x89PNG\r\n\x1a\nfake",
        goto_error: Exception | None = None,
        wait_for_selector_error: Exception | None = None,
    ) -> None:
        self.url = url
        self._html = html
        self._status = status
        self._pdf_bytes = pdf_bytes
        self._screenshot_bytes = screenshot_bytes
        self._goto_error = goto_error
        self._wait_for_selector_error = wait_for_selector_error
        self.calls: list[tuple[Any, ...]] = []
        self.keyboard = FakeKeyboard(self.calls)
        self.mouse = FakeMouse(self.calls)
        self.closed = False

    async def goto(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("goto", url, kwargs))
        if self._goto_error is not None:
            raise self._goto_error
        self.url = url
        return FakeResponse(url=url, status=self._status)

    async def wait_for_load_state(self, state: str, timeout: int | None = None) -> None:
        self.calls.append(("wait_for_load_state", state, timeout))

    async def wait_for_selector(
        self, selector: str, *, state: str = "visible", timeout: int | None = None
    ) -> None:
        self.calls.append(("wait_for_selector", selector, state, timeout))
        if self._wait_for_selector_error is not None:
            raise self._wait_for_selector_error

    async def click(self, selector: str, timeout: int | None = None) -> None:
        self.calls.append(("click", selector, timeout))

    async def fill(self, selector: str, value: str, timeout: int | None = None) -> None:
        self.calls.append(("fill", selector, value, timeout))

    async def select_option(self, selector: str, value: str, timeout: int | None = None) -> None:
        self.calls.append(("select", selector, value, timeout))

    async def press(self, selector: str, key: str, timeout: int | None = None) -> None:
        self.calls.append(("press", selector, key, timeout))

    async def hover(self, selector: str, timeout: int | None = None) -> None:
        self.calls.append(("hover", selector, timeout))

    def locator(self, selector: str) -> FakeLocator:
        self.calls.append(("locator", selector))
        return FakeLocator(self.calls)

    async def wait_for_timeout(self, duration_ms: int) -> None:
        self.calls.append(("wait", duration_ms))

    async def evaluate(self, script: str, *args: Any) -> Any:
        self.calls.append(("evaluate", script, args))
        return None

    async def content(self) -> str:
        self.calls.append(("content",))
        return self._html

    async def pdf(self, **options: Any) -> bytes:
        self.calls.append(("pdf", options))
        return self._pdf_bytes

    async def screenshot(self, **options: Any) -> bytes:
        self.calls.append(("screenshot", options))
        return self._screenshot_bytes

    async def close(self) -> None:
        self.closed = True


class FakeContext:
    """Minimal fake for Playwright `BrowserContext` supporting `route`, `on`, `new_page`."""

    def __init__(self, page: FakePage | None = None) -> None:
        self.page = page or FakePage()
        self.routes: list[tuple[str, Any]] = []
        self.listeners: dict[str, list[Any]] = {}
        self.closed = False

    async def route(self, pattern: str, handler: Any) -> None:
        self.routes.append((pattern, handler))

    def on(self, event: str, handler: Any) -> None:
        self.listeners.setdefault(event, []).append(handler)

    async def new_page(self) -> FakePage:
        return self.page

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    """Minimal fake for Playwright `Browser`."""

    def __init__(self, context: FakeContext | None = None) -> None:
        self.context = context or FakeContext()

    def is_connected(self) -> bool:
        return True

    async def new_context(self, **kwargs: Any) -> FakeContext:
        del kwargs
        return self.context


class FakeBrowserPool:
    """Minimal fake for `BrowserPool` exposing the public `BrowserSession` contract."""

    def __init__(
        self,
        *,
        page: FakePage | None = None,
        context: FakeContext | None = None,
        acquire_error: Exception | None = None,
    ) -> None:
        self.context = context or FakeContext(page=page)
        self.browser = FakeBrowser(context=self.context)
        self._acquire_error = acquire_error
        self.released = 0
        self.available_count = 1

    async def get_browser(self) -> FakeBrowser:
        if self._acquire_error is not None:
            raise self._acquire_error
        return self.browser

    async def create_context(self, browser: FakeBrowser, **kwargs: Any) -> FakeContext:
        return await browser.new_context(**kwargs)

    async def release_browser(self, browser: FakeBrowser) -> None:
        del browser
        self.released += 1
